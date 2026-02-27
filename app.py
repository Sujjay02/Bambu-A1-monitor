"""
Bambu A1 Web Monitor
--------------------
Flask web dashboard for real-time 3D printer monitoring via MQTT.
Camera-based failure detection via the Bambu proprietary JPEG-over-TLS stream.

Usage:
    python app.py

Configuration is entered via the web setup page on first run,
or loaded from config.json / environment variables.
"""

import cv2
import numpy as np
import ssl
import json
import time
import csv
import os
import re
import socket
import struct
import logging
import threading
from datetime import datetime
from collections import deque
from flask import Flask, render_template, Response, jsonify, request, redirect, send_from_directory
import paho.mqtt.client as mqtt
from paho.mqtt.enums import CallbackAPIVersion

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("bambu-monitor")

# ── CONFIG ────────────────────────────────────────────────────────────────────
CONFIG_FILE = "config.json"
MQTT_PORT   = 8883
CAMERA_PORT = 6000
LOG_FILE    = "print_telemetry.csv"
FAILURE_DIR = "failure_snapshots"

detector_settings = {
    "spaghetti_sensitivity": 0.6,
    "layer_shift_sensitivity": 0.5,
    "warp_sensitivity": 0.5,
    "trigger_frames": 5,
}
detector_settings_lock = threading.Lock()
settings_changed = threading.Event()

# Mutable printer config (set via web UI or config.json)
config = {"printer_ip": "", "access_code": "", "serial_number": ""}
config_lock = threading.Lock()
config_ready = threading.Event()

def get_config():
    with config_lock:
        return config.copy()

def load_config():
    """Load config from config.json, falling back to env vars."""
    global config
    loaded = {}
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE) as f:
                loaded = json.load(f)
        except (json.JSONDecodeError, OSError):
            pass

    ip = loaded.get("printer_ip") or os.environ.get("PRINTER_IP", "")
    code = loaded.get("access_code") or os.environ.get("ACCESS_CODE", "")
    sn = loaded.get("serial_number") or os.environ.get("SERIAL_NUMBER", "")

    if ip and code and sn:
        with config_lock:
            config["printer_ip"] = ip
            config["access_code"] = code
            config["serial_number"] = sn
        config_ready.set()
        log.info("Config loaded — Printer: %s  SN: %s", ip, sn)
        return True
    return False

def save_config(printer_ip, access_code, serial_number):
    """Save config to config.json and update in-memory state."""
    global config
    data = {
        "printer_ip": printer_ip,
        "access_code": access_code,
        "serial_number": serial_number,
    }
    with open(CONFIG_FILE, "w") as f:
        json.dump(data, f, indent=2)
    with config_lock:
        config.update(data)
    config_ready.set()
    log.info("Config saved — Printer: %s  SN: %s", printer_ip, serial_number)

# ── SYSTEM METRICS ────────────────────────────────────────────────────────────
_start_time = datetime.now()
_mqtt_msg_count = 0
_mqtt_msg_lock = threading.Lock()
_connection_log = deque(maxlen=100)   # list of {time, event, detail}
_connection_log_lock = threading.Lock()

def log_connection_event(event: str, detail: str = ""):
    with _connection_log_lock:
        _connection_log.append({
            "time": datetime.now().isoformat(),
            "event": event,
            "detail": detail,
        })

# ── SHARED STATE ──────────────────────────────────────────────────────────────
telemetry = {
    "print_state": "UNKNOWN",
    "layer_current": 0,
    "layer_total": 0,
    "percent_complete": 0,
    "time_remaining_min": 0,
    "nozzle_temp": 0,
    "nozzle_target": 0,
    "bed_temp": 0,
    "bed_target": 0,
    "print_speed": 0,
    "fan_speed": 0,
}
telemetry_lock = threading.Lock()
print_paused = threading.Event()
mqtt_connected = threading.Event()
camera_available = threading.Event()

temp_history = {
    "timestamps": deque(maxlen=300),
    "nozzle_temp": deque(maxlen=300),
    "nozzle_target": deque(maxlen=300),
    "bed_temp": deque(maxlen=300),
    "bed_target": deque(maxlen=300),
}
temp_history_lock = threading.Lock()

vision_status = {
    "Spaghetti": {"triggered": False, "score": 0.0, "count": 0},
    "LayerShift": {"triggered": False, "score": 0.0, "count": 0},
    "Warp": {"triggered": False, "score": 0.0, "count": 0},
}
vision_lock = threading.Lock()

latest_frame = None
frame_lock = threading.Lock()

mqtt_cmd_client = None
mqtt_cmd_lock = threading.Lock()

csv_lock = threading.Lock()

_sequence_id = 0
_sequence_lock = threading.Lock()

def next_sequence_id():
    global _sequence_id
    with _sequence_lock:
        _sequence_id += 1
        return str(_sequence_id)

# ── MQTT CLIENT ───────────────────────────────────────────────────────────────
def make_mqtt_client():
    cfg = get_config()
    client = mqtt.Client(CallbackAPIVersion.VERSION2)
    client.username_pw_set("bblp", cfg["access_code"])
    tls_ctx = ssl.create_default_context()
    tls_ctx.check_hostname = False
    tls_ctx.verify_mode = ssl.CERT_NONE
    client.tls_set_context(tls_ctx)
    return client

def pause_print(reason: str):
    global mqtt_cmd_client
    if print_paused.is_set():
        return
    log.warning("FAILURE DETECTED: %s", reason)
    cfg = get_config()
    topic = f"device/{cfg['serial_number']}/request"
    with mqtt_cmd_lock:
        if mqtt_cmd_client:
            payload = {"print": {"command": "pause", "sequence_id": next_sequence_id()}}
            mqtt_cmd_client.publish(topic, json.dumps(payload))
    print_paused.set()

def resume_print():
    """Send MQTT resume command and reset vision triggers."""
    cfg = get_config()
    topic = f"device/{cfg['serial_number']}/request"
    with mqtt_cmd_lock:
        if mqtt_cmd_client:
            payload = {"print": {"command": "resume", "sequence_id": next_sequence_id()}}
            mqtt_cmd_client.publish(topic, json.dumps(payload))
    print_paused.clear()
    with vision_lock:
        for name in vision_status:
            vision_status[name]["count"] = 0
    log.info("Print resumed, triggers reset")

# ── CSV LOGGER ────────────────────────────────────────────────────────────────
def init_csv():
    with csv_lock:
        if not os.path.exists(LOG_FILE):
            with open(LOG_FILE, "w", newline="") as f:
                writer = csv.writer(f)
                writer.writerow([
                    "timestamp", "print_state", "layer_current", "layer_total",
                    "nozzle_temp", "nozzle_target", "bed_temp", "bed_target",
                    "print_speed", "fan_speed", "percent_complete", "time_remaining_min"
                ])

def log_to_csv(data: dict):
    with csv_lock:
        with open(LOG_FILE, "a", newline="") as f:
            writer = csv.writer(f)
            writer.writerow([
                datetime.now().isoformat(),
                data.get("print_state", ""),
                data.get("layer_current", ""),
                data.get("layer_total", ""),
                data.get("nozzle_temp", ""),
                data.get("nozzle_target", ""),
                data.get("bed_temp", ""),
                data.get("bed_target", ""),
                data.get("print_speed", ""),
                data.get("fan_speed", ""),
                data.get("percent_complete", ""),
                data.get("time_remaining_min", ""),
            ])

# ── TELEMETRY PARSER ──────────────────────────────────────────────────────────
def parse_telemetry(payload: dict) -> dict:
    data = {}
    if "print" in payload:
        p = payload["print"]
        data["print_state"]        = p.get("gcode_state", "UNKNOWN")
        data["layer_current"]      = p.get("layer_num", 0)
        data["layer_total"]        = p.get("total_layer_num", 0)
        data["percent_complete"]   = p.get("mc_percent", 0)
        data["time_remaining_min"] = round(p.get("mc_remaining_time", 0))
        data["print_speed"]        = p.get("spd_lvl", 0)
        nozzle = p.get("nozzle_temper", None)
        if nozzle is not None:
            data["nozzle_temp"]   = nozzle
            data["nozzle_target"] = p.get("nozzle_target_temper", 0)
        bed = p.get("bed_temper", None)
        if bed is not None:
            data["bed_temp"]   = bed
            data["bed_target"] = p.get("bed_target_temper", 0)
        data["fan_speed"] = p.get("fan_gear", 0)
    return data

# ── TELEMETRY THREAD ──────────────────────────────────────────────────────────
def telemetry_thread():
    global mqtt_cmd_client

    config_ready.wait()
    cfg = get_config()

    client = make_mqtt_client()
    topic_sub = f"device/{cfg['serial_number']}/report"

    def on_connect(c, userdata, flags, rc, properties=None):
        if rc == 0 or rc.value == 0:
            c.subscribe(topic_sub)
            mqtt_connected.set()
            with mqtt_cmd_lock:
                global mqtt_cmd_client
                mqtt_cmd_client = c
            log.info("MQTT connected and subscribed")
            log_connection_event("mqtt_connected", cfg["printer_ip"])
        else:
            log.error("MQTT connection failed: %s", rc)
            log_connection_event("mqtt_failed", str(rc))

    def on_disconnect(c, userdata, flags, rc, properties=None):
        mqtt_connected.clear()
        log.warning("MQTT disconnected")
        log_connection_event("mqtt_disconnected")

    def on_message(c, userdata, msg, properties=None):
        global _mqtt_msg_count
        with _mqtt_msg_lock:
            _mqtt_msg_count += 1
        try:
            payload = json.loads(msg.payload.decode("utf-8"))
            data = parse_telemetry(payload)
            if data:
                with telemetry_lock:
                    telemetry.update(data)
                    nz = telemetry["nozzle_temp"]
                    nt = telemetry["nozzle_target"]
                    bt = telemetry["bed_temp"]
                    btt = telemetry["bed_target"]
                log_to_csv(data)
                with temp_history_lock:
                    temp_history["timestamps"].append(datetime.now().strftime("%H:%M:%S"))
                    temp_history["nozzle_temp"].append(nz)
                    temp_history["nozzle_target"].append(nt)
                    temp_history["bed_temp"].append(bt)
                    temp_history["bed_target"].append(btt)
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as e:
            log.warning("MQTT parse error: %s", e)

    client.on_connect = on_connect
    client.on_disconnect = on_disconnect
    client.on_message = on_message
    while True:
        try:
            client.connect(cfg["printer_ip"], MQTT_PORT, keepalive=60)
            client.loop_forever()
        except (TimeoutError, OSError) as e:
            mqtt_connected.clear()
            log.error("MQTT connection failed: %s — retrying in 5s", e)
            time.sleep(5)

# ── DETECTORS ─────────────────────────────────────────────────────────────────
class SpaghettiDetector:
    def __init__(self, sensitivity=0.6):
        self.threshold = 1.0 - sensitivity
        self.baseline_density = 0
        self.frame_count = 0
        self.densities = deque(maxlen=30)

    def detect(self, frame):
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        edges = cv2.Canny(gray, 50, 150)
        density = np.sum(edges > 0) / edges.size
        if self.frame_count < 30:
            self.densities.append(density)
            self.frame_count += 1
            if self.frame_count == 30:
                self.baseline_density = np.mean(self.densities)
            return False, density
        ratio = density / (self.baseline_density + 1e-6)
        return ratio > (1.0 + self.threshold * 2), ratio

class LayerShiftDetector:
    def __init__(self, sensitivity=0.5):
        self.threshold = sensitivity * 30
        self.prev_frame = None

    def detect(self, frame):
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        gray = cv2.GaussianBlur(gray, (5, 5), 0)
        if self.prev_frame is None:
            self.prev_frame = gray
            return False, 0.0
        h, w = gray.shape
        r_curr = gray[h//4:3*h//4, w//4:3*w//4].astype(np.float32)
        r_prev = self.prev_frame[h//4:3*h//4, w//4:3*w//4].astype(np.float32)
        shift, _ = cv2.phaseCorrelate(r_prev, r_curr)
        h_shift = abs(shift[0])
        self.prev_frame = gray
        return h_shift > self.threshold, h_shift

class WarpDetector:
    def __init__(self, sensitivity=0.5):
        self.threshold = sensitivity
        self.baseline = 0.0
        self.frame_count = 0
        self.history = deque(maxlen=30)

    def corner_score(self, frame):
        h, w = frame.shape[:2]
        ch, cw = h // 6, w // 6
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        corners = [
            gray[0:ch, 0:cw], gray[0:ch, w-cw:w],
            gray[h-ch:h, 0:cw], gray[h-ch:h, w-cw:w],
        ]
        return np.mean([np.std(c) for c in corners])

    def detect(self, frame):
        score = self.corner_score(frame)
        if self.frame_count < 30:
            self.history.append(score)
            self.frame_count += 1
            if self.frame_count == 30:
                self.baseline = np.mean(self.history)
            return False, score
        ratio = score / (self.baseline + 1e-6)
        return ratio > (1.0 + self.threshold), ratio

# ── CAMERA AUTH ───────────────────────────────────────────────────────────────
JPEG_START = bytes([0xFF, 0xD8, 0xFF, 0xE0])
JPEG_END   = bytes([0xFF, 0xD9])

def create_camera_auth(access_code: str) -> bytes:
    auth = bytearray(80)
    struct.pack_into('<I', auth, 0, 0x40)
    struct.pack_into('<I', auth, 4, 0x3000)
    auth[16:16 + 4] = b'bblp'
    code = access_code.encode('utf-8')
    auth[48:48 + len(code)] = code
    return bytes(auth)

def read_exact(sock, n):
    buf = bytearray()
    while len(buf) < n:
        try:
            chunk = sock.recv(n - len(buf))
            if not chunk:
                return None
            buf.extend(chunk)
        except ssl.SSLWantReadError:
            time.sleep(0.05)
    return bytes(buf)

# ── VISION THREAD ─────────────────────────────────────────────────────────────
def vision_thread():
    global latest_frame

    config_ready.wait()
    cfg = get_config()

    tls_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    tls_ctx.check_hostname = False
    tls_ctx.verify_mode = ssl.CERT_NONE
    auth_data = create_camera_auth(cfg["access_code"])

    while True:
        # Check camera port
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(3)
            s.connect((cfg["printer_ip"], CAMERA_PORT))
            s.close()
        except (socket.timeout, OSError):
            log.info("Camera port %d not available — retrying in 30s", CAMERA_PORT)
            time.sleep(30)
            continue

        raw = None
        sock = None
        try:
            log.info("Camera connecting to %s:%d", cfg["printer_ip"], CAMERA_PORT)
            raw = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            raw.settimeout(10)
            raw.connect((cfg["printer_ip"], CAMERA_PORT))
            sock = tls_ctx.wrap_socket(raw)
            raw = None  # sock now owns the underlying socket
            sock.sendall(auth_data)

            log.info("Camera authenticated, receiving frames")
            camera_available.set()
            log_connection_event("camera_connected", f"{cfg['printer_ip']}:{CAMERA_PORT}")
            os.makedirs(FAILURE_DIR, exist_ok=True)

            with detector_settings_lock:
                ds = detector_settings.copy()
            spaghetti   = SpaghettiDetector(ds["spaghetti_sensitivity"])
            layer_shift = LayerShiftDetector(ds["layer_shift_sensitivity"])
            warp        = WarpDetector(ds["warp_sensitivity"])
            trigger_counts = {"Spaghetti": 0, "LayerShift": 0, "Warp": 0}

            while True:
                header = read_exact(sock, 16)
                if header is None:
                    log.warning("Camera connection closed")
                    break

                payload_size = struct.unpack_from('<I', header, 0)[0]
                if payload_size == 0 or payload_size > 5_000_000:
                    continue

                jpeg_data = read_exact(sock, payload_size)
                if jpeg_data is None:
                    log.warning("Camera connection lost during frame read")
                    break

                if not (jpeg_data[:4] == JPEG_START and jpeg_data[-2:] == JPEG_END):
                    continue

                np_arr = np.frombuffer(jpeg_data, dtype=np.uint8)
                frame = cv2.imdecode(np_arr, cv2.IMREAD_COLOR)
                if frame is None:
                    continue

                with frame_lock:
                    latest_frame = frame.copy()

                # Re-create detectors if settings changed
                if settings_changed.is_set():
                    settings_changed.clear()
                    with detector_settings_lock:
                        ds = detector_settings.copy()
                    spaghetti   = SpaghettiDetector(ds["spaghetti_sensitivity"])
                    layer_shift = LayerShiftDetector(ds["layer_shift_sensitivity"])
                    warp        = WarpDetector(ds["warp_sensitivity"])
                    log.info("Detectors re-initialized with updated settings")

                sp_trig, sp_score = spaghetti.detect(frame)
                ls_trig, ls_score = layer_shift.detect(frame)
                wp_trig, wp_score = warp.detect(frame)

                results = {
                    "Spaghetti":  (sp_trig, sp_score),
                    "LayerShift": (ls_trig, ls_score),
                    "Warp":       (wp_trig, wp_score),
                }

                for name, (triggered, score) in results.items():
                    trigger_counts[name] = trigger_counts[name] + 1 if triggered else max(0, trigger_counts[name] - 1)

                with vision_lock:
                    for name, (triggered, score) in results.items():
                        vision_status[name]["triggered"] = bool(triggered)
                        vision_status[name]["score"] = float(score)
                        vision_status[name]["count"] = trigger_counts[name]

                if not print_paused.is_set():
                    with detector_settings_lock:
                        tf = detector_settings["trigger_frames"]
                    for name, count in trigger_counts.items():
                        if count >= tf:
                            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
                            path = os.path.join(FAILURE_DIR, f"{name}_{ts}.jpg")
                            cv2.imwrite(path, frame)
                            log.info("Snapshot saved: %s", path)
                            pause_print(name)
                            trigger_counts[name] = 0
                            break

        except Exception as e:
            log.error("Camera error: %s — retrying in 5s", e)
            log_connection_event("camera_error", str(e))
        finally:
            camera_available.clear()
            for s in (sock, raw):
                if s:
                    try:
                        s.close()
                    except Exception:
                        pass
        time.sleep(5)

# ── FLASK APP ─────────────────────────────────────────────────────────────────
app = Flask(__name__)

@app.route("/")
def index():
    if not config_ready.is_set():
        return render_template("setup.html")
    cfg = get_config()
    return render_template("dashboard.html",
                           printer_ip=cfg["printer_ip"],
                           serial_number=cfg["serial_number"])

@app.route("/setup")
def setup_page():
    cfg = get_config()
    return render_template("setup.html",
                           printer_ip=cfg["printer_ip"],
                           access_code=cfg["access_code"],
                           serial_number=cfg["serial_number"])

@app.route("/api/config", methods=["GET"])
def api_config_get():
    cfg = get_config()
    # Mask access code for display
    code = cfg["access_code"]
    masked = code[:2] + "*" * (len(code) - 2) if len(code) > 2 else "*" * len(code)
    return jsonify({
        "printer_ip": cfg["printer_ip"],
        "access_code_masked": masked,
        "serial_number": cfg["serial_number"],
        "configured": config_ready.is_set(),
    })

@app.route("/api/config", methods=["POST"])
def api_config_post():
    data = request.get_json(silent=True) or {}
    ip = data.get("printer_ip", "").strip()
    code = data.get("access_code", "").strip()
    sn = data.get("serial_number", "").strip()

    errors = []
    if not ip:
        errors.append("Printer IP is required")
    elif not re.match(r"^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}$", ip):
        errors.append("Invalid IP address format")
    elif any(int(octet) > 255 for octet in ip.split(".")):
        errors.append("Invalid IP address: octet out of range")
    if not code:
        errors.append("Access Code is required")
    elif len(code) < 4 or len(code) > 16:
        errors.append("Access Code should be 4-16 characters")
    if not sn:
        errors.append("Serial Number is required")
    if errors:
        return jsonify({"errors": errors}), 400

    save_config(ip, code, sn)
    return jsonify({"status": "saved"})

@app.route("/api/telemetry")
def api_telemetry():
    with telemetry_lock:
        data = telemetry.copy()
    data["mqtt_connected"] = mqtt_connected.is_set()
    data["camera_available"] = camera_available.is_set()
    data["print_paused"] = print_paused.is_set()
    with vision_lock:
        data["detectors"] = {k: v.copy() for k, v in vision_status.items()}
    return jsonify(data)

@app.route("/api/history")
def api_history():
    with temp_history_lock:
        return jsonify({
            "timestamps": list(temp_history["timestamps"]),
            "nozzle_temp": list(temp_history["nozzle_temp"]),
            "nozzle_target": list(temp_history["nozzle_target"]),
            "bed_temp": list(temp_history["bed_temp"]),
            "bed_target": list(temp_history["bed_target"]),
        })

@app.route("/history")
def history_page():
    if not config_ready.is_set():
        return redirect("/")
    cfg = get_config()
    return render_template("history.html",
                           printer_ip=cfg["printer_ip"],
                           serial_number=cfg["serial_number"])

# ── HISTORY JOB PARSER ───────────────────────────────────────────────────────
def _safe_int(val):
    try:
        return int(float(val)) if val else 0
    except (ValueError, TypeError):
        return 0

def _finalize_job(job):
    duration = (job["end_time"] - job["start_time"]).total_seconds() / 60
    end_state = job["end_state"]
    if end_state == "FINISH" or job["last_percent"] >= 99:
        status = "completed"
    elif end_state == "FAILED":
        status = "failed"
    elif end_state == "RUNNING":
        status = "printing"
    else:
        status = "cancelled"
    return {
        "start_time": job["start_time"].isoformat(),
        "duration_min": round(duration, 1),
        "total_layers": job["total_layers"],
        "status": status,
        "failure_reason": "",
    }

def parse_jobs_from_csv():
    """Parse CSV telemetry into a list of print job dicts, most recent first."""
    jobs = []
    current_job = None
    printing_states = {"RUNNING", "PREPARE", "PAUSE"}
    terminal_states = {"IDLE", "FINISH", "FAILED"}
    with csv_lock:
        try:
            with open(LOG_FILE, "r") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    state = row.get("print_state", "UNKNOWN").strip()
                    ts_str = row.get("timestamp", "")
                    try:
                        ts = datetime.fromisoformat(ts_str)
                    except (ValueError, TypeError):
                        continue
                    layer_cur = _safe_int(row.get("layer_current"))
                    layer_tot = _safe_int(row.get("layer_total"))
                    pct = _safe_int(row.get("percent_complete"))
                    if current_job is None:
                        if state in printing_states:
                            current_job = {
                                "start_time": ts, "end_time": None,
                                "max_layer": layer_cur, "total_layers": layer_tot,
                                "last_percent": pct, "end_state": None,
                            }
                    else:
                        current_job["max_layer"] = max(current_job["max_layer"], layer_cur)
                        current_job["total_layers"] = max(current_job["total_layers"], layer_tot)
                        current_job["last_percent"] = pct
                        if state in terminal_states:
                            current_job["end_time"] = ts
                            current_job["end_state"] = state
                            jobs.append(_finalize_job(current_job))
                            current_job = None
                if current_job is not None:
                    current_job["end_time"] = datetime.now()
                    current_job["end_state"] = "RUNNING"
                    jobs.append(_finalize_job(current_job))
        except FileNotFoundError:
            pass
    jobs.reverse()
    return jobs

@app.route("/api/history/stats")
def api_history_stats():
    jobs = parse_jobs_from_csv()
    finished = [j for j in jobs if j["status"] != "printing"]
    total = len(finished)
    success = sum(1 for j in finished if j["status"] == "completed")
    total_time = sum(j["duration_min"] for j in finished)
    return jsonify({
        "total_prints": total,
        "success_rate": round((success / total * 100) if total > 0 else 0, 1),
        "total_print_time_min": round(total_time, 1),
        "avg_duration_min": round((total_time / total) if total > 0 else 0, 1),
    })

@app.route("/api/history/jobs")
def api_history_jobs():
    return jsonify(parse_jobs_from_csv())

# ── DETECTOR SETTINGS API ────────────────────────────────────────────────────
@app.route("/api/detector-settings", methods=["GET"])
def api_detector_settings_get():
    with detector_settings_lock:
        return jsonify(detector_settings.copy())

@app.route("/api/detector-settings", methods=["POST"])
def api_detector_settings_post():
    data = request.get_json(silent=True) or {}
    errors = []
    for key in ("spaghetti_sensitivity", "layer_shift_sensitivity", "warp_sensitivity"):
        if key in data:
            try:
                val = float(data[key])
                if not (0.1 <= val <= 1.0):
                    errors.append(f"{key} must be between 0.1 and 1.0")
            except (ValueError, TypeError):
                errors.append(f"{key} must be a number")
    if "trigger_frames" in data:
        try:
            val = int(data["trigger_frames"])
            if not (1 <= val <= 30):
                errors.append("trigger_frames must be between 1 and 30")
        except (ValueError, TypeError):
            errors.append("trigger_frames must be an integer")
    if errors:
        return jsonify({"errors": errors}), 400
    with detector_settings_lock:
        for key in detector_settings:
            if key in data:
                if key == "trigger_frames":
                    detector_settings[key] = int(data[key])
                else:
                    detector_settings[key] = float(data[key])
    settings_changed.set()
    log.info("Detector settings updated: %s", {k: detector_settings[k] for k in detector_settings})
    return jsonify({"status": "updated"})

# ── SNAPSHOT GALLERY API ─────────────────────────────────────────────────────
@app.route("/snapshots/<path:filename>")
def serve_snapshot(filename):
    return send_from_directory(FAILURE_DIR, filename)

@app.route("/api/snapshots")
def api_snapshots():
    snapshots = []
    if os.path.isdir(FAILURE_DIR):
        for fname in sorted(os.listdir(FAILURE_DIR), reverse=True):
            if not fname.lower().endswith(".jpg"):
                continue
            match = re.match(r"^(\w+)_(\d{8})_(\d{6})\.jpg$", fname)
            if match:
                detector = match.group(1)
                date_str = match.group(2)
                time_str = match.group(3)
                try:
                    ts = datetime.strptime(date_str + time_str, "%Y%m%d%H%M%S")
                    timestamp = ts.isoformat()
                except ValueError:
                    timestamp = ""
            else:
                detector = "Unknown"
                timestamp = ""
            snapshots.append({
                "filename": fname,
                "detector": detector,
                "timestamp": timestamp,
                "url": f"/snapshots/{fname}",
            })
    return jsonify(snapshots)

@app.route("/gallery")
def gallery_page():
    if not config_ready.is_set():
        return redirect("/")
    cfg = get_config()
    return render_template("gallery.html",
                           printer_ip=cfg["printer_ip"],
                           serial_number=cfg["serial_number"])

# ── GCODE / PRINT CONTROL API ───────────────────────────────────────────────
def send_gcode(gcode_line: str):
    """Send a G-code command to the printer via MQTT."""
    cfg = get_config()
    topic = f"device/{cfg['serial_number']}/request"
    payload = {
        "print": {
            "command": "gcode_line",
            "sequence_id": next_sequence_id(),
            "param": gcode_line + "\n",
        }
    }
    with mqtt_cmd_lock:
        if mqtt_cmd_client:
            mqtt_cmd_client.publish(topic, json.dumps(payload))
            return True
    return False

@app.route("/api/speed", methods=["POST"])
def api_set_speed():
    data = request.get_json(silent=True) or {}
    try:
        speed = int(data.get("speed_percent", 100))
        if not (10 <= speed <= 300):
            return jsonify({"error": "Speed must be 10-300%"}), 400
    except (ValueError, TypeError):
        return jsonify({"error": "Invalid speed value"}), 400
    if send_gcode(f"M220 S{speed}"):
        log.info("Speed set to %d%%", speed)
        return jsonify({"status": "ok", "speed_percent": speed})
    return jsonify({"error": "MQTT not connected"}), 503

@app.route("/api/temperature", methods=["POST"])
def api_set_temperature():
    data = request.get_json(silent=True) or {}
    target = data.get("target")
    try:
        temp = int(data.get("value", 0))
    except (ValueError, TypeError):
        return jsonify({"error": "Invalid temperature value"}), 400
    if target == "nozzle":
        if not (0 <= temp <= 300):
            return jsonify({"error": "Nozzle temp must be 0-300"}), 400
        gcode = f"M104 S{temp}"
    elif target == "bed":
        if not (0 <= temp <= 120):
            return jsonify({"error": "Bed temp must be 0-120"}), 400
        gcode = f"M140 S{temp}"
    else:
        return jsonify({"error": "target must be 'nozzle' or 'bed'"}), 400
    if send_gcode(gcode):
        log.info("%s temp set to %d", target.capitalize(), temp)
        return jsonify({"status": "ok", "target": target, "value": temp})
    return jsonify({"error": "MQTT not connected"}), 503

# ── PWA ──────────────────────────────────────────────────────────────────────
@app.route("/offline")
def offline_page():
    return render_template("offline.html")

# ── SYSTEM STATUS API ────────────────────────────────────────────────────────
@app.route("/status")
def status_page():
    if not config_ready.is_set():
        return redirect("/")
    cfg = get_config()
    return render_template("status.html",
                           printer_ip=cfg["printer_ip"],
                           serial_number=cfg["serial_number"])

@app.route("/api/system-status")
def api_system_status():
    import resource
    uptime_sec = (datetime.now() - _start_time).total_seconds()
    with _mqtt_msg_lock:
        msg_count = _mqtt_msg_count
    with _connection_log_lock:
        conn_log = list(_connection_log)
    mem_mb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024  # KB to MB
    rate = msg_count / max(uptime_sec, 1) * 60  # msgs per minute
    return jsonify({
        "uptime_seconds": round(uptime_sec),
        "start_time": _start_time.isoformat(),
        "mqtt_connected": mqtt_connected.is_set(),
        "camera_connected": camera_available.is_set(),
        "mqtt_messages_total": msg_count,
        "mqtt_messages_per_min": round(rate, 1),
        "memory_mb": round(mem_mb, 1),
        "connection_log": conn_log[-50:],
    })

# ── ANALYTICS API ────────────────────────────────────────────────────────────
@app.route("/analytics")
def analytics_page():
    if not config_ready.is_set():
        return redirect("/")
    cfg = get_config()
    return render_template("analytics.html",
                           printer_ip=cfg["printer_ip"],
                           serial_number=cfg["serial_number"])

@app.route("/api/analytics")
def api_analytics():
    """Compute analytics from CSV: daily print counts, failure breakdown, temp stats, duration distribution."""
    jobs = parse_jobs_from_csv()
    # Daily print counts (last 30 days)
    daily = {}
    for j in jobs:
        if j["status"] == "printing":
            continue
        day = j["start_time"][:10]
        if day not in daily:
            daily[day] = {"completed": 0, "failed": 0, "cancelled": 0}
        daily[day][j["status"]] = daily[day].get(j["status"], 0) + 1
    daily_sorted = sorted(daily.items())[-30:]

    # Failure type breakdown
    failure_types = {}
    for j in jobs:
        if j["status"] == "failed":
            reason = j.get("failure_reason") or "Unknown"
            failure_types[reason] = failure_types.get(reason, 0) + 1

    # Duration distribution (buckets: <30m, 30-60m, 1-2h, 2-4h, 4-8h, 8h+)
    buckets = {"< 30m": 0, "30-60m": 0, "1-2h": 0, "2-4h": 0, "4-8h": 0, "8h+": 0}
    for j in jobs:
        d = j.get("duration_min", 0)
        if d < 30: buckets["< 30m"] += 1
        elif d < 60: buckets["30-60m"] += 1
        elif d < 120: buckets["1-2h"] += 1
        elif d < 240: buckets["2-4h"] += 1
        elif d < 480: buckets["4-8h"] += 1
        else: buckets["8h+"] += 1

    # Temperature stats from CSV
    temp_stats = {"nozzle_avg": 0, "nozzle_max": 0, "bed_avg": 0, "bed_max": 0}
    nozzle_temps = []
    bed_temps = []
    with csv_lock:
        try:
            with open(LOG_FILE, "r") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    nt = row.get("nozzle_temp", "")
                    bt = row.get("bed_temp", "")
                    try:
                        nv = float(nt)
                        if nv > 30:  # ignore cold readings
                            nozzle_temps.append(nv)
                    except (ValueError, TypeError):
                        pass
                    try:
                        bv = float(bt)
                        if bv > 20:
                            bed_temps.append(bv)
                    except (ValueError, TypeError):
                        pass
        except FileNotFoundError:
            pass
    if nozzle_temps:
        temp_stats["nozzle_avg"] = round(sum(nozzle_temps) / len(nozzle_temps), 1)
        temp_stats["nozzle_max"] = round(max(nozzle_temps), 1)
    if bed_temps:
        temp_stats["bed_avg"] = round(sum(bed_temps) / len(bed_temps), 1)
        temp_stats["bed_max"] = round(max(bed_temps), 1)

    return jsonify({
        "daily_prints": [{"date": d, **v} for d, v in daily_sorted],
        "failure_types": failure_types,
        "duration_distribution": buckets,
        "temp_stats": temp_stats,
        "total_jobs": len(jobs),
    })

# ── PRINT COST TRACKER API ──────────────────────────────────────────────────
@app.route("/costs")
def costs_page():
    if not config_ready.is_set():
        return redirect("/")
    cfg = get_config()
    return render_template("costs.html",
                           printer_ip=cfg["printer_ip"],
                           serial_number=cfg["serial_number"])

@app.route("/api/costs")
def api_costs():
    """Estimate print costs from job data. Uses configurable cost per hour."""
    try:
        cost_per_kg = float(request.args.get("cost_per_kg", 25))
        power_cost_kwh = float(request.args.get("power_cost_kwh", 0.12))
    except (ValueError, TypeError):
        cost_per_kg = 25
        power_cost_kwh = 0.12
    printer_watts = 150  # Bambu A1 typical power draw

    jobs = parse_jobs_from_csv()
    finished = [j for j in jobs if j["status"] != "printing"]
    total_time_h = sum(j["duration_min"] for j in finished) / 60
    # Rough filament estimate: ~5g/min for typical prints
    est_filament_g = sum(j["duration_min"] * 0.3 for j in finished)
    est_filament_kg = est_filament_g / 1000
    filament_cost = est_filament_kg * cost_per_kg
    power_cost = total_time_h * (printer_watts / 1000) * power_cost_kwh
    total_cost = filament_cost + power_cost

    # Per-job breakdown
    job_costs = []
    for j in finished:
        dur_h = j["duration_min"] / 60
        fg = j["duration_min"] * 0.3
        fc = (fg / 1000) * cost_per_kg
        pc = dur_h * (printer_watts / 1000) * power_cost_kwh
        job_costs.append({
            "start_time": j["start_time"],
            "duration_min": j["duration_min"],
            "status": j["status"],
            "est_filament_g": round(fg, 1),
            "filament_cost": round(fc, 2),
            "power_cost": round(pc, 2),
            "total_cost": round(fc + pc, 2),
        })

    return jsonify({
        "summary": {
            "total_prints": len(finished),
            "total_time_hours": round(total_time_h, 1),
            "est_filament_kg": round(est_filament_kg, 2),
            "filament_cost": round(filament_cost, 2),
            "power_cost": round(power_cost, 2),
            "total_cost": round(total_cost, 2),
            "avg_cost_per_print": round(total_cost / max(len(finished), 1), 2),
            "cost_per_kg": cost_per_kg,
            "power_cost_kwh": power_cost_kwh,
        },
        "jobs": job_costs,
    })

# ── LIVE TELEMETRY VIEWER ────────────────────────────────────────────────────
@app.route("/telemetry")
def telemetry_page():
    if not config_ready.is_set():
        return redirect("/")
    cfg = get_config()
    return render_template("telemetry.html",
                           printer_ip=cfg["printer_ip"],
                           serial_number=cfg["serial_number"])

@app.route("/api/telemetry/recent")
def api_telemetry_recent():
    """Return the most recent N rows from the CSV as JSON."""
    try:
        count = min(int(request.args.get("count", 200)), 2000)
    except (ValueError, TypeError):
        count = 200
    rows = []
    with csv_lock:
        try:
            with open(LOG_FILE, "r") as f:
                reader = csv.DictReader(f)
                all_rows = list(reader)
        except FileNotFoundError:
            all_rows = []
    for row in all_rows[-count:]:
        rows.append(row)
    return jsonify(rows)

# ── CSV EXPORT ───────────────────────────────────────────────────────────────
@app.route("/api/export/csv")
def api_export_csv():
    if not os.path.exists(LOG_FILE):
        return "", 204
    return send_from_directory(
        ".", LOG_FILE,
        mimetype="text/csv",
        as_attachment=True,
        download_name=f"bambu_telemetry_{datetime.now().strftime('%Y%m%d')}.csv",
    )

@app.route("/api/camera/frame")
def api_camera_frame():
    with frame_lock:
        frame = latest_frame
    if frame is None:
        return "", 204
    _, jpeg = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 80])
    return Response(jpeg.tobytes(), mimetype="image/jpeg")

@app.route("/api/pause", methods=["POST"])
def api_pause():
    pause_print("Manual pause from dashboard")
    return jsonify({"status": "paused"})

@app.route("/api/resume", methods=["POST"])
def api_resume():
    resume_print()
    return jsonify({"status": "resumed"})

@app.route("/events")
def events():
    def stream():
        prev_state = None
        prev_paused = False
        try:
            while True:
                with telemetry_lock:
                    data = telemetry.copy()
                data["mqtt_connected"] = mqtt_connected.is_set()
                data["camera_available"] = camera_available.is_set()
                data["print_paused"] = print_paused.is_set()
                with vision_lock:
                    data["detectors"] = {k: v.copy() for k, v in vision_status.items()}
                with detector_settings_lock:
                    data["trigger_frames"] = detector_settings["trigger_frames"]

                current_state = data.get("print_state", "UNKNOWN")
                current_paused = data["print_paused"]

                # Emit named events for state transitions
                if prev_state is not None:
                    if prev_state == "RUNNING" and current_state == "FINISH":
                        yield f"event: print_complete\ndata: {json.dumps({'message': 'Print completed successfully!'})}\n\n"
                    if not prev_paused and current_paused:
                        triggered = [n for n, info in data["detectors"].items() if info["triggered"]]
                        reason = ", ".join(triggered) if triggered else "Manual pause"
                        yield f"event: failure_detected\ndata: {json.dumps({'message': f'Print paused: {reason}', 'detectors': triggered})}\n\n"

                prev_state = current_state
                prev_paused = current_paused

                yield f"data: {json.dumps(data)}\n\n"
                time.sleep(2)
        except GeneratorExit:
            pass
    return Response(stream(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

# ── MAIN ──────────────────────────────────────────────────────────────────────
def main():
    log.info("=" * 55)
    log.info("  BAMBU A1 WEB MONITOR")
    log.info("=" * 55)

    init_csv()
    loaded = load_config()

    if not loaded:
        log.info("No config found — open http://0.0.0.0:5000 to set up")

    # Start background threads (they wait for config_ready)
    t_telemetry = threading.Thread(target=telemetry_thread, daemon=True)
    t_telemetry.start()

    t_vision = threading.Thread(target=vision_thread, daemon=True)
    t_vision.start()

    log.info("Dashboard at http://0.0.0.0:5000")
    app.run(host="0.0.0.0", port=5000, threaded=True)

if __name__ == "__main__":
    main()
