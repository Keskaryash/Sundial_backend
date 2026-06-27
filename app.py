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
import secrets
import hashlib
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


# Recovery codes are shown to the user exactly once (at profile creation) and
# never stored in plaintext — only a SHA-256 hash is kept, same principle as
# password storage. Format: four groups of four uppercase alphanumeric
# characters (e.g. "7K2M-9XQP-4RTN-8VWL"), excluding visually ambiguous
# characters (0/O, 1/I/L) to reduce transcription mistakes when someone
# writes it down by hand.
RECOVERY_CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"


def generate_recovery_code():
    groups = []
    for _ in range(4):
        groups.append("".join(secrets.choice(RECOVERY_CODE_ALPHABET) for _ in range(4)))
    return "-".join(groups)


def hash_recovery_code(code):
    normalized = code.strip().upper()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


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
    # ALTER TABLE ... ADD COLUMN IF NOT EXISTS handles both fresh databases
    # (where the CREATE TABLE above already lacks this column) and existing
    # databases created before this feature existed — safe to run every startup.
    cur.execute("ALTER TABLE profiles ADD COLUMN IF NOT EXISTS recovery_code_hash TEXT")
    cur.execute("""
        CREATE TABLE IF NOT EXISTS friend_requests (
            id SERIAL PRIMARY KEY,
            requester_id TEXT NOT NULL,
            target_id TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            created_at TEXT NOT NULL,
            responded_at TEXT,
            UNIQUE(requester_id, target_id)
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
    recovery_code = generate_recovery_code()
    recovery_hash = hash_recovery_code(recovery_code)
    now = datetime.now(dt_timezone.utc).isoformat()

    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO profiles (id, name, timezone, latitude, longitude, city_name, "
        "status, status_message, photo_base64, updated_at, recovery_code_hash) "
        "VALUES (%s, %s, %s, %s, %s, %s, 'available', '', '', %s, %s)",
        (profile_id, name, timezone_str, latitude, longitude, city_name, now, recovery_hash),
    )
    conn.commit()
    cur.execute("SELECT * FROM profiles WHERE id = %s", (profile_id,))
    row = cur.fetchone()
    cur.close()
    conn.close()

    response = serialize_profile(row)
    response["recovery_code"] = recovery_code  # shown only this once — never returned again by any other endpoint
    return jsonify(response), 201


@app.route("/api/profile/<profile_id>", methods=["GET"])
def get_profile(profile_id):
    """
    Fetch a profile. Requires a `viewer_id` query parameter identifying who's
    asking. Access is allowed only if:
      - viewer_id matches profile_id (fetching your own profile), or
      - there's an approved friend_request from viewer_id -> profile_id.
    This is what actually enforces the approval system at the API level,
    not just in the app's UI flow.
    """
    viewer_id = request.args.get("viewer_id", "").strip()
    if not viewer_id:
        return jsonify({"error": "viewer_id is required"}), 400

    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM profiles WHERE id = %s", (profile_id,))
    row = cur.fetchone()

    if not row:
        cur.close()
        conn.close()
        return jsonify({"error": "profile not found"}), 404

    if viewer_id != profile_id:
        cur.execute(
            "SELECT status FROM friend_requests WHERE requester_id = %s AND target_id = %s",
            (viewer_id, profile_id),
        )
        req_row = cur.fetchone()
        if not req_row or req_row["status"] != "approved":
            cur.close()
            conn.close()
            return jsonify({"error": "not authorized to view this profile", "status": (req_row["status"] if req_row else "none")}), 403

    cur.close()
    conn.close()
    result = serialize_profile(row)
    if viewer_id == profile_id:
        result["has_recovery_code"] = bool(row["recovery_code_hash"])  # only visible to the profile's own owner
    return jsonify(result)


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
    Look up multiple friends' profiles at once by ID. Requires viewer_id in
    the request body, identifying who's asking — only profiles where the
    viewer has an approved friend_request are returned; others come back
    as null in the results array (same shape as a not-found profile, so the
    frontend doesn't need special-case handling for "denied" vs "missing").
    Body: {"viewer_id": "...", "ids": ["abc123", "def456", ...]}
    """
    data = request.get_json(force=True) or {}
    ids = data.get("ids", [])
    viewer_id = (data.get("viewer_id") or "").strip()

    if not viewer_id:
        return jsonify({"error": "viewer_id is required"}), 400
    if not isinstance(ids, list) or not ids:
        return jsonify({"error": "ids must be a non-empty list"}), 400
    if len(ids) > 100:
        return jsonify({"error": "too many ids (max 100)"}), 400

    conn = get_db()
    cur = conn.cursor()
    placeholders = ",".join("%s" for _ in ids)
    cur.execute(f"SELECT * FROM profiles WHERE id IN ({placeholders})", ids)
    rows = cur.fetchall()

    # Find which of these the viewer is actually approved to see.
    cur.execute(
        f"SELECT target_id FROM friend_requests WHERE requester_id = %s "
        f"AND status = 'approved' AND target_id IN ({placeholders})",
        [viewer_id] + ids,
    )
    approved_ids = set(r["target_id"] for r in cur.fetchall())
    cur.close()
    conn.close()

    found = {row["id"]: serialize_profile(row) for row in rows}
    results = [
        found.get(pid) if (pid == viewer_id or pid in approved_ids) else None
        for pid in ids
    ]  # preserve order; null for not-found OR not-yet-approved

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

    profiles = []
    for row in rows:
        p = serialize_profile(row)
        p["has_recovery_code"] = bool(row["recovery_code_hash"])  # presence only, never the hash itself
        profiles.append(p)

    return jsonify({"profiles": profiles, "count": len(rows)})


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


@app.route("/api/admin/profile/<profile_id>/regenerate-recovery-code", methods=["POST"])
def admin_regenerate_recovery_code(profile_id):
    """
    Admin-only: generate a brand new recovery code for a profile, replacing
    any existing one (the old code stops working immediately). Intended for
    when someone contacts the admin directly after losing their code — the
    new plaintext code is returned once here, for the admin to relay to them
    through some other trusted channel (e.g. a text message).
    """
    auth_error = require_admin()
    if auth_error:
        return auth_error

    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT id FROM profiles WHERE id = %s", (profile_id,))
    if not cur.fetchone():
        cur.close()
        conn.close()
        return jsonify({"error": "profile not found"}), 404

    new_code = generate_recovery_code()
    new_hash = hash_recovery_code(new_code)
    cur.execute(
        "UPDATE profiles SET recovery_code_hash = %s WHERE id = %s",
        (new_hash, profile_id),
    )
    conn.commit()
    cur.close()
    conn.close()

    return jsonify({"id": profile_id, "recovery_code": new_code})


def serialize_request(row, name_lookup=None):
    result = {
        "id": row["id"],
        "requester_id": row["requester_id"],
        "target_id": row["target_id"],
        "status": row["status"],
        "created_at": row["created_at"],
        "responded_at": row["responded_at"],
    }
    if name_lookup and row["requester_id"] in name_lookup:
        result["requester_name"] = name_lookup[row["requester_id"]]
    return result


@app.route("/api/friend-request", methods=["POST"])
def create_friend_request():
    """
    Create (or re-fetch the status of) a friend request from requester_id
    to target_id. If one already exists, returns its current status rather
    than erroring — so retrying an add is harmless and idempotent.
    Body: {"requester_id": "...", "target_id": "..."}
    """
    data = request.get_json(force=True) or {}
    requester_id = (data.get("requester_id") or "").strip()
    target_id = (data.get("target_id") or "").strip()

    if not requester_id or not target_id:
        return jsonify({"error": "requester_id and target_id are required"}), 400
    if requester_id == target_id:
        return jsonify({"error": "cannot send a friend request to yourself"}), 400

    conn = get_db()
    cur = conn.cursor()

    cur.execute("SELECT id FROM profiles WHERE id = %s", (target_id,))
    if not cur.fetchone():
        cur.close()
        conn.close()
        return jsonify({"error": "target profile not found"}), 404

    cur.execute(
        "SELECT * FROM friend_requests WHERE requester_id = %s AND target_id = %s",
        (requester_id, target_id),
    )
    existing = cur.fetchone()
    if existing:
        cur.close()
        conn.close()
        return jsonify(serialize_request(existing)), 200

    now = datetime.now(dt_timezone.utc).isoformat()
    cur.execute(
        "INSERT INTO friend_requests (requester_id, target_id, status, created_at) "
        "VALUES (%s, %s, 'pending', %s) RETURNING *",
        (requester_id, target_id, now),
    )
    row = cur.fetchone()
    conn.commit()
    cur.close()
    conn.close()

    return jsonify(serialize_request(row)), 201


@app.route("/api/friend-request/backfill-legacy", methods=["POST"])
def backfill_legacy_friendship():
    """
    One-time self-healing endpoint for friendships that existed before the
    approval system was introduced. Since those relationships have no
    friend_requests row at all (neither pending nor approved), the normal
    approval logic would incorrectly hide them. This creates an ALREADY
    APPROVED request in both directions between two existing profiles, so
    a pre-existing friendship keeps working without requiring either side
    to manually re-approve the other.

    This is intentionally NOT how new friendships are created (those still
    go through the normal pending -> approve flow) — it only ever upgrades
    a relationship with zero existing request rows, and is a no-op if any
    request already exists in either direction.
    Body: {"profile_id_a": "...", "profile_id_b": "..."}
    """
    data = request.get_json(force=True) or {}
    a = (data.get("profile_id_a") or "").strip()
    b = (data.get("profile_id_b") or "").strip()

    if not a or not b or a == b:
        return jsonify({"error": "two distinct profile IDs are required"}), 400

    conn = get_db()
    cur = conn.cursor()

    cur.execute("SELECT id FROM profiles WHERE id IN (%s, %s)", (a, b))
    if len(cur.fetchall()) != 2:
        cur.close()
        conn.close()
        return jsonify({"error": "one or both profiles not found"}), 404

    now = datetime.now(dt_timezone.utc).isoformat()
    created = []
    for requester, target in [(a, b), (b, a)]:
        cur.execute(
            "SELECT * FROM friend_requests WHERE requester_id = %s AND target_id = %s",
            (requester, target),
        )
        if cur.fetchone():
            continue  # a request already exists this direction — leave it untouched
        cur.execute(
            "INSERT INTO friend_requests (requester_id, target_id, status, created_at, responded_at) "
            "VALUES (%s, %s, 'approved', %s, %s) RETURNING *",
            (requester, target, now, now),
        )
        created.append(serialize_request(cur.fetchone()))

    conn.commit()
    cur.close()
    conn.close()

    return jsonify({"created": created})


@app.route("/api/friend-requests/incoming/<target_id>", methods=["GET"])
def list_incoming_requests(target_id):
    """Lists pending requests where someone wants to add target_id."""
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        "SELECT * FROM friend_requests WHERE target_id = %s AND status = 'pending' "
        "ORDER BY created_at DESC",
        (target_id,),
    )
    requests_rows = cur.fetchall()

    requester_ids = [r["requester_id"] for r in requests_rows]
    name_lookup = {}
    if requester_ids:
        placeholders = ",".join("%s" for _ in requester_ids)
        cur.execute(f"SELECT id, name FROM profiles WHERE id IN ({placeholders})", requester_ids)
        name_lookup = {r["id"]: r["name"] for r in cur.fetchall()}

    cur.close()
    conn.close()

    return jsonify({"requests": [serialize_request(r, name_lookup) for r in requests_rows]})


@app.route("/api/friend-requests/outgoing/<requester_id>", methods=["GET"])
def list_outgoing_requests(requester_id):
    """Lists requests this person has sent, with their current status — lets
    the requester's device know when a pending request becomes approved."""
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        "SELECT * FROM friend_requests WHERE requester_id = %s",
        (requester_id,),
    )
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return jsonify({"requests": [serialize_request(r) for r in rows]})


@app.route("/api/friend-request/<int:request_id>/respond", methods=["POST"])
def respond_to_friend_request(request_id):
    """
    Approve or deny a pending request. Only the target of the request should
    do this — enforced via a responder_id field that must match target_id.
    Body: {"responder_id": "...", "action": "approve" | "deny"}
    """
    data = request.get_json(force=True) or {}
    responder_id = (data.get("responder_id") or "").strip()
    action = (data.get("action") or "").strip().lower()

    if action not in ("approve", "deny"):
        return jsonify({"error": "action must be 'approve' or 'deny'"}), 400
    if not responder_id:
        return jsonify({"error": "responder_id is required"}), 400

    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM friend_requests WHERE id = %s", (request_id,))
    row = cur.fetchone()

    if not row:
        cur.close()
        conn.close()
        return jsonify({"error": "request not found"}), 404
    if row["target_id"] != responder_id:
        cur.close()
        conn.close()
        return jsonify({"error": "only the request's target can respond to it"}), 403

    new_status = "approved" if action == "approve" else "denied"
    now = datetime.now(dt_timezone.utc).isoformat()
    cur.execute(
        "UPDATE friend_requests SET status = %s, responded_at = %s WHERE id = %s RETURNING *",
        (new_status, now, request_id),
    )
    updated = cur.fetchone()
    conn.commit()
    cur.close()
    conn.close()

    return jsonify(serialize_request(updated))


@app.route("/api/profile/<profile_id>/recover", methods=["POST"])
def recover_profile(profile_id):
    """
    Recover access to an existing profile on a new device, using the
    recovery code shown once at profile creation. Returns the full profile
    on success — the frontend then saves this ID locally, restoring login.

    Error responses are intentionally generic and identically shaped whether
    the profile doesn't exist or the code is wrong, so this endpoint can't be
    used to probe which IDs exist on the server.
    Body: {"recovery_code": "XXXX-XXXX-XXXX-XXXX"}
    """
    data = request.get_json(force=True) or {}
    recovery_code = (data.get("recovery_code") or "").strip()
    generic_error = jsonify({"error": "Invalid ID or recovery code."}), 401

    if not recovery_code:
        return generic_error

    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM profiles WHERE id = %s", (profile_id,))
    row = cur.fetchone()
    cur.close()
    conn.close()

    if not row or not row["recovery_code_hash"]:
        return generic_error

    if hash_recovery_code(recovery_code) != row["recovery_code_hash"]:
        return generic_error

    return jsonify(serialize_profile(row))


@app.route("/api/profile/<profile_id>/generate-recovery-code", methods=["POST"])
def generate_recovery_code_for_existing_profile(profile_id):
    """
    Generates a recovery code for a profile that doesn't have one yet —
    covers profiles created before this feature existed. If a code already
    exists, this refuses rather than silently replacing it, so a person
    can't be locked out by someone else re-generating their code without
    them noticing (the dedicated regenerate flow for "I lost my code" is
    intentionally only available via the admin, not this self-serve route).
    Returns the new plaintext code once, same as profile creation does.
    """
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM profiles WHERE id = %s", (profile_id,))
    row = cur.fetchone()

    if not row:
        cur.close()
        conn.close()
        return jsonify({"error": "profile not found"}), 404

    if row["recovery_code_hash"]:
        cur.close()
        conn.close()
        return jsonify({"error": "This profile already has a recovery code set."}), 409

    new_code = generate_recovery_code()
    new_hash = hash_recovery_code(new_code)
    cur.execute(
        "UPDATE profiles SET recovery_code_hash = %s WHERE id = %s",
        (new_hash, profile_id),
    )
    conn.commit()
    cur.close()
    conn.close()

    return jsonify({"id": profile_id, "recovery_code": new_code})


@app.route("/api/profile/<profile_id>/regenerate-recovery-code", methods=["POST"])
def regenerate_recovery_code_self_service(profile_id):
    """
    Self-service version of regenerating a recovery code, for the common
    "I never actually wrote my code down" situation. Unlike the admin
    regenerate route, this requires no special auth beyond knowing your own
    profile ID — the same trust level as everything else self-service in
    this app. This is safe specifically because it's the profile's own
    owner replacing their own code (not a third party silently locking
    someone else out), which is the scenario the original generate-only-once
    restriction was guarding against.
    Always succeeds if the profile exists, replacing any existing code.
    """
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT id FROM profiles WHERE id = %s", (profile_id,))
    if not cur.fetchone():
        cur.close()
        conn.close()
        return jsonify({"error": "profile not found"}), 404

    new_code = generate_recovery_code()
    new_hash = hash_recovery_code(new_code)
    cur.execute(
        "UPDATE profiles SET recovery_code_hash = %s WHERE id = %s",
        (new_hash, profile_id),
    )
    conn.commit()
    cur.close()
    conn.close()

    return jsonify({"id": profile_id, "recovery_code": new_code})


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
