import os
import json
from datetime import datetime, timedelta
from flask import Flask, request, jsonify, render_template_string
from flask_sqlalchemy import SQLAlchemy
from flask_cors import CORS
from sqlalchemy import func

database_url = os.environ.get('DATABASE_URL', 'sqlite:///smartroad_dev.db')
# Render/Supabase gives postgres://, SQLAlchemy needs postgresql://
if database_url.startswith('postgres://'):
    database_url = database_url.replace('postgres://', 'postgresql://', 1)

app.config['SQLALCHEMY_DATABASE_URI'] = database_url

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

CURRENT_VERSION = os.environ.get('APP_VERSION', '2.0.0')
APK_URL = os.environ.get('APK_URL', '')


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
    csv_path      = db.Column(db.String(300))
    csv_uploaded  = db.Column(db.Boolean, default=False)
    retried       = db.Column(db.Boolean, default=False)
    ml_processed  = db.Column(db.Boolean, default=False)
    created_at    = db.Column(db.DateTime, default=datetime.utcnow, index=True)
    events        = db.relationship('PotholeEvent', backref='trip', lazy='dynamic',
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


with app.app_context():
    db.create_all()


@app.route('/health')
def health():
    try:
        db.session.execute(db.text('SELECT 1'))
        trip_count = Trip.query.count()
        return jsonify({
            'status': 'alive',
            'trips': trip_count,
            'time': datetime.utcnow().isoformat(),
        }), 200
    except Exception as e:
        return jsonify({'status': 'db_error', 'error': str(e)}), 500


@app.route('/upload_trip', methods=['POST'])
def upload_trip():
    try:
        data = request.get_json(force=True)
        if not data:
            return jsonify({'error': 'No JSON body'}), 400

        trip_id       = str(data.get('trip_id', '')).strip()
        device_id     = str(data.get('device_id', '')).strip()
        vehicle_type  = str(data.get('vehicle_type', 'unknown'))
        pothole_count = int(data.get('pothole_count', 0))
        city          = str(data.get('city', 'Bangalore'))
        pothole_json  = data.get('pothole_json', [])
        start_lat     = float(data.get('start_lat', 0))
        start_lon     = float(data.get('start_lon', 0))
        end_lat       = float(data.get('end_lat', 0))
        end_lon       = float(data.get('end_lon', 0))
        csv_uploaded  = bool(data.get('csv_uploaded', False))
        retried       = bool(data.get('retried', False))

        if not trip_id or not device_id:
            return jsonify({'error': 'Missing trip_id or device_id'}), 400

        existing = Trip.query.get(trip_id)
        if existing:
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
            csv_path='supabase://trip-csvs/' + trip_id + '.csv' if csv_uploaded else None,
            csv_uploaded=csv_uploaded,
            retried=retried,
        )
        db.session.add(trip)

        db_events = []
        for e in pothole_json:
            try:
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
                    severity=str(e.get('severity', 'mild')),
                    vibration=float(e.get('vibration', 0)),
                    acc_x=float(e.get('acc_x', 0)),
                    acc_y=float(e.get('acc_y', 0)),
                    acc_z=float(e.get('acc_z', 0)),
                    gyro_x=float(e.get('gyro_x', 0)),
                    gyro_y=float(e.get('gyro_y', 0)),
                    gyro_z=float(e.get('gyro_z', 0)),
                    timestamp=int(e.get('timestamp', 0)),
                ))
            except Exception:
                continue

        db.session.bulk_save_objects(db_events)
        db.session.commit()

        return jsonify({
            'status': 'ok',
            'trip_id': trip_id,
            'events_saved': len(db_events),
        }), 200

    except Exception as ex:
        db.session.rollback()
        app.logger.error('Upload error: ' + str(ex))
        return jsonify({'error': str(ex)}), 500


@app.route('/version')
def get_version():
    return jsonify({
        'version': CURRENT_VERSION,
        'apk_url': APK_URL,
        'notes': os.environ.get('RELEASE_NOTES', 'Bug fixes and improvements'),
        'force': os.environ.get('FORCE_UPDATE', 'false').lower() == 'true',
        'min_version': '1.0.0',
    }), 200


@app.route('/api/stats')
def api_stats():
    try:
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
        }), 200
    except Exception as ex:
        return jsonify({'error': str(ex)}), 500


@app.route('/api/potholes')
def api_potholes():
    try:
        limit    = min(int(request.args.get('limit', 2000)), 5000)
        severity = request.args.get('severity')
        min_lat  = request.args.get('min_lat', type=float)
        max_lat  = request.args.get('max_lat', type=float)
        min_lon  = request.args.get('min_lon', type=float)
        max_lon  = request.args.get('max_lon', type=float)

        q = PotholeEvent.query
        if severity:
            q = q.filter_by(severity=severity)
        if min_lat:
            q = q.filter(PotholeEvent.lat >= min_lat)
        if max_lat:
            q = q.filter(PotholeEvent.lat <= max_lat)
        if min_lon:
            q = q.filter(PotholeEvent.lon >= min_lon)
        if max_lon:
            q = q.filter(PotholeEvent.lon <= max_lon)

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
        } for e in events]), 200
    except Exception as ex:
        return jsonify({'error': str(ex)}), 500


@app.route('/api/heatmap')
def api_heatmap():
    try:
        events = PotholeEvent.query.with_entities(
            PotholeEvent.lat, PotholeEvent.lon, PotholeEvent.severity
        ).limit(5000).all()
        weight = {'severe': 1.0, 'moderate': 0.6, 'mild': 0.3}
        return jsonify([
            [e.lat, e.lon, weight.get(e.severity, 0.3)]
            for e in events if e.lat and e.lon
        ]), 200
    except Exception as ex:
        return jsonify({'error': str(ex)}), 500


@app.route('/api/trips')
def api_trips():
    try:
        page     = int(request.args.get('page', 1))
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
        }), 200
    except Exception as ex:
        return jsonify({'error': str(ex)}), 500


@app.route('/api/trip/<trip_id>')
def api_trip_detail(trip_id):
    try:
        t = Trip.query.get_or_404(trip_id)
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
        }), 200
    except Exception as ex:
        return jsonify({'error': str(ex)}), 500


@app.route('/dashboard')
def dashboard():
    return render_template_string(DASHBOARD_HTML)


DASHBOARD_HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>SmartRoad Admin</title>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<script src="https://unpkg.com/leaflet.heat@0.2.0/dist/leaflet-heat.js"></script>
<style>
:root{--p:#1A73E8;--pl:#E8F0FE;--s:#fff;--bg:#F5F7FA;--b:#E8ECF0;--t:#1C1E21;--t2:#65676B;--th:#9EA3A8;--g:#1E8E3E;--gl:#E6F4EA;--r:#D93025;--rl:#FCE8E6;--o:#F9AB00;--ol:#FEF7E0;--f:'Inter',sans-serif;}
*{margin:0;padding:0;box-sizing:border-box;}
body{background:var(--bg);color:var(--t);font-family:var(--f);height:100vh;display:flex;flex-direction:column;overflow:hidden;}
header{background:var(--s);border-bottom:1px solid var(--b);padding:12px 24px;display:flex;align-items:center;gap:12px;z-index:1000;}
.logo{width:32px;height:32px;background:var(--pl);border-radius:8px;display:flex;align-items:center;justify-content:center;font-size:16px;}
header h1{font-size:16px;font-weight:700;}
.hr{margin-left:auto;display:flex;align-items:center;gap:12px;}
.live{display:flex;align-items:center;gap:6px;background:var(--gl);border:1px solid rgba(30,142,62,0.2);border-radius:20px;padding:4px 12px;font-size:11px;font-weight:600;color:var(--g);}
.ld{width:6px;height:6px;background:var(--g);border-radius:50%;animation:pulse 2s infinite;}
@keyframes pulse{0%,100%{opacity:1;transform:scale(1)}50%{opacity:0.5;transform:scale(1.3)}}
#lr{font-size:11px;color:var(--th);font-family:monospace;}
.main{display:grid;grid-template-columns:320px 1fr;flex:1;overflow:hidden;}
.sidebar{background:var(--s);border-right:1px solid var(--b);display:flex;flex-direction:column;overflow:hidden;}
.sg{display:grid;grid-template-columns:1fr 1fr;gap:1px;background:var(--b);}
.sc{background:var(--s);padding:14px 16px;}
.sc:hover{background:var(--bg);}
.sl{font-size:10px;color:var(--th);text-transform:uppercase;letter-spacing:0.8px;font-weight:600;margin-bottom:4px;}
.sv{font-size:26px;font-weight:700;font-family:monospace;line-height:1;}
.sv.blue{color:var(--p)}.sv.green{color:var(--g)}.sv.orange{color:var(--o)}.sv.red{color:var(--r)}
.ss{font-size:10px;color:var(--th);margin-top:2px;}
.sev{padding:12px 16px;border-bottom:1px solid var(--b);}
.sevt{font-size:10px;color:var(--th);text-transform:uppercase;letter-spacing:0.8px;font-weight:600;margin-bottom:10px;}
.sr{margin-bottom:8px;}
.st{display:flex;justify-content:space-between;font-size:12px;margin-bottom:4px;}
.sb{height:5px;background:var(--b);border-radius:3px;overflow:hidden;}
.sf{height:100%;border-radius:3px;transition:width 0.6s ease;}
.fr{background:var(--r)}.fo{background:var(--o)}.fy{background:#FDD663;}
.fb{padding:10px 16px;border-bottom:1px solid var(--b);display:flex;gap:6px;flex-wrap:wrap;}
.fbn{padding:4px 10px;border-radius:20px;border:1px solid var(--b);background:transparent;color:var(--th);font-family:var(--f);font-size:11px;font-weight:500;cursor:pointer;transition:all 0.15s;}
.fbn:hover,.fbn.active{border-color:var(--p);color:var(--p);background:var(--pl);}
.ts{flex:1;overflow:hidden;display:flex;flex-direction:column;}
.th2{padding:10px 16px;display:flex;justify-content:space-between;align-items:center;border-bottom:1px solid var(--b);}
.thl{font-size:10px;color:var(--th);text-transform:uppercase;letter-spacing:0.8px;font-weight:600;}
#tc{font-family:monospace;color:var(--p);font-size:12px;font-weight:600;}
#tl{flex:1;overflow-y:auto;}
#tl::-webkit-scrollbar{width:3px;}
#tl::-webkit-scrollbar-thumb{background:var(--b);border-radius:2px;}
.tr2{padding:10px 16px;border-bottom:1px solid var(--b);cursor:pointer;transition:background 0.1s;display:flex;align-items:center;gap:10px;}
.tr2:hover{background:var(--bg);}
.tr2.selected{background:var(--pl);border-left:2px solid var(--p);}
.ti{flex:1;min-width:0;}
.tid{font-family:monospace;font-size:11px;color:var(--p);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;}
.tm{font-size:11px;color:var(--th);margin-top:1px;}
.badge{padding:2px 8px;border-radius:8px;font-size:10px;font-weight:600;white-space:nowrap;}
.br{background:var(--rl);color:var(--r)}.bo{background:var(--ol);color:var(--o)}.bg2{background:var(--gl);color:var(--g)}
.mc{position:relative;}
#map{width:100%;height:100%;}
.leaflet-tile-pane{filter:saturate(0.8) brightness(1.05);}
.mc2{position:absolute;top:12px;right:12px;z-index:1000;display:flex;gap:8px;}
.mb{padding:7px 14px;background:var(--s);border:1px solid var(--b);border-radius:8px;font-family:var(--f);font-size:12px;font-weight:500;color:var(--t2);cursor:pointer;transition:all 0.15s;box-shadow:0 1px 4px rgba(0,0,0,0.08);}
.mb:hover,.mb.active{border-color:var(--p);color:var(--p);background:var(--pl);}
.ml{position:absolute;bottom:24px;left:12px;z-index:1000;background:var(--s);border:1px solid var(--b);border-radius:10px;padding:12px 14px;box-shadow:0 2px 8px rgba(0,0,0,0.08);}
.mlt{font-size:10px;color:var(--th);text-transform:uppercase;letter-spacing:0.8px;font-weight:600;margin-bottom:8px;}
.mlr{display:flex;align-items:center;gap:8px;font-size:12px;color:var(--t2);margin-bottom:5px;}
.mld{width:9px;height:9px;border-radius:50%;}
#dp{display:none;position:absolute;bottom:0;left:0;right:0;z-index:900;background:var(--s);border-top:1px solid var(--b);padding:14px 20px;max-height:180px;overflow-y:auto;box-shadow:0 -2px 12px rgba(0,0,0,0.06);}
#dp.visible{display:block;}
.dph{display:flex;justify-content:space-between;align-items:center;margin-bottom:10px;}
.dpt{font-family:monospace;font-size:12px;color:var(--p);font-weight:600;}
.dpc{background:none;border:none;color:var(--th);cursor:pointer;font-size:16px;padding:2px 6px;border-radius:6px;}
.dpc:hover{background:var(--bg);}
.dpg{display:grid;grid-template-columns:repeat(auto-fill,minmax(110px,1fr));gap:10px;}
.dcl{font-size:10px;color:var(--th);text-transform:uppercase;letter-spacing:0.6px;font-weight:600;}
.dcv{font-size:14px;font-weight:700;font-family:monospace;color:var(--t);margin-top:2px;}
#ld2{position:fixed;inset:0;background:var(--s);display:flex;flex-direction:column;align-items:center;justify-content:center;z-index:9999;transition:opacity 0.3s;}
#ld2.hidden{opacity:0;pointer-events:none;}
.lr2{width:40px;height:40px;border-radius:50%;border:3px solid var(--b);border-top-color:var(--p);animation:spin 0.8s linear infinite;margin-bottom:14px;}
@keyframes spin{to{transform:rotate(360deg)}}
.lt{color:var(--th);font-size:13px;}
.es{padding:24px 16px;text-align:center;color:var(--th);font-size:12px;}
</style>
</head>
<body>
<div id="ld2"><div class="lr2"></div><div class="lt">Loading dashboard...</div></div>
<header>
  <div class="logo">&#128739;</div>
  <h1>SmartRoad <span style="color:var(--th);font-weight:400">Admin</span></h1>
  <div class="hr">
    <div class="live"><div class="ld"></div>LIVE</div>
    <div id="lr">-</div>
  </div>
</header>
<div class="main">
  <div class="sidebar">
    <div class="sg">
      <div class="sc"><div class="sl">Trips</div><div class="sv blue" id="s-trips">-</div><div class="ss" id="s-recent">- today</div></div>
      <div class="sc"><div class="sl">Devices</div><div class="sv green" id="s-devices">-</div><div class="ss">unique</div></div>
      <div class="sc"><div class="sl">Potholes</div><div class="sv orange" id="s-events">-</div><div class="ss">detected</div></div>
      <div class="sc"><div class="sl">Severe</div><div class="sv red" id="s-severe">-</div><div class="ss">critical</div></div>
    </div>
    <div class="sev">
      <div class="sevt">Severity Breakdown</div>
      <div class="sr"><div class="st"><span style="color:var(--r);font-weight:500">Severe</span><span id="p-s" style="font-family:monospace;font-size:11px">0%</span></div><div class="sb"><div class="sf fr" id="b-s" style="width:0%"></div></div></div>
      <div class="sr"><div class="st"><span style="color:var(--o);font-weight:500">Moderate</span><span id="p-m" style="font-family:monospace;font-size:11px">0%</span></div><div class="sb"><div class="sf fo" id="b-m" style="width:0%"></div></div></div>
      <div class="sr"><div class="st"><span style="color:#B8860B;font-weight:500">Mild</span><span id="p-l" style="font-family:monospace;font-size:11px">0%</span></div><div class="sb"><div class="sf fy" id="b-l" style="width:0%"></div></div></div>
    </div>
    <div class="fb">
      <button class="fbn active" onclick="ft('all',this)">All</button>
      <button class="fbn" onclick="ft('two_wheeler',this)">&#x1F3CD; Bike</button>
      <button class="fbn" onclick="ft('auto',this)">&#x1F6FA; Auto</button>
      <button class="fbn" onclick="ft('car',this)">&#x1F697; Car</button>
      <button class="fbn" onclick="ft('bus',this)">&#x1F68C; Bus</button>
    </div>
    <div class="ts">
      <div class="th2"><span class="thl">Recent Trips</span><span id="tc">-</span></div>
      <div id="tl"></div>
    </div>
  </div>
  <div class="mc">
    <div id="map"></div>
    <div class="mc2">
      <button class="mb active" id="btn-markers" onclick="sm('markers')">&#x1F4CD; Markers</button>
      <button class="mb" id="btn-heatmap" onclick="sm('heatmap')">&#x1F321; Heatmap</button>
      <button class="mb" onclick="rv()">&#x1F3AF; Bangalore</button>
    </div>
    <div class="ml">
      <div class="mlt">Severity</div>
      <div class="mlr"><div class="mld" style="background:#D93025"></div>Severe</div>
      <div class="mlr"><div class="mld" style="background:#F9AB00"></div>Moderate</div>
      <div class="mlr"><div class="mld" style="background:#FDD663"></div>Mild</div>
    </div>
    <div id="dp">
      <div class="dph"><div class="dpt" id="dp-t">Trip Detail</div><button class="dpc" onclick="cd()">&#x2715;</button></div>
      <div class="dpg" id="dp-g"></div>
    </div>
  </div>
</div>
<script>
const BLR=[12.9716,77.5946],RF=30000;
let map,hl,ml2,all=[],cf='all',sid=null,mode='markers';
function im(){
  map=L.map('map',{zoomControl:false}).setView(BLR,12);
  L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png',{attribution:'OpenStreetMap',maxZoom:19}).addTo(map);
  L.control.zoom({position:'bottomright'}).addTo(map);
  ml2=L.layerGroup().addTo(map);
}
function sc(s){return s==='severe'?'#D93025':s==='moderate'?'#F9AB00':'#FDD663';}
async function ls(){
  try{
    const s=await fetch('/api/stats').then(r=>r.json());
    document.getElementById('s-trips').textContent=s.total_trips;
    document.getElementById('s-devices').textContent=s.active_devices;
    document.getElementById('s-events').textContent=s.total_events;
    document.getElementById('s-severe').textContent=s.severe;
    document.getElementById('s-recent').textContent=s.trips_last_24h+' today';
    const t=(s.severe+s.moderate+s.mild)||1;
    const sp=Math.round(s.severe/t*100),mp=Math.round(s.moderate/t*100),lp=Math.round(s.mild/t*100);
    document.getElementById('p-s').textContent=sp+'%';
    document.getElementById('p-m').textContent=mp+'%';
    document.getElementById('p-l').textContent=lp+'%';
    document.getElementById('b-s').style.width=sp+'%';
    document.getElementById('b-m').style.width=mp+'%';
    document.getElementById('b-l').style.width=lp+'%';
  }catch(e){console.error(e);}
}
async function lt2(){
  try{
    const d=await fetch('/api/trips?per_page=100').then(r=>r.json());
    all=d.trips;af();
  }catch(e){console.error(e);}
}
function af(){
  const fl=cf==='all'?all:all.filter(t=>t.vehicle_type===cf);
  document.getElementById('tc').textContent=fl.length;
  const el=document.getElementById('tl');
  if(!fl.length){el.innerHTML='<div class="es">No trips yet</div>';return;}
  el.innerHTML=fl.map(t=>{
    const dt=new Date(t.created_at+'Z');
    const ts=dt.toLocaleTimeString([],{hour:'2-digit',minute:'2-digit'});
    const ds=dt.toLocaleDateString([],{month:'short',day:'numeric'});
    const bc=t.pothole_count===0?'bg2':t.pothole_count>=10?'br':'bo';
    const vi={two_wheeler:'&#x1F3CD;',auto:'&#x1F6FA;',car:'&#x1F697;',heavy_vehicle:'&#x1F69B;',bus:'&#x1F68C;'}[t.vehicle_type]||'&#x1F697;';
    const sel=t.id===sid?' selected':'';
    return '<div class="tr2'+sel+'" onclick="st2(\''+t.id+'\')"><div class="ti"><div class="tid">'+t.id.substring(5,22)+'...</div><div class="tm">'+vi+' '+ds+' '+ts+'</div></div><div class="badge '+bc+'">'+t.pothole_count+' &#x1F573;</div></div>';
  }).join('');
}
function ft(v,btn){cf=v;document.querySelectorAll('.fbn').forEach(b=>b.classList.remove('active'));btn.classList.add('active');af();}
async function st2(id){
  sid=id;af();
  try{
    const d=await fetch('/api/trip/'+id).then(r=>r.json());
    const t=d.trip,ev=d.events;
    document.getElementById('dp-t').textContent=t.id.substring(5,22)+'...';
    document.getElementById('dp-g').innerHTML=
      '<div><div class="dcl">Vehicle</div><div class="dcv">'+(t.vehicle_type||'-')+'</div></div>'+
      '<div><div class="dcl">Potholes</div><div class="dcv" style="color:var(--o)">'+t.pothole_count+'</div></div>'+
      '<div><div class="dcl">CSV</div><div class="dcv" style="color:'+(t.csv_uploaded?'var(--g)':'var(--th)')+'>'+(t.csv_uploaded?'Saved':'No')+'</div></div>'+
      '<div><div class="dcl">City</div><div class="dcv">'+t.city+'</div></div>';
    document.getElementById('dp').classList.add('visible');
    ml2.clearLayers();
    const bounds=[];
    ev.forEach(e=>{
      if(!e.lat||!e.lon)return;
      const c=sc(e.severity);
      L.circleMarker([e.lat,e.lon],{radius:e.severity==='severe'?9:e.severity==='moderate'?7:5,fillColor:c,color:'#fff',weight:1.5,fillOpacity:0.9})
       .addTo(ml2).bindPopup('<b style="color:'+c+'">'+e.severity.toUpperCase()+'</b><br>'+(e.speed?e.speed.toFixed(1)+' km/h':''));
      bounds.push([e.lat,e.lon]);
    });
    if(bounds.length)map.fitBounds(bounds,{padding:[40,40]});
    else if(t.start_lat)map.setView([t.start_lat,t.start_lon],14);
  }catch(e){console.error(e);}
}
function cd(){document.getElementById('dp').classList.remove('visible');sid=null;af();lm();}
async function lm(){
  if(mode!=='markers')return;
  try{
    const ev=await fetch('/api/potholes?limit=2000').then(r=>r.json());
    ml2.clearLayers();
    ev.forEach(e=>{
      if(!e.lat||!e.lon)return;
      const c=sc(e.severity);
      L.circleMarker([e.lat,e.lon],{radius:e.severity==='severe'?7:e.severity==='moderate'?5:3,fillColor:c,color:'transparent',fillOpacity:0.75})
       .addTo(ml2).bindPopup('<b style="color:'+c+'">'+e.severity.toUpperCase()+'</b>');
    });
  }catch(e){console.error(e);}
}
async function lh(){
  if(mode!=='heatmap')return;
  try{
    const pts=await fetch('/api/heatmap').then(r=>r.json());
    if(hl)map.removeLayer(hl);
    hl=L.heatLayer(pts,{radius:20,blur:15,gradient:{'0.2':'#FDD663','0.5':'#F9AB00','0.8':'#D93025','1.0':'#9C1A13'}}).addTo(map);
  }catch(e){console.error(e);}
}
function sm(m){
  mode=m;
  document.querySelectorAll('.mb').forEach(b=>b.classList.remove('active'));
  document.getElementById('btn-'+m).classList.add('active');
  if(m==='markers'){if(hl)map.removeLayer(hl);ml2.addTo(map);lm();}
  else{ml2.clearLayers();lh();}
}
function rv(){map.setView(BLR,12);}
function ut(){document.getElementById('lr').textContent='Updated '+new Date().toLocaleTimeString([],{hour:'2-digit',minute:'2-digit',second:'2-digit'});}
async function ra(){
  await Promise.all([ls(),lt2()]);
  if(!sid){mode==='markers'?lm():lh();}
  ut();
}
window.addEventListener('load',async()=>{
  im();await ra();
  document.getElementById('ld2').classList.add('hidden');
  setInterval(ra,RF);
});
</script>
</body>
</html>
"""

if __name__ == '__main__':
    with app.app_context():
        db.create_all()
    app.run(host='0.0.0.0', port=5000, debug=False)
