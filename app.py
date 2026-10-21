from flask import Flask, jsonify, request, render_template
from datetime import datetime, timedelta
import threading
import time
from database import get_db_connection, init_db
import qrcode
import io
import base64
import os
import hmac
from functools import wraps
from flask import session

app = Flask(__name__)

app.config["SECRET_KEY"] = os.environ.get(
    "LIBSPACE_SECRET_KEY", "change-this-secret-before-deployment"
)
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"

ADMIN_USERNAME = os.environ.get("LIBSPACE_ADMIN_USER", "admin")
ADMIN_PASSWORD = os.environ.get("LIBSPACE_ADMIN_PASSWORD", "LibSpace@2026")


def admin_required(view_function):
    @wraps(view_function)
    def wrapped(*args, **kwargs):
        if not session.get("is_admin"):
            return jsonify({"error": "Admin sign-in required."}), 401
        return view_function(*args, **kwargs)
    return wrapped


CONFIG = {
    "demo_mode": True,  # True = 20s study / 15s grace for testing
    "prod_study_sec": 90 * 60,
    "prod_grace_sec": 15 * 60,
    "demo_study_sec": 20,
    "demo_grace_sec": 15
}

def get_durations():
    if CONFIG["demo_mode"]:
        return CONFIG["demo_study_sec"], CONFIG["demo_grace_sec"]
    return CONFIG["prod_study_sec"], CONFIG["prod_grace_sec"]

def evaluate_timers():
    conn = get_db_connection()
    cursor = conn.cursor()
    now = datetime.now()
    now_str = now.strftime("%Y-%m-%d %H:%M:%S")

    # 1. Expired Grace Period -> Auto-Release Seat
    expired_sessions = cursor.execute("""
        SELECT id, seat_id FROM sessions
        WHERE status IN ('OCCUPIED', 'AWAITING_RENEWAL')
          AND grace_expires_at <= ?
    """, (now_str,)).fetchall()

    for sess in expired_sessions:
        cursor.execute("UPDATE sessions SET status = 'AUTO_RELEASED', ended_at = ? WHERE id = ?", (now_str, sess["id"]))
        cursor.execute("UPDATE seats SET status = 'AVAILABLE' WHERE id = ?", (sess["seat_id"],))

    # 2. Study Period Ended -> Enter 15-Min Renewal Window
    due_sessions = cursor.execute("""
        SELECT id, seat_id FROM sessions
        WHERE status = 'OCCUPIED'
          AND next_scan_due_at <= ?
          AND grace_expires_at > ?
    """, (now_str, now_str)).fetchall()

    for sess in due_sessions:
        cursor.execute("UPDATE sessions SET status = 'AWAITING_RENEWAL' WHERE id = ?", (sess["id"],))
        cursor.execute("UPDATE seats SET status = 'AWAITING_RENEWAL' WHERE id = ?", (sess["seat_id"],))

    conn.commit()
    conn.close()

def background_timer_worker():
    while True:
        try:
            evaluate_timers()
        except Exception as e:
            print(f"Timer Worker Error: {e}")
        time.sleep(1)

@app.route("/")
def index():
    return render_template("index.html")

@app.route("/print-qrs")
def print_qrs():
    conn = get_db_connection()

    seats = [
        dict(row)
        for row in conn.execute(
            "SELECT * FROM seats ORDER BY id"
        ).fetchall()
    ]

    conn.close()

    qr_data_list = []

    for seat in seats:
        qr = qrcode.QRCode(box_size=6, border=2)
        qr.add_data(seat["qr_token"])
        qr.make(fit=True)

        img = qr.make_image(
            fill_color="black",
            back_color="white"
        )

        buffer = io.BytesIO()
        img.save(buffer, format="PNG")

        img_str = base64.b64encode(
            buffer.getvalue()
        ).decode("utf-8")

        qr_data_list.append({
            "label": seat["seat_label"],
            "zone": seat["zone"],
            "token": seat["qr_token"],
            "image_b64": img_str
        })

    return render_template(
        "print_qrs.html",
        seats=qr_data_list
    )

@app.route("/api/login", methods=["POST"])
def login():
    data = request.get_json()
    name = data.get("name", "").strip()
    roll_number = data.get("roll_number", "").strip()

    if not name or not roll_number:
        return jsonify({"error": "Please enter both name and registration ID."}), 400

    conn = get_db_connection()
    cursor = conn.cursor()

    user = cursor.execute("SELECT * FROM users WHERE roll_number = ?", (roll_number,)).fetchone()
    if not user:
        cursor.execute("INSERT INTO users (name, roll_number) VALUES (?, ?)", (name, roll_number))
        conn.commit()
        user = cursor.execute("SELECT * FROM users WHERE roll_number = ?", (roll_number,)).fetchone()

    conn.close()
    return jsonify({"success": True, "user": dict(user)})

@app.route("/api/state", methods=["GET"])
def get_state():
    evaluate_timers()
    conn = get_db_connection()
    now = datetime.now()

    seats = [dict(row) for row in conn.execute("SELECT * FROM seats ORDER BY id").fetchall()]

    active_rows = conn.execute("""
        SELECT s.*, st.seat_label
        FROM sessions s
        JOIN seats st ON s.seat_id = st.id
        WHERE s.status IN ('OCCUPIED', 'AWAITING_RENEWAL')
    """).fetchall()

    active_sessions = {}
    for row in active_rows:
        sess = dict(row)
        due_dt = datetime.strptime(sess["next_scan_due_at"], "%Y-%m-%d %H:%M:%S")
        exp_dt = datetime.strptime(sess["grace_expires_at"], "%Y-%m-%d %H:%M:%S")
        sess["seconds_until_due"] = max(0, int((due_dt - now).total_seconds()))
        sess["seconds_until_expiry"] = max(0, int((exp_dt - now).total_seconds()))
        active_sessions[sess["seat_id"]] = sess

    # Show recent activity only for the logged-in student
    user_id = request.args.get("user_id", type=int)

    logs = []

    if user_id is not None:
        logs = [
            dict(row)
            for row in conn.execute("""
                SELECT s.id, s.status, s.started_at,
                    s.ended_at, s.renewal_count,
                    st.seat_label
                FROM sessions s
                JOIN seats st ON s.seat_id = st.id
                WHERE s.user_id = ?
                ORDER BY s.id DESC
                LIMIT 8
            """, (user_id,)).fetchall()
        ]

    conn.close()
    study_sec, grace_sec = get_durations()

    return jsonify({
        "demo_mode": CONFIG["demo_mode"],
        "study_duration_sec": study_sec,
        "grace_duration_sec": grace_sec,
        "seats": seats,
        "active_sessions": active_sessions,
        "logs": logs
    })

@app.route("/api/scan", methods=["POST"])
def scan_qr():
    evaluate_timers()
    data = request.get_json()
    user_id = data.get("user_id")
    qr_token = data.get("qr_token", "").strip()

    if not user_id or not qr_token:
        return jsonify({"error": "Missing user or QR token"}), 400

    conn = get_db_connection()
    cursor = conn.cursor()

    seat = cursor.execute("SELECT * FROM seats WHERE qr_token = ?", (qr_token,)).fetchone()
    if not seat:
        conn.close()
        return jsonify({"error": "Invalid QR Code."}), 404

    now = datetime.now()
    study_sec, grace_sec = get_durations()
    due_at = (now + timedelta(seconds=study_sec)).strftime("%Y-%m-%d %H:%M:%S")
    exp_at = (now + timedelta(seconds=study_sec + grace_sec)).strftime("%Y-%m-%d %H:%M:%S")
    now_str = now.strftime("%Y-%m-%d %H:%M:%S")

    active_sess = cursor.execute("""
        SELECT * FROM sessions
        WHERE seat_id = ? AND status IN ('OCCUPIED', 'AWAITING_RENEWAL')
    """, (seat["id"],)).fetchone()

    # Renewal
    if active_sess:
        if active_sess["user_id"] != int(user_id):
            conn.close()
            return jsonify({"error": f"Seat {seat['seat_label']} is occupied by another student."}), 403

        cursor.execute("""
            UPDATE sessions
            SET status = 'OCCUPIED', next_scan_due_at = ?, grace_expires_at = ?, renewal_count = renewal_count + 1
            WHERE id = ?
        """, (due_at, exp_at, active_sess["id"]))
        cursor.execute("UPDATE seats SET status = 'OCCUPIED' WHERE id = ?", (seat["id"],))
        conn.commit()
        conn.close()
        return jsonify({"message": f"Seat {seat['seat_label']} renewed successfully!", "action": "RENEWED"})

    # Check if user already holds another seat
    existing_user_sess = cursor.execute("""
        SELECT st.seat_label FROM sessions s
        JOIN seats st ON s.seat_id = st.id
        WHERE s.user_id = ? AND s.status IN ('OCCUPIED', 'AWAITING_RENEWAL')
    """, (user_id,)).fetchone()

    if existing_user_sess:
        conn.close()
        return jsonify({"error": f"You already have an active seat at {existing_user_sess['seat_label']}."}), 400

    # New Check-In
    cursor.execute("""
        INSERT INTO sessions (user_id, seat_id, status, started_at, next_scan_due_at, grace_expires_at)
        VALUES (?, ?, 'OCCUPIED', ?, ?, ?)
    """, (user_id, seat["id"], now_str, due_at, exp_at))
    cursor.execute("UPDATE seats SET status = 'OCCUPIED' WHERE id = ?", (seat["id"],))
    conn.commit()
    conn.close()

    return jsonify({"message": f"Checked into seat {seat['seat_label']}!", "action": "CHECKED_IN"})

@app.route("/api/checkout", methods=["POST"])
def checkout():
    data = request.get_json()
    seat_id = data.get("seat_id")
    user_id = data.get("user_id")

    conn = get_db_connection()
    cursor = conn.cursor()
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    sess = cursor.execute("""
        SELECT * FROM sessions WHERE seat_id = ? AND status IN ('OCCUPIED', 'AWAITING_RENEWAL')
    """, (seat_id,)).fetchone()

    if not sess:
        conn.close()
        return jsonify({"error": "No active session found."}), 404

    if user_id and sess["user_id"] != int(user_id):
        conn.close()
        return jsonify({"error": "You can only leave your own seat."}), 403

    cursor.execute("UPDATE sessions SET status = 'COMPLETED', ended_at = ? WHERE id = ?", (now_str, sess["id"]))
    cursor.execute("UPDATE seats SET status = 'AVAILABLE' WHERE id = ?", (seat_id,))
    conn.commit()
    conn.close()
    return jsonify({"message": "You have left your seat. It is now open!"})

@app.route("/api/toggle-mode", methods=["POST"])
def toggle_mode():
    CONFIG["demo_mode"] = not CONFIG["demo_mode"]
    study_sec, grace_sec = get_durations()
    return jsonify({"demo_mode": CONFIG["demo_mode"]})

@app.route("/api/reset", methods=["POST"])
def reset_all():
    conn = get_db_connection()
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn.execute("UPDATE sessions SET status = 'COMPLETED', ended_at = ? WHERE ended_at IS NULL", (now_str,))
    conn.execute("UPDATE seats SET status = 'AVAILABLE'")
    conn.commit()
    conn.close()
    return jsonify({"message": "Floor reset successfully!"})

@app.route("/admin")
def admin_page():
    return render_template("admin.html")


@app.route("/api/admin/login", methods=["POST"])
def admin_login():
    data = request.get_json(silent=True) or {}
    username = str(data.get("username", ""))
    password = str(data.get("password", ""))

    valid_username = hmac.compare_digest(username, ADMIN_USERNAME)
    valid_password = hmac.compare_digest(password, ADMIN_PASSWORD)
    if not (valid_username and valid_password):
        return jsonify({"error": "Incorrect admin username or password."}), 401

    session.clear()
    session["is_admin"] = True
    session.permanent = True
    return jsonify({"success": True, "message": "Admin signed in."})


@app.route("/api/admin/logout", methods=["POST"])
@admin_required
def admin_logout():
    session.clear()
    return jsonify({"success": True, "message": "Admin signed out."})


@app.route("/api/admin/state", methods=["GET"])
@admin_required
def admin_state():
    # Keep seat/session timers current before reading the dashboard.
    evaluate_timers()
    conn = get_db_connection()
    now = datetime.now()
    now_str = now.strftime("%Y-%m-%d %H:%M:%S")

    seats = [dict(row) for row in conn.execute(
        "SELECT id, seat_label, zone, status FROM seats ORDER BY id"
    ).fetchall()]

    rows = conn.execute("""
        SELECT
            s.id AS session_id,
            s.user_id,
            s.seat_id,
            s.status,
            s.started_at,
            s.next_scan_due_at,
            s.grace_expires_at,
            s.renewal_count,
            u.name AS student_name,
            u.roll_number,
            st.seat_label,
            st.zone
        FROM sessions s
        JOIN users u ON u.id = s.user_id
        JOIN seats st ON st.id = s.seat_id
        WHERE s.status IN ('OCCUPIED', 'AWAITING_RENEWAL')
        ORDER BY s.started_at ASC
    """).fetchall()

    active_sessions = []
    for row in rows:
        item = dict(row)
        start_dt = datetime.strptime(item["started_at"], "%Y-%m-%d %H:%M:%S")
        due_dt = datetime.strptime(item["next_scan_due_at"], "%Y-%m-%d %H:%M:%S")
        expiry_dt = datetime.strptime(item["grace_expires_at"], "%Y-%m-%d %H:%M:%S")
        item["elapsed_seconds"] = max(0, int((now - start_dt).total_seconds()))
        item["seconds_until_due"] = max(0, int((due_dt - now).total_seconds()))
        item["seconds_until_expiry"] = max(0, int((expiry_dt - now).total_seconds()))
        active_sessions.append(item)

    conn.close()
    summary = {
        "total": len(seats),
        "available": sum(1 for seat in seats if seat["status"] == "AVAILABLE"),
        "occupied": sum(1 for seat in seats if seat["status"] == "OCCUPIED"),
        "awaiting_renewal": sum(1 for seat in seats if seat["status"] == "AWAITING_RENEWAL"),
    }
    return jsonify({"seats": seats, "active_sessions": active_sessions, "summary": summary})


if __name__ == "__main__":
    init_db()
    worker = threading.Thread(target=background_timer_worker, daemon=True)
    worker.start()
    app.run(debug=True, port=5050, use_reloader=False)


