"""
SmartRoad 2.0 — Flask Backend
Team P55 | IEEE IAM Pro CS 2026

Endpoints:
  POST /upload_trip      — receive CSV + pothole JSON from Flutter app
  GET  /health           — uptime ping (UptimeRobot target)
  GET  /dashboard        — admin dashboard HTML
  GET  /api/stats        — JSON stats for dashboard
  GET  /api/potholes     — all pothole events (with optional bbox filter)
  GET  /api/trips        — trip list
  GET  /api/trip/<id>    — single trip detail
  GET  /api/csv/<id>     — download raw CSV for a trip
  GET  /api/heatmap      — pothole density grid for heatmap layer
"""

import os, json, csv, io
from datetime import datetime
from flask import Flask, request, jsonify, send_file, render_template_string, Response
from flask_sqlalchemy import SQLAlchemy
from flask_cors import CORS
from sqlalchemy import func

# ─────────────────────────────────────────────────────────────────────────────
# APP & DB SETUP
# ─────────────────────────────────────────────────────────────────────────────
app = Flask(__name__)
CORS(app)

app.config['SQLALCHEMY_DATABASE_URI'] = os.environ.get(
    'DATABASE_URL', 'sqlite:///smartroad_dev.db'
)
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {
    'pool_pre_ping': True,
    'pool_recycle': 300,
}
app.config['MAX_CONTENT_LENGTH'] = 50 * 1024 * 1024  # 50 MB max upload

db = SQLAlchemy(app)

CSV_STORAGE = os.environ.get('CSV_STORAGE_PATH', '/tmp/smartroad_csvs')
os.makedirs(CSV_STORAGE, exist_ok=True)

# ─────────────────────────────────────────────────────────────────────────────
# MODELS
# ─────────────────────────────────────────────────────────────────────────────
class Trip(db.Model):
    __tablename__ = 'trips'

    id            = db.Column(db.String(80), primary_key=True)
    device_id     = db.Column(db.String(36), nullable=False, index=True)
    vehicle_type  = db.Column(db.String(20))
    pothole_count = db.Column(db.Integer, default=0)
    city          = db.Column(db.String(50), default='Bangalore')
    start_lat     = db.Column(db.Float)
    start_lon     = db.Column(db.Float)
    end_lat       = db.Column(db.Float)
    end_lon       = db.Column(db.Float)
    csv_path      = db.Column(db.String(200))
    file_size_kb  = db.Column(db.Float)
    retried       = db.Column(db.Boolean, default=False)
    ml_processed  = db.Column(db.Boolean, default=False)
    created_at    = db.Column(db.DateTime, default=datetime.utcnow, index=True)

    events = db.relationship('PotholeEvent', backref='trip', lazy='dynamic',
                              cascade='all, delete-orphan')


class PotholeEvent(db.Model):
    __tablename__ = 'pothole_events'

    id         = db.Column(db.Integer, primary_key=True, autoincrement=True)
    trip_id    = db.Column(db.String(80), db.ForeignKey('trips.id'), index=True)
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

# ─────────────────────────────────────────────────────────────────────────────
# ROUTES — DATA INGESTION
# ─────────────────────────────────────────────────────────────────────────────
@app.route('/upload_trip', methods=['POST'])
def upload_trip():
    try:
        trip_id       = request.form.get('trip_id', '').strip()
        device_id     = request.form.get('device_id', '').strip()
        vehicle_type  = request.form.get('vehicle_type', 'unknown').strip()
        pothole_count = int(request.form.get('pothole_count', 0))
        city          = request.form.get('city', 'Bangalore').strip()
        pothole_json  = request.form.get('pothole_json', '[]')
        start_lat     = float(request.form.get('start_lat', 0))
        start_lon     = float(request.form.get('start_lon', 0))
        end_lat       = float(request.form.get('end_lat', 0))
        end_lon       = float(request.form.get('end_lon', 0))
        retried       = request.form.get('retried', 'false').lower() == 'true'

        if not trip_id or not device_id:
            return jsonify({'error': 'Missing trip_id or device_id'}), 400

        # Idempotent — skip if already uploaded
        if Trip.query.get(trip_id):
            return jsonify({'status': 'already_exists', 'trip_id': trip_id}), 200

        # Save CSV file
        csv_path  = None
        file_size = 0
        if 'csv_file' in request.files:
            f = request.files['csv_file']
            csv_path = os.path.join(CSV_STORAGE, f'{trip_id}.csv')
            f.save(csv_path)
            file_size = os.path.getsize(csv_path) / 1024  # KB

        # Save trip record
        trip = Trip(
            id=trip_id,
            device_id=device_id,
            vehicle_type=vehicle_type,
            pothole_count=pothole_count,
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
            lat = float(e.get('lat', 0))
            lon = float(e.get('lon', 0))
            if lat == 0 or lon == 0:
                continue
            db_events.append(PotholeEvent(
                trip_id=trip_id,
                device_id=device_id,
                lat=lat,
                lon=lon,
                speed=float(e.get('speed', 0)),
                severity=e.get('severity', 'mild'),
                vibration=float(e.get('vibration', 0)),
                acc_x=float(e.get('acc_x', 0)),
                acc_y=float(e.get('acc_y', 0)),
                acc_z=float(e.get('acc_z', 0)),
                gyro_x=float(e.get('gyro_x', 0)),
                gyro_y=float(e.get('gyro_y', 0)),
                gyro_z=float(e.get('gyro_z', 0)),
                timestamp=int(e.get('timestamp', 0)),
            ))

        db.session.bulk_save_objects(db_events)
        db.session.commit()

        return jsonify({
            'status': 'ok',
            'trip_id': trip_id,
            'events_saved': len(db_events),
            'file_size_kb': round(file_size, 2),
        }), 200

    except Exception as ex:
        db.session.rollback()
        app.logger.error(f'Upload error: {ex}')
        return jsonify({'error': str(ex)}), 500


@app.route('/health')
def health():
    """UptimeRobot target — also keeps Supabase alive via DB ping."""
    try:
        db.session.execute(db.text('SELECT 1'))
        trip_count = Trip.query.count()
        return jsonify({'status': 'alive', 'trips': trip_count}), 200
    except Exception as e:
        return jsonify({'status': 'db_error', 'error': str(e)}), 500


# ─────────────────────────────────────────────────────────────────────────────
# ROUTES — API (consumed by dashboard)
# ─────────────────────────────────────────────────────────────────────────────
@app.route('/api/stats')
def api_stats():
    total_trips   = Trip.query.count()
    total_devices = db.session.query(func.count(func.distinct(Trip.device_id))).scalar()
    total_events  = PotholeEvent.query.count()
    severe_count  = PotholeEvent.query.filter_by(severity='severe').count()
    moderate_count= PotholeEvent.query.filter_by(severity='moderate').count()
    mild_count    = PotholeEvent.query.filter_by(severity='mild').count()

    # Trips in last 24 hours
    from datetime import timedelta
    recent = Trip.query.filter(
        Trip.created_at >= datetime.utcnow() - timedelta(hours=24)
    ).count()

    return jsonify({
        'total_trips': total_trips,
        'active_devices': total_devices,
        'total_events': total_events,
        'severe': severe_count,
        'moderate': moderate_count,
        'mild': mild_count,
        'trips_last_24h': recent,
    })


@app.route('/api/potholes')
def api_potholes():
    """Return pothole events, optional bbox: ?min_lat=&max_lat=&min_lon=&max_lon="""
    limit    = min(int(request.args.get('limit', 2000)), 5000)
    severity = request.args.get('severity')  # filter by severity
    min_lat  = request.args.get('min_lat', type=float)
    max_lat  = request.args.get('max_lat', type=float)
    min_lon  = request.args.get('min_lon', type=float)
    max_lon  = request.args.get('max_lon', type=float)

    q = PotholeEvent.query
    if severity:
        q = q.filter_by(severity=severity)
    if min_lat: q = q.filter(PotholeEvent.lat >= min_lat)
    if max_lat: q = q.filter(PotholeEvent.lat <= max_lat)
    if min_lon: q = q.filter(PotholeEvent.lon >= min_lon)
    if max_lon: q = q.filter(PotholeEvent.lon <= max_lon)

    events = q.order_by(PotholeEvent.created_at.desc()).limit(limit).all()

    return jsonify([{
        'id': e.id,
        'lat': e.lat,
        'lon': e.lon,
        'severity': e.severity,
        'speed': e.speed,
        'vibration': e.vibration,
        'vehicle': Trip.query.get(e.trip_id).vehicle_type if e.trip_id else None,
        'timestamp': e.timestamp,
        'created_at': e.created_at.isoformat(),
    } for e in events])


@app.route('/api/heatmap')
def api_heatmap():
    """Return [lat, lon, intensity] array for Leaflet.heat plugin."""
    events = PotholeEvent.query.with_entities(
        PotholeEvent.lat, PotholeEvent.lon, PotholeEvent.severity
    ).limit(5000).all()

    weight_map = {'severe': 1.0, 'moderate': 0.6, 'mild': 0.3}
    points = [
        [e.lat, e.lon, weight_map.get(e.severity, 0.3)]
        for e in events if e.lat and e.lon
    ]
    return jsonify(points)


@app.route('/api/trips')
def api_trips():
    page     = int(request.args.get('page', 1))
    per_page = int(request.args.get('per_page', 50))
    trips    = Trip.query.order_by(Trip.created_at.desc()).paginate(
        page=page, per_page=per_page, error_out=False
    )
    return jsonify({
        'trips': [{
            'id': t.id,
            'device_id': t.device_id[:8] + '…',
            'vehicle_type': t.vehicle_type,
            'pothole_count': t.pothole_count,
            'city': t.city,
            'file_size_kb': t.file_size_kb,
            'start_lat': t.start_lat,
            'start_lon': t.start_lon,
            'created_at': t.created_at.isoformat(),
            'has_csv': t.csv_path is not None,
        } for t in trips.items],
        'total': trips.total,
        'page': trips.page,
        'pages': trips.pages,
    })


@app.route('/api/trip/<trip_id>')
def api_trip_detail(trip_id):
    t = Trip.query.get_or_404(trip_id)
    events = t.events.all()
    return jsonify({
        'trip': {
            'id': t.id,
            'device_id': t.device_id,
            'vehicle_type': t.vehicle_type,
            'pothole_count': t.pothole_count,
            'city': t.city,
            'file_size_kb': t.file_size_kb,
            'start_lat': t.start_lat,
            'start_lon': t.start_lon,
            'end_lat': t.end_lat,
            'end_lon': t.end_lon,
            'created_at': t.created_at.isoformat(),
        },
        'events': [{
            'lat': e.lat, 'lon': e.lon,
            'severity': e.severity, 'speed': e.speed,
            'vibration': e.vibration, 'timestamp': e.timestamp,
        } for e in events],
    })


@app.route('/api/csv/<trip_id>')
def api_csv_download(trip_id):
    t = Trip.query.get_or_404(trip_id)
    if not t.csv_path or not os.path.exists(t.csv_path):
        return jsonify({'error': 'CSV not found'}), 404
    return send_file(
        t.csv_path,
        mimetype='text/csv',
        as_attachment=True,
        download_name=f'SmartRoad_{trip_id}.csv',
    )


# ─────────────────────────────────────────────────────────────────────────────
# ADMIN DASHBOARD
# ─────────────────────────────────────────────────────────────────────────────
DASHBOARD_HTML = r"""
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>SmartRoad 2.0 — Admin Dashboard</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@300;400;500;600;700&family=JetBrains+Mono:wght@400;600&display=swap" rel="stylesheet">
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<script src="https://unpkg.com/leaflet.heat@0.2.0/dist/leaflet-heat.js"></script>
<style>
  :root {
    --bg:        #050510;
    --surface:   #0C0C1E;
    --surface2:  #12122A;
    --border:    #1E1E3F;
    --cyan:      #00C8E8;
    --cyan-dim:  #007A90;
    --green:     #00E676;
    --orange:    #FF9800;
    --red:       #FF4444;
    --yellow:    #FFD600;
    --text:      #E8E8F0;
    --muted:     #5A5A7A;
    --font:      'Space Grotesk', sans-serif;
    --mono:      'JetBrains Mono', monospace;
  }

  * { margin: 0; padding: 0; box-sizing: border-box; }

  body {
    background: var(--bg);
    color: var(--text);
    font-family: var(--font);
    min-height: 100vh;
    overflow-x: hidden;
  }

  /* ── HEADER ─────────────────────────────────────────── */
  header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 16px 28px;
    background: var(--surface);
    border-bottom: 1px solid var(--border);
    position: sticky; top: 0; z-index: 1000;
  }
  .logo { display: flex; align-items: center; gap: 10px; }
  .logo-dot {
    width: 10px; height: 10px; border-radius: 50%;
    background: var(--cyan);
    box-shadow: 0 0 8px var(--cyan);
    animation: pulse 2s ease-in-out infinite;
  }
  @keyframes pulse {
    0%,100% { opacity: 1; transform: scale(1); }
    50%      { opacity: 0.5; transform: scale(1.3); }
  }
  .logo h1 { font-size: 20px; font-weight: 700; letter-spacing: 1px; }
  .logo span { color: var(--cyan); }

  .header-right { display: flex; align-items: center; gap: 16px; }
  #live-badge {
    display: flex; align-items: center; gap: 6px;
    padding: 5px 12px;
    background: rgba(0,200,232,0.08);
    border: 1px solid var(--cyan-dim);
    border-radius: 20px;
    font-size: 12px; color: var(--cyan);
  }
  #last-refresh { font-size: 11px; color: var(--muted); font-family: var(--mono); }

  /* ── LAYOUT ─────────────────────────────────────────── */
  .main { display: grid; grid-template-columns: 340px 1fr; height: calc(100vh - 57px); }

  /* ── SIDEBAR ─────────────────────────────────────────── */
  .sidebar {
    background: var(--surface);
    border-right: 1px solid var(--border);
    display: flex; flex-direction: column;
    overflow: hidden;
  }

  .stats-grid {
    display: grid; grid-template-columns: 1fr 1fr;
    gap: 1px; background: var(--border);
    border-bottom: 1px solid var(--border);
  }
  .stat-cell {
    background: var(--surface);
    padding: 16px;
    transition: background 0.2s;
  }
  .stat-cell:hover { background: var(--surface2); }
  .stat-label { font-size: 10px; color: var(--muted); text-transform: uppercase; letter-spacing: 1px; margin-bottom: 4px; }
  .stat-value { font-size: 28px; font-weight: 700; font-family: var(--mono); line-height: 1; }
  .stat-value.cyan   { color: var(--cyan); }
  .stat-value.green  { color: var(--green); }
  .stat-value.orange { color: var(--orange); }
  .stat-value.red    { color: var(--red); }
  .stat-sub { font-size: 10px; color: var(--muted); margin-top: 2px; }

  /* ── SEVERITY BARS ─────────────────────────────────────── */
  .sev-section {
    padding: 14px 16px;
    border-bottom: 1px solid var(--border);
  }
  .sev-title { font-size: 10px; color: var(--muted); text-transform: uppercase; letter-spacing: 1px; margin-bottom: 10px; }
  .sev-row { margin-bottom: 8px; }
  .sev-row-top { display: flex; justify-content: space-between; margin-bottom: 4px; font-size: 12px; }
  .sev-bar { height: 6px; border-radius: 3px; background: var(--border); overflow: hidden; }
  .sev-fill { height: 100%; border-radius: 3px; transition: width 0.8s ease; }
  .fill-red    { background: var(--red); }
  .fill-orange { background: var(--orange); }
  .fill-yellow { background: var(--yellow); }

  /* ── FILTER BAR ─────────────────────────────────────── */
  .filter-bar {
    padding: 10px 16px;
    border-bottom: 1px solid var(--border);
    display: flex; gap: 6px; flex-wrap: wrap;
  }
  .filter-btn {
    padding: 4px 10px; border-radius: 12px;
    border: 1px solid var(--border);
    background: transparent; color: var(--muted);
    font-family: var(--font); font-size: 11px; cursor: pointer;
    transition: all 0.2s;
  }
  .filter-btn:hover, .filter-btn.active {
    border-color: var(--cyan); color: var(--cyan);
    background: rgba(0,200,232,0.08);
  }

  /* ── TRIPS TABLE ─────────────────────────────────────── */
  .trips-section {
    flex: 1; overflow: hidden;
    display: flex; flex-direction: column;
  }
  .trips-header {
    padding: 10px 16px;
    font-size: 10px; color: var(--muted);
    text-transform: uppercase; letter-spacing: 1px;
    border-bottom: 1px solid var(--border);
    display: flex; justify-content: space-between; align-items: center;
  }
  #trip-count { font-family: var(--mono); color: var(--cyan); }
  #trips-list { flex: 1; overflow-y: auto; }
  #trips-list::-webkit-scrollbar { width: 4px; }
  #trips-list::-webkit-scrollbar-thumb { background: var(--border); border-radius: 2px; }

  .trip-row {
    padding: 10px 16px;
    border-bottom: 1px solid var(--border);
    cursor: pointer;
    transition: background 0.15s;
    display: grid;
    grid-template-columns: 1fr auto;
    gap: 6px;
    align-items: center;
  }
  .trip-row:hover { background: var(--surface2); }
  .trip-row.selected { background: rgba(0,200,232,0.06); border-left: 2px solid var(--cyan); }
  .trip-id { font-family: var(--mono); font-size: 11px; color: var(--cyan); }
  .trip-meta { font-size: 11px; color: var(--muted); margin-top: 2px; }
  .trip-badge {
    padding: 2px 7px; border-radius: 8px; font-size: 10px; font-weight: 600;
    white-space: nowrap;
  }
  .badge-severe   { background: rgba(255,68,68,0.15);  color: var(--red); }
  .badge-moderate { background: rgba(255,152,0,0.15);  color: var(--orange); }
  .badge-mild     { background: rgba(255,214,0,0.12);  color: var(--yellow); }
  .badge-clean    { background: rgba(0,230,118,0.1);   color: var(--green); }

  /* ── MAP ─────────────────────────────────────────────── */
  .map-container { position: relative; }
  #map { width: 100%; height: 100%; }

  /* Leaflet dark override */
  .leaflet-tile-pane { filter: brightness(0.7) saturate(0.6) hue-rotate(200deg); }

  .map-controls {
    position: absolute; top: 14px; right: 14px; z-index: 1000;
    display: flex; flex-direction: column; gap: 8px;
  }
  .map-btn {
    padding: 8px 14px;
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 8px;
    color: var(--text); font-family: var(--font); font-size: 12px;
    cursor: pointer; transition: all 0.2s;
    white-space: nowrap;
  }
  .map-btn:hover, .map-btn.active {
    border-color: var(--cyan); color: var(--cyan);
    background: rgba(0,200,232,0.08);
  }

  .map-legend {
    position: absolute; bottom: 28px; left: 14px; z-index: 1000;
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 10px;
    padding: 12px 16px;
  }
  .legend-title { font-size: 10px; color: var(--muted); text-transform: uppercase; letter-spacing: 1px; margin-bottom: 8px; }
  .legend-row { display: flex; align-items: center; gap: 8px; margin-bottom: 6px; font-size: 12px; }
  .legend-dot { width: 10px; height: 10px; border-radius: 50%; }

  /* ── DETAIL PANEL ─────────────────────────────────────── */
  #detail-panel {
    display: none;
    position: absolute; bottom: 0; left: 0; right: 0; z-index: 900;
    background: var(--surface);
    border-top: 1px solid var(--border);
    padding: 14px 20px;
    max-height: 200px; overflow-y: auto;
  }
  #detail-panel.visible { display: block; }
  .detail-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 10px; }
  .detail-title { font-size: 13px; font-weight: 600; color: var(--cyan); font-family: var(--mono); }
  .detail-close { background: none; border: none; color: var(--muted); cursor: pointer; font-size: 18px; }
  .detail-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(120px, 1fr)); gap: 10px; }
  .detail-cell .dc-label { font-size: 10px; color: var(--muted); text-transform: uppercase; }
  .detail-cell .dc-value { font-size: 14px; font-weight: 600; font-family: var(--mono); }
  .dl-btn {
    padding: 5px 12px; border-radius: 6px;
    background: rgba(0,200,232,0.08); border: 1px solid var(--cyan-dim);
    color: var(--cyan); font-size: 12px; cursor: pointer; font-family: var(--font);
    text-decoration: none; display: inline-block; margin-top: 10px;
  }

  /* ── LOADING ─────────────────────────────────────────── */
  #loading {
    position: fixed; inset: 0; background: var(--bg);
    display: flex; flex-direction: column;
    align-items: center; justify-content: center;
    z-index: 9999; transition: opacity 0.4s;
  }
  #loading.hidden { opacity: 0; pointer-events: none; }
  .loader-ring {
    width: 48px; height: 48px; border-radius: 50%;
    border: 3px solid var(--border);
    border-top-color: var(--cyan);
    animation: spin 0.8s linear infinite;
    margin-bottom: 16px;
  }
  @keyframes spin { to { transform: rotate(360deg); } }
  .loader-text { color: var(--muted); font-size: 13px; }
</style>
</head>
<body>

<div id="loading">
  <div class="loader-ring"></div>
  <div class="loader-text">Loading SmartRoad dashboard…</div>
</div>

<header>
  <div class="logo">
    <div class="logo-dot"></div>
    <h1>Smart<span>Road</span> <span style="color:var(--muted);font-weight:300;font-size:14px">2.0 Admin</span></h1>
  </div>
  <div class="header-right">
    <div id="live-badge">
      <div style="width:6px;height:6px;border-radius:50%;background:var(--green);box-shadow:0 0 5px var(--green)"></div>
      LIVE
    </div>
    <div id="last-refresh">Refreshing…</div>
  </div>
</header>

<div class="main">
  <!-- ── SIDEBAR ── -->
  <div class="sidebar">

    <div class="stats-grid">
      <div class="stat-cell">
        <div class="stat-label">Total Trips</div>
        <div class="stat-value cyan" id="s-trips">—</div>
        <div class="stat-sub" id="s-recent">— last 24h</div>
      </div>
      <div class="stat-cell">
        <div class="stat-label">Devices</div>
        <div class="stat-value green" id="s-devices">—</div>
        <div class="stat-sub">unique</div>
      </div>
      <div class="stat-cell">
        <div class="stat-label">Potholes</div>
        <div class="stat-value orange" id="s-events">—</div>
        <div class="stat-sub">total detected</div>
      </div>
      <div class="stat-cell">
        <div class="stat-label">Severe</div>
        <div class="stat-value red" id="s-severe">—</div>
        <div class="stat-sub">critical events</div>
      </div>
    </div>

    <div class="sev-section">
      <div class="sev-title">Severity Breakdown</div>
      <div class="sev-row">
        <div class="sev-row-top">
          <span style="color:var(--red)">Severe</span>
          <span id="sev-severe-pct" style="font-family:var(--mono);font-size:12px">0%</span>
        </div>
        <div class="sev-bar"><div class="sev-fill fill-red" id="bar-severe" style="width:0%"></div></div>
      </div>
      <div class="sev-row">
        <div class="sev-row-top">
          <span style="color:var(--orange)">Moderate</span>
          <span id="sev-moderate-pct" style="font-family:var(--mono);font-size:12px">0%</span>
        </div>
        <div class="sev-bar"><div class="sev-fill fill-orange" id="bar-moderate" style="width:0%"></div></div>
      </div>
      <div class="sev-row">
        <div class="sev-row-top">
          <span style="color:var(--yellow)">Mild</span>
          <span id="sev-mild-pct" style="font-family:var(--mono);font-size:12px">0%</span>
        </div>
        <div class="sev-bar"><div class="sev-fill fill-yellow" id="bar-mild" style="width:0%"></div></div>
      </div>
    </div>

    <div class="filter-bar">
      <button class="filter-btn active" onclick="filterTrips('all',this)">All</button>
      <button class="filter-btn" onclick="filterTrips('two_wheeler',this)">🏍 Bike</button>
      <button class="filter-btn" onclick="filterTrips('auto',this)">🛺 Auto</button>
      <button class="filter-btn" onclick="filterTrips('car',this)">🚗 Car</button>
      <button class="filter-btn" onclick="filterTrips('bus',this)">🚌 Bus</button>
    </div>

    <div class="trips-section">
      <div class="trips-header">
        <span>Recent Trips</span>
        <span id="trip-count">—</span>
      </div>
      <div id="trips-list"></div>
    </div>
  </div>

  <!-- ── MAP ── -->
  <div class="map-container">
    <div id="map"></div>

    <div class="map-controls">
      <button class="map-btn active" id="btn-markers" onclick="setMode('markers')">📍 Markers</button>
      <button class="map-btn" id="btn-heatmap" onclick="setMode('heatmap')">🌡 Heatmap</button>
      <button class="map-btn" onclick="resetView()">🎯 Bangalore</button>
    </div>

    <div class="map-legend">
      <div class="legend-title">Severity</div>
      <div class="legend-row"><div class="legend-dot" style="background:#FF4444"></div>Severe</div>
      <div class="legend-row"><div class="legend-dot" style="background:#FF9800"></div>Moderate</div>
      <div class="legend-row"><div class="legend-dot" style="background:#FFD600"></div>Mild</div>
    </div>

    <div id="detail-panel">
      <div class="detail-header">
        <div class="detail-title" id="dp-title">Trip Detail</div>
        <button class="detail-close" onclick="closeDetail()">✕</button>
      </div>
      <div class="detail-grid" id="dp-grid"></div>
      <a id="dp-csv" class="dl-btn" href="#">⬇ Download CSV</a>
    </div>
  </div>
</div>

<script>
// ── CONFIG ────────────────────────────────────────────────────────────────
const SERVER = '';  // same origin
const REFRESH_MS = 30000;
const BANGALORE = [12.9716, 77.5946];

// ── STATE ─────────────────────────────────────────────────────────────────
let map, heatLayer, markerLayer;
let allTrips = [], filteredTrips = [];
let currentFilter = 'all';
let mapMode = 'markers';
let selectedTripId = null;
let statsCache = {};

// ── MAP INIT ──────────────────────────────────────────────────────────────
function initMap() {
  map = L.map('map', { zoomControl: false }).setView(BANGALORE, 12);
  L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
    attribution: '© OpenStreetMap',
    maxZoom: 19,
  }).addTo(map);
  L.control.zoom({ position: 'bottomright' }).addTo(map);
  markerLayer = L.layerGroup().addTo(map);
}

// ── SEVERITY COLOR ─────────────────────────────────────────────────────────
function sevColor(sev) {
  return sev === 'severe' ? '#FF4444' : sev === 'moderate' ? '#FF9800' : '#FFD600';
}

function sevBadgeClass(count) {
  if (count === 0) return 'badge-clean';
  return count >= 10 ? 'badge-severe' : count >= 4 ? 'badge-moderate' : 'badge-mild';
}

// ── FETCH + RENDER STATS ───────────────────────────────────────────────────
async function loadStats() {
  try {
    const r = await fetch(`${SERVER}/api/stats`);
    const s = await r.json();
    statsCache = s;

    document.getElementById('s-trips').textContent   = s.total_trips.toLocaleString();
    document.getElementById('s-devices').textContent = s.active_devices.toLocaleString();
    document.getElementById('s-events').textContent  = s.total_events.toLocaleString();
    document.getElementById('s-severe').textContent  = s.severe.toLocaleString();
    document.getElementById('s-recent').textContent  = `${s.trips_last_24h} last 24h`;

    const total = s.severe + s.moderate + s.mild || 1;
    const sp = Math.round(s.severe   / total * 100);
    const mp = Math.round(s.moderate / total * 100);
    const lp = Math.round(s.mild     / total * 100);
    document.getElementById('sev-severe-pct').textContent   = sp + '%';
    document.getElementById('sev-moderate-pct').textContent = mp + '%';
    document.getElementById('sev-mild-pct').textContent     = lp + '%';
    document.getElementById('bar-severe').style.width   = sp + '%';
    document.getElementById('bar-moderate').style.width = mp + '%';
    document.getElementById('bar-mild').style.width     = lp + '%';
  } catch(e) { console.error('Stats error', e); }
}

// ── FETCH + RENDER TRIPS ───────────────────────────────────────────────────
async function loadTrips() {
  try {
    const r = await fetch(`${SERVER}/api/trips?per_page=100`);
    const d = await r.json();
    allTrips = d.trips;
    applyFilter();
  } catch(e) { console.error('Trips error', e); }
}

function applyFilter() {
  filteredTrips = currentFilter === 'all'
    ? allTrips
    : allTrips.filter(t => t.vehicle_type === currentFilter);

  document.getElementById('trip-count').textContent = filteredTrips.length;
  renderTripsList();
}

function filterTrips(vehicle, btn) {
  currentFilter = vehicle;
  document.querySelectorAll('.filter-btn').forEach(b => b.classList.remove('active'));
  btn.classList.add('active');
  applyFilter();
}

function renderTripsList() {
  const el = document.getElementById('trips-list');
  if (!filteredTrips.length) {
    el.innerHTML = '<div style="padding:20px;color:var(--muted);font-size:12px;text-align:center">No trips yet</div>';
    return;
  }
  el.innerHTML = filteredTrips.map(t => {
    const dt = new Date(t.created_at + 'Z');
    const timeStr = dt.toLocaleTimeString([], {hour:'2-digit',minute:'2-digit'});
    const dateStr = dt.toLocaleDateString([], {month:'short',day:'numeric'});
    const badgeClass = sevBadgeClass(t.pothole_count);
    const selected = t.id === selectedTripId ? 'selected' : '';
    const vIcon = {two_wheeler:'🏍',auto:'🛺',car:'🚗',heavy_vehicle:'🚛',bus:'🚌'}[t.vehicle_type] || '🚗';
    return `
      <div class="trip-row ${selected}" onclick="selectTrip('${t.id}')">
        <div>
          <div class="trip-id">${t.id.substring(5,21)}…</div>
          <div class="trip-meta">${vIcon} ${t.vehicle_type || '—'} · ${dateStr} ${timeStr} · ${t.file_size_kb || 0} KB</div>
        </div>
        <div class="trip-badge ${badgeClass}">${t.pothole_count} 🕳</div>
      </div>`;
  }).join('');
}

// ── SELECT TRIP ────────────────────────────────────────────────────────────
async function selectTrip(tripId) {
  selectedTripId = tripId;
  renderTripsList();

  try {
    const r = await fetch(`${SERVER}/api/trip/${tripId}`);
    const d = await r.json();
    const trip = d.trip;
    const events = d.events;

    // Show detail panel
    document.getElementById('dp-title').textContent = trip.id.substring(5,24) + '…';
    document.getElementById('dp-csv').href = `${SERVER}/api/csv/${tripId}`;
    document.getElementById('dp-grid').innerHTML = `
      <div class="detail-cell"><div class="dc-label">Vehicle</div><div class="dc-value">${trip.vehicle_type || '—'}</div></div>
      <div class="detail-cell"><div class="dc-label">Potholes</div><div class="dc-value" style="color:var(--orange)">${trip.pothole_count}</div></div>
      <div class="detail-cell"><div class="dc-label">File Size</div><div class="dc-value">${trip.file_size_kb} KB</div></div>
      <div class="detail-cell"><div class="dc-label">City</div><div class="dc-value">${trip.city}</div></div>
    `;
    document.getElementById('detail-panel').classList.add('visible');

    // Plot events on map
    markerLayer.clearLayers();
    const bounds = [];
    events.forEach(e => {
      if (!e.lat || !e.lon) return;
      const color = sevColor(e.severity);
      const marker = L.circleMarker([e.lat, e.lon], {
        radius: e.severity === 'severe' ? 9 : e.severity === 'moderate' ? 7 : 5,
        fillColor: color, color: '#000',
        weight: 1, opacity: 1, fillOpacity: 0.85,
      }).addTo(markerLayer);
      marker.bindPopup(`
        <b style="color:${color}">${e.severity.toUpperCase()}</b><br>
        Speed: ${e.speed ? e.speed.toFixed(1) : '—'} km/h<br>
        Vibration: ${e.vibration ? e.vibration.toFixed(2) : '—'}
      `);
      bounds.push([e.lat, e.lon]);
    });

    if (bounds.length) {
      map.fitBounds(bounds, { padding: [40, 40] });
    } else if (trip.start_lat) {
      map.setView([trip.start_lat, trip.start_lon], 14);
    }

  } catch(e) { console.error('Trip detail error', e); }
}

function closeDetail() {
  document.getElementById('detail-panel').classList.remove('visible');
  selectedTripId = null;
  renderTripsList();
  loadAllMarkers();
}

// ── ALL MARKERS MODE ───────────────────────────────────────────────────────
async function loadAllMarkers() {
  if (mapMode !== 'markers') return;
  try {
    const r = await fetch(`${SERVER}/api/potholes?limit=2000`);
    const events = await r.json();
    markerLayer.clearLayers();
    events.forEach(e => {
      if (!e.lat || !e.lon) return;
      const color = sevColor(e.severity);
      L.circleMarker([e.lat, e.lon], {
        radius: e.severity === 'severe' ? 7 : e.severity === 'moderate' ? 5 : 3,
        fillColor: color, color: 'transparent',
        weight: 0, fillOpacity: 0.75,
      }).addTo(markerLayer)
       .bindPopup(`<b style="color:${color}">${e.severity.toUpperCase()}</b><br>${e.speed ? e.speed.toFixed(1) + ' km/h' : ''}`);
    });
  } catch(e) { console.error('Markers error', e); }
}

// ── HEATMAP MODE ───────────────────────────────────────────────────────────
async function loadHeatmap() {
  if (mapMode !== 'heatmap') return;
  try {
    const r = await fetch(`${SERVER}/api/heatmap`);
    const points = await r.json();
    if (heatLayer) map.removeLayer(heatLayer);
    heatLayer = L.heatLayer(points, {
      radius: 20, blur: 15, maxZoom: 17,
      gradient: { 0.2: '#FFD600', 0.5: '#FF9800', 0.8: '#FF4444', 1.0: '#CC0000' },
    }).addTo(map);
  } catch(e) { console.error('Heatmap error', e); }
}

// ── MAP MODE TOGGLE ────────────────────────────────────────────────────────
function setMode(mode) {
  mapMode = mode;
  document.querySelectorAll('.map-btn').forEach(b => b.classList.remove('active'));
  document.getElementById('btn-' + mode).classList.add('active');

  if (mode === 'markers') {
    if (heatLayer) map.removeLayer(heatLayer);
    markerLayer.addTo(map);
    loadAllMarkers();
  } else {
    markerLayer.clearLayers();
    loadHeatmap();
  }
}

function resetView() { map.setView(BANGALORE, 12); }

// ── REFRESH LOOP ───────────────────────────────────────────────────────────
function updateTimestamp() {
  const now = new Date();
  document.getElementById('last-refresh').textContent =
    'Updated ' + now.toLocaleTimeString([], {hour:'2-digit',minute:'2-digit',second:'2-digit'});
}

async function refreshAll() {
  await Promise.all([loadStats(), loadTrips()]);
  if (!selectedTripId) {
    if (mapMode === 'markers') await loadAllMarkers();
    else await loadHeatmap();
  }
  updateTimestamp();
}

// ── INIT ───────────────────────────────────────────────────────────────────
window.addEventListener('load', async () => {
  initMap();
  await refreshAll();
  document.getElementById('loading').classList.add('hidden');
  setInterval(refreshAll, REFRESH_MS);
});
</script>
</body>
</html>
"""

@app.route('/dashboard')
def dashboard():
    return render_template_string(DASHBOARD_HTML)


# ─────────────────────────────────────────────────────────────────────────────
# STARTUP
# ─────────────────────────────────────────────────────────────────────────────
if __name__ == '__main__':
    with app.app_context():
        db.create_all()
    app.run(host='0.0.0.0', port=5000, debug=False)