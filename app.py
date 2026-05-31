"""
SmartRoad 2.0 — Flask Backend
Team P55 | IEEE IAM Pro CS 2026
"""

import os
import json
from datetime import datetime, timedelta
from flask import Flask, request, jsonify, render_template_string, send_file
from flask_sqlalchemy import SQLAlchemy
from flask_cors import CORS
from sqlalchemy import func

# ─────────────────────────────────────────────────────────────────────────────
# APP SETUP
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
app.config['MAX_CONTENT_LENGTH'] = 50 * 1024 * 1024

db = SQLAlchemy(app)

# OTA version info
CURRENT_VERSION = os.environ.get('APP_VERSION', '2.0.0')
APK_URL         = os.environ.get('APK_URL', '')

# ─────────────────────────────────────────────────────────────────────────────
# MODELS
# ─────────────────────────────────────────────────────────────────────────────
class Trip(db.Model):
    __tablename__ = 'trips'
    id            = db.Column(db.String(80),  primary_key=True)
    device_id     = db.Column(db.String(36),  nullable=False, index=True)
    vehicle_type  = db.Column(db.String(20))
    pothole_count = db.Column(db.Integer, default=0)
    city          = db.Column(db.String(50),  default='Bangalore')
    start_lat     = db.Column(db.Float)
    start_lon     = db.Column(db.Float)
    end_lat       = db.Column(db.Float)
    end_lon       = db.Column(db.Float)
    csv_path      = db.Column(db.String(300))
    csv_uploaded  = db.Column(db.Boolean, default=False)
    retried       = db.Column(db.Boolean, default=False)
    ml_processed  = db.Column(db.Boolean, default=False)
    created_at    = db.Column(db.DateTime, default=datetime.utcnow, index=True)
    events        = db.relationship('PotholeEvent', backref='trip', lazy='dynamic',
                                    cascade='all, delete-orphan')


class PotholeEvent(db.Model):
    __tablename__ = 'pothole_events'
    id         = db.Column(db.Integer,    primary_key=True, autoincrement=True)
    trip_id    = db.Column(db.String(80), db.ForeignKey('trips.id'), index=True)
    device_id  = db.Column(db.String(36), index=True)
    lat        = db.Column(db.Float,  nullable=False)
    lon        = db.Column(db.Float,  nullable=False)
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
# UPLOAD TRIP — accepts JSON body (CSV is in Supabase Storage)
# ─────────────────────────────────────────────────────────────────────────────
@app.route('/upload_trip', methods=['POST'])
def upload_trip():
    try:
        data = request.get_json(force=True)
        if not data:
            return jsonify({'error': 'No JSON body'}), 400

        trip_id       = str(data.get('trip_id',       '')).strip()
        device_id     = str(data.get('device_id',     '')).strip()
        vehicle_type  = str(data.get('vehicle_type',  'unknown'))
        pothole_count = int(data.get('pothole_count',  0))
        city          = str(data.get('city',          'Bangalore'))
        pothole_json  = data.get('pothole_json',      [])
        start_lat     = float(data.get('start_lat',   0))
        start_lon     = float(data.get('start_lon',   0))
        end_lat       = float(data.get('end_lat',     0))
        end_lon       = float(data.get('end_lon',     0))
        csv_uploaded  = bool(data.get('csv_uploaded', False))
        retried       = bool(data.get('retried',      False))

        if not trip_id or not device_id:
            return jsonify({'error': 'Missing trip_id or device_id'}), 400

        # Idempotent — skip if already saved
        if Trip.query.get(trip_id):
            return jsonify({'status': 'already_exists', 'trip_id': trip_id}), 200

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
            csv_path=f'supabase://trip-csvs/{trip_id}.csv' if csv_uploaded else None,
            csv_uploaded=csv_uploaded,
            retried=retried,
        )
        db.session.add(trip)

        db_events = []
        for e in pothole_json:
            lat = float(e.get('lat', 0))
            lon = float(e.get('lon', 0))
            if lat == 0 or lon == 0:
                continue
            db_events.append(PotholeEvent(
                trip_id=trip_id, device_id=device_id,
                lat=lat, lon=lon,
                speed=float(e.get('speed',     0)),
                severity=e.get('severity',     'mild'),
                vibration=float(e.get('vibration', 0)),
                acc_x=float(e.get('acc_x',     0)),
                acc_y=float(e.get('acc_y',     0)),
                acc_z=float(e.get('acc_z',     0)),
                gyro_x=float(e.get('gyro_x',   0)),
                gyro_y=float(e.get('gyro_y',   0)),
                gyro_z=float(e.get('gyro_z',   0)),
                timestamp=int(e.get('timestamp', 0)),
            ))

        db.session.bulk_save_objects(db_events)
        db.session.commit()

        return jsonify({
            'status': 'ok',
            'trip_id': trip_id,
            'events_saved': len(db_events),
        }), 200

    except Exception as ex:
        db.session.rollback()
        app.logger.error(f'Upload error: {ex}')
        return jsonify({'error': str(ex)}), 500


# ─────────────────────────────────────────────────────────────────────────────
# HEALTH — UptimeRobot target + DB ping
# ─────────────────────────────────────────────────────────────────────────────
@app.route('/health')
def health():
    try:
        db.session.execute(db.text('SELECT 1'))
        trip_count = Trip.query.count()
        return jsonify({
            'status': 'alive',
            'trips':  trip_count,
            'time':   datetime.utcnow().isoformat(),
        }), 200
    except Exception as e:
        return jsonify({'status': 'db_error', 'error': str(e)}), 500


# ─────────────────────────────────────────────────────────────────────────────
# OTA VERSION
# ─────────────────────────────────────────────────────────────────────────────
@app.route('/version')
def get_version():
    return jsonify({
        'version':     CURRENT_VERSION,
        'apk_url':     APK_URL,
        'notes':       os.environ.get('RELEASE_NOTES', 'Bug fixes and improvements'),
        'force':       os.environ.get('FORCE_UPDATE', 'false').lower() == 'true',
        'min_version': '1.0.0',
    }), 200


# ─────────────────────────────────────────────────────────────────────────────
# API — STATS
# ─────────────────────────────────────────────────────────────────────────────
@app.route('/api/stats')
def api_stats():
    total_trips    = Trip.query.count()
    total_devices  = db.session.query(func.count(func.distinct(Trip.device_id))).scalar()
    total_events   = PotholeEvent.query.count()
    severe_count   = PotholeEvent.query.filter_by(severity='severe').count()
    moderate_count = PotholeEvent.query.filter_by(severity='moderate').count()
    mild_count     = PotholeEvent.query.filter_by(severity='mild').count()
    recent         = Trip.query.filter(
        Trip.created_at >= datetime.utcnow() - timedelta(hours=24)
    ).count()
    return jsonify({
        'total_trips':    total_trips,
        'active_devices': total_devices,
        'total_events':   total_events,
        'severe':         severe_count,
        'moderate':       moderate_count,
        'mild':           mild_count,
        'trips_last_24h': recent,
    })


# ─────────────────────────────────────────────────────────────────────────────
# API — POTHOLES
# ─────────────────────────────────────────────────────────────────────────────
@app.route('/api/potholes')
def api_potholes():
    limit    = min(int(request.args.get('limit',    2000)), 5000)
    severity = request.args.get('severity')
    min_lat  = request.args.get('min_lat', type=float)
    max_lat  = request.args.get('max_lat', type=float)
    min_lon  = request.args.get('min_lon', type=float)
    max_lon  = request.args.get('max_lon', type=float)

    q = PotholeEvent.query
    if severity: q = q.filter_by(severity=severity)
    if min_lat:  q = q.filter(PotholeEvent.lat >= min_lat)
    if max_lat:  q = q.filter(PotholeEvent.lat <= max_lat)
    if min_lon:  q = q.filter(PotholeEvent.lon >= min_lon)
    if max_lon:  q = q.filter(PotholeEvent.lon <= max_lon)

    events = q.order_by(PotholeEvent.created_at.desc()).limit(limit).all()
    return jsonify([{
        'id':         e.id,
        'lat':        e.lat,
        'lon':        e.lon,
        'severity':   e.severity,
        'speed':      e.speed,
        'vibration':  e.vibration,
        'timestamp':  e.timestamp,
        'created_at': e.created_at.isoformat(),
    } for e in events])


# ─────────────────────────────────────────────────────────────────────────────
# API — HEATMAP
# ─────────────────────────────────────────────────────────────────────────────
@app.route('/api/heatmap')
def api_heatmap():
    events = PotholeEvent.query.with_entities(
        PotholeEvent.lat, PotholeEvent.lon, PotholeEvent.severity
    ).limit(5000).all()
    weight = {'severe': 1.0, 'moderate': 0.6, 'mild': 0.3}
    return jsonify([
        [e.lat, e.lon, weight.get(e.severity, 0.3)]
        for e in events if e.lat and e.lon
    ])


# ─────────────────────────────────────────────────────────────────────────────
# API — TRIPS
# ─────────────────────────────────────────────────────────────────────────────
@app.route('/api/trips')
def api_trips():
    page     = int(request.args.get('page',     1))
    per_page = int(request.args.get('per_page', 50))
    trips    = Trip.query.order_by(Trip.created_at.desc()).paginate(
        page=page, per_page=per_page, error_out=False
    )
    return jsonify({
        'trips': [{
            'id':            t.id,
            'device_id':     t.device_id[:8] + '...',
            'vehicle_type':  t.vehicle_type,
            'pothole_count': t.pothole_count,
            'city':          t.city,
            'csv_uploaded':  t.csv_uploaded,
            'start_lat':     t.start_lat,
            'start_lon':     t.start_lon,
            'created_at':    t.created_at.isoformat(),
        } for t in trips.items],
        'total': trips.total,
        'page':  trips.page,
        'pages': trips.pages,
    })


# ─────────────────────────────────────────────────────────────────────────────
# API — SINGLE TRIP
# ─────────────────────────────────────────────────────────────────────────────
@app.route('/api/trip/<trip_id>')
def api_trip_detail(trip_id):
    t      = Trip.query.get_or_404(trip_id)
    events = t.events.all()
    return jsonify({
        'trip': {
            'id':            t.id,
            'device_id':     t.device_id,
            'vehicle_type':  t.vehicle_type,
            'pothole_count': t.pothole_count,
            'city':          t.city,
            'csv_uploaded':  t.csv_uploaded,
            'csv_path':      t.csv_path,
            'start_lat':     t.start_lat,
            'start_lon':     t.start_lon,
            'end_lat':       t.end_lat,
            'end_lon':       t.end_lon,
            'created_at':    t.created_at.isoformat(),
        },
        'events': [{
            'lat':       e.lat,
            'lon':       e.lon,
            'severity':  e.severity,
            'speed':     e.speed,
            'vibration': e.vibration,
            'timestamp': e.timestamp,
        } for e in events],
    })


# ─────────────────────────────────────────────────────────────────────────────
# DASHBOARD HTML
# ─────────────────────────────────────────────────────────────────────────────
DASHBOARD_HTML = r"""
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>SmartRoad Admin</title>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&family=JetBrains+Mono:wght@400;600&display=swap" rel="stylesheet">
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<script src="https://unpkg.com/leaflet.heat@0.2.0/dist/leaflet-heat.js"></script>
<style>
  :root {
    --primary:   #1A73E8;
    --primary-l: #E8F0FE;
    --surface:   #FFFFFF;
    --bg:        #F5F7FA;
    --border:    #E8ECF0;
    --text:      #1C1E21;
    --text2:     #65676B;
    --hint:      #9EA3A8;
    --green:     #1E8E3E;
    --green-l:   #E6F4EA;
    --red:       #D93025;
    --red-l:     #FCE8E6;
    --orange:    #F9AB00;
    --orange-l:  #FEF7E0;
    --font:      'Inter', sans-serif;
    --mono:      'JetBrains Mono', monospace;
    --radius:    12px;
  }
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body { background: var(--bg); color: var(--text); font-family: var(--font); height: 100vh; display: flex; flex-direction: column; overflow: hidden; }

  /* HEADER */
  header {
    background: var(--surface); border-bottom: 1px solid var(--border);
    padding: 12px 24px; display: flex; align-items: center; gap: 12px;
    position: sticky; top: 0; z-index: 1000;
  }
  .logo-icon {
    width: 32px; height: 32px; background: var(--primary-l);
    border-radius: 8px; display: flex; align-items: center; justify-content: center;
    font-size: 16px;
  }
  header h1 { font-size: 16px; font-weight: 700; color: var(--text); letter-spacing: -0.3px; }
  .header-right { margin-left: auto; display: flex; align-items: center; gap: 12px; }
  .live-badge {
    display: flex; align-items: center; gap: 6px;
    background: var(--green-l); border: 1px solid rgba(30,142,62,0.2);
    border-radius: 20px; padding: 4px 12px;
    font-size: 11px; font-weight: 600; color: var(--green);
  }
  .live-dot { width: 6px; height: 6px; background: var(--green); border-radius: 50%; animation: pulse 2s infinite; }
  @keyframes pulse { 0%,100%{opacity:1;transform:scale(1)} 50%{opacity:0.5;transform:scale(1.3)} }
  #last-refresh { font-size: 11px; color: var(--hint); font-family: var(--mono); }

  /* LAYOUT */
  .main { display: grid; grid-template-columns: 320px 1fr; flex: 1; overflow: hidden; }

  /* SIDEBAR */
  .sidebar { background: var(--surface); border-right: 1px solid var(--border); display: flex; flex-direction: column; overflow: hidden; }

  /* STATS GRID */
  .stats-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 1px; background: var(--border); }
  .stat-cell { background: var(--surface); padding: 14px 16px; }
  .stat-cell:hover { background: var(--bg); }
  .stat-label { font-size: 10px; color: var(--hint); text-transform: uppercase; letter-spacing: 0.8px; font-weight: 600; margin-bottom: 4px; }
  .stat-value { font-size: 26px; font-weight: 700; font-family: var(--mono); line-height: 1; }
  .stat-value.blue   { color: var(--primary); }
  .stat-value.green  { color: var(--green); }
  .stat-value.orange { color: var(--orange); }
  .stat-value.red    { color: var(--red); }
  .stat-sub { font-size: 10px; color: var(--hint); margin-top: 2px; }

  /* SEV BARS */
  .sev-section { padding: 12px 16px; border-bottom: 1px solid var(--border); }
  .sev-title { font-size: 10px; color: var(--hint); text-transform: uppercase; letter-spacing: 0.8px; font-weight: 600; margin-bottom: 10px; }
  .sev-row { margin-bottom: 8px; }
  .sev-top { display: flex; justify-content: space-between; font-size: 12px; margin-bottom: 4px; }
  .sev-bar { height: 5px; background: var(--border); border-radius: 3px; overflow: hidden; }
  .sev-fill { height: 100%; border-radius: 3px; transition: width 0.6s ease; }
  .fill-red    { background: var(--red); }
  .fill-orange { background: var(--orange); }
  .fill-yellow { background: #FDD663; }

  /* FILTERS */
  .filter-bar { padding: 10px 16px; border-bottom: 1px solid var(--border); display: flex; gap: 6px; flex-wrap: wrap; }
  .filter-btn { padding: 4px 10px; border-radius: 20px; border: 1px solid var(--border); background: transparent; color: var(--hint); font-family: var(--font); font-size: 11px; font-weight: 500; cursor: pointer; transition: all 0.15s; }
  .filter-btn:hover, .filter-btn.active { border-color: var(--primary); color: var(--primary); background: var(--primary-l); }

  /* TRIPS LIST */
  .trips-section { flex: 1; overflow: hidden; display: flex; flex-direction: column; }
  .trips-header { padding: 10px 16px; display: flex; justify-content: space-between; align-items: center; border-bottom: 1px solid var(--border); }
  .trips-header-label { font-size: 10px; color: var(--hint); text-transform: uppercase; letter-spacing: 0.8px; font-weight: 600; }
  #trip-count { font-family: var(--mono); color: var(--primary); font-size: 12px; font-weight: 600; }
  #trips-list { flex: 1; overflow-y: auto; }
  #trips-list::-webkit-scrollbar { width: 3px; }
  #trips-list::-webkit-scrollbar-thumb { background: var(--border); border-radius: 2px; }

  .trip-row { padding: 10px 16px; border-bottom: 1px solid var(--border); cursor: pointer; transition: background 0.1s; display: flex; align-items: center; gap: 10px; }
  .trip-row:hover { background: var(--bg); }
  .trip-row.selected { background: var(--primary-l); border-left: 2px solid var(--primary); }
  .trip-info { flex: 1; min-width: 0; }
  .trip-id { font-family: var(--mono); font-size: 11px; color: var(--primary); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .trip-meta { font-size: 11px; color: var(--hint); margin-top: 1px; }
  .badge { padding: 2px 8px; border-radius: 8px; font-size: 10px; font-weight: 600; white-space: nowrap; }
  .badge-red    { background: var(--red-l);    color: var(--red); }
  .badge-orange { background: var(--orange-l); color: var(--orange); }
  .badge-green  { background: var(--green-l);  color: var(--green); }

  /* MAP */
  .map-container { position: relative; }
  #map { width: 100%; height: 100%; }
  .leaflet-tile-pane { filter: saturate(0.8) brightness(1.05); }

  .map-controls { position: absolute; top: 12px; right: 12px; z-index: 1000; display: flex; gap: 8px; }
  .map-btn { padding: 7px 14px; background: var(--surface); border: 1px solid var(--border); border-radius: 8px; font-family: var(--font); font-size: 12px; font-weight: 500; color: var(--text2); cursor: pointer; transition: all 0.15s; box-shadow: 0 1px 4px rgba(0,0,0,0.08); }
  .map-btn:hover, .map-btn.active { border-color: var(--primary); color: var(--primary); background: var(--primary-l); }

  .map-legend { position: absolute; bottom: 24px; left: 12px; z-index: 1000; background: var(--surface); border: 1px solid var(--border); border-radius: 10px; padding: 12px 14px; box-shadow: 0 2px 8px rgba(0,0,0,0.08); }
  .legend-title { font-size: 10px; color: var(--hint); text-transform: uppercase; letter-spacing: 0.8px; font-weight: 600; margin-bottom: 8px; }
  .legend-row { display: flex; align-items: center; gap: 8px; font-size: 12px; color: var(--text2); margin-bottom: 5px; }
  .legend-dot { width: 9px; height: 9px; border-radius: 50%; }

  /* DETAIL PANEL */
  #detail-panel { display: none; position: absolute; bottom: 0; left: 0; right: 0; z-index: 900; background: var(--surface); border-top: 1px solid var(--border); padding: 14px 20px; max-height: 180px; overflow-y: auto; box-shadow: 0 -2px 12px rgba(0,0,0,0.06); }
  #detail-panel.visible { display: block; }
  .detail-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 10px; }
  .detail-title { font-family: var(--mono); font-size: 12px; color: var(--primary); font-weight: 600; }
  .detail-close { background: none; border: none; color: var(--hint); cursor: pointer; font-size: 16px; padding: 2px 6px; border-radius: 6px; }
  .detail-close:hover { background: var(--bg); }
  .detail-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(110px, 1fr)); gap: 10px; }
  .dc-label { font-size: 10px; color: var(--hint); text-transform: uppercase; letter-spacing: 0.6px; font-weight: 600; }
  .dc-value { font-size: 14px; font-weight: 700; font-family: var(--mono); color: var(--text); margin-top: 2px; }

  /* LOADING */
  #loading { position: fixed; inset: 0; background: var(--surface); display: flex; flex-direction: column; align-items: center; justify-content: center; z-index: 9999; transition: opacity 0.3s; }
  #loading.hidden { opacity: 0; pointer-events: none; }
  .loader-ring { width: 40px; height: 40px; border-radius: 50%; border: 3px solid var(--border); border-top-color: var(--primary); animation: spin 0.8s linear infinite; margin-bottom: 14px; }
  @keyframes spin { to { transform: rotate(360deg); } }
  .loader-text { color: var(--hint); font-size: 13px; }

  /* EMPTY */
  .empty-state { padding: 24px 16px; text-align: center; color: var(--hint); font-size: 12px; }
</style>
</head>
<body>

<div id="loading">
  <div class="loader-ring"></div>
  <div class="loader-text">Loading dashboard...</div>
</div>

<header>
  <div class="logo-icon">🛣</div>
  <h1>SmartRoad <span style="color:var(--hint);font-weight:400">Admin</span></h1>
  <div class="header-right">
    <div class="live-badge"><div class="live-dot"></div>LIVE</div>
    <div id="last-refresh">—</div>
  </div>
</header>

<div class="main">
  <div class="sidebar">
    <div class="stats-grid">
      <div class="stat-cell">
        <div class="stat-label">Trips</div>
        <div class="stat-value blue" id="s-trips">—</div>
        <div class="stat-sub" id="s-recent">— today</div>
      </div>
      <div class="stat-cell">
        <div class="stat-label">Devices</div>
        <div class="stat-value green" id="s-devices">—</div>
        <div class="stat-sub">unique</div>
      </div>
      <div class="stat-cell">
        <div class="stat-label">Potholes</div>
        <div class="stat-value orange" id="s-events">—</div>
        <div class="stat-sub">detected</div>
      </div>
      <div class="stat-cell">
        <div class="stat-label">Severe</div>
        <div class="stat-value red" id="s-severe">—</div>
        <div class="stat-sub">critical</div>
      </div>
    </div>

    <div class="sev-section">
      <div class="sev-title">Severity Breakdown</div>
      <div class="sev-row">
        <div class="sev-top"><span style="color:var(--red);font-weight:500">Severe</span><span id="p-severe" style="font-family:var(--mono);font-size:11px">0%</span></div>
        <div class="sev-bar"><div class="sev-fill fill-red"    id="bar-severe"   style="width:0%"></div></div>
      </div>
      <div class="sev-row">
        <div class="sev-top"><span style="color:var(--orange);font-weight:500">Moderate</span><span id="p-moderate" style="font-family:var(--mono);font-size:11px">0%</span></div>
        <div class="sev-bar"><div class="sev-fill fill-orange" id="bar-moderate" style="width:0%"></div></div>
      </div>
      <div class="sev-row">
        <div class="sev-top"><span style="color:#B8860B;font-weight:500">Mild</span><span id="p-mild" style="font-family:var(--mono);font-size:11px">0%</span></div>
        <div class="sev-bar"><div class="sev-fill fill-yellow" id="bar-mild"     style="width:0%"></div></div>
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
        <span class="trips-header-label">Recent Trips</span>
        <span id="trip-count">—</span>
      </div>
      <div id="trips-list"></div>
    </div>
  </div>

  <div class="map-container">
    <div id="map"></div>
    <div class="map-controls">
      <button class="map-btn active" id="btn-markers" onclick="setMode('markers')">📍 Markers</button>
      <button class="map-btn"        id="btn-heatmap" onclick="setMode('heatmap')">🌡 Heatmap</button>
      <button class="map-btn"        onclick="resetView()">🎯 Bangalore</button>
    </div>
    <div class="map-legend">
      <div class="legend-title">Severity</div>
      <div class="legend-row"><div class="legend-dot" style="background:#D93025"></div>Severe</div>
      <div class="legend-row"><div class="legend-dot" style="background:#F9AB00"></div>Moderate</div>
      <div class="legend-row"><div class="legend-dot" style="background:#FDD663"></div>Mild</div>
    </div>
    <div id="detail-panel">
      <div class="detail-header">
        <div class="detail-title" id="dp-title">Trip Detail</div>
        <button class="detail-close" onclick="closeDetail()">✕</button>
      </div>
      <div class="detail-grid" id="dp-grid"></div>
    </div>
  </div>
</div>

<script>
const BANGALORE = [12.9716, 77.5946];
const REFRESH   = 30000;
let map, heatLayer, markerLayer;
let allTrips = [], currentFilter = 'all', selectedTripId = null, mapMode = 'markers';

function initMap() {
  map = L.map('map', { zoomControl: false }).setView(BANGALORE, 12);
  L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', { attribution: '© OpenStreetMap', maxZoom: 19 }).addTo(map);
  L.control.zoom({ position: 'bottomright' }).addTo(map);
  markerLayer = L.layerGroup().addTo(map);
}

function sevColor(s) { return s==='severe'?'#D93025':s==='moderate'?'#F9AB00':'#FDD663'; }

async function loadStats() {
  try {
    const s = await fetch('/api/stats').then(r=>r.json());
    document.getElementById('s-trips').textContent   = s.total_trips.toLocaleString();
    document.getElementById('s-devices').textContent = s.active_devices.toLocaleString();
    document.getElementById('s-events').textContent  = s.total_events.toLocaleString();
    document.getElementById('s-severe').textContent  = s.severe.toLocaleString();
    document.getElementById('s-recent').textContent  = s.trips_last_24h + ' today';
    const total = (s.severe + s.moderate + s.mild) || 1;
    const sp = Math.round(s.severe/total*100);
    const mp = Math.round(s.moderate/total*100);
    const lp = Math.round(s.mild/total*100);
    document.getElementById('p-severe').textContent   = sp+'%';
    document.getElementById('p-moderate').textContent = mp+'%';
    document.getElementById('p-mild').textContent     = lp+'%';
    document.getElementById('bar-severe').style.width   = sp+'%';
    document.getElementById('bar-moderate').style.width = mp+'%';
    document.getElementById('bar-mild').style.width     = lp+'%';
  } catch(e) { console.error(e); }
}

async function loadTrips() {
  try {
    const d = await fetch('/api/trips?per_page=100').then(r=>r.json());
    allTrips = d.trips; applyFilter();
  } catch(e) { console.error(e); }
}

function applyFilter() {
  const filtered = currentFilter==='all' ? allTrips : allTrips.filter(t=>t.vehicle_type===currentFilter);
  document.getElementById('trip-count').textContent = filtered.length;
  const el = document.getElementById('trips-list');
  if (!filtered.length) { el.innerHTML = '<div class="empty-state">No trips recorded yet</div>'; return; }
  el.innerHTML = filtered.map(t => {
    const dt = new Date(t.created_at+'Z');
    const ts = dt.toLocaleTimeString([],{hour:'2-digit',minute:'2-digit'});
    const ds = dt.toLocaleDateString([],{month:'short',day:'numeric'});
    const bc = t.pothole_count===0?'badge-green':t.pothole_count>=10?'badge-red':'badge-orange';
    const vi = {two_wheeler:'🏍',auto:'🛺',car:'🚗',heavy_vehicle:'🚛',bus:'🚌'}[t.vehicle_type]||'🚗';
    const sel = t.id===selectedTripId?' selected':'';
    return `<div class="trip-row${sel}" onclick="selectTrip('${t.id}')">
      <div class="trip-info">
        <div class="trip-id">${t.id.substring(5,22)}...</div>
        <div class="trip-meta">${vi} ${ds} ${ts}</div>
      </div>
      <div class="badge ${bc}">${t.pothole_count} 🕳</div>
    </div>`;
  }).join('');
}

function filterTrips(v, btn) {
  currentFilter = v;
  document.querySelectorAll('.filter-btn').forEach(b=>b.classList.remove('active'));
  btn.classList.add('active');
  applyFilter();
}

async function selectTrip(id) {
  selectedTripId = id; applyFilter();
  try {
    const d = await fetch(`/api/trip/${id}`).then(r=>r.json());
    const t = d.trip; const events = d.events;
    document.getElementById('dp-title').textContent = t.id.substring(5,22)+'...';
    document.getElementById('dp-grid').innerHTML = `
      <div><div class="dc-label">Vehicle</div><div class="dc-value">${t.vehicle_type||'—'}</div></div>
      <div><div class="dc-label">Potholes</div><div class="dc-value" style="color:var(--orange)">${t.pothole_count}</div></div>
      <div><div class="dc-label">CSV</div><div class="dc-value" style="color:${t.csv_uploaded?'var(--green)':'var(--hint)'}">${t.csv_uploaded?'Saved':'No'}</div></div>
      <div><div class="dc-label">City</div><div class="dc-value">${t.city}</div></div>`;
    document.getElementById('detail-panel').classList.add('visible');
    markerLayer.clearLayers();
    const bounds = [];
    events.forEach(e => {
      if (!e.lat||!e.lon) return;
      const c = sevColor(e.severity);
      L.circleMarker([e.lat,e.lon],{radius:e.severity==='severe'?9:e.severity==='moderate'?7:5,fillColor:c,color:'#fff',weight:1.5,fillOpacity:0.9})
       .addTo(markerLayer)
       .bindPopup(`<b style="color:${c}">${e.severity.toUpperCase()}</b><br>${e.speed?e.speed.toFixed(1)+' km/h':''}`);
      bounds.push([e.lat,e.lon]);
    });
    if (bounds.length) map.fitBounds(bounds, {padding:[40,40]});
    else if (t.start_lat) map.setView([t.start_lat,t.start_lon],14);
  } catch(e) { console.error(e); }
}

function closeDetail() {
  document.getElementById('detail-panel').classList.remove('visible');
  selectedTripId = null; applyFilter(); loadAllMarkers();
}

async function loadAllMarkers() {
  if (mapMode!=='markers') return;
  try {
    const events = await fetch('/api/potholes?limit=2000').then(r=>r.json());
    markerLayer.clearLayers();
    events.forEach(e => {
      if (!e.lat||!e.lon) return;
      const c = sevColor(e.severity);
      L.circleMarker([e.lat,e.lon],{radius:e.severity==='severe'?7:e.severity==='moderate'?5:3,fillColor:c,color:'transparent',fillOpacity:0.75})
       .addTo(markerLayer)
       .bindPopup(`<b style="color:${c}">${e.severity.toUpperCase()}</b>`);
    });
  } catch(e) { console.error(e); }
}

async function loadHeatmap() {
  if (mapMode!=='heatmap') return;
  try {
    const pts = await fetch('/api/heatmap').then(r=>r.json());
    if (heatLayer) map.removeLayer(heatLayer);
    heatLayer = L.heatLayer(pts,{radius:20,blur:15,gradient:{'0.2':'#FDD663','0.5':'#F9AB00','0.8':'#D93025','1.0':'#9C1A13'}}).addTo(map);
  } catch(e) { console.error(e); }
}

function setMode(mode) {
  mapMode = mode;
  document.querySelectorAll('.map-btn').forEach(b=>b.classList.remove('active'));
  document.getElementById('btn-'+mode).classList.add('active');
  if (mode==='markers') { if(heatLayer) map.removeLayer(heatLayer); markerLayer.addTo(map); loadAllMarkers(); }
  else { markerLayer.clearLayers(); loadHeatmap(); }
}

function resetView() { map.setView(BANGALORE, 12); }

function updateTimestamp() {
  const now = new Date();
  document.getElementById('last-refresh').textContent =
    'Updated ' + now.toLocaleTimeString([],{hour:'2-digit',minute:'2-digit',second:'2-digit'});
}

async function refresh() {
  await Promise.all([loadStats(), loadTrips()]);
  if (!selectedTripId) { mapMode==='markers'?loadAllMarkers():loadHeatmap(); }
  updateTimestamp();
}

window.addEventListener('load', async () => {
  initMap();
  await refresh();
  document.getElementById('loading').classList.add('hidden');
  setInterval(refresh, REFRESH);
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