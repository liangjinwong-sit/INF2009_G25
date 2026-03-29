# GymPulse — Real-Time Gym Equipment Monitoring System

**INF2009: Edge Computing and Analytics — Group 25**
Singapore Institute of Technology

## Overview

GymPulse is a real-time gym equipment monitoring system designed for condominium gyms, built on a 2-Raspberry Pi edge computing architecture. It uses computer vision to detect people, track their movement across equipment zones, and determine whether gym machines are actively in use, all of which processed at the edge with no cloud dependency.

A live web dashboard displays zone occupancy, session timers, and usage history. Telegram alerts notify condo gym users when equipment has been occupied beyond a configurable threshold.

## System Architecture

```
┌──────────────────────┐     MQTT (QoS 0)     ┌──────────────────────┐
│       RPi 1          │ ──────────────────▶   │       RPi 2          │
│  (Vision Pipeline)   │                       │  (Dashboard + Alerts)│
│                      │                       │                      │
│  • USB Camera        │                       │  • Mosquitto Broker  │
│  • YOLO26n (NCNN)    │                       │  • Flask Web Server  │
│  • ByteTrack         │                       │  • SQLite History    │
│  • Zone Ownership    │                       │  • Telegram Bot      │
└──────────────────────┘                       └──────────────────────┘
```

The two Raspberry Pis communicate over a local network. RPi 1 runs the vision pipeline with a USB camera, publishing zone state to RPi 2's MQTT broker at 1 Hz. RPi 2 serves a live dashboard accessible from any device on the same network.

## Key Features

- **Person detection and tracking** — YOLO26n (exported to NCNN for ARM optimisation) detects people in the camera feed. ByteTrack maintains persistent track IDs across frames.
- **Equipment zone ownership** — Two configurable equipment zones with polygon boundaries. The system assigns a zone owner based on proximity to the equipment centre, with a still-duration gate to prevent walk-through false positives.
- **Session continuity (ghost recovery)** — When the tracker briefly loses an ID during occlusion, the system preserves the session state and restores it when the same person reappears nearby, preventing session timer resets.
- **Validity checking** — Determines whether the zone owner is actually using the machine (based on bounding box centre proximity to equipment centre) rather than just standing nearby.
- **Live web dashboard** — Real-time zone status, session timers, occupancy counts, unique visitor counts, and historical usage charts served via Flask.
- **Telegram alerts** — Configurable notifications when equipment has been in use beyond a set duration, with bot commands for on-demand status checks.
- **Performance profiling** — Built-in 1 Hz CSV logging of FPS, per-stage latency (capture, inference, postprocess, display), CPU usage, memory, and context switches for edge performance analysis.

## Repository Contents

| File | Description |
|------|-------------|
| `gym_roi_people_time_v3.py` | RPi 1 vision pipeline — YOLO detection, ByteTrack tracking, zone ownership, MQTT publishing |
| `app.py` | RPi 3 dashboard — Flask web server, MQTT subscriber, SQLite storage, Telegram bot |
| `gym_equipment_zone_records.csv` | Sample zone enter/exit/session records from a test run |
| `gym_profile_1hz.csv` | Sample 1 Hz performance profiling data (FPS, latency, CPU, memory) |

## Quick Start

### RPi 1 — Vision Pipeline

```bash
# Dependencies
pip install ultralytics opencv-python-headless paho-mqtt psutil numpy

# Run (adjust model path and broker IP as needed)
python gym_roi_people_time_v3.py
```

Key configuration (edit at the top of the script):

```python
MODEL_PATH = "/home/keithpi5/Desktop/Project/yolo26n_ncnn_model"
MQTT_BROKER = "172.20.10.3"   # IP of RPi 2
CAMERA_INDEX = 0
```

### RPi 2 — Dashboard + MQTT Broker

```bash
# Install Mosquitto broker
sudo apt install mosquitto mosquitto-clients

# Dependencies
pip install flask paho-mqtt

# Optional: create telegram.env for alerts
echo 'TELEGRAM_ENABLED=true' > telegram.env
echo 'TELEGRAM_BOT_TOKEN=your_token' >> telegram.env
echo 'TELEGRAM_CHAT_ID=your_chat_id' >> telegram.env

# Run
python app.py
```

The dashboard is accessible at `http://<RPi2-IP>:5000`.

## Equipment Zone Configuration

Zones are defined as polygons in pixel coordinates (base resolution 640x480). Edit `BASE_EQUIPMENT_ZONES` in `gym_roi_people_time_v3.py` to match your gym layout:

```python
BASE_EQUIPMENT_ZONES = {
    "equipment_a": {
        "polygon": np.array([(20, 60), (360, 60), (360, 430), (20, 430)]),
        "equipment_center": (200, 245),
    },
    "equipment_b": {
        "polygon": np.array([(380, 60), (620, 60), (620, 430), (380, 430)]),
        "equipment_center": (480, 245),
    },
}
```

## MQTT Topic

All vision state is published to `gympulse/rpi1/vision/state` as a JSON payload containing per-zone occupancy, session timers, owner IDs, validity flags, and performance metrics.

## Tech Stack

- **Detection**: YOLO26n (Ultralytics), exported to NCNN for Raspberry Pi ARM inference
- **Tracking**: ByteTrack (via Ultralytics)
- **Communication**: MQTT (Mosquitto broker, paho-mqtt client)
- **Dashboard**: Flask, SQLite, HTML/JS
- **Alerts**: Telegram Bot API
- **Hardware**: Raspberry Pi 4/5, USB webcam

## Module — INF2009: Edge Computing and Analytics

Singapore Institute of Technology, Trimester 2, 2025/2026
