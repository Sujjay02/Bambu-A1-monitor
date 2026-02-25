# Bambu A1 Web Monitor

Real-time web dashboard for monitoring Bambu Lab A1 3D printers over your local network. Connects via MQTT for telemetry and the printer's camera port for a live feed with failure detection.

## Features

- **Live telemetry** — print state, layer progress, temperatures, speed, fan, ETA
- **Temperature chart** — real-time nozzle and bed temperature history
- **Camera feed** — live JPEG stream from the printer's built-in camera
- **Failure detection** — spaghetti, layer shift, and warping detection via computer vision
- **Auto-pause** — automatically pauses the print when a failure is detected
- **Manual controls** — pause print and reset triggers from the dashboard

## Prerequisites

- Python 3.10+
- Bambu Lab A1 printer on the same local network
- **LAN Mode Liveview** enabled on the printer:
  **Printer LCD > Settings > Network > LAN Mode Liveview > ON**

## Setup

1. **Clone the repository**

   ```bash
   git clone https://github.com/your-username/Bambu-A1-monitor.git
   cd Bambu-A1-monitor
   ```

2. **Create a virtual environment and install dependencies**

   ```bash
   python3 -m venv venv
   source venv/bin/activate
   pip install -r requirements.txt
   ```

3. **Configure your printer**

   ```bash
   cp .env.example .env
   ```

   Edit `.env` with your printer details:

   ```
   PRINTER_IP=192.168.1.100
   ACCESS_CODE=your_access_code
   SERIAL_NUMBER=your_serial_number
   ```

   **Where to find these values:**
   - **Printer IP** — Printer LCD > Settings > Network, or check your router's DHCP leases
   - **Access Code** — Printer LCD > Settings > Network > Access Code
   - **Serial Number** — Printer LCD > Settings > Device > Serial Number, or the sticker on the back of the printer

4. **Run the app**

   ```bash
   source venv/bin/activate
   export $(cat .env | xargs)
   python app.py
   ```

   Open **http://localhost:5000** in your browser.

## Docker

```bash
cp .env.example .env
# Edit .env with your printer details
docker compose up -d
```

The dashboard will be available at **http://localhost:5000**.

## Standalone Monitor (GUI)

`monitor.py` is a standalone version with an OpenCV GUI window instead of a web dashboard. It shows the camera feed with telemetry overlays directly on your desktop:

```bash
source venv/bin/activate
python monitor.py
```

Press `q` to quit, `r` to reset failure triggers.

## Architecture

- **MQTT** (port 8883) — subscribes to printer telemetry reports over TLS
- **Camera** (port 6000) — connects via the Bambu proprietary JPEG-over-TLS protocol
- **Flask** (port 5000) — serves the web dashboard with Server-Sent Events for live updates

## Troubleshooting

| Problem | Solution |
|---|---|
| MQTT won't connect | Verify `PRINTER_IP` is correct and the printer is on |
| Camera shows "not available" | Enable LAN Mode Liveview on the printer LCD |
| Camera connects then drops | Check that no other client is streaming from the printer |
| Detectors trigger false positives | Adjust sensitivity values in `app.py` (`SPAGHETTI_SENSITIVITY`, etc.) |
