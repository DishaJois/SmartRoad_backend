"""
SmartRoad 2.0 — Flask Backend
Team P55 | IEEE IAM Pro CS 2026
"""

import os
import json
from datetime import datetime, timedelta

from flask import (
    Flask,
    request,
    jsonify,
    send_file,
    render_template_string
)

from flask_sqlalchemy import SQLAlchemy
from flask_cors import CORS
from sqlalchemy import func

# ─────────────────────────────────────────────────────────────
# APP SETUP
# ─────────────────────────────────────────────────────────────
app = Flask(__name__)
CORS(app)

# ─────────────────────────────────────────────────────────────
# DATABASE CONFIG
# ─────────────────────────────────────────────────────────────
DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "sqlite:///smartroad_dev.db"
)

# Render + Supabase compatibility
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace(
        "postgres://",
        "postgresql+psycopg://",
        1
    )

elif DATABASE_URL.startswith("postgresql://"):
    DATABASE_URL = DATABASE_URL.replace(
        "postgresql://",
        "postgresql+psycopg://",
        1
    )

app.config["SQLALCHEMY_DATABASE_URI"] = DATABASE_URL
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {
    "pool_pre_ping": True,
    "pool_recycle": 300,
}

app.config["MAX_CONTENT_LENGTH"] = 50 * 1024 * 1024

db = SQLAlchemy(app)

# ─────────────────────────────────────────────────────────────
# CSV STORAGE
# ─────────────────────────────────────────────────────────────
CSV_STORAGE = os.environ.get(
    "CSV_STORAGE_PATH",
    "/tmp/smartroad_csvs"
)

os.makedirs(CSV_STORAGE, exist_ok=True)

# ─────────────────────────────────────────────────────────────
# MODELS
# ─────────────────────────────────────────────────────────────
class Trip(db.Model):
    __tablename__ = "trips"

    id = db.Column(db.String(80), primary_key=True)
    device_id = db.Column(db.String(36), nullable=False, index=True)
    vehicle_type = db.Column(db.String(20))
    pothole_count = db.Column(db.Integer, default=0)

    city = db.Column(db.String(50), default="Bangalore")

    start_lat = db.Column(db.Float)
    start_lon = db.Column(db.Float)

    end_lat = db.Column(db.Float)
    end_lon = db.Column(db.Float)

    csv_path = db.Column(db.String(200))
    file_size_kb = db.Column(db.Float)

    retried = db.Column(db.Boolean, default=False)
    ml_processed = db.Column(db.Boolean, default=False)

    created_at = db.Column(
        db.DateTime,
        default=datetime.utcnow,
        index=True
    )

    events = db.relationship(
        "PotholeEvent",
        backref="trip",
        lazy="dynamic",
        cascade="all, delete-orphan"
    )


class PotholeEvent(db.Model):
    __tablename__ = "pothole_events"

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)

    trip_id = db.Column(
        db.String(80),
        db.ForeignKey("trips.id"),
        index=True
    )

    device_id = db.Column(db.String(36), index=True)

    lat = db.Column(db.Float, nullable=False)
    lon = db.Column(db.Float, nullable=False)

    speed = db.Column(db.Float)
    severity = db.Column(db.String(10), index=True)

    vibration = db.Column(db.Float)

    acc_x = db.Column(db.Float)
    acc_y = db.Column(db.Float)
    acc_z = db.Column(db.Float)

    gyro_x = db.Column(db.Float)
    gyro_y = db.Column(db.Float)
    gyro_z = db.Column(db.Float)

    timestamp = db.Column(db.BigInteger)

    created_at = db.Column(
        db.DateTime,
        default=datetime.utcnow
    )

# ─────────────────────────────────────────────────────────────
# HEALTH
# ─────────────────────────────────────────────────────────────
@app.route("/health")
def health():
    try:
        db.session.execute(db.text("SELECT 1"))

        return jsonify({
            "status": "alive",
            "time": datetime.utcnow().isoformat()
        }), 200

    except Exception as e:
        return jsonify({
            "status": "error",
            "error": str(e)
        }), 500

# ─────────────────────────────────────────────────────────────
# HOME
# ─────────────────────────────────────────────────────────────
@app.route("/")
def home():
    return jsonify({
        "message": "SmartRoad Backend Running",
        "status": "ok"
    })

# ─────────────────────────────────────────────────────────────
# UPLOAD TRIP
# ─────────────────────────────────────────────────────────────
@app.route("/upload_trip", methods=["POST"])
def upload_trip():

    try:
        trip_id = request.form.get("trip_id", "").strip()
        device_id = request.form.get("device_id", "").strip()

        vehicle_type = request.form.get(
            "vehicle_type",
            "unknown"
        ).strip()

        pothole_count = int(
            request.form.get("pothole_count", 0)
        )

        city = request.form.get(
            "city",
            "Bangalore"
        ).strip()

        pothole_json = request.form.get(
            "pothole_json",
            "[]"
        )

        start_lat = float(request.form.get("start_lat", 0))
        start_lon = float(request.form.get("start_lon", 0))

        end_lat = float(request.form.get("end_lat", 0))
        end_lon = float(request.form.get("end_lon", 0))

        retried = (
            request.form.get("retried", "false").lower()
            == "true"
        )

        if not trip_id or not device_id:
            return jsonify({
                "error": "Missing trip_id or device_id"
            }), 400

        # Prevent duplicate uploads
        existing = Trip.query.get(trip_id)

        if existing:
            return jsonify({
                "status": "already_exists",
                "trip_id": trip_id
            }), 200

        # Save CSV
        csv_path = None
        file_size = 0

        if "csv_file" in request.files:
            f = request.files["csv_file"]

            csv_path = os.path.join(
                CSV_STORAGE,
                f"{trip_id}.csv"
            )

            f.save(csv_path)

            file_size = (
                os.path.getsize(csv_path) / 1024
            )

        # Create trip
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

            retried=retried
        )

        db.session.add(trip)

        # Save pothole events
        events = json.loads(pothole_json)

        db_events = []

        for e in events:

            lat = float(e.get("lat", 0))
            lon = float(e.get("lon", 0))

            if lat == 0 or lon == 0:
                continue

            db_events.append(
                PotholeEvent(
                    trip_id=trip_id,
                    device_id=device_id,

                    lat=lat,
                    lon=lon,

                    speed=float(e.get("speed", 0)),
                    severity=e.get("severity", "mild"),

                    vibration=float(
                        e.get("vibration", 0)
                    ),

                    acc_x=float(e.get("acc_x", 0)),
                    acc_y=float(e.get("acc_y", 0)),
                    acc_z=float(e.get("acc_z", 0)),

                    gyro_x=float(e.get("gyro_x", 0)),
                    gyro_y=float(e.get("gyro_y", 0)),
                    gyro_z=float(e.get("gyro_z", 0)),

                    timestamp=int(
                        e.get("timestamp", 0)
                    )
                )
            )

        db.session.bulk_save_objects(db_events)

        db.session.commit()

        return jsonify({
            "status": "ok",
            "trip_id": trip_id,
            "events_saved": len(db_events),
            "file_size_kb": round(file_size, 2)
        }), 200

    except Exception as ex:

        db.session.rollback()

        return jsonify({
            "error": str(ex)
        }), 500

# ─────────────────────────────────────────────────────────────
# STATS API
# ─────────────────────────────────────────────────────────────
@app.route("/api/stats")
def api_stats():

    total_trips = Trip.query.count()

    total_devices = db.session.query(
        func.count(func.distinct(Trip.device_id))
    ).scalar()

    total_events = PotholeEvent.query.count()

    severe_count = PotholeEvent.query.filter_by(
        severity="severe"
    ).count()

    moderate_count = PotholeEvent.query.filter_by(
        severity="moderate"
    ).count()

    mild_count = PotholeEvent.query.filter_by(
        severity="mild"
    ).count()

    recent = Trip.query.filter(
        Trip.created_at >= (
            datetime.utcnow() - timedelta(hours=24)
        )
    ).count()

    return jsonify({
        "total_trips": total_trips,
        "active_devices": total_devices,
        "total_events": total_events,
        "severe": severe_count,
        "moderate": moderate_count,
        "mild": mild_count,
        "trips_last_24h": recent
    })

# ─────────────────────────────────────────────────────────────
# ALL POTHOLES
# ─────────────────────────────────────────────────────────────
@app.route("/api/potholes")
def api_potholes():

    events = PotholeEvent.query.order_by(
        PotholeEvent.created_at.desc()
    ).limit(2000).all()

    return jsonify([
        {
            "id": e.id,
            "lat": e.lat,
            "lon": e.lon,
            "severity": e.severity,
            "speed": e.speed,
            "vibration": e.vibration,
            "timestamp": e.timestamp
        }
        for e in events
    ])

# ─────────────────────────────────────────────────────────────
# TRIPS API
# ─────────────────────────────────────────────────────────────
@app.route("/api/trips")
def api_trips():

    trips = Trip.query.order_by(
        Trip.created_at.desc()
    ).limit(100).all()

    return jsonify({
        "trips": [
            {
                "id": t.id,
                "device_id": t.device_id,
                "vehicle_type": t.vehicle_type,
                "pothole_count": t.pothole_count,
                "city": t.city,
                "created_at": t.created_at.isoformat(),
                "file_size_kb": t.file_size_kb
            }
            for t in trips
        ]
    })

# ─────────────────────────────────────────────────────────────
# CSV DOWNLOAD
# ─────────────────────────────────────────────────────────────
@app.route("/api/csv/<trip_id>")
def api_csv_download(trip_id):

    trip = Trip.query.get_or_404(trip_id)

    if (
        not trip.csv_path
        or not os.path.exists(trip.csv_path)
    ):
        return jsonify({
            "error": "CSV not found"
        }), 404

    return send_file(
        trip.csv_path,
        mimetype="text/csv",
        as_attachment=True,
        download_name=f"{trip_id}.csv"
    )

# ─────────────────────────────────────────────────────────────
# SIMPLE DASHBOARD
# ─────────────────────────────────────────────────────────────
@app.route("/dashboard")
def dashboard():

    html = """
    <html>
    <head>
        <title>SmartRoad Dashboard</title>
        <style>
            body{
                background:#0f172a;
                color:white;
                font-family:Arial;
                padding:40px;
            }

            h1{
                color:#38bdf8;
            }

            .card{
                background:#1e293b;
                padding:20px;
                border-radius:12px;
                margin-bottom:20px;
            }
        </style>
    </head>

    <body>

        <h1>SmartRoad 2.0 Dashboard</h1>

        <div class="card">
            Backend is running successfully.
        </div>

        <div class="card">
            Database connected successfully.
        </div>

    </body>
    </html>
    """

    return render_template_string(html)

# ─────────────────────────────────────────────────────────────
# STARTUP
# ─────────────────────────────────────────────────────────────
with app.app_context():
    db.create_all()

if __name__ == "__main__":

    app.run(
        host="0.0.0.0",
        port=5000,
        debug=False
    )