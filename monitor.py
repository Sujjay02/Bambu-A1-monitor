"""
Bambu A1 Combined Monitor
--------------------------
Runs telemetry monitoring (MQTT) and computer vision failure detection
simultaneously using threads.

Features:
  - Real-time telemetry: temps, layer, speed, progress
  - Computer vision: spaghetti, layer shift, warp detection
  - Auto-pause on failure detection
  - CSV logging of all telemetry
  - Failure snapshots saved to disk

Usage:
    python bambu_combined.py

Requirements:
    pip install paho-mqtt opencv-python numpy --break-system-packages
"""

import cv2
import numpy as np
import ssl
import json
import time
import csv
import os
import socket
import struct
import threading
from datetime import datetime
from collections import deque
import paho.mqtt.client as mqtt
from paho.mqtt.enums import CallbackAPIVersion

# ── CONFIG ────────────────────────────────────────────────────────────────────
PRINTER_IP    = "10.8.103.8"
ACCESS_CODE   = "962473bf"
SERIAL_NUMBER = "03919D532701879"

MQTT_PORT      = 8883
MQTT_TOPIC_SUB = f"device/{SERIAL_NUMBER}/report"
MQTT_TOPIC_PUB = f"device/{SERIAL_NUMBER}/request"
CAMERA_PORT    = 6000

LOG_FILE       = "print_telemetry.csv"
SAVE_FAILURES  = True
FAILURE_DIR    = "failure_snapshots"

# Detection sensitivity (0.0 - 1.0)
SPAGHETTI_SENSITIVITY   = 0.6
LAYER_SHIFT_SENSITIVITY = 0.5
WARP_SENSITIVITY        = 0.5
TRIGGER_FRAMES          = 5

# ── SHARED STATE ──────────────────────────────────────────────────────────────
# Telemetry data shared between threads
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

# ── MQTT CLIENT ───────────────────────────────────────────────────────────────
def make_mqtt_client():
    client = mqtt.Client(CallbackAPIVersion.VERSION2)
    client.username_pw_set("bblp", ACCESS_CODE)
    tls_ctx = ssl.create_default_context()
    tls_ctx.check_hostname = False
    tls_ctx.verify_mode = ssl.CERT_NONE
    client.tls_set_context(tls_ctx)
    return client

def pause_print(client, reason: str):
    if print_paused.is_set():
        return
    print(f"\n⚠️  FAILURE DETECTED: {reason}")
    print("🛑 Pausing print...")
    payload = {"print": {"command": "pause", "sequence_id": "1"}}
    client.publish(MQTT_TOPIC_PUB, json.dumps(payload))
    print_paused.set()

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
    client = make_mqtt_client()

    def on_connect(c, userdata, flags, rc, properties=None):
        if rc == 0 or rc.value == 0:
            c.subscribe(MQTT_TOPIC_SUB)
            print("[MQTT] Connected and subscribed")
        else:
            print(f"[MQTT] Connection failed: {rc}")

    def on_message(c, userdata, msg, properties=None):
        try:
            payload = json.loads(msg.payload.decode("utf-8"))
            data = parse_telemetry(payload)
            if data:
                with telemetry_lock:
                    telemetry.update(data)
                log_to_csv(data)
        except Exception as e:
            print(f"[MQTT] Parse error: {e}")

    client.on_connect = on_connect
    client.on_message = on_message
    while True:
        try:
            client.connect(PRINTER_IP, MQTT_PORT, keepalive=60)
            client.loop_forever()
        except (TimeoutError, OSError) as e:
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

# ── CAMERA AUTH ────────────────────────────────────────────────────────────────
JPEG_START = bytes([0xFF, 0xD8, 0xFF, 0xE0])
JPEG_END   = bytes([0xFF, 0xD9])

def create_camera_auth(access_code: str) -> bytes:
    """Build the 80-byte auth payload for Bambu A1 camera (port 6000)."""
    auth = bytearray(80)
    struct.pack_into('<I', auth, 0, 0x40)
    struct.pack_into('<I', auth, 4, 0x3000)
    auth[16:16 + 4] = b'bblp'
    code = access_code.encode('utf-8')
    auth[48:48 + len(code)] = code
    return bytes(auth)

def read_exact(sock, n):
    """Read exactly n bytes from a TLS socket."""
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

# ── OVERLAY ───────────────────────────────────────────────────────────────────
def draw_overlay(frame, vision_results, trigger_counts):
    h, w = frame.shape[:2]
    overlay = frame.copy()

    # Vision detectors — top left
    y = 30
    for name, (triggered, score) in vision_results.items():
        color = (0, 0, 255) if triggered else (0, 200, 0)
        label = f"{'[!] ' if triggered else '[ok] '}{name}: {score:.3f} [{trigger_counts[name]}/{TRIGGER_FRAMES}]"
        cv2.putText(overlay, label, (10, y), cv2.FONT_HERSHEY_SIMPLEX, 0.55, color, 2)
        y += 28

    # Telemetry — bottom panel
    with telemetry_lock:
        t = telemetry.copy()

    panel_h = 130
    cv2.rectangle(overlay, (0, h - panel_h), (w, h), (20, 20, 20), -1)

    lines = [
        f"State: {t['print_state']}   Progress: {t['percent_complete']}%   Layer: {t['layer_current']}/{t['layer_total']}   ETA: {t['time_remaining_min']} min",
        f"Nozzle: {t['nozzle_temp']}C -> {t['nozzle_target']}C   Bed: {t['bed_temp']}C -> {t['bed_target']}C",
        f"Speed Level: {t['print_speed']}   Fan: {t['fan_speed']}",
    ]
    for i, line in enumerate(lines):
        cv2.putText(overlay, line, (10, h - panel_h + 30 + i * 30),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.52, (200, 200, 200), 1)

    # Paused banner
    if print_paused.is_set():
        cv2.rectangle(overlay, (0, h//2 - 40), (w, h//2 + 40), (0, 0, 180), -1)
        cv2.putText(overlay, "PRINT PAUSED - FAILURE DETECTED",
                    (w//2 - 220, h//2 + 10), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (255, 255, 255), 2)

    # Timestamp
    ts = datetime.now().strftime("%H:%M:%S")
    cv2.putText(overlay, ts, (w - 90, 25), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (180, 180, 180), 1)

    return cv2.addWeighted(overlay, 0.9, frame, 0.1, 0)

# ── VISION THREAD ─────────────────────────────────────────────────────────────
def vision_thread():
    mqtt_client = make_mqtt_client()
    mqtt_client.connect(PRINTER_IP, MQTT_PORT, keepalive=60)
    mqtt_client.loop_start()

    tls_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    tls_ctx.check_hostname = False
    tls_ctx.verify_mode = ssl.CERT_NONE
    auth_data = create_camera_auth(ACCESS_CODE)

    print(f"[Camera] Connecting to {PRINTER_IP}:{CAMERA_PORT}...")

    sock = None
    try:
        raw = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        raw.settimeout(10)
        raw.connect((PRINTER_IP, CAMERA_PORT))
        sock = tls_ctx.wrap_socket(raw)
        sock.sendall(auth_data)
    except Exception as e:
        print(f"[ERROR] Could not open camera stream: {e}")
        print("  Make sure: Settings > Network > LAN Mode Liveview > ON")
        mqtt_client.loop_stop()
        mqtt_client.disconnect()
        return

    print("[Camera] Stream connected")
    os.makedirs(FAILURE_DIR, exist_ok=True)

    spaghetti   = SpaghettiDetector(SPAGHETTI_SENSITIVITY)
    layer_shift = LayerShiftDetector(LAYER_SHIFT_SENSITIVITY)
    warp        = WarpDetector(WARP_SENSITIVITY)

    trigger_counts = {"Spaghetti": 0, "LayerShift": 0, "Warp": 0}

    print("[Vision] Running — press 'q' to quit, 'r' to reset triggers\n")

    while True:
        # Read 16-byte frame header
        header = read_exact(sock, 16)
        if header is None:
            print("[Camera] Connection closed")
            break

        payload_size = struct.unpack_from('<I', header, 0)[0]
        if payload_size == 0 or payload_size > 5_000_000:
            continue

        jpeg_data = read_exact(sock, payload_size)
        if jpeg_data is None:
            print("[Camera] Connection lost during frame read")
            break

        if not (jpeg_data[:4] == JPEG_START and jpeg_data[-2:] == JPEG_END):
            continue

        np_arr = np.frombuffer(jpeg_data, dtype=np.uint8)
        frame = cv2.imdecode(np_arr, cv2.IMREAD_COLOR)
        if frame is None:
            continue

        sp_trig,  sp_score  = spaghetti.detect(frame)
        ls_trig,  ls_score  = layer_shift.detect(frame)
        wp_trig,  wp_score  = warp.detect(frame)

        vision_results = {
            "Spaghetti":  (sp_trig, sp_score),
            "LayerShift": (ls_trig, ls_score),
            "Warp":       (wp_trig, wp_score),
        }

        for name, (triggered, _) in vision_results.items():
            trigger_counts[name] = trigger_counts[name] + 1 if triggered else max(0, trigger_counts[name] - 1)

        if not print_paused.is_set():
            for name, count in trigger_counts.items():
                if count >= TRIGGER_FRAMES:
                    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
                    path = os.path.join(FAILURE_DIR, f"{name}_{ts}.jpg")
                    cv2.imwrite(path, frame)
                    print(f"[Snapshot] Saved: {path}")
                    pause_print(mqtt_client, name)
                    trigger_counts[name] = 0
                    break

        display = draw_overlay(frame, vision_results, trigger_counts)
        cv2.imshow("Bambu A1 Monitor", display)

        key = cv2.waitKey(1) & 0xFF
        if key == ord('q'):
            break
        elif key == ord('r'):
            trigger_counts = {k: 0 for k in trigger_counts}
            print_paused.clear()
            print("[INFO] Triggers reset, print resumed monitoring")

    if sock:
        try:
            sock.close()
        except Exception:
            pass
    cv2.destroyAllWindows()
    mqtt_client.loop_stop()
    mqtt_client.disconnect()

# ── MAIN ──────────────────────────────────────────────────────────────────────
def main():
    print("=" * 55)
    print("  🖨️  BAMBU A1 COMBINED MONITOR")
    print(f"  Printer: {PRINTER_IP}  |  SN: {SERIAL_NUMBER}")
    print("=" * 55)

    init_csv()

    # Start telemetry thread
    t_thread = threading.Thread(target=telemetry_thread, daemon=True)
    t_thread.start()
    print("[Telemetry] Thread started")

    # Run vision on main thread (OpenCV needs main thread on Linux)
    vision_thread()

    print("[INFO] Monitor stopped.")

if __name__ == "__main__":
    main()