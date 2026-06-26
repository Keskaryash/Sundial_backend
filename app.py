"""
Clockwise-style Time Zone Tracker — Backend API
-------------------------------------------------
Each user has a profile (id, name, timezone, status, photo).
Profiles are looked up by ID. There is no "friends" concept server-side —
each phone keeps its own local list of friend IDs and fetches their
profiles from this server.

Uses PostgreSQL (via DATABASE_URL env var) for persistent storage, since
Render's free web service filesystem is wiped on every restart/redeploy.
"""

import os
import uuid
import psycopg2
import psycopg2.extras
from datetime import datetime, timezone as dt_timezone
from zoneinfo import available_timezones

from flask import Flask, request, jsonify

app = Flask(__name__)


@app.after_request
def add_cors_headers(response):
    """Allow requests from the PWA frontend (a different origin)."""
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, PATCH, DELETE, OPTIONS"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type, X-Admin-Key"
    return response


@app.route("/api/<path:_any>", methods=["OPTIONS"])
def cors_preflight(_any):
    """Handle CORS preflight requests from browsers."""
    return "", 204


DATABASE_URL = os.environ.get("DATABASE_URL", "")
VALID_TIMEZONES = available_timezones()
ADMIN_KEY = os.environ.get("ADMIN_KEY", "")


def require_admin():
    """
    Returns None if the request has a valid admin key, otherwise returns
    a Flask response describing the auth failure. Caller should `return`
    that response immediately if it isn't None.
    """
    if not ADMIN_KEY:
        return jsonify({"error": "Admin access is not configured on this server."}), 503
    provided = request.headers.get("X-Admin-Key", "")
    if provided != ADMIN_KEY:
        return jsonify({"error": "Unauthorized."}), 401
    return None


def get_db():
    conn = psycopg2.connect(DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor)
    return conn


def init_db():
    if not DATABASE_URL:
        # No database configured yet (e.g. local dev without Postgres) — skip silently.
        return
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS profiles (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            timezone TEXT NOT NULL,
            latitude DOUBLE PRECISION,
            longitude DOUBLE PRECISION,
            city_name TEXT DEFAULT '',
            status TEXT NOT NULL DEFAULT 'available',
            status_message TEXT DEFAULT '',
            photo_base64 TEXT DEFAULT '',
            updated_at TEXT NOT NULL
        )
    """)
    conn.commit()
    cur.close()
    conn.close()


def serialize_profile(row):
    return {
        "id": row["id"],
        "name": row["name"],
        "timezone": row["timezone"],
        "latitude": row["latitude"],
        "longitude": row["longitude"],
        "city_name": row["city_name"],
        "status": row["status"],
        "status_message": row["status_message"],
        "photo_base64": row["photo_base64"],
        "updated_at": row["updated_at"],
    }


@app.route("/api/health", methods=["GET"])
def health():
    return jsonify({"ok": True})


@app.route("/api/profile", methods=["POST"])
def create_profile():
    """
    Create a new profile. Returns a unique ID that the creator must
    save locally — this ID is what friends use to look up this person.
    """
    data = request.get_json(force=True) or {}
    name = (data.get("name") or "").strip()
    timezone_str = (data.get("timezone") or "").strip()
    latitude = data.get("latitude")
    longitude = data.get("longitude")
    city_name = (data.get("city_name") or "").strip()

    if not name:
        return jsonify({"error": "name is required"}), 400
    if timezone_str not in VALID_TIMEZONES:
        return jsonify({"error": "invalid timezone"}), 400
    if latitude is not None and not (-90 <= latitude <= 90):
        return jsonify({"error": "invalid latitude"}), 400
    if longitude is not None and not (-180 <= longitude <= 180):
        return jsonify({"error": "invalid longitude"}), 400

    profile_id = uuid.uuid4().hex[:10]  # short-ish unique ID, easy to share
    now = datetime.now(dt_timezone.utc).isoformat()

    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO profiles (id, name, timezone, latitude, longitude, city_name, "
        "status, status_message, photo_base64, updated_at) "
        "VALUES (%s, %s, %s, %s, %s, %s, 'available', '', '', %s)",
        (profile_id, name, timezone_str, latitude, longitude, city_name, now),
    )
    conn.commit()
    cur.execute("SELECT * FROM profiles WHERE id = %s", (profile_id,))
    row = cur.fetchone()
    cur.close()
    conn.close()

    return jsonify(serialize_profile(row)), 201


@app.route("/api/profile/<profile_id>", methods=["GET"])
def get_profile(profile_id):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM profiles WHERE id = %s", (profile_id,))
    row = cur.fetchone()
    cur.close()
    conn.close()

    if not row:
        return jsonify({"error": "profile not found"}), 404

    return jsonify(serialize_profile(row))


@app.route("/api/profile/<profile_id>", methods=["PATCH"])
def update_profile(profile_id):
    """
    Update your own profile — timezone, status (available/busy), status
    message, or photo. Only the fields provided are updated.
    """
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM profiles WHERE id = %s", (profile_id,))
    row = cur.fetchone()
    if not row:
        cur.close()
        conn.close()
        return jsonify({"error": "profile not found"}), 404

    data = request.get_json(force=True) or {}
    updates = {}

    if "name" in data:
        name = (data["name"] or "").strip()
        if not name:
            cur.close()
            conn.close()
            return jsonify({"error": "name cannot be empty"}), 400
        updates["name"] = name

    if "timezone" in data:
        tz = (data["timezone"] or "").strip()
        if tz not in VALID_TIMEZONES:
            cur.close()
            conn.close()
            return jsonify({"error": "invalid timezone"}), 400
        updates["timezone"] = tz

    if "latitude" in data:
        lat = data["latitude"]
        if lat is not None and not (-90 <= lat <= 90):
            cur.close()
            conn.close()
            return jsonify({"error": "invalid latitude"}), 400
        updates["latitude"] = lat

    if "longitude" in data:
        lon = data["longitude"]
        if lon is not None and not (-180 <= lon <= 180):
            cur.close()
            conn.close()
            return jsonify({"error": "invalid longitude"}), 400
        updates["longitude"] = lon

    if "city_name" in data:
        updates["city_name"] = (data["city_name"] or "")[:80]

    if "status" in data:
        status = (data["status"] or "").strip().lower()
        if status not in ("available", "busy"):
            cur.close()
            conn.close()
            return jsonify({"error": "status must be 'available' or 'busy'"}), 400
        updates["status"] = status

    if "status_message" in data:
        updates["status_message"] = (data["status_message"] or "")[:120]

    if "photo_base64" in data:
        updates["photo_base64"] = data["photo_base64"] or ""

    if not updates:
        cur.close()
        conn.close()
        return jsonify({"error": "no valid fields provided"}), 400

    updates["updated_at"] = datetime.now(dt_timezone.utc).isoformat()

    set_clause = ", ".join(f"{k} = %s" for k in updates)
    values = list(updates.values()) + [profile_id]
    cur.execute(f"UPDATE profiles SET {set_clause} WHERE id = %s", values)
    conn.commit()

    cur.execute("SELECT * FROM profiles WHERE id = %s", (profile_id,))
    row = cur.fetchone()
    cur.close()
    conn.close()

    return jsonify(serialize_profile(row))


@app.route("/api/profile/<profile_id>", methods=["DELETE"])
def delete_profile(profile_id):
    """
    Permanently delete a profile. Used by the 'delete & reset' option in the
    app, so a person can fully remove their data rather than just clearing
    local storage and leaving an orphaned profile in the database.
    """
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT id FROM profiles WHERE id = %s", (profile_id,))
    row = cur.fetchone()
    if not row:
        cur.close()
        conn.close()
        return jsonify({"error": "profile not found"}), 404

    cur.execute("DELETE FROM profiles WHERE id = %s", (profile_id,))
    conn.commit()
    cur.close()
    conn.close()

    return jsonify({"id": profile_id, "deleted": True})


@app.route("/api/profiles/batch", methods=["POST"])
def get_profiles_batch():
    """
    Look up multiple friends' profiles at once by ID.
    Body: {"ids": ["abc123", "def456", ...]}
    """
    data = request.get_json(force=True) or {}
    ids = data.get("ids", [])

    if not isinstance(ids, list) or not ids:
        return jsonify({"error": "ids must be a non-empty list"}), 400
    if len(ids) > 100:
        return jsonify({"error": "too many ids (max 100)"}), 400

    conn = get_db()
    cur = conn.cursor()
    placeholders = ",".join("%s" for _ in ids)
    cur.execute(f"SELECT * FROM profiles WHERE id IN ({placeholders})", ids)
    rows = cur.fetchall()
    cur.close()
    conn.close()

    found = {row["id"]: serialize_profile(row) for row in rows}
    results = [found.get(pid) for pid in ids]  # preserve order, None if not found

    return jsonify({"profiles": results})


@app.route("/api/admin/profiles", methods=["GET"])
def admin_list_profiles():
    """
    Admin-only: list every profile in the database. Requires the
    X-Admin-Key header to match the ADMIN_KEY environment variable.
    """
    auth_error = require_admin()
    if auth_error:
        return auth_error

    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM profiles ORDER BY updated_at DESC")
    rows = cur.fetchall()
    cur.close()
    conn.close()

    return jsonify({"profiles": [serialize_profile(row) for row in rows], "count": len(rows)})


@app.route("/api/admin/profile/<profile_id>", methods=["DELETE"])
def admin_delete_profile(profile_id):
    """
    Admin-only: delete any profile by ID, regardless of who owns it.
    Requires the X-Admin-Key header to match the ADMIN_KEY environment variable.
    """
    auth_error = require_admin()
    if auth_error:
        return auth_error

    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT id FROM profiles WHERE id = %s", (profile_id,))
    row = cur.fetchone()
    if not row:
        cur.close()
        conn.close()
        return jsonify({"error": "profile not found"}), 404

    cur.execute("DELETE FROM profiles WHERE id = %s", (profile_id,))
    conn.commit()
    cur.close()
    conn.close()

    return jsonify({"id": profile_id, "deleted": True})


@app.route("/api/timezones", methods=["GET"])
def list_timezones():
    """Returns all valid IANA timezone names, sorted."""
    return jsonify({"timezones": sorted(VALID_TIMEZONES)})


# Initialize the database on import, so it's ready whether run directly
# (python app.py, for local testing) or via gunicorn (production deployment).
init_db()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
