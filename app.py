"""
SmartRoad 2.0 — Flask Backend
Team P55 | IEEE IAM Pro CS 2026

Endpoints:
  POST /upload_trip      — receive CSV + pothole JSON from Flutter app
  GET  /health           — uptime ping (UptimeRobot target)
  GET  /                 — status check
  GET  /dashboard        — admin dashboard
  GET  /api/stats        — JSON stats
  GET  /api/potholes     — all pothole events (bbox filter supported)
  GET  /api/heatmap      — [lat,lon,weight] for heatmap layer
  GET  /api/trips        — trip list with GPS
  GET  /api/trip/<id>    — single trip detail + events
  GET  /api/csv/<id>     — download raw CSV
"""

import os
import json
from datetime import datetime, timedelta
from flask import Flask, request, jsonify, send_file, render_template_string
from flask_sqlalchemy import SQLAlchemy
from flask_cors import CORS
from sqlalchemy import func

# ─────────────────────────────────────────────────────────────
# APP SETUP
# ─────────────────────────────────────────────────────────────
app = Flask(__name__)
CORS(app)

DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite:///smartroad_dev.db")

# Render + Supabase compatibility
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql+psycopg://", 1)
elif DATABASE_URL.startswith("postgresql://"):
    DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+psycopg://", 1)

app.config["SQLALCHEMY_DATABASE_URI"] = DATABASE_URL
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {
    "pool_pre_ping": True,
    "pool_recycle": 300,
}
app.config["MAX_CONTENT_LENGTH"] = 50 * 1024 * 1024  # 50 MB

db = SQLAlchemy(app)

CSV_STORAGE = os.environ.get("CSV_STORAGE_PATH", "/tmp/smartroad_csvs")
os.makedirs(CSV_STORAGE, exist_ok=True)

# ─────────────────────────────────────────────────────────────
# MODELS
# ─────────────────────────────────────────────────────────────
class Trip(db.Model):
    __tablename__ = "trips"
    id            = db.Column(db.String(80), primary_key=True)
    device_id     = db.Column(db.String(36), nullable=False, index=True)
    vehicle_type  = db.Column(db.String(20))
    pothole_count = db.Column(db.Integer, default=0)
    city          = db.Column(db.String(50), default="Bangalore")
    start_lat     = db.Column(db.Float)
    start_lon     = db.Column(db.Float)
    end_lat       = db.Column(db.Float)
    end_lon       = db.Column(db.Float)
    csv_path      = db.Column(db.String(500))   # FIX: increased length for URLs
    file_size_kb  = db.Column(db.Float)
    retried       = db.Column(db.Boolean, default=False)
    ml_processed  = db.Column(db.Boolean, default=False)
    created_at    = db.Column(db.DateTime, default=datetime.utcnow, index=True)
    events        = db.relationship("PotholeEvent", backref="trip",
                                     lazy="dynamic", cascade="all, delete-orphan")


class PotholeEvent(db.Model):
    __tablename__ = "pothole_events"
    id         = db.Column(db.Integer, primary_key=True, autoincrement=True)
    trip_id    = db.Column(db.String(80), db.ForeignKey("trips.id"), index=True)
    device_id  = db.Column(db.String(36), index=True)
    lat        = db.Column(db.Float, nullable=False)
    lon        = db.Column(db.Float, nullable=False)
    speed      = db.Column(db.Float)
    severity   = db.Column(db.String(10), index=True)
    vibration  = db.Column(db.Float)
    acc_x      = db.Column(db.Float)
    acc_y      = db.Column(db.Float)
    acc_z      = db.Column(db.Float)
    gyro_x     = db.Column(db.Float)
    gyro_y     = db.Column(db.Float)
    gyro_z     = db.Column(db.Float)
    timestamp  = db.Column(db.BigInteger)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


# ─────────────────────────────────────────────────────────────
# HEALTH
# ─────────────────────────────────────────────────────────────
@app.route("/health")
def health():
    try:
        db.session.execute(db.text("SELECT 1"))
        trip_count = Trip.query.count()
        return jsonify({"status": "alive", "trips": trip_count,
                        "time": datetime.utcnow().isoformat()}), 200
    except Exception as e:
        return jsonify({"status": "error", "error": str(e)}), 500


@app.route("/")
def home():
    return jsonify({"message": "SmartRoad 2.0 Backend", "status": "ok"})


# ─────────────────────────────────────────────────────────────
# UPLOAD TRIP
# ─────────────────────────────────────────────────────────────
@app.route("/upload_trip", methods=["POST"])
def upload_trip():
    try:
        trip_id       = request.form.get("trip_id", "").strip()
        device_id     = request.form.get("device_id", "").strip()
        vehicle_type  = request.form.get("vehicle_type", "unknown").strip()
        pothole_count = int(request.form.get("pothole_count", 0))
        city          = request.form.get("city", "Bangalore").strip()
        pothole_json  = request.form.get("pothole_json", "[]")
        start_lat     = float(request.form.get("start_lat", 0))
        start_lon     = float(request.form.get("start_lon", 0))
        end_lat       = float(request.form.get("end_lat", 0))
        end_lon       = float(request.form.get("end_lon", 0))
        retried       = request.form.get("retried", "false").lower() == "true"

        if not trip_id or not device_id:
            return jsonify({"error": "Missing trip_id or device_id"}), 400

        # Idempotent — skip duplicate uploads safely
        if Trip.query.get(trip_id):
            return jsonify({"status": "already_exists", "trip_id": trip_id}), 200

        # Save CSV file
        csv_path  = None
        file_size = 0
        if "csv_file" in request.files:
            f = request.files["csv_file"]
            csv_path = os.path.join(CSV_STORAGE, f"{trip_id}.csv")
            f.save(csv_path)
            file_size = os.path.getsize(csv_path) / 1024

        # Save trip record
        trip = Trip(
            id=trip_id, device_id=device_id,
            vehicle_type=vehicle_type, pothole_count=pothole_count,
            city=city,
            start_lat=start_lat if start_lat != 0 else None,
            start_lon=start_lon if start_lon != 0 else None,
            end_lat=end_lat if end_lat != 0 else None,
            end_lon=end_lon if end_lon != 0 else None,
            csv_path=csv_path,
            file_size_kb=round(file_size, 2),
            retried=retried,
        )
        db.session.add(trip)

        # Bulk-insert pothole events
        events = json.loads(pothole_json)
        db_events = []
        for e in events:
            lat = float(e.get("lat", 0))
            lon = float(e.get("lon", 0))
            if lat == 0 or lon == 0:
                continue
            db_events.append(PotholeEvent(
                trip_id=trip_id, device_id=device_id,
                lat=lat, lon=lon,
                speed=float(e.get("speed", 0)),
                severity=e.get("severity", "mild"),
                vibration=float(e.get("vibration", 0)),
                acc_x=float(e.get("acc_x", 0)),
                acc_y=float(e.get("acc_y", 0)),
                acc_z=float(e.get("acc_z", 0)),
                gyro_x=float(e.get("gyro_x", 0)),
                gyro_y=float(e.get("gyro_y", 0)),
                gyro_z=float(e.get("gyro_z", 0)),
                timestamp=int(e.get("timestamp", 0)),
            ))

        db.session.bulk_save_objects(db_events)
        db.session.commit()

        return jsonify({
            "status": "ok", "trip_id": trip_id,
            "events_saved": len(db_events),
            "file_size_kb": round(file_size, 2),
        }), 200

    except Exception as ex:
        db.session.rollback()
        app.logger.error(f"Upload error: {ex}")
        return jsonify({"error": str(ex)}), 500


# ─────────────────────────────────────────────────────────────
# STATS API
# ─────────────────────────────────────────────────────────────
@app.route("/api/stats")
def api_stats():
    total_trips    = Trip.query.count()
    total_devices  = db.session.query(func.count(func.distinct(Trip.device_id))).scalar()
    total_events   = PotholeEvent.query.count()
    severe_count   = PotholeEvent.query.filter_by(severity="severe").count()
    moderate_count = PotholeEvent.query.filter_by(severity="moderate").count()
    mild_count     = PotholeEvent.query.filter_by(severity="mild").count()
    recent         = Trip.query.filter(
        Trip.created_at >= datetime.utcnow() - timedelta(hours=24)
    ).count()
    return jsonify({
        "total_trips": total_trips, "active_devices": total_devices,
        "total_events": total_events, "severe": severe_count,
        "moderate": moderate_count, "mild": mild_count,
        "trips_last_24h": recent,
    })


# ─────────────────────────────────────────────────────────────
# POTHOLES API — with bbox filter + vehicle type
# ─────────────────────────────────────────────────────────────
@app.route("/api/potholes")
def api_potholes():
    limit    = min(int(request.args.get("limit", 2000)), 5000)
    severity = request.args.get("severity")
    min_lat  = request.args.get("min_lat", type=float)
    max_lat  = request.args.get("max_lat", type=float)
    min_lon  = request.args.get("min_lon", type=float)
    max_lon  = request.args.get("max_lon", type=float)

    q = PotholeEvent.query
    if severity:
        q = q.filter_by(severity=severity)
    if min_lat: q = q.filter(PotholeEvent.lat >= min_lat)
    if max_lat: q = q.filter(PotholeEvent.lat <= max_lat)
    if min_lon: q = q.filter(PotholeEvent.lon >= min_lon)
    if max_lon: q = q.filter(PotholeEvent.lon <= max_lon)

    events = q.order_by(PotholeEvent.created_at.desc()).limit(limit).all()

    # FIX: include vehicle_type from parent trip
    result = []
    for e in events:
        trip = Trip.query.get(e.trip_id)
        result.append({
            "id": e.id, "lat": e.lat, "lon": e.lon,
            "severity": e.severity, "speed": e.speed,
            "vibration": e.vibration, "timestamp": e.timestamp,
            "vehicle": trip.vehicle_type if trip else "unknown",
            "created_at": e.created_at.isoformat(),
        })
    return jsonify(result)


# ─────────────────────────────────────────────────────────────
# HEATMAP API
# ─────────────────────────────────────────────────────────────
@app.route("/api/heatmap")
def api_heatmap():
    events = PotholeEvent.query.with_entities(
        PotholeEvent.lat, PotholeEvent.lon, PotholeEvent.severity
    ).limit(5000).all()
    weight_map = {"severe": 1.0, "moderate": 0.6, "mild": 0.3}
    points = [
        [e.lat, e.lon, weight_map.get(e.severity, 0.3)]
        for e in events if e.lat and e.lon
    ]
    return jsonify(points)


# ─────────────────────────────────────────────────────────────
# TRIPS API — FIX: include GPS coordinates
# ─────────────────────────────────────────────────────────────
@app.route("/api/trips")
def api_trips():
    page     = int(request.args.get("page", 1))
    per_page = int(request.args.get("per_page", 50))
    trips    = Trip.query.order_by(Trip.created_at.desc()).paginate(
        page=page, per_page=per_page, error_out=False
    )
    return jsonify({
        "trips": [{
            "id": t.id,
            "device_id": t.device_id[:8] + "…",
            "vehicle_type": t.vehicle_type,
            "pothole_count": t.pothole_count,
            "city": t.city,
            "file_size_kb": t.file_size_kb,
            "start_lat": t.start_lat,   # FIX: was missing
            "start_lon": t.start_lon,   # FIX: was missing
            "created_at": t.created_at.isoformat(),
            "has_csv": t.csv_path is not None,
        } for t in trips.items],
        "total": trips.total,
        "page": trips.page,
        "pages": trips.pages,
    })


# ─────────────────────────────────────────────────────────────
# TRIP DETAIL
# ─────────────────────────────────────────────────────────────
@app.route("/api/trip/<trip_id>")
def api_trip_detail(trip_id):
    t = Trip.query.get_or_404(trip_id)
    events = t.events.all()
    return jsonify({
        "trip": {
            "id": t.id, "device_id": t.device_id,
            "vehicle_type": t.vehicle_type,
            "pothole_count": t.pothole_count,
            "city": t.city, "file_size_kb": t.file_size_kb,
            "start_lat": t.start_lat, "start_lon": t.start_lon,
            "end_lat": t.end_lat, "end_lon": t.end_lon,
            "created_at": t.created_at.isoformat(),
        },
        "events": [{
            "lat": e.lat, "lon": e.lon, "severity": e.severity,
            "speed": e.speed, "vibration": e.vibration,
            "timestamp": e.timestamp,
        } for e in events],
    })


# ─────────────────────────────────────────────────────────────
# CSV DOWNLOAD
# ─────────────────────────────────────────────────────────────
@app.route("/api/csv/<trip_id>")
def api_csv_download(trip_id):
    t = Trip.query.get_or_404(trip_id)
    if not t.csv_path or not os.path.exists(t.csv_path):
        return jsonify({"error": "CSV not found on server. File may have been cleared after Render restart."}), 404
    return send_file(t.csv_path, mimetype="text/csv",
                     as_attachment=True, download_name=f"{trip_id}.csv")


# ─────────────────────────────────────────────────────────────
# DASHBOARD — Full production UI with Leaflet map
# ─────────────────────────────────────────────────────────────
DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>SmartRoad 2.0 — Admin</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<script src="https://unpkg.com/leaflet.heat@0.2.0/dist/leaflet-heat.js"></script>
<style>
  *{margin:0;padding:0;box-sizing:border-box}
  body{background:#050510;color:#e8e8f0;font-family:'Segoe UI',sans-serif;height:100vh;overflow:hidden}
  header{background:#0c0c1e;padding:12px 24px;display:flex;align-items:center;justify-content:space-between;border-bottom:1px solid #1e1e3f}
  header h1{color:#00c8e8;font-size:18px;letter-spacing:1px}
  #refresh-info{font-size:11px;color:#5a5a7a}
  .main{display:grid;grid-template-columns:320px 1fr;height:calc(100vh - 49px)}
  .sidebar{background:#0c0c1e;border-right:1px solid #1e1e3f;overflow-y:auto;display:flex;flex-direction:column}
  .stats{display:grid;grid-template-columns:1fr 1fr;gap:1px;background:#1e1e3f}
  .stat{background:#0c0c1e;padding:14px;text-align:center}
  .stat .v{font-size:26px;font-weight:700}
  .stat .l{font-size:10px;color:#5a5a7a;text-transform:uppercase;letter-spacing:1px;margin-top:2px}
  .cyan{color:#00c8e8}.green{color:#00e676}.orange{color:#ff9800}.red{color:#ff4444}
  .sev-bars{padding:12px 16px;border-bottom:1px solid #1e1e3f}
  .sev-bars h4{font-size:10px;color:#5a5a7a;text-transform:uppercase;letter-spacing:1px;margin-bottom:8px}
  .bar-row{margin-bottom:6px}
  .bar-row-top{display:flex;justify-content:space-between;font-size:11px;margin-bottom:3px}
  .bar-bg{height:5px;border-radius:3px;background:#1e1e3f;overflow:hidden}
  .bar-fill{height:100%;border-radius:3px;transition:width 1s ease}
  .filters{display:flex;gap:6px;padding:10px 16px;flex-wrap:wrap;border-bottom:1px solid #1e1e3f}
  .fbtn{padding:3px 10px;border-radius:10px;border:1px solid #1e1e3f;background:transparent;color:#5a5a7a;font-size:11px;cursor:pointer;transition:all 0.2s}
  .fbtn:hover,.fbtn.active{border-color:#00c8e8;color:#00c8e8;background:rgba(0,200,232,0.08)}
  .trips-hdr{padding:8px 16px;font-size:10px;color:#5a5a7a;text-transform:uppercase;letter-spacing:1px;display:flex;justify-content:space-between;border-bottom:1px solid #1e1e3f}
  #trips-list{flex:1;overflow-y:auto}
  .trow{padding:10px 16px;border-bottom:1px solid #1e1e3f;cursor:pointer;transition:background 0.15s}
  .trow:hover{background:#12122a}
  .trow.sel{background:rgba(0,200,232,0.06);border-left:2px solid #00c8e8}
  .tid{font-size:11px;color:#00c8e8;font-family:monospace}
  .tmeta{font-size:11px;color:#5a5a7a;margin-top:2px}
  .tbadge{display:inline-block;padding:1px 7px;border-radius:8px;font-size:10px;font-weight:600;float:right;margin-top:2px}
  .bs{background:rgba(255,68,68,0.15);color:#ff4444}
  .bm{background:rgba(255,152,0,0.15);color:#ff9800}
  .bl{background:rgba(255,214,0,0.12);color:#ffd600}
  .bc{background:rgba(0,230,118,0.1);color:#00e676}
  #map{width:100%;height:100%}
  .leaflet-tile-pane{filter:brightness(0.65) saturate(0.5) hue-rotate(200deg)}
  .mctrl{position:absolute;top:14px;right:14px;z-index:1000;display:flex;flex-direction:column;gap:6px}
  .mbtn{padding:7px 13px;background:#0c0c1e;border:1px solid #1e1e3f;border-radius:7px;color:#e8e8f0;font-size:12px;cursor:pointer;transition:all 0.2s}
  .mbtn:hover,.mbtn.active{border-color:#00c8e8;color:#00c8e8}
  .legend{position:absolute;bottom:24px;left:14px;z-index:1000;background:#0c0c1e;border:1px solid #1e1e3f;border-radius:8px;padding:10px 14px;font-size:12px}
  .legend h4{font-size:10px;color:#5a5a7a;text-transform:uppercase;letter-spacing:1px;margin-bottom:6px}
  .leg-row{display:flex;align-items:center;gap:7px;margin-bottom:4px}
  .ldot{width:10px;height:10px;border-radius:50%}
  #detail{display:none;position:absolute;bottom:0;left:0;right:0;z-index:900;background:#0c0c1e;border-top:1px solid #1e1e3f;padding:12px 20px}
  #detail.show{display:block}
  .dhdr{display:flex;justify-content:space-between;align-items:center;margin-bottom:8px}
  .dtitle{font-size:13px;font-weight:600;color:#00c8e8;font-family:monospace}
  .dclose{background:none;border:none;color:#5a5a7a;cursor:pointer;font-size:18px}
  .dgrid{display:grid;grid-template-columns:repeat(auto-fill,minmax(110px,1fr));gap:8px}
  .dcell .dl{font-size:10px;color:#5a5a7a;text-transform:uppercase}
  .dcell .dv{font-size:14px;font-weight:700;font-family:monospace}
  .dlbtn{display:inline-block;margin-top:8px;padding:4px 12px;background:rgba(0,200,232,0.08);border:1px solid #00788a;border-radius:6px;color:#00c8e8;font-size:12px;text-decoration:none}
  ::-webkit-scrollbar{width:4px}
  ::-webkit-scrollbar-thumb{background:#1e1e3f;border-radius:2px}
</style>
</head>
<body>
<header>
  <h1>🛣️ SmartRoad 2.0 — Admin Dashboard</h1>
  <div id="refresh-info">Loading…</div>
</header>
<div class="main">
  <div class="sidebar">
    <div class="stats">
      <div class="stat"><div class="v cyan" id="s-trips">—</div><div class="l">Trips</div></div>
      <div class="stat"><div class="v green" id="s-dev">—</div><div class="l">Devices</div></div>
      <div class="stat"><div class="v orange" id="s-ev">—</div><div class="l">Potholes</div></div>
      <div class="stat"><div class="v red" id="s-sev">—</div><div class="l">Severe</div></div>
    </div>
    <div class="sev-bars">
      <h4>Severity Breakdown</h4>
      <div class="bar-row">
        <div class="bar-row-top"><span style="color:#ff4444">Severe</span><span id="p-s">0%</span></div>
        <div class="bar-bg"><div class="bar-fill" id="b-s" style="width:0%;background:#ff4444"></div></div>
      </div>
      <div class="bar-row">
        <div class="bar-row-top"><span style="color:#ff9800">Moderate</span><span id="p-m">0%</span></div>
        <div class="bar-bg"><div class="bar-fill" id="b-m" style="width:0%;background:#ff9800"></div></div>
      </div>
      <div class="bar-row">
        <div class="bar-row-top"><span style="color:#ffd600">Mild</span><span id="p-l">0%</span></div>
        <div class="bar-bg"><div class="bar-fill" id="b-l" style="width:0%;background:#ffd600"></div></div>
      </div>
    </div>
    <div class="filters">
      <button class="fbtn active" onclick="filt('all',this)">All</button>
      <button class="fbtn" onclick="filt('two_wheeler',this)">🏍 Bike</button>
      <button class="fbtn" onclick="filt('auto',this)">🛺 Auto</button>
      <button class="fbtn" onclick="filt('car',this)">🚗 Car</button>
      <button class="fbtn" onclick="filt('bus',this)">🚌 Bus</button>
    </div>
    <div class="trips-hdr">
      <span>Recent Trips</span>
      <span id="tc" style="color:#00c8e8">—</span>
    </div>
    <div id="trips-list"></div>
  </div>
  <div style="position:relative">
    <div id="map"></div>
    <div class="mctrl">
      <button class="mbtn active" id="bm" onclick="setMode('markers')">📍 Markers</button>
      <button class="mbtn" id="bh" onclick="setMode('heatmap')">🌡 Heatmap</button>
      <button class="mbtn" onclick="map.setView([12.9716,77.5946],12)">🎯 Reset</button>
    </div>
    <div class="legend">
      <h4>Severity</h4>
      <div class="leg-row"><div class="ldot" style="background:#ff4444"></div>Severe</div>
      <div class="leg-row"><div class="ldot" style="background:#ff9800"></div>Moderate</div>
      <div class="leg-row"><div class="ldot" style="background:#ffd600"></div>Mild</div>
    </div>
    <div id="detail">
      <div class="dhdr">
        <div class="dtitle" id="dt">Trip Detail</div>
        <button class="dclose" onclick="closeDetail()">✕</button>
      </div>
      <div class="dgrid" id="dg"></div>
      <a id="dcsv" class="dlbtn" href="#">⬇ Download CSV</a>
    </div>
  </div>
</div>
<script>
let map,heatLayer,mLayer;
let allTrips=[],curFilt='all',selId=null,mapMode='markers';
const SEV={severe:'#ff4444',moderate:'#ff9800',mild:'#ffd600'};
const RAD={severe:9,moderate:6,mild:4};

function init(){
  map=L.map('map',{zoomControl:false}).setView([12.9716,77.5946],12);
  L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png',{maxZoom:19}).addTo(map);
  L.control.zoom({position:'bottomright'}).addTo(map);
  mLayer=L.layerGroup().addTo(map);
  refresh();
  setInterval(refresh,30000);
}

async function refresh(){
  await Promise.all([loadStats(),loadTrips()]);
  if(!selId) await loadMarkers();
  document.getElementById('refresh-info').textContent='Updated '+new Date().toLocaleTimeString();
}

async function loadStats(){
  const r=await fetch('/api/stats'),s=await r.json();
  document.getElementById('s-trips').textContent=s.total_trips;
  document.getElementById('s-dev').textContent=s.active_devices;
  document.getElementById('s-ev').textContent=s.total_events;
  document.getElementById('s-sev').textContent=s.severe;
  const tot=s.severe+s.moderate+s.mild||1;
  const sp=Math.round(s.severe/tot*100),mp=Math.round(s.moderate/tot*100),lp=Math.round(s.mild/tot*100);
  ['s','m','l'].forEach((k,i)=>{
    const pct=[sp,mp,lp][i];
    document.getElementById('p-'+k).textContent=pct+'%';
    document.getElementById('b-'+k).style.width=pct+'%';
  });
}

async function loadTrips(){
  const r=await fetch('/api/trips?per_page=100'),d=await r.json();
  allTrips=d.trips||[];
  renderTrips();
}

function renderTrips(){
  const list=allTrips.filter(t=>curFilt==='all'||t.vehicle_type===curFilt);
  document.getElementById('tc').textContent=list.length;
  const vicon={two_wheeler:'🏍',auto:'🛺',car:'🚗',heavy_vehicle:'🚛',bus:'🚌'};
  document.getElementById('trips-list').innerHTML=list.map(t=>{
    const dt=new Date(t.created_at+'Z');
    const bc=t.pothole_count>=10?'bs':t.pothole_count>=4?'bm':t.pothole_count>0?'bl':'bc';
    return `<div class="trow ${t.id===selId?'sel':''}" onclick="selTrip('${t.id}',${t.start_lat||0},${t.start_lon||0})">
      <span class="tbadge ${bc}">${t.pothole_count} 🕳</span>
      <div class="tid">${t.id.substring(5,22)}…</div>
      <div class="tmeta">${vicon[t.vehicle_type]||'🚗'} ${t.vehicle_type} · ${dt.toLocaleDateString()} · ${t.file_size_kb||0}KB</div>
    </div>`;
  }).join('');
}

function filt(v,btn){
  curFilt=v;
  document.querySelectorAll('.fbtn').forEach(b=>b.classList.remove('active'));
  btn.classList.add('active');
  renderTrips();
}

async function selTrip(id,lat,lon){
  selId=id;renderTrips();
  const r=await fetch('/api/trip/'+id),d=await r.json();
  const t=d.trip,evs=d.events;
  document.getElementById('dt').textContent=id.substring(5,24)+'…';
  document.getElementById('dcsv').href='/api/csv/'+id;
  document.getElementById('dg').innerHTML=`
    <div class="dcell"><div class="dl">Vehicle</div><div class="dv">${t.vehicle_type}</div></div>
    <div class="dcell"><div class="dl">Potholes</div><div class="dv orange">${t.pothole_count}</div></div>
    <div class="dcell"><div class="dl">File Size</div><div class="dv">${t.file_size_kb}KB</div></div>
    <div class="dcell"><div class="dl">City</div><div class="dv">${t.city}</div></div>`;
  document.getElementById('detail').classList.add('show');
  mLayer.clearLayers();
  const bounds=[];
  evs.forEach(e=>{
    if(!e.lat||!e.lon)return;
    L.circleMarker([e.lat,e.lon],{radius:RAD[e.severity]||5,fillColor:SEV[e.severity]||'#ffd600',color:'#000',weight:1,fillOpacity:0.85})
     .addTo(mLayer)
     .bindPopup(`<b style="color:${SEV[e.severity]}">${e.severity?.toUpperCase()}</b><br>Speed: ${e.speed?.toFixed(1)} km/h<br>Vib: ${e.vibration?.toFixed(2)}`);
    bounds.push([e.lat,e.lon]);
  });
  if(bounds.length)map.fitBounds(bounds,{padding:[40,40]});
  else if(lat)map.setView([lat,lon],14);
}

function closeDetail(){
  document.getElementById('detail').classList.remove('show');
  selId=null;renderTrips();loadMarkers();
}

async function loadMarkers(){
  const r=await fetch('/api/potholes?limit=2000'),evs=await r.json();
  mLayer.clearLayers();
  evs.forEach(e=>{
    if(!e.lat||!e.lon)return;
    L.circleMarker([e.lat,e.lon],{radius:RAD[e.severity]||4,fillColor:SEV[e.severity]||'#ffd600',color:'transparent',weight:0,fillOpacity:0.75})
     .addTo(mLayer)
     .bindPopup(`<b style="color:${SEV[e.severity]}">${e.severity?.toUpperCase()}</b><br>${e.speed?.toFixed(1)} km/h · ${e.vehicle}`);
  });
}

async function loadHeatmap(){
  const r=await fetch('/api/heatmap'),pts=await r.json();
  if(heatLayer)map.removeLayer(heatLayer);
  heatLayer=L.heatLayer(pts,{radius:20,blur:15,gradient:{0.2:'#ffd600',0.5:'#ff9800',0.8:'#ff4444'}}).addTo(map);
}

function setMode(mode){
  mapMode=mode;
  document.getElementById('bm').classList.toggle('active',mode==='markers');
  document.getElementById('bh').classList.toggle('active',mode==='heatmap');
  if(mode==='markers'){if(heatLayer)map.removeLayer(heatLayer);mLayer.addTo(map);loadMarkers();}
  else{mLayer.clearLayers();loadHeatmap();}
}

window.addEventListener('load',init);
</script>
</body>
</html>"""


@app.route("/dashboard")
def dashboard():
    return render_template_string(DASHBOARD_HTML)


# ─────────────────────────────────────────────────────────────
# STARTUP
# ─────────────────────────────────────────────────────────────
with app.app_context():
    db.create_all()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)