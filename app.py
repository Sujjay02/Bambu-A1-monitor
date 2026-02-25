"""
Bambu A1 Web Monitor
--------------------
Flask web dashboard for real-time 3D printer monitoring via MQTT.
Optional camera-based failure detection when RTSP stream is available.

Usage:
    python app.py

Environment variables:
    PRINTER_IP      (default: 10.8.103.8)
    ACCESS_CODE     (default: 962473bf)
    SERIAL_NUMBER   (default: 03919D532701879)
"""

import cv2
import numpy as np
import ssl
import json
import time
import csv
import os
import socket
import threading
from datetime import datetime
from collections import deque
from flask import Flask, render_template, Response, jsonify, request
import paho.mqtt.client as mqtt
from paho.mqtt.enums import CallbackAPIVersion

# ── CONFIG ────────────────────────────────────────────────────────────────────
PRINTER_IP    = os.environ.get("PRINTER_IP", "10.8.103.8")
ACCESS_CODE   = os.environ.get("ACCESS_CODE", "962473bf")
SERIAL_NUMBER = os.environ.get("SERIAL_NUMBER", "03919D532701879")

MQTT_PORT      = 8883
MQTT_TOPIC_SUB = f"device/{SERIAL_NUMBER}/report"
MQTT_TOPIC_PUB = f"device/{SERIAL_NUMBER}/request"
CAMERA_URL     = f"rtsps://bblp:{ACCESS_CODE}@{PRINTER_IP}:322/streaming/live/1"

LOG_FILE       = "print_telemetry.csv"
FAILURE_DIR    = "failure_snapshots"

SPAGHETTI_SENSITIVITY   = 0.6
LAYER_SHIFT_SENSITIVITY = 0.5
WARP_SENSITIVITY        = 0.5
TRIGGER_FRAMES          = 5

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

# Temperature history (last 300 readings ~5 min)
temp_history = {
    "timestamps": deque(maxlen=300),
    "nozzle_temp": deque(maxlen=300),
    "nozzle_target": deque(maxlen=300),
    "bed_temp": deque(maxlen=300),
    "bed_target": deque(maxlen=300),
}
temp_history_lock = threading.Lock()

# Vision state
vision_status = {
    "Spaghetti": {"triggered": False, "score": 0.0, "count": 0},
    "LayerShift": {"triggered": False, "score": 0.0, "count": 0},
    "Warp": {"triggered": False, "score": 0.0, "count": 0},
}
vision_lock = threading.Lock()

# Latest camera frame
latest_frame = None
frame_lock = threading.Lock()

# Global MQTT client for sending commands
mqtt_cmd_client = None
mqtt_cmd_lock = threading.Lock()

# ── MQTT CLIENT ───────────────────────────────────────────────────────────────
def make_mqtt_client():
    client = mqtt.Client(CallbackAPIVersion.VERSION2)
    client.username_pw_set("bblp", ACCESS_CODE)
    tls_ctx = ssl.create_default_context()
    tls_ctx.check_hostname = False
    tls_ctx.verify_mode = ssl.CERT_NONE
    client.tls_set_context(tls_ctx)
    return client

def pause_print(reason: str):
    global mqtt_cmd_client
    if print_paused.is_set():
        return
    print(f"[ALERT] FAILURE DETECTED: {reason}")
    with mqtt_cmd_lock:
        if mqtt_cmd_client:
            payload = {"print": {"command": "pause", "sequence_id": "1"}}
            mqtt_cmd_client.publish(MQTT_TOPIC_PUB, json.dumps(payload))
    print_paused.set()

def resume_monitoring():
    print_paused.clear()
    with vision_lock:
        for name in vision_status:
            vision_status[name]["count"] = 0
    print("[INFO] Triggers reset, monitoring resumed")

# ── CSV LOGGER ────────────────────────────────────────────────────────────────
def init_csv():
    if not os.path.exists(LOG_FILE):
        with open(LOG_FILE, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow([
                "timestamp", "print_state", "layer_current", "layer_total",
                "nozzle_temp", "nozzle_target", "bed_temp", "bed_target",
                "print_speed", "fan_speed", "percent_complete", "time_remaining_min"
            ])

def log_to_csv(data: dict):
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
    client = make_mqtt_client()

    def on_connect(c, userdata, flags, rc, properties=None):
        if rc == 0 or rc.value == 0:
            c.subscribe(MQTT_TOPIC_SUB)
            mqtt_connected.set()
            with mqtt_cmd_lock:
                global mqtt_cmd_client
                mqtt_cmd_client = c
            print("[MQTT] Connected and subscribed")
        else:
            print(f"[MQTT] Connection failed: {rc}")

    def on_disconnect(c, userdata, flags, rc, properties=None):
        mqtt_connected.clear()
        print("[MQTT] Disconnected")

    def on_message(c, userdata, msg, properties=None):
        try:
            payload = json.loads(msg.payload.decode("utf-8"))
            data = parse_telemetry(payload)
            if data:
                with telemetry_lock:
                    telemetry.update(data)
                log_to_csv(data)
                # Update temperature history
                with temp_history_lock:
                    temp_history["timestamps"].append(datetime.now().strftime("%H:%M:%S"))
                    with telemetry_lock:
                        temp_history["nozzle_temp"].append(telemetry["nozzle_temp"])
                        temp_history["nozzle_target"].append(telemetry["nozzle_target"])
                        temp_history["bed_temp"].append(telemetry["bed_temp"])
                        temp_history["bed_target"].append(telemetry["bed_target"])
        except Exception as e:
            print(f"[MQTT] Parse error: {e}")

    client.on_connect = on_connect
    client.on_disconnect = on_disconnect
    client.on_message = on_message
    while True:
        try:
            client.connect(PRINTER_IP, MQTT_PORT, keepalive=60)
            client.loop_forever()
        except (TimeoutError, OSError) as e:
            mqtt_connected.clear()
            print(f"[MQTT] Connection failed: {e} — retrying in 5s...")
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

# ── VISION THREAD ─────────────────────────────────────────────────────────────
def check_camera_port():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(3)
        s.connect((PRINTER_IP, 322))
        s.close()
        return True
    except (socket.timeout, OSError):
        return False

def vision_thread():
    global latest_frame

    while True:
        if not check_camera_port():
            print("[Camera] Port 322 not available — retrying in 30s...")
            time.sleep(30)
            continue

        print(f"[Camera] Connecting to stream...")
        cap = cv2.VideoCapture(CAMERA_URL)

        if not cap.isOpened():
            print("[Camera] Could not open stream — retrying in 30s...")
            time.sleep(30)
            continue

        print("[Camera] Stream connected")
        camera_available.set()
        os.makedirs(FAILURE_DIR, exist_ok=True)

        spaghetti   = SpaghettiDetector(SPAGHETTI_SENSITIVITY)
        layer_shift = LayerShiftDetector(LAYER_SHIFT_SENSITIVITY)
        warp        = WarpDetector(WARP_SENSITIVITY)

        trigger_counts = {"Spaghetti": 0, "LayerShift": 0, "Warp": 0}

        while True:
            ret, frame = cap.read()
            if not ret:
                print("[Camera] Stream lost — reconnecting...")
                camera_available.clear()
                break

            # Store latest frame for web endpoint
            with frame_lock:
                latest_frame = frame.copy()

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
                    vision_status[name]["triggered"] = triggered
                    vision_status[name]["score"] = float(score)
                    vision_status[name]["count"] = trigger_counts[name]

            if not print_paused.is_set():
                for name, count in trigger_counts.items():
                    if count >= TRIGGER_FRAMES:
                        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
                        path = os.path.join(FAILURE_DIR, f"{name}_{ts}.jpg")
                        cv2.imwrite(path, frame)
                        print(f"[Snapshot] Saved: {path}")
                        pause_print(name)
                        trigger_counts[name] = 0
                        break

            time.sleep(0.1)

        cap.release()
        time.sleep(5)

# ── FLASK APP ─────────────────────────────────────────────────────────────────
app = Flask(__name__)

@app.route("/")
def index():
    return render_template("dashboard.html",
                           printer_ip=PRINTER_IP,
                           serial_number=SERIAL_NUMBER)

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
    resume_monitoring()
    return jsonify({"status": "resumed"})

@app.route("/events")
def events():
    def stream():
        while True:
            with telemetry_lock:
                data = telemetry.copy()
            data["mqtt_connected"] = mqtt_connected.is_set()
            data["camera_available"] = camera_available.is_set()
            data["print_paused"] = print_paused.is_set()
            with vision_lock:
                data["detectors"] = {k: v.copy() for k, v in vision_status.items()}
            yield f"data: {json.dumps(data)}\n\n"
            time.sleep(2)
    return Response(stream(), mimetype="text/event-stream")

# ── MAIN ──────────────────────────────────────────────────────────────────────
def main():
    print("=" * 55)
    print("  BAMBU A1 WEB MONITOR")
    print(f"  Printer: {PRINTER_IP}  |  SN: {SERIAL_NUMBER}")
    print("=" * 55)

    init_csv()

    # Start background threads
    t_telemetry = threading.Thread(target=telemetry_thread, daemon=True)
    t_telemetry.start()
    print("[Telemetry] Thread started")

    t_vision = threading.Thread(target=vision_thread, daemon=True)
    t_vision.start()
    print("[Vision] Thread started")

    # Start Flask
    print("[Web] Dashboard at http://0.0.0.0:5000")
    app.run(host="0.0.0.0", port=5000, threaded=True)

if __name__ == "__main__":
    main()
