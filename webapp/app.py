import os
import cv2
import csv
import time
import base64
import threading
import numpy as np
import pandas as pd
import smtplib
from pathlib import Path
from datetime import datetime, time as dtime
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

from flask import Flask, render_template, request, redirect, session, jsonify, url_for
from authlib.integrations.flask_client import OAuth

# Base paths
BASE_DIR = Path(__file__).resolve().parent.parent
LOG_DIR = BASE_DIR / "logs"
DATA_DIR = BASE_DIR / "data"
os.makedirs(LOG_DIR, exist_ok=True)
os.makedirs(DATA_DIR, exist_ok=True)

def get_user_log_file(user_email):
    """Get user-specific events.csv path"""
    user_log_dir = LOG_DIR / user_email
    os.makedirs(user_log_dir, exist_ok=True)
    return user_log_dir / "events.csv"

def get_user_dose_status_file(user_email):
    """Get user-specific dose_status.csv path"""
    user_data_dir = DATA_DIR / user_email
    os.makedirs(user_data_dir, exist_ok=True)
    return user_data_dir / "dose_status.csv"

def init_user_log_file(user_email):
    """Initialize events.csv for a user if it doesn't exist"""
    log_file = get_user_log_file(user_email)
    if not os.path.exists(log_file):
        with open(log_file, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["timestamp", "marker_id", "dose_name", "event"])

# Legacy global paths (for backward compatibility)
LOG_FILE = LOG_DIR / "events.csv"
DOSE_STATUS_FILE = DATA_DIR / "dose_status.csv"

# Initialize events.csv if missing (legacy)
if not os.path.exists(LOG_FILE):
    with open(LOG_FILE, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["timestamp", "marker_id", "dose_name", "event"])

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "medisense-secret-key-2026-super-secret")

# ---- Google OAuth Setup ----
oauth = OAuth(app)
GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "YOUR_CLIENT_ID")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "YOUR_CLIENT_SECRET")

google = oauth.register(
    name="google",
    client_id=GOOGLE_CLIENT_ID,
    client_secret=GOOGLE_CLIENT_SECRET,
    server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
    client_kwargs={"scope": "openid email profile"},
)

# ---- Email credentials ----
GMAIL_ADDRESS = os.environ.get("GMAIL_ADDRESS", "")
GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD", "")
DEFAULT_RECIPIENT_EMAIL = os.environ.get("DEFAULT_RECIPIENT_EMAIL", "")

# ---- ArUco Detector Setup ----
aruco_dict = cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_4X4_50)
params = cv2.aruco.DetectorParameters()
detector = cv2.aruco.ArucoDetector(aruco_dict, params)
dose_names = {0: "Morning", 1: "Afternoon", 2: "Night"}

# Marker state tracking & debounce
last_state = {0: False, 1: False, 2: False}
pending_state = {k: {"state": False, "count": 0} for k in dose_names.keys()}
CONFIRM_FRAMES = 3  # Debounce frame threshold for browser capture

# ---- Dose Time Windows ----
DOSE_WINDOWS = {
    "Morning":   {"start": dtime(5, 0),  "end": dtime(11, 59)},
    "Afternoon": {"start": dtime(12, 0), "end": dtime(18, 59)},
    "Night":     {"start": dtime(19, 0), "end": dtime(4, 59)},
}

# Pending wrong lid tracking state
pending_wrong_lid = {"dose": None, "opened_at": None, "alert_sent": False}


def in_window(t, start, end):
    if start <= end:
        return start <= t <= end
    else:
        return t >= start or t <= end


def get_expected_dose_now():
    now = datetime.now().time()
    for dose_name, window in DOSE_WINDOWS.items():
        if in_window(now, window["start"], window["end"]):
            return dose_name
    return None


def get_today_taken_doses(user_email=None):
    log_file = get_user_log_file(user_email) if user_email else LOG_FILE
    if not os.path.exists(log_file):
        return set()
    try:
        df = pd.read_csv(log_file)
        if df.empty or "dose_name" not in df.columns or "event" not in df.columns:
            return set()
        df["dose_name"] = df["dose_name"].replace({"Evening": "Afternoon"})
        df["timestamp"] = pd.to_datetime(df["timestamp"])
        today = datetime.now().date()
        today_opens = df[(df["event"] == "opened") & (df["timestamp"].dt.date == today)]
        return set(today_opens["dose_name"].unique())
    except Exception as e:
        print(f"Error reading taken doses: {e}")
        return set()


active_user_email = DEFAULT_RECIPIENT_EMAIL


def send_email_message(text, recipient_email=None):
    global active_user_email
    if not recipient_email:
        try:
            recipient_email = session.get("user_email", active_user_email)
        except Exception:
            recipient_email = active_user_email
    try:
        msg = MIMEMultipart()
        msg["Subject"] = "MediSense Medication Warning / Reminder"
        msg["From"] = f"MediSense <{GMAIL_ADDRESS}>"
        msg["To"] = recipient_email
        msg["Date"] = datetime.now().strftime("%a, %d %b %Y %H:%M:%S")
        msg.attach(MIMEText(text, "plain"))

        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
            server.send_message(msg)
        print(f"[{datetime.now().isoformat()}] Warning Email sent successfully to {recipient_email}")
    except Exception as e:
        print(f"(Email send failed: {e})")


def log_event(marker_id, event_type, user_email=None):
    timestamp = datetime.now().isoformat(timespec="seconds")
    dose = dose_names.get(marker_id, f"Unknown_{marker_id}")
    
    # Get user-specific log file
    log_file = get_user_log_file(user_email) if user_email else LOG_FILE
    
    with open(log_file, "a", newline="") as f:
        writer = csv.writer(f)
        writer.writerow([timestamp, marker_id, dose, event_type])
    print(f"[{timestamp}] {dose} lid {event_type}")

    # Step 9 - Check wrong-lid opening
    if event_type == "opened":
        check_wrong_lid(dose)


def check_wrong_lid(opened_dose):
    global pending_wrong_lid
    expected = get_expected_dose_now()
    if expected and opened_dose != expected:
        pending_wrong_lid["dose"] = expected
        pending_wrong_lid["opened_at"] = datetime.now()
        pending_wrong_lid["alert_sent"] = False
        print(f"[WRONG LID DETECTED] Opened: {opened_dose}, Expected: {expected}. Starting 1-minute grace period...")


def check_pending_wrong_lid():
    global pending_wrong_lid
    if pending_wrong_lid["dose"] is None:
        return

    taken_today = get_today_taken_doses()
    expected_dose = pending_wrong_lid["dose"]

    # If correct lid has been opened, resolve and clear wrong lid state
    if expected_dose in taken_today:
        print(f"[CORRECT LID RESOLVED] Correct lid ({expected_dose}) opened! Clearing wrong lid alert.")
        pending_wrong_lid["dose"] = None
        pending_wrong_lid["opened_at"] = None
        pending_wrong_lid["alert_sent"] = False
        return

    if pending_wrong_lid["opened_at"] is None:
        return

    elapsed_minutes = (datetime.now() - pending_wrong_lid["opened_at"]).total_seconds() / 60.0
    if elapsed_minutes >= 1.0 and not pending_wrong_lid["alert_sent"]:
        msg_text = f"You opened the wrong compartment - please open your {expected_dose} dose instead."
        print(f"\n[GRACE PERIOD EXPIRED] Sending warning email: '{msg_text}'\n")
        send_email_message(msg_text)
        pending_wrong_lid["alert_sent"] = True
        pending_wrong_lid["dose"] = None  # Send once


# Background loop for grace period checking
def background_checker_loop():
    while True:
        try:
            check_pending_wrong_lid()
        except Exception as e:
            print(f"Error in background checker loop: {e}")
        time.sleep(10)


checker_thread = threading.Thread(target=background_checker_loop, daemon=True)
checker_thread.start()


# ---- Flask Web Routes ----

@app.route("/")
def index():
    if "user_email" in session:
        return redirect(url_for("dashboard"))
    return redirect(url_for("login"))


@app.route("/login")
def login():
    return render_template("login.html")


@app.route("/demo-login")
def demo_login():
    session["user_email"] = "demo.user@medisense.org"
    return redirect(url_for("dashboard"))


@app.route("/email-login", methods=["POST"])
def email_login():
    email = request.form.get("email", "").strip()
    if email:
        session["user_email"] = email
    else:
        session["user_email"] = DEFAULT_RECIPIENT_EMAIL
    return redirect(url_for("dashboard"))


@app.route("/google-login")
def google_login():
    if GOOGLE_CLIENT_ID == "YOUR_CLIENT_ID" or not GOOGLE_CLIENT_ID:
        session["user_email"] = DEFAULT_RECIPIENT_EMAIL
        return redirect(url_for("dashboard"))
    try:
        redirect_uri = url_for("callback", _external=True)
        return google.authorize_redirect(redirect_uri)
    except Exception as e:
        print(f"Google login redirect error: {e}")
        session["user_email"] = DEFAULT_RECIPIENT_EMAIL
        return redirect(url_for("dashboard"))


@app.route("/callback")
def callback():
    try:
        token = google.authorize_access_token()
        user_info = token.get("userinfo")
        if user_info and "email" in user_info:
            session["user_email"] = user_info["email"]
        return redirect(url_for("dashboard"))
    except Exception as e:
        print(f"OAuth callback error: {e}")
        session["user_email"] = DEFAULT_RECIPIENT_EMAIL
        return redirect(url_for("dashboard"))


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/dashboard")
def dashboard():
    global active_user_email
    if "user_email" not in session:
        return redirect(url_for("login"))

    active_user_email = session["user_email"]
    
    # Initialize user's log file if needed
    init_user_log_file(active_user_email)
    
    expected_dose = get_expected_dose_now()
    readable_last_state = {dose_names[k]: last_state[k] for k in dose_names.keys()}
    return render_template(
        "dashboard.html",
        user_email=session["user_email"],
        expected_dose=expected_dose,
        last_state=readable_last_state
    )


@app.route("/detect", methods=["POST"])
def detect():
    global last_state, pending_state
    
    # Get user email from session
    user_email = session.get("user_email")
    if not user_email:
        return jsonify({"status": "error", "message": "Not authenticated"})
    
    # Initialize user's log file if needed
    init_user_log_file(user_email)
    
    data = request.json.get("image", "")
    if not data or "," not in data:
        return jsonify({"status": "error", "message": "Invalid image payload"})

    try:
        header, encoded = data.split(",", 1)
        img_bytes = base64.b64decode(encoded)
        img_array = np.frombuffer(img_bytes, dtype=np.uint8)
        frame = cv2.imdecode(img_array, cv2.IMREAD_COLOR)

        corners, ids, _ = detector.detectMarkers(frame)
        visible_ids = set(ids.flatten()) if ids is not None else set()

        for marker_id in dose_names.keys():
            currently_open = marker_id in visible_ids

            if currently_open != last_state[marker_id]:
                if pending_state[marker_id]["state"] == currently_open:
                    pending_state[marker_id]["count"] += 1
                else:
                    pending_state[marker_id]["state"] = currently_open
                    pending_state[marker_id]["count"] = 1

                if pending_state[marker_id]["count"] >= CONFIRM_FRAMES:
                    log_event(marker_id, "opened" if currently_open else "closed", user_email)
                    last_state[marker_id] = currently_open
            else:
                pending_state[marker_id]["count"] = 0

        readable_last_state = {dose_names[k]: last_state[k] for k in dose_names.keys()}
        expected_dose = get_expected_dose_now()

        return jsonify({
            "status": "ok",
            "detected_ids": list(visible_ids),
            "last_state": readable_last_state,
            "expected_dose": expected_dose,
            "pending_wrong_lid": pending_wrong_lid
        })

    except Exception as e:
        print(f"Error in /detect route: {e}")
        return jsonify({"status": "error", "message": str(e)})


@app.route("/data")
def data():
    if "user_email" not in session:
        return redirect(url_for("login"))

    user_email = session["user_email"]
    user_log_file = get_user_log_file(user_email)
    user_dose_status_file = get_user_dose_status_file(user_email)

    try:
        events = pd.read_csv(user_log_file).to_html(classes="table", index=False)
    except Exception:
        events = "<p>No event log data found.</p>"

    try:
        # Generate or update dose_status.csv dynamically before rendering
        if os.path.exists(BASE_DIR / "scripts" / "dose_status.py"):
            import sys
            sys.path.append(str(BASE_DIR / "scripts"))
            from dose_status import process_dose_status
            process_dose_status(user_email)

        status = pd.read_csv(user_dose_status_file).to_html(classes="table", index=False)
    except Exception:
        status = "<p>No dose status data calculated yet.</p>"

    return render_template(
        "data.html",
        events=events,
        status=status,
        user_email=session["user_email"]
    )


if __name__ == "__main__":
    print("\n" + "="*60)
    print(">> MediSense Web Application Starting on http://localhost:5000")
    print("="*60 + "\n")
    app.run(host="0.0.0.0", port=5000, debug=True)
