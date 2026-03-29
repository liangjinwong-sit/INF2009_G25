"""
INF2009 Lab 5-style instrumentation for your Gym ROI YOLO loop.

Updated for two equipment zones (v3: simplified to bbox-proximity-only):
  - Replaces the single ROI with 2 named equipment areas.
  - Tracks per-ID dwell time for each equipment area.
  - Removes the old total ROI occupied timer.
  - Saves per-zone enter/exit/final records to a separate CSV.
  - Machine-use validity is now determined by proximity of person's bbox center
    to equipment center. No pose/keypoint checking needed.

What this adds (based on Lab 5 measurement checklist):
  - Wall-clock vs CPU time (per-loop) using time.perf_counter() / time.process_time()
  - Tail latency distribution (mean, p50, p95, p99) for each pipeline stage
  - Resource sampling (CPU%, RSS, peak RSS, context switches, thread count) via psutil
  - Optional CSV logging (1 Hz) for evidence screenshots/plots

External tools (Lab 5) you can still run on this script (not embedded in Python):
  - /usr/bin/time -v, cProfile, perf stat, perf record, pidstat, taskset, chrt
"""

import argparse
import os
import time
import csv
import json
import resource
from datetime import datetime

import cv2
import numpy as np
import psutil
from ultralytics import YOLO
import paho.mqtt.client as mqtt

from collections import deque, Counter
import statistics
import math


# ----------------------------
# Settings
# ----------------------------
MODEL_PATH = "/home/keithpi5/Desktop/Project/yolo26n_ncnn_model"
CAMERA_INDEX = 0             # USB cam usually 0

# Camera capture resolution (more pixels, but NOT wider physical field of view)
CAMERA_WIDTH = 1280
CAMERA_HEIGHT = 720

# Zone coordinates were likely drawn on this original frame size.
# Change these if you created your polygons on a different resolution.
ZONE_BASE_WIDTH = 640
ZONE_BASE_HEIGHT = 480

IMG_SIZE = 640               # YOLO inference size; bigger = slower, not wider FOV
CONF = 0.45

# Average FPS window shown on the overlay
FPS_AVG_WINDOW_SEC = 5.0

# Keep a person "in ROI" for this long after last seen in ROI (reduces flicker)
LEAVE_GRACE_SEC = 5.0
# Remove stale track IDs after not being seen for this long
STALE_TRACK_SEC = 20.0

# Terminal printing
PRINT_EVERY_SEC = 1.0        # print summary every N seconds
PRINT_TOP_N = 5              # top N IDs to show in terminal
LOG_ENTER_EXIT = True        # set False if you don't want enter/exit logs

# ------------------------------------------------------------------
# TWO EQUIPMENT ZONES
# Edit these points to match your gym equipment areas.
# ------------------------------------------------------------------
BASE_EQUIPMENT_ZONES = {
    "equipment_a": {
        "label": "Zone A",
        "polygon": np.array([(20, 60), (360, 60), (360, 430), (20, 430)], dtype=np.int32),
        "equipment_center": (200, 245),
        "color": (255, 200, 0),
    },
    "equipment_b": {
        "label": "Zone B",
        "polygon": np.array([(380, 60), (620, 60), (620, 430), (380, 430)], dtype=np.int32),
        "equipment_center": (480, 245),
        "color": (0, 200, 255),
    },
}


def clone_zones(zones: dict) -> dict:
    return {
        zone_name: {
            "label": zone_cfg["label"],
            "polygon": zone_cfg["polygon"].copy(),
            "equipment_center": tuple(zone_cfg["equipment_center"]),
            "color": zone_cfg["color"],
        }
        for zone_name, zone_cfg in zones.items()
    }


def scale_zones(zones: dict, src_w: int, src_h: int, dst_w: int, dst_h: int) -> dict:
    sx = float(dst_w) / float(src_w)
    sy = float(dst_h) / float(src_h)

    scaled = {}
    for zone_name, zone_cfg in zones.items():
        poly = zone_cfg["polygon"].astype(np.float32).copy()
        poly[:, 0] *= sx
        poly[:, 1] *= sy

        eq_cx, eq_cy = zone_cfg["equipment_center"]
        scaled_center = (
            int(round(float(eq_cx) * sx)),
            int(round(float(eq_cy) * sy)),
        )

        scaled[zone_name] = {
            "label": zone_cfg["label"],
            "polygon": np.round(poly).astype(np.int32),
            "equipment_center": scaled_center,
            "color": zone_cfg["color"],
        }
    return scaled


EQUIPMENT_ZONES = clone_zones(BASE_EQUIPMENT_ZONES)

SHOW_ROI = True   # press 'r' to toggle equipment zone boxes

# Display control (useful if later you run headless / stream via Flask)
HEADLESS = False
WINDOW_NAME = "Gym Equipment Zone Tracking"

# Raw camera recording
# Toggle this to True if you want to save the raw camera stream while the script runs.
SAVE_RAW_VIDEO = False
RAW_VIDEO_DIR = "."
RAW_VIDEO_PREFIX = "gym_raw_stream"
RAW_VIDEO_FOURCC = "MJPG"
RAW_VIDEO_EXT = ".mp4"
RAW_VIDEO_FPS = 15.0

# Rolling-window latency buffers
WIN = 120            # keep last ~120 frames worth of timings
WARMUP_FRAMES = 20   # skip first frames (model warmup/caching effects)

t_cap_ms  = deque(maxlen=WIN)   # camera read time
t_inf_ms  = deque(maxlen=WIN)   # YOLO model.track inference time
t_post_ms = deque(maxlen=WIN)   # your ROI + tracking logic + drawing overlays
t_disp_ms = deque(maxlen=WIN)   # imshow + waitKey
t_loop_ms = deque(maxlen=WIN)   # total loop time (wall)
t_cpu_ms  = deque(maxlen=WIN)   # total loop time (CPU)

# Resource samples (1 Hz)
cpu_pct_samples = []
rss_mb_samples = []

# Optional CSV logging (1 Hz)
LOG_CSV = True
CSV_PATH = "gym_profile_1hz.csv"
ZONE_CSV_PATH = "gym_equipment_zone_records.csv"
OUT_OF_ZONE_LABEL = "out_of_zone"
CSV_CLOSE_EXIT = "EXIT"
CSV_CLOSE_CLOSED = "CLOSED"

# MQTT publish (RPi1 -> RPi2)
MQTT_ENABLE = True
MQTT_BROKERS = ["192.168.1.2", "172.20.10.3"]
MQTT_PORT = 1883
MQTT_TOPIC = "gympulse/rpi1/vision/state"
mqtt_broker_index = 0

# ----------------------------
# per-person confidence smoothing
# ----------------------------
CONF_EMA_BETA = 0.8   # 0.8 old + 0.2 new

# ----------------------------
# motion-first gating
# ----------------------------
MOTION_HIST_LEN = 6           # increased from 4 — smooths out brief motion spikes
MOTION_EMA_BETA = 0.55
MOTION_SPEED_THR = 0.25      # raised from 0.18 — small fidgets/exercise motion won't trigger "moving"
MOTION_STILL_GATE = 0.60
MOTION_MOVING_GATE = 0.52

# Speed tweak: ByteTrack is usually faster than BoT-SORT
TRACKER_CFG = "bytetrack.yaml"

# ----------------------------
# accuracy knobs
# ----------------------------
STATE_VOTE_LEN = 7            # per-ID vote smoothing window
OWNER_STILL_GRACE_SEC = 2.0   # keep current equipment owner briefly if still-state flickers
USAGE_GRACE_SEC = 2.0         # keep current equipment owner briefly if usage gate flickers
OWNER_STILL_MIN_SEC = 3.0    # person must be continuously "still" for this long before becoming zone owner
                               # (prevents walk-throughs from triggering sessions)
MACHINE_USAGE_STABLE_CHECKS = 2

# Practical machine-use gate tuning knobs
MACHINE_BODY_CENTER_MAX_ZONE_FRAC = 0.55

# Ghost owner mechanism (ID churn mitigation)
GHOST_OWNER_MAX_SEC = 10.0        # ghost owner record expires after this
GHOST_OWNER_CENTER_DIST_FRAC = 0.30  # ghost match: candidate centre must be within this fraction of zone diagonal
USAGE_INVALID_RELEASE_SEC = 5.0   # release owner only after this many seconds of continuous invalidity

mqtt_client = None

def mqtt_connect_to_available_broker():
    global mqtt_client, mqtt_broker_index

    last_error = None
    brokers = MQTT_BROKERS if MQTT_BROKERS else ["127.0.0.1"]

    for offset in range(len(brokers)):
        broker_index = (mqtt_broker_index + offset) % len(brokers)
        broker = brokers[broker_index]
        client = mqtt.Client(client_id="gympulse-rpi1-vision")

        try:
            client.connect(broker, MQTT_PORT, 60)
            client.loop_start()
            mqtt_client = client
            mqtt_broker_index = broker_index
            print(f"[MQTT] connected to {broker}:{MQTT_PORT}", flush=True)
            return True
        except Exception as e:
            last_error = e
            try:
                client.loop_stop()
            except Exception:
                pass
            print(f"[MQTT] connect failed for {broker}:{MQTT_PORT} -> {e}", flush=True)

    mqtt_client = None
    if last_error is not None:
        print(f"[MQTT] unable to connect to any broker: {last_error}", flush=True)
    return False


def mqtt_setup():
    if not MQTT_ENABLE:
        return
    mqtt_connect_to_available_broker()


def mqtt_publish(payload: dict):
    global mqtt_client, mqtt_broker_index

    if not MQTT_ENABLE:
        return

    if mqtt_client is None and not mqtt_connect_to_available_broker():
        return

    try:
        info = mqtt_client.publish(MQTT_TOPIC, json.dumps(payload), qos=0, retain=True)
        if info.rc != mqtt.MQTT_ERR_SUCCESS:
            raise RuntimeError(f"MQTT publish returned rc={info.rc}")
    except Exception as e:
        print("[MQTT] publish failed:", e, flush=True)

        if mqtt_client is not None:
            try:
                mqtt_client.loop_stop()
            except Exception:
                pass
            mqtt_client = None

        mqtt_broker_index = (mqtt_broker_index + 1) % max(1, len(MQTT_BROKERS))

        if mqtt_connect_to_available_broker() and mqtt_client is not None:
            try:
                info = mqtt_client.publish(MQTT_TOPIC, json.dumps(payload), qos=0, retain=True)
                if info.rc != mqtt.MQTT_ERR_SUCCESS:
                    raise RuntimeError(f"MQTT publish returned rc={info.rc}")
            except Exception as retry_e:
                print("[MQTT] publish retry failed:", retry_e, flush=True)


def _pctl(values, q: float) -> float:
    """Simple percentile from list/deque of floats."""
    if not values:
        return 0.0
    xs = sorted(values)
    idx = int((q / 100.0) * (len(xs) - 1))
    return float(xs[idx])


def _stage_stats_ms(values: deque) -> str:
    """Format mean/p50/p95/p99 for a stage."""
    if not values:
        return "avg/p50/p95/p99=0/0/0/0"
    avg = statistics.mean(values)
    return f"avg/p50/p95/p99={avg:.1f}/{_pctl(values,50):.1f}/{_pctl(values,95):.1f}/{_pctl(values,99):.1f}"


def fmt_hhmmss(seconds: float) -> str:
    s = int(seconds)
    h = s // 3600
    m = (s % 3600) // 60
    s = s % 60
    return f"{h:02d}:{m:02d}:{s:02d}"


def draw_text_with_outline(frame, text: str, org, color, font_scale: float = 0.5,
                           thickness: int = 2, outline_color=(0, 0, 0),
                           outline_thickness: int = None):
    if outline_thickness is None:
        outline_thickness = max(1, int(thickness + 1))
    x, y = int(org[0]), int(org[1])
    cv2.putText(frame, str(text), (x, y), cv2.FONT_HERSHEY_SIMPLEX,
                float(font_scale), outline_color, int(outline_thickness), cv2.LINE_AA)
    cv2.putText(frame, str(text), (x, y), cv2.FONT_HERSHEY_SIMPLEX,
                float(font_scale), color, int(thickness), cv2.LINE_AA)


def draw_alpha_box(frame, x1: int, y1: int, x2: int, y2: int,
                   color=(12, 12, 12), alpha: float = 0.40,
                   border_color=None, border_thickness: int = 1):
    frame_h, frame_w = frame.shape[:2]
    x1 = max(0, min(int(x1), frame_w - 1))
    y1 = max(0, min(int(y1), frame_h - 1))
    x2 = max(0, min(int(x2), frame_w))
    y2 = max(0, min(int(y2), frame_h))
    if x2 <= x1 or y2 <= y1:
        return

    roi = frame[y1:y2, x1:x2]
    if roi.size == 0:
        return

    overlay = roi.copy()
    cv2.rectangle(overlay, (0, 0), (x2 - x1 - 1, y2 - y1 - 1), color, -1)
    cv2.addWeighted(overlay, float(alpha), roi, 1.0 - float(alpha), 0.0, roi)

    if border_color is not None and int(border_thickness) > 0:
        cv2.rectangle(frame, (x1, y1), (x2 - 1, y2 - 1), border_color, int(border_thickness), cv2.LINE_AA)


def draw_segment_bar(frame, segments, x: int, y: int,
                     font_scale: float = 0.50, thickness: int = 2,
                     gap: int = 16, padding_x: int = 12, padding_y: int = 8,
                     bg_color=(12, 12, 12), bg_alpha: float = 0.42,
                     border_color=(70, 70, 70)):
    valid_segments = [(str(txt), col) for txt, col in segments if str(txt).strip()]
    if not valid_segments:
        return

    sizes = [cv2.getTextSize(txt, cv2.FONT_HERSHEY_SIMPLEX, float(font_scale), int(thickness))[0]
             for txt, _ in valid_segments]
    total_w = sum(w for w, _ in sizes) + (max(0, len(valid_segments) - 1) * int(gap))
    text_h = max((h for _, h in sizes), default=0)

    box_x1 = int(x)
    box_y1 = int(y)
    box_x2 = box_x1 + int(padding_x * 2) + int(total_w)
    box_y2 = box_y1 + int(padding_y * 2) + int(text_h) + 4
    draw_alpha_box(frame, box_x1, box_y1, box_x2, box_y2,
                   color=bg_color, alpha=bg_alpha,
                   border_color=border_color, border_thickness=1)

    cursor_x = box_x1 + int(padding_x)
    baseline_y = box_y1 + int(padding_y) + int(text_h)
    for (txt, col), (w, _) in zip(valid_segments, sizes):
        draw_text_with_outline(
            frame,
            txt,
            (cursor_x, baseline_y),
            col,
            font_scale=font_scale,
            thickness=thickness,
            outline_color=(0, 0, 0),
            outline_thickness=max(1, thickness + 1),
        )
        cursor_x += int(w) + int(gap)


def draw_text_panel(frame, lines, x: int, y: int, color,
                    font_scale: float = 0.48, thickness: int = 2,
                    line_gap: int = 20, padding: int = 10,
                    bg_color=(12, 12, 12), bg_alpha: float = 0.40,
                    border_color=None):
    clean_lines = [str(line) for line in lines if str(line).strip()]
    if not clean_lines:
        return None

    sizes = [cv2.getTextSize(line, cv2.FONT_HERSHEY_SIMPLEX, float(font_scale), int(thickness))[0]
             for line in clean_lines]
    max_w = max((w for w, _ in sizes), default=0)
    text_h = max((h for _, h in sizes), default=0)
    panel_w = int(max_w) + (int(padding) * 2)
    panel_h = int(text_h) + (max(0, len(clean_lines) - 1) * int(line_gap)) + (int(padding) * 2)

    x = max(0, min(int(x), frame.shape[1] - max(1, panel_w)))
    y = max(0, min(int(y), frame.shape[0] - max(1, panel_h)))

    draw_alpha_box(
        frame,
        x,
        y,
        x + panel_w,
        y + panel_h,
        color=bg_color,
        alpha=bg_alpha,
        border_color=border_color,
        border_thickness=1 if border_color is not None else 0,
    )

    baseline_y = y + int(padding) + int(text_h)
    for i, line in enumerate(clean_lines):
        draw_text_with_outline(
            frame,
            line,
            (x + int(padding), baseline_y + (i * int(line_gap))),
            color,
            font_scale=font_scale,
            thickness=thickness,
        )
    return (x, y, x + panel_w, y + panel_h)


def _stacked_text_origin(frame_shape, x_anchor: int, y1: int, y2: int,
                         line_count: int, line_gap: int = 20, margin: int = 8):
    frame_h, frame_w = frame_shape[:2]
    x = max(6, min(int(x_anchor), frame_w - 6))
    block_h = max(0, (max(0, line_count - 1) * int(line_gap)))

    above_start_y = int(y1) - int(margin) - block_h
    if above_start_y >= 18:
        return x, above_start_y

    below_start_y = int(y2) + 20
    max_start_y = max(18, frame_h - 10 - block_h)
    return x, min(below_start_y, max_start_y)


def draw_stacked_text(frame, lines, x_anchor: int, y1: int, y2: int, color,
                      font_scale: float = 0.48, thickness: int = 2,
                      line_gap: int = 20, bg_color=None,
                      bg_alpha: float = 0.0, padding: int = 6,
                      border_color=None):
    clean_lines = [str(line) for line in lines if str(line).strip()]
    if not clean_lines:
        return

    x, start_y = _stacked_text_origin(frame.shape, x_anchor, y1, y2, len(clean_lines), line_gap=line_gap)
    text_sizes = [cv2.getTextSize(line, cv2.FONT_HERSHEY_SIMPLEX, float(font_scale), int(thickness))[0]
                  for line in clean_lines]
    max_w = max((w for w, _ in text_sizes), default=0)
    text_h = max((h for _, h in text_sizes), default=0)

    if bg_color is not None and float(bg_alpha) > 0.0:
        box_x1 = x - int(padding)
        box_y1 = start_y - int(text_h) - int(padding)
        box_x2 = x + int(max_w) + int(padding)
        box_y2 = start_y + (max(0, len(clean_lines) - 1) * int(line_gap)) + int(padding)
        draw_alpha_box(
            frame,
            box_x1,
            box_y1,
            box_x2,
            box_y2,
            color=bg_color,
            alpha=bg_alpha,
            border_color=border_color,
            border_thickness=1 if border_color is not None else 0,
        )

    for i, line in enumerate(clean_lines):
        draw_text_with_outline(
            frame,
            line,
            (x, start_y + (i * int(line_gap))),
            color,
            font_scale=font_scale,
            thickness=thickness,
        )


def point_in_polygon(polygon, cx, cy) -> bool:
    return cv2.pointPolygonTest(polygon, (int(cx), int(cy)), False) >= 0


def get_zone_name(cx, cy):
    for zone_name, zone_cfg in EQUIPMENT_ZONES.items():
        if point_in_polygon(zone_cfg["polygon"], cx, cy):
            return zone_name
    return None


def make_zone_state():
    return {
        "inside": False,
        "enter_t": None,
        "last_seen_in_zone": 0.0,
        "total_dwell": 0.0,
        "counting": False,
        "count_start_t": None,
    }


def _state_counts_for_dwell(state: str) -> bool:
    """
    Count dwell when the person is present in the zone.
    Previously only counted for 'still', but exercises like push-ups
    involve continuous motion while the person is genuinely using the zone.
    """
    return state in ("still", "moving")


def _pause_zone_counting(z: dict, stop_t: float):
    if z["counting"] and z["count_start_t"] is not None:
        z["total_dwell"] += max(0.0, float(stop_t) - float(z["count_start_t"]))
    z["counting"] = False
    z["count_start_t"] = None


def update_zone_counting(track: dict, zone_name: str, now: float, state: str):
    z = track["zones"][zone_name]
    should_count = _state_counts_for_dwell(state)

    if should_count:
        if not z["counting"]:
            z["counting"] = True
            z["count_start_t"] = now
    else:
        _pause_zone_counting(z, now)


def live_zone_dwell(track: dict, zone_name: str, now: float) -> float:
    z = track["zones"][zone_name]
    if z["inside"] and z["counting"] and z["count_start_t"] is not None:
        return z["total_dwell"] + max(0.0, now - z["count_start_t"])
    return z["total_dwell"]


def live_total_dwell(track: dict, now: float) -> float:
    return sum(live_zone_dwell(track, zone_name, now) for zone_name in EQUIPMENT_ZONES)


def current_ids_in_zone(tracks: dict, zone_name: str):
    return [tid for tid, t in tracks.items() if t["zones"][zone_name]["inside"]]


def current_still_ids_in_zone(tracks: dict, zone_name: str):
    return [
        tid for tid, t in tracks.items()
        if t["zones"][zone_name]["inside"] and t.get("state_final") == "still"
    ]


def make_zone_owner_state():
    return {
        "current_owner_id": None,
        "owner_since_t": None,
        "owner_not_still_since_t": None,
        "is_valid_machine_usage": False,
        "multi_person_detected": False,
        "current_session_start_t": None,
        "current_session_accumulated_sec": 0.0,
        "current_session_running": False,
        "current_session_last_resume_t": None,
        "last_completed_session_duration_sec": 0.0,
        "completed_sessions_count": 0,
        "usage_invalid_since_t": None,
        "owner_last_bbox": None,
        "ghost_bbox": None,
        "ghost_released_t": None,
        "ghost_session_accumulated_sec": 0.0,
        "ghost_session_start_t": None,
        "ghost_owner_tid": None,          # original track ID of the ghost
        "_ghost_dedup_tid": None,         # set by ghost restore; main loop consumes to fix unique counts
    }


def current_zone_owner_session_elapsed(owner_state: dict, now: float) -> float:
    if owner_state.get("current_session_start_t") is None:
        return 0.0
    total = float(owner_state.get("current_session_accumulated_sec", 0.0))
    if owner_state.get("current_session_running") and owner_state.get("current_session_last_resume_t") is not None:
        total += max(0.0, float(now) - float(owner_state["current_session_last_resume_t"]))
    return total


def _track_is_stale(track: dict, now: float) -> bool:
    return (float(now) - float(track.get("last_seen", 0.0))) > STALE_TRACK_SEC


def _track_box_center(track: dict):
    center = track.get("bbox_center")
    if center is not None:
        return center

    bbox = track.get("current_bbox")
    if bbox is None:
        return None

    x1, y1, x2, y2 = bbox
    return (float(x1 + x2) * 0.5, float(y1 + y2) * 0.5)


def _track_bbox_wh(track: dict):
    bbox = track.get("current_bbox")
    if bbox is None:
        return (0.0, 0.0)
    x1, y1, x2, y2 = bbox
    return (max(0.0, float(x2 - x1)), max(0.0, float(y2 - y1)))


def _squared_distance(a, b) -> float:
    return ((float(a[0]) - float(b[0])) ** 2) + ((float(a[1]) - float(b[1])) ** 2)


def _distance(a, b) -> float:
    return math.sqrt(_squared_distance(a, b))


def _bbox_iou(boxA, boxB) -> float:
    """Compute IoU between two (x1,y1,x2,y2) bboxes."""
    xA = max(boxA[0], boxB[0])
    yA = max(boxA[1], boxB[1])
    xB = min(boxA[2], boxB[2])
    yB = min(boxA[3], boxB[3])
    inter = max(0, xB - xA) * max(0, yB - yA)
    if inter == 0:
        return 0.0
    areaA = max(1, (boxA[2] - boxA[0]) * (boxA[3] - boxA[1]))
    areaB = max(1, (boxB[2] - boxB[0]) * (boxB[3] - boxB[1]))
    return float(inter) / float(areaA + areaB - inter)


def equipment_center_distance(track: dict, zone_name: str) -> float:
    center = _track_box_center(track)
    if center is None:
        return float("inf")
    return _distance(center, EQUIPMENT_ZONES[zone_name]["equipment_center"])


def _zone_bounds(zone_name: str):
    x, y, w, h = cv2.boundingRect(EQUIPMENT_ZONES[zone_name]["polygon"])
    return int(x), int(y), int(w), int(h)


def _zone_usage_state(track: dict, zone_name: str):
    return track["zone_usage"][zone_name]


def _make_track_zone_usage_state():
    return {
        "last_update_f": -999999,
        "last_update_t": 0.0,
        "body_center_dist_norm": 999.0,
        "raw_valid": False,
        "stable_valid": False,
        "debug_reason": "no_proximity",
        "valid_hist": deque(maxlen=MACHINE_USAGE_STABLE_CHECKS),
    }


def _update_zone_usage_stability(zone_usage: dict, raw_valid: bool):
    zone_usage["raw_valid"] = bool(raw_valid)
    zone_usage["valid_hist"].append(bool(raw_valid))

    prev_stable = bool(zone_usage.get("stable_valid", False))
    hist = list(zone_usage["valid_hist"])

    if not prev_stable:
        stable = len(hist) >= MACHINE_USAGE_STABLE_CHECKS and all(hist[-MACHINE_USAGE_STABLE_CHECKS:])
    else:
        stable = sum(1 for v in hist if v) >= max(1, MACHINE_USAGE_STABLE_CHECKS - 1)

    zone_usage["stable_valid"] = bool(stable)
    return zone_usage["stable_valid"]


def _set_zone_usage_invalid(track: dict, zone_name: str, frame_i: int, now: float, reason: str):
    zone_usage = _zone_usage_state(track, zone_name)
    zone_usage["last_update_f"] = frame_i
    zone_usage["last_update_t"] = float(now)
    zone_usage["body_center_dist_norm"] = 999.0
    zone_usage["debug_reason"] = reason
    _update_zone_usage_stability(zone_usage, False)
    return zone_usage


def classify_valid_machine_usage(track: dict, zone_name: str, frame_i: int, now: float):
    """
    Simplified proximity-based validity check (v3).

    For a condo-gym crowd detector the only question is:
        'Is someone physically at this equipment station?'

    Validity = person's body centre is close to the equipment centre.
    No pose/keypoint checking needed.
    """
    zone_usage = _zone_usage_state(track, zone_name)
    bbox_center = _track_box_center(track)
    x, y, w, h = _zone_bounds(zone_name)

    zone_usage["last_update_f"] = int(frame_i)
    zone_usage["last_update_t"] = float(now)

    # ---- body-centre proximity (the only gate) ----
    eq_center = EQUIPMENT_ZONES[zone_name]["equipment_center"]
    body_dist_norm = 999.0
    if bbox_center is not None:
        body_dist_norm = _distance(bbox_center, eq_center) / max(1.0, float(min(w, h)))
    body_close = body_dist_norm <= MACHINE_BODY_CENTER_MAX_ZONE_FRAC

    # ---- simple validity: person is near the equipment ----
    raw_valid = bool(body_close)

    zone_usage["body_center_dist_norm"] = float(body_dist_norm)
    zone_usage["debug_reason"] = "ok" if raw_valid else "body_far"

    stable_valid = _update_zone_usage_stability(zone_usage, raw_valid)
    return {
        "raw_valid": raw_valid,
        "stable_valid": stable_valid,
        "body_center_dist_norm": float(body_dist_norm),
    }


def is_owner_eligible_track(track: dict, zone_name: str, now: float) -> bool:
    if track is None:
        return False
    if _track_is_stale(track, now):
        return False
    if not track["zones"][zone_name]["inside"]:
        return False
    # Accept both "still" and "moving" so that exercises like push-ups
    # (which cause brief bbox movement) don't drop the owner mid-exercise.
    return track.get("state_final") in ("still", "moving")


def select_zone_owner_candidate(tracks: dict, zone_name: str, now: float):
    equipment_center = EQUIPMENT_ZONES[zone_name]["equipment_center"]
    candidates = []

    for tid, track in tracks.items():
        if not is_owner_eligible_track(track, zone_name, now):
            continue
        center = _track_box_center(track)
        if center is None:
            continue
        # Prefer "still" people over "moving" ones (sort key 0 vs 1)
        # so that a walker doesn't steal ownership from someone exercising.
        motion_penalty = 0 if track.get("state_final") == "still" else 1
        candidates.append((motion_penalty, _squared_distance(center, equipment_center), int(tid), int(tid)))

    candidates.sort(key=lambda item: (item[0], item[1], item[2]))
    candidate_ids = [tid for *_, tid in candidates]
    owner_id = candidate_ids[0] if candidate_ids else None
    multi_person_detected = len(candidate_ids) > 1
    return owner_id, candidate_ids, multi_person_detected


def _pause_zone_owner_session(owner_state: dict, now: float):
    if owner_state.get("current_session_running") and owner_state.get("current_session_last_resume_t") is not None:
        owner_state["current_session_accumulated_sec"] += max(
            0.0, float(now) - float(owner_state["current_session_last_resume_t"])
        )
    owner_state["current_session_running"] = False
    owner_state["current_session_last_resume_t"] = None


def _start_or_resume_zone_owner_session(owner_state: dict, now: float):
    if owner_state.get("current_session_start_t") is None:
        owner_state["current_session_start_t"] = float(now)
        owner_state["current_session_accumulated_sec"] = 0.0
    if not owner_state.get("current_session_running"):
        owner_state["current_session_running"] = True
        owner_state["current_session_last_resume_t"] = float(now)


def _finalize_zone_owner_session(owner_state: dict, now: float, completed_session_times: list = None):
    completed_duration = current_zone_owner_session_elapsed(owner_state, now)
    if owner_state.get("current_session_start_t") is not None and completed_duration > 0.0:
        owner_state["last_completed_session_duration_sec"] = float(completed_duration)
        owner_state["completed_sessions_count"] = int(owner_state.get("completed_sessions_count", 0)) + 1
        if completed_session_times is not None:
            completed_session_times.append(float(completed_duration))

    owner_state["current_session_start_t"] = None
    owner_state["current_session_accumulated_sec"] = 0.0
    owner_state["current_session_running"] = False
    owner_state["current_session_last_resume_t"] = None
    return completed_duration


def _clear_ghost(owner_state: dict):
    """Clear ghost owner state."""
    owner_state["ghost_bbox"] = None
    owner_state["ghost_released_t"] = None
    owner_state["ghost_session_accumulated_sec"] = 0.0
    owner_state["ghost_session_start_t"] = None
    owner_state["ghost_owner_tid"] = None


def _bbox_center(bbox):
    """Return (cx, cy) from an (x1, y1, x2, y2) bbox."""
    if bbox is None:
        return None
    x1, y1, x2, y2 = bbox
    return (float(x1 + x2) * 0.5, float(y1 + y2) * 0.5)


def _try_restore_ghost_session(owner_state: dict, candidate_track: dict, zone_name: str, now: float) -> bool:
    """If candidate matches a recent ghost owner (by centre proximity), restore the ghost's session."""
    ghost_bbox = owner_state.get("ghost_bbox")
    ghost_released_t = owner_state.get("ghost_released_t")

    if ghost_bbox is None or ghost_released_t is None:
        return False

    # Ghost expired?
    if (now - ghost_released_t) > GHOST_OWNER_MAX_SEC:
        _clear_ghost(owner_state)
        return False

    # Centre-distance match (much more resilient than IoU after occlusion)
    ghost_center = _bbox_center(ghost_bbox)
    candidate_bbox = candidate_track.get("current_bbox")
    candidate_center = _bbox_center(candidate_bbox)

    if ghost_center is None or candidate_center is None:
        return False

    # Normalise distance by the zone diagonal so the threshold is resolution-independent
    zx, zy, zw, zh = cv2.boundingRect(EQUIPMENT_ZONES[zone_name]["polygon"])
    zone_diag = math.sqrt(float(zw * zw + zh * zh))
    if zone_diag < 1.0:
        zone_diag = 1.0

    dist = _distance(ghost_center, candidate_center)
    dist_frac = dist / zone_diag

    if dist_frac > GHOST_OWNER_CENTER_DIST_FRAC:
        return False

    # Match! Restore the ghost's session
    ghost_tid = owner_state.get("ghost_owner_tid")
    new_tid = owner_state.get("current_owner_id")  # the new track ID just assigned

    owner_state["current_session_start_t"] = owner_state.get("ghost_session_start_t")
    owner_state["current_session_accumulated_sec"] = float(owner_state.get("ghost_session_accumulated_sec", 0.0))
    owner_state["current_session_running"] = True
    owner_state["current_session_last_resume_t"] = float(now)

    # Signal to main loop: new_tid is a duplicate of ghost_tid in zone_unique_ids
    if ghost_tid is not None and new_tid is not None and ghost_tid != new_tid:
        owner_state["_ghost_dedup_tid"] = int(new_tid)

    _clear_ghost(owner_state)
    return True


def _release_zone_owner(owner_state: dict, now: float, completed_session_times: list = None, track: dict = None, reason: str = "unknown"):
    # Save ghost state before finalizing session.
    # Ghost is saved on ALL release reasons (not just "stale") so that
    # ByteTrack ID swaps during brief occlusion don't lose the session.
    # The ghost has built-in safety: 10s expiry + centre-distance matching.
    # Compute accumulated time before finalize resets it
    accumulated = current_zone_owner_session_elapsed(owner_state, now)

    # Get bbox from track or from owner_last_bbox
    ghost_bbox = None
    if track is not None:
        ghost_bbox = track.get("current_bbox")
    if ghost_bbox is None:
        ghost_bbox = owner_state.get("owner_last_bbox")

    if ghost_bbox is not None and accumulated > 0.0:
        owner_state["ghost_bbox"] = ghost_bbox
        owner_state["ghost_released_t"] = float(now)
        owner_state["ghost_session_accumulated_sec"] = float(accumulated)
        owner_state["ghost_session_start_t"] = owner_state.get("current_session_start_t")
        owner_state["ghost_owner_tid"] = owner_state.get("current_owner_id")

    _pause_zone_owner_session(owner_state, now)
    _finalize_zone_owner_session(owner_state, now, completed_session_times)
    owner_state["current_owner_id"] = None
    owner_state["owner_since_t"] = None
    owner_state["owner_not_still_since_t"] = None
    owner_state["usage_invalid_since_t"] = None
    owner_state["is_valid_machine_usage"] = False


def update_zone_owner_state(owner_state: dict, tracks: dict, zone_name: str, now: float, completed_session_times: list = None):
    next_candidate_id, eligible_ids, multi_person_detected = select_zone_owner_candidate(tracks, zone_name, now)
    owner_state["multi_person_detected"] = bool(multi_person_detected)

    owner_id = owner_state.get("current_owner_id")
    owner_track = tracks.get(owner_id) if owner_id is not None else None

    if owner_id is not None and owner_track is not None:
        # Owner track still alive
        if not owner_track["zones"][zone_name]["inside"]:
            # Left the zone — release immediately, no ghost
            _release_zone_owner(owner_state, now, completed_session_times, track=owner_track, reason="left_zone")
        elif _track_is_stale(owner_track, now):
            # Track stale — release with ghost
            _release_zone_owner(owner_state, now, completed_session_times, track=owner_track, reason="stale")
        else:
            # Owner is inside zone and track is alive
            # Save last known bbox for potential ghost use
            owner_state["owner_last_bbox"] = owner_track.get("current_bbox")

            usage_result = classify_valid_machine_usage(owner_track, zone_name, 0, now)
            usage_valid = usage_result.get("stable_valid", False)
            owner_state["is_valid_machine_usage"] = bool(usage_valid)

            if usage_valid:
                owner_state["usage_invalid_since_t"] = None  # reset invalid timer
                _start_or_resume_zone_owner_session(owner_state, now)
                return owner_state["current_owner_id"]
            else:
                # Invalid but still in zone — use grace period
                _pause_zone_owner_session(owner_state, now)
                if owner_state.get("usage_invalid_since_t") is None:
                    owner_state["usage_invalid_since_t"] = float(now)

                invalid_duration = float(now) - float(owner_state["usage_invalid_since_t"])
                if invalid_duration > USAGE_INVALID_RELEASE_SEC:
                    _release_zone_owner(owner_state, now, completed_session_times, track=owner_track, reason="invalid_timeout")
                # else: keep owner, just paused — they might come back into range

    elif owner_id is not None and owner_track is None:
        # Owner track completely gone from tracks dict (already deleted)
        # Release with ghost using last known state
        _release_zone_owner(owner_state, now, completed_session_times, track=None, reason="stale")

    # Assign new owner if no current owner
    if owner_state.get("current_owner_id") is None and next_candidate_id is not None:
        new_owner_track = tracks.get(next_candidate_id)

        # Still-duration gate
        still_long_enough = False
        if new_owner_track is not None:
            still_since = new_owner_track.get("still_since_t")
            if still_since is not None and (now - still_since) >= OWNER_STILL_MIN_SEC:
                still_long_enough = True

        if still_long_enough:
            owner_state["current_owner_id"] = int(next_candidate_id)
            owner_state["owner_since_t"] = float(now)
            owner_state["owner_not_still_since_t"] = None
            owner_state["usage_invalid_since_t"] = None
            owner_state["is_valid_machine_usage"] = False

            # Try to restore session from ghost (same person, new track ID)
            ghost_restored = False
            if new_owner_track is not None:
                ghost_restored = _try_restore_ghost_session(owner_state, new_owner_track, zone_name, now)

            if not ghost_restored and new_owner_track is not None:
                # Fresh owner — check proximity
                usage_result = classify_valid_machine_usage(new_owner_track, zone_name, 0, now)
                if usage_result.get("stable_valid", False):
                    owner_state["is_valid_machine_usage"] = True
                    _start_or_resume_zone_owner_session(owner_state, now)
            elif ghost_restored:
                owner_state["is_valid_machine_usage"] = True

    return owner_state.get("current_owner_id")


def current_zone_session_start_time_text(owner_state: dict) -> str:
    start_t = owner_state.get("current_session_start_t")
    if start_t is None:
        return ""
    return datetime.fromtimestamp(float(start_t)).strftime("%Y-%m-%d %H:%M:%S")


def make_live_zone_timer_state():
    return {
        "active": False,
        "start_t": None,
        "total_sec": 0.0,
    }


def update_live_zone_timer(timer_state: dict, should_run: bool, now: float):
    if should_run:
        if not timer_state["active"]:
            timer_state["active"] = True
            timer_state["start_t"] = now
    else:
        if timer_state["active"] and timer_state["start_t"] is not None:
            timer_state["total_sec"] += max(0.0, float(now) - float(timer_state["start_t"]))
        timer_state["active"] = False
        timer_state["start_t"] = None


def live_zone_timer_elapsed(timer_state: dict, now: float) -> float:
    if timer_state["active"] and timer_state["start_t"] is not None:
        return timer_state["total_sec"] + max(0.0, float(now) - float(timer_state["start_t"]))
    return timer_state["total_sec"]


def make_zone_session_state():
    return {
        "active": False,
        "start_t": None,
    }


def update_zone_session_state(session_state: dict, people_now: int, now: float, completed_session_times: list = None):
    completed_duration = None

    if people_now > 0:
        if not session_state["active"]:
            session_state["active"] = True
            session_state["start_t"] = now
    else:
        if session_state["active"] and session_state["start_t"] is not None:
            completed_duration = max(0.0, float(now) - float(session_state["start_t"]))
            if completed_session_times is not None:
                completed_session_times.append(completed_duration)
        session_state["active"] = False
        session_state["start_t"] = None

    return completed_duration


def current_zone_session_elapsed(session_state: dict, now: float) -> float:
    if session_state["active"] and session_state["start_t"] is not None:
        return max(0.0, float(now) - float(session_state["start_t"]))
    return 0.0


def cumulative_zone_usage_sec(zone_name: str, zone_owner_states: dict, zone_completed_session_times: dict, now: float) -> float:
    completed_total = sum(float(v) for v in zone_completed_session_times.get(zone_name, []))
    owner_state = zone_owner_states[zone_name]
    return float(completed_total) + float(current_zone_owner_session_elapsed(owner_state, now))


def compute_zone_avg_session_duration_sec(zone_name: str, zone_completed_session_times: dict) -> float:
    completed = zone_completed_session_times.get(zone_name, [])
    if not completed:
        return 0.0
    return sum(completed) / len(completed)


def compute_zone_estimated_waiting_time_sec(zone_name: str, owner_id, zone_completed_session_times: dict, zone_owner_states: dict, now: float) -> float:
    """
    Owner-session-based waiting estimate.

    - Uses completed single-owner equipment sessions, not zone occupancy sessions.
    - Waiting decays from the historical average only while a valid owner session exists.
    """
    owner_state = zone_owner_states[zone_name]
    if owner_id is None or owner_state.get("current_session_start_t") is None:
        return 0.0

    avg_session_duration = compute_zone_avg_session_duration_sec(zone_name, zone_completed_session_times)
    if avg_session_duration <= 0.0:
        return 0.0

    current_session_elapsed = current_zone_owner_session_elapsed(owner_state, now)
    return max(avg_session_duration - current_session_elapsed, 1.0)


def current_ids_out_of_zone(tracks: dict):
    return [tid for tid, t in tracks.items() if t.get("current_csv_zone") == OUT_OF_ZONE_LABEL]


def live_open_record_elapsed(zone_records: list, open_zone_records: dict, tid, zone_name: str, now: float) -> float:
    idx = open_zone_records.get((tid, zone_name))
    if idx is None:
        return 0.0
    enter_ts = zone_records[idx].get("enter_ts", "")
    if enter_ts in ("", None):
        return 0.0
    return max(0.0, float(now) - float(enter_ts))


def crowd_status_from_count(total_now: int) -> str:
    if total_now <= 2:
        return "LOW"
    elif total_now <= 5:
        return "MID"
    return "HIGH"


def _zone_csv_headers():
    return [
        "ts",
        "timestamp",
        "zone",
        "people_now",
        "total_ids_seen_in_zone",
        "tracked_ids",
        "zone_timer_sec",
        "zone_timer_hhmmss",
        "zone_occupied_duration_sec",
        "zone_occupied_duration_hhmmss",
        "current_owner_id",
        "current_session_start_time",
        "current_session_duration_sec",
        "is_valid_machine_usage",
        "multi_person_detected",
        "last_completed_session_duration_sec",
        "completed_sessions_count",
        "person_id",
        "person_state",
        "person_conf",
    ]


def open_zone_record(zone_csv_path, zone_records, open_zone_records,
                     now, zone_name, tid, people_now, unique_now, state, conf):
    record_idx = len(zone_records)
    record = {
        "enter_ts": now,
        "enter_datetime": datetime.fromtimestamp(now).isoformat(),
        "zone": zone_name,
        "enter_people_now": people_now,
        "enter_unique_now": unique_now,
        "person_id": tid,
        "person_state": state,
        "person_conf": conf,
        "exit_ts": None,
        "exit_datetime": None,
        "close_reason": None,
        "exit_people_now": None,
        "exit_unique_now": None,
        "dwell_sec": 0.0,
    }
    zone_records.append(record)
    open_zone_records[(tid, zone_name)] = record_idx


def close_zone_record(zone_csv_path, zone_records, open_zone_records,
                      now, close_reason, zone_name, tid, dwell_sec,
                      exit_people_now, exit_unique_now, state, conf):
    key = (tid, zone_name)
    if key not in open_zone_records:
        return
    idx = open_zone_records[key]
    del open_zone_records[key]

    if idx >= len(zone_records):
        return

    record = zone_records[idx]
    record["exit_ts"] = now
    record["exit_datetime"] = datetime.fromtimestamp(now).isoformat()
    record["close_reason"] = close_reason
    record["exit_people_now"] = exit_people_now
    record["exit_unique_now"] = exit_unique_now
    record["dwell_sec"] = dwell_sec


def update_motion_from_bbox(track: dict, now: float, cx: float, cy: float, bbox_h: float):
    """
    Motion-first logic using bbox-center speed.
    Speed is normalized by bbox height so it is less sensitive to perspective.
    """
    track["motion_hist"].append((now, float(cx), float(cy), float(bbox_h)))

    hist = track["motion_hist"]
    if len(hist) < 2:
        return

    speed_sum = 0.0
    cnt = 0
    last_speed = 0.0
    for i in range(1, len(hist)):
        t0, x0, y0, h0 = hist[i - 1]
        t1, x1, y1, h1 = hist[i]
        dt = max(1e-3, t1 - t0)
        h = max(1.0, 0.5 * (h0 + h1))
        disp = math.hypot(x1 - x0, y1 - y0) / h
        speed = disp / dt
        speed_sum += speed
        last_speed = speed
        cnt += 1

    avg_speed = max(speed_sum / max(1, cnt), last_speed)
    moving = (avg_speed >= MOTION_SPEED_THR)

    new_scores = {"moving": 1.0, "still": 0.0} if moving else {"moving": 0.0, "still": 1.0}
    # NOTE: ema_update_scores removed — use simple score update
    track["motion_scores"] = new_scores
    track["motion_speed"] = avg_speed


def _maybe_init_csv(path: str):
    if not LOG_CSV:
        return None
    f = open(path, "w", newline="")
    w = csv.writer(f)
    w.writerow([
        "ts", "fps_est",
        "crowd_status", "total_ids",
        "loop_avg_ms", "loop_p95_ms", "loop_p99_ms",
        "cap_avg_ms", "inf_avg_ms", "post_avg_ms", "disp_avg_ms",
        "cpu_pct", "rss_mb", "maxrss_mb", "ctx_vol", "ctx_invol", "threads",
        "timestamp"
    ])
    f.flush()
    return f


def _maybe_init_zone_csv(path: str):
    if not LOG_CSV:
        return None
    f = open(path, "w", newline="")
    w = csv.writer(f)
    w.writerow(_zone_csv_headers())
    f.flush()
    return f


def _maxrss_mb() -> float:
    # Linux ru_maxrss is in KB
    return float(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss) / 1024.0


def _make_raw_video_path(output_dir: str, prefix: str, ext: str = ".avi") -> str:
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    return os.path.join(output_dir, f"{prefix}_{ts}{ext}")


def run_validation(data: str = "coco128.yaml", imgsz: int = 640, device: str = "cpu"):
    """
    Measure detection accuracy (mAP50-95) for the NCNN DETECT model.
    Note: This validates the DETECT model (MODEL_PATH), not the pose model.
    """
    print(f"[VAL] Loading NCNN detect model: {MODEL_PATH}")
    ncnn_model = YOLO(MODEL_PATH, task="detect")
    print(f"[VAL] Running val on {data} (imgsz={imgsz}, device={device}) ...")
    metrics = ncnn_model.val(data=data, imgsz=imgsz, device=device)
    try:
        print(f"mAP50-95: {metrics.box.map:.4f}")
    except Exception:
        print("mAP50-95:", getattr(getattr(metrics, "box", None), "map", None))
    return metrics


def main():
    global SHOW_ROI, EQUIPMENT_ZONES

    proc = psutil.Process(os.getpid())
    proc.cpu_percent(interval=None)
    prev_ctx = proc.num_ctx_switches()

    csv_f = _maybe_init_csv(CSV_PATH)
    csv_w = csv.writer(csv_f) if csv_f else None

    zone_csv_f = _maybe_init_zone_csv(ZONE_CSV_PATH)
    zone_csv_w = csv.writer(zone_csv_f) if zone_csv_f else None
    zone_csv_path = None
    zone_records = []
    open_zone_records = {}

    raw_video_writer = None
    raw_video_path = None

    tracks = {}
    zone_unique_ids = {zone_name: set() for zone_name in EQUIPMENT_ZONES}
    zone_completed_session_times = {zone_name: [] for zone_name in EQUIPMENT_ZONES}
    zone_live_timers = {zone_name: make_live_zone_timer_state() for zone_name in EQUIPMENT_ZONES}
    zone_owner_states = {zone_name: make_zone_owner_state() for zone_name in EQUIPMENT_ZONES}
    out_of_zone_unique_ids = set()

    last_t = time.time()
    last_print_t = 0.0
    frame_i = 0

    # FPS averaging (qualitative webcam evaluation)
    fps_win_start = time.perf_counter()
    fps_win_frames = 0
    fps_avg = 0.0

    # Init model + camera
    model = YOLO(MODEL_PATH, task="detect")
    print(f"YOLO detect model loaded: {MODEL_PATH}")

    cap = cv2.VideoCapture(CAMERA_INDEX)
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, CAMERA_WIDTH)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, CAMERA_HEIGHT)
    cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)

    # tells camera to send MJPEG over USB instead of raw YUV
    cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc('M', 'J', 'P', 'G'))
    cap.set(cv2.CAP_PROP_FPS, 15)
    if not cap.isOpened():
        raise RuntimeError("Could not open camera. Try CAMERA_INDEX=1 or check camera connection.")

    actual_width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    actual_height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    print(f"Camera opened at {actual_width}x{actual_height}")

    if SAVE_RAW_VIDEO:
        os.makedirs(RAW_VIDEO_DIR, exist_ok=True)
        raw_video_path = _make_raw_video_path(RAW_VIDEO_DIR, RAW_VIDEO_PREFIX, RAW_VIDEO_EXT)
        raw_video_fps = float(cap.get(cv2.CAP_PROP_FPS))
        if raw_video_fps <= 1.0:
            raw_video_fps = float(RAW_VIDEO_FPS)

        raw_video_writer = cv2.VideoWriter(
            raw_video_path,
            cv2.VideoWriter_fourcc(*RAW_VIDEO_FOURCC),
            raw_video_fps,
            (actual_width, actual_height),
        )
        if not raw_video_writer.isOpened():
            raise RuntimeError(f"Could not open raw video writer: {raw_video_path}")
        print(f"Raw video recording enabled: {raw_video_path}")

    EQUIPMENT_ZONES = scale_zones(
        BASE_EQUIPMENT_ZONES,
        ZONE_BASE_WIDTH,
        ZONE_BASE_HEIGHT,
        actual_width,
        actual_height,
    )
    print(f"Zones scaled from {ZONE_BASE_WIDTH}x{ZONE_BASE_HEIGHT} to {actual_width}x{actual_height}")

    print("Starting... press 'q' to quit.")
    mqtt_setup()

    try:
        while True:
            loop_wall_t0 = time.perf_counter()
            loop_cpu_t0 = time.process_time()

            cap_t0 = time.perf_counter()
            ok, frame = cap.read()
            cap_t1 = time.perf_counter()
            if not ok:
                print("Failed to read frame.")
                break

            if raw_video_writer is not None:
                raw_video_writer.write(frame)

            inf_t0 = time.perf_counter()

            now = time.time()
            last_t = now

            res = model.track(
                frame,
                imgsz=IMG_SIZE,
                conf=CONF,
                iou=0.45,
                classes=[0],
                persist=True,
                tracker=TRACKER_CFG,
                verbose=False
            )[0]
            inf_t1 = time.perf_counter()

            post_t0 = time.perf_counter()

            if SHOW_ROI:
                for zone_name, zone_cfg in EQUIPMENT_ZONES.items():
                    poly = zone_cfg["polygon"]
                    color = zone_cfg["color"]
                    cv2.polylines(frame, [poly], True, color, 2)
                    eq_cx, eq_cy = zone_cfg["equipment_center"]
                    cv2.circle(frame, (int(eq_cx), int(eq_cy)), 4, color, -1)

            seen_this_frame = set()

            if res.boxes is not None and len(res.boxes) > 0 and res.boxes.id is not None:
                boxes = res.boxes.xyxy.cpu().numpy().astype(int)
                ids = res.boxes.id.cpu().numpy().astype(int)
                confs = res.boxes.conf.cpu().numpy()

                for (x1, y1, x2, y2), tid, cf in zip(boxes, ids, confs):
                    seen_this_frame.add(tid)
                    cx = int((x1 + x2) // 2)
                    cy = int((y1 + y2) // 2)
                    zone_name = get_zone_name(cx, cy)

                    if tid not in tracks:
                        tracks[tid] = {
                            "last_seen": now,
                            "last_conf": float(cf),
                            "conf_ema": float(cf),
                            "current_zone": None,
                            "zones": {zn: make_zone_state() for zn in EQUIPMENT_ZONES},
                            "zone_usage": {zn: _make_track_zone_usage_state() for zn in EQUIPMENT_ZONES},
                            "motion_hist": deque(maxlen=MOTION_HIST_LEN),
                            "motion_scores": {"moving": 0.5, "still": 0.5},
                            "motion_speed": 0.0,
                            "state_hist": deque(maxlen=STATE_VOTE_LEN),
                            "state_final": "still",
                            "still_since_t": now,  # when this track last became "still"
                            "current_csv_zone": None,
                            "current_bbox": None,
                            "bbox_center": None,
                        }

                    t = tracks[tid]
                    t["last_seen"] = now
                    t["current_bbox"] = (int(x1), int(y1), int(x2), int(y2))
                    t["bbox_center"] = (float(cx), float(cy))

                    t["last_conf"] = float(cf)
                    prev_ema = t.get("conf_ema", float(cf))
                    t["conf_ema"] = (CONF_EMA_BETA * float(prev_ema)) + ((1.0 - CONF_EMA_BETA) * float(cf))
                    conf_txt = f"{t['conf_ema']:.2f}"

                    update_motion_from_bbox(t, now, cx, cy, bbox_h=float(max(1, y2 - y1)))
                    moving_score = float(t["motion_scores"].get("moving", 0.0))

                    if moving_score >= MOTION_MOVING_GATE:
                        state_candidate = "moving"
                    else:
                        state_candidate = "still"

                    t["state_hist"].append(state_candidate)
                    prev_state = t["state_final"]
                    if t["state_hist"]:
                        t["state_final"] = Counter(t["state_hist"]).most_common(1)[0][0]
                    state = t["state_final"]

                    # Track when this person became "still" (for ownership debounce)
                    if state == "still" and prev_state != "still":
                        t["still_since_t"] = now          # just transitioned to still
                    elif state != "still":
                        t["still_since_t"] = None          # moving → reset

                    prev_zone = t["current_zone"]
                    if zone_name != prev_zone:
                        if prev_zone is not None:
                            prev_z = t["zones"][prev_zone]
                            if prev_z["inside"]:
                                _pause_zone_counting(prev_z, prev_z["last_seen_in_zone"])
                            prev_z["inside"] = False
                            prev_z["enter_t"] = None
                            exit_dwell = prev_z["total_dwell"]
                            current_people_prev = len(current_ids_in_zone(tracks, prev_zone))
                            unique_people_prev = len(zone_unique_ids[prev_zone])
                            if LOG_ENTER_EXIT:
                                print(f"[{time.strftime('%H:%M:%S')}] ID {tid} EXIT {prev_zone.upper()} dwell={fmt_hhmmss(exit_dwell)}", flush=True)
                            close_zone_record(zone_csv_path, zone_records, open_zone_records,
                                              now, CSV_CLOSE_EXIT, prev_zone, tid, exit_dwell,
                                              current_people_prev, unique_people_prev, state, t["conf_ema"])

                        t["current_zone"] = zone_name

                        if zone_name is not None:
                            z = t["zones"][zone_name]
                            z["inside"] = True
                            z["enter_t"] = now
                            z["last_seen_in_zone"] = now
                            z["counting"] = False
                            z["count_start_t"] = None
                            zone_unique_ids[zone_name].add(tid)
                            update_zone_counting(t, zone_name, now, state)
                            current_people_now = len(current_ids_in_zone(tracks, zone_name))
                            unique_people_now = len(zone_unique_ids[zone_name])
                            if LOG_ENTER_EXIT:
                                print(f"[{time.strftime('%H:%M:%S')}] ID {tid} ENTER {zone_name.upper()}", flush=True)
                            open_zone_record(zone_csv_path, zone_records, open_zone_records,
                                             now, zone_name, tid, current_people_now, unique_people_now, state, t["conf_ema"])
                    elif zone_name is not None:
                        z = t["zones"][zone_name]
                        z["last_seen_in_zone"] = now
                        update_zone_counting(t, zone_name, now, state)

                    if zone_name is None:
                        if t.get("current_csv_zone") != OUT_OF_ZONE_LABEL:
                            t["current_csv_zone"] = OUT_OF_ZONE_LABEL
                            out_of_zone_unique_ids.add(tid)
                            current_people_out = len(current_ids_out_of_zone(tracks))
                            unique_people_out = len(out_of_zone_unique_ids)
                            open_zone_record(zone_csv_path, zone_records, open_zone_records,
                                             now, OUT_OF_ZONE_LABEL, tid, current_people_out, unique_people_out, state, t["conf_ema"])
                    else:
                        if t.get("current_csv_zone") == OUT_OF_ZONE_LABEL:
                            t["current_csv_zone"] = None
                            current_people_out = len(current_ids_out_of_zone(tracks))
                            unique_people_out = len(out_of_zone_unique_ids)
                            out_dwell = live_open_record_elapsed(zone_records, open_zone_records, tid, OUT_OF_ZONE_LABEL, now)
                            close_zone_record(zone_csv_path, zone_records, open_zone_records,
                                              now, CSV_CLOSE_EXIT, OUT_OF_ZONE_LABEL, tid, out_dwell,
                                              current_people_out, unique_people_out, state, t["conf_ema"])

                    if zone_name is not None:
                        zone_label = EQUIPMENT_ZONES[zone_name]["label"]
                        zone_color = EQUIPMENT_ZONES[zone_name]["color"]
                        owner_state = zone_owner_states[zone_name]
                        owner_id = owner_state.get("current_owner_id")
                        if owner_id == tid:
                            session_txt = fmt_hhmmss(current_zone_owner_session_elapsed(owner_state, now))
                        else:
                            session_txt = fmt_hhmmss(0.0)
                        zone_usage = _zone_usage_state(t, zone_name)
                        valid_txt = "Y" if zone_usage.get("stable_valid", False) else "N"
                        cv2.rectangle(frame, (x1, y1), (x2, y2), zone_color, 2)
                        reason_txt = str(zone_usage.get("debug_reason", ""))
                        if len(reason_txt) > 24:
                            reason_txt = reason_txt[:24]
                        track_lines = [
                            f"ID {tid} {zone_label} {session_txt}",
                            f"conf:{conf_txt}  state:{state}",
                            f"use:{valid_txt}",
                            f"why:{reason_txt}",
                        ]
                        draw_stacked_text(
                            frame,
                            track_lines,
                            x1,
                            y1,
                            y2,
                            zone_color,
                            font_scale=0.44,
                            thickness=2,
                            line_gap=18,
                            bg_color=(12, 12, 12),
                            bg_alpha=0.36,
                            padding=7,
                            border_color=zone_color,
                        )
                    else:
                        cv2.rectangle(frame, (x1, y1), (x2, y2), (180, 180, 180), 1)
                        draw_stacked_text(
                            frame,
                            [f"ID {tid}", f"conf:{conf_txt}  state:{state}"],
                            x1,
                            y1,
                            y2,
                            (180, 180, 180),
                            font_scale=0.44,
                            thickness=2,
                            line_gap=18,
                            bg_color=(12, 12, 12),
                            bg_alpha=0.30,
                            padding=7,
                            border_color=(110, 110, 110),
                        )

            to_delete = []

            for tid, t in list(tracks.items()):
                for zone_name in EQUIPMENT_ZONES:
                    z = t["zones"][zone_name]
                    if z["inside"] and (now - z["last_seen_in_zone"]) > LEAVE_GRACE_SEC:
                        _pause_zone_counting(z, z["last_seen_in_zone"])
                        z["inside"] = False
                        z["enter_t"] = None
                        if t["current_zone"] == zone_name:
                            t["current_zone"] = None
                        dwell_sec = z["total_dwell"]
                        current_people = len(current_ids_in_zone(tracks, zone_name))
                        unique_people = len(zone_unique_ids[zone_name])
                        if LOG_ENTER_EXIT:
                            print(f"[{time.strftime('%H:%M:%S')}] ID {tid} EXIT {zone_name.upper()} dwell={fmt_hhmmss(dwell_sec)}", flush=True)
                        close_zone_record(zone_csv_path, zone_records, open_zone_records,
                                          now, CSV_CLOSE_EXIT, zone_name, tid, dwell_sec,
                                          current_people, unique_people, t.get("state_final", "unknown"), t.get("conf_ema", 0.0))

                if (now - t["last_seen"]) > STALE_TRACK_SEC:
                    for zone_name in EQUIPMENT_ZONES:
                        z = t["zones"][zone_name]
                        if z["inside"]:
                            _pause_zone_counting(z, z["last_seen_in_zone"])
                            z["inside"] = False
                            z["enter_t"] = None
                            dwell_sec = z["total_dwell"]
                            current_people = len(current_ids_in_zone(tracks, zone_name))
                            unique_people = len(zone_unique_ids[zone_name])
                            if LOG_ENTER_EXIT:
                                print(f"[{time.strftime('%H:%M:%S')}] ID {tid} FINAL {zone_name.upper()} dwell={fmt_hhmmss(dwell_sec)}", flush=True)
                            close_zone_record(zone_csv_path, zone_records, open_zone_records,
                                              now, CSV_CLOSE_CLOSED, zone_name, tid, dwell_sec,
                                              current_people, unique_people, t.get("state_final", "unknown"), t.get("conf_ema", 0.0))

                    if t.get("current_csv_zone") == OUT_OF_ZONE_LABEL:
                        t["current_csv_zone"] = None
                        current_people_out = len(current_ids_out_of_zone(tracks))
                        unique_people_out = len(out_of_zone_unique_ids)
                        out_dwell = live_open_record_elapsed(zone_records, open_zone_records, tid, OUT_OF_ZONE_LABEL, now)
                        if LOG_ENTER_EXIT:
                            print(f"[{time.strftime('%H:%M:%S')}] ID {tid} FINAL OUT_OF_ZONE dwell={fmt_hhmmss(out_dwell)}", flush=True)
                        close_zone_record(zone_csv_path, zone_records, open_zone_records,
                                          now, CSV_CLOSE_CLOSED, OUT_OF_ZONE_LABEL, tid, out_dwell,
                                          current_people_out, unique_people_out, t.get("state_final", "unknown"), t.get("conf_ema", 0.0))
                    to_delete.append(tid)

            for tid in to_delete:
                tracks.pop(tid, None)

            for zone_name in EQUIPMENT_ZONES:
                people_now = len(current_ids_in_zone(tracks, zone_name))
                update_live_zone_timer(zone_live_timers[zone_name], people_now > 0, now)

                owner_state = zone_owner_states[zone_name]
                current_owner_id = owner_state.get("current_owner_id")

                # Update machine usage validity for owner/candidate
                for track_id in [current_owner_id] + [cid for cid in tracks.keys() if cid != current_owner_id]:
                    if track_id is None or track_id not in tracks:
                        continue
                    track = tracks[track_id]
                    if not is_owner_eligible_track(track, zone_name, now):
                        continue
                    classify_valid_machine_usage(track, zone_name, frame_i, now)

                update_zone_owner_state(
                    zone_owner_states[zone_name],
                    tracks,
                    zone_name,
                    now,
                    zone_completed_session_times[zone_name]
                )

                # Ghost dedup: if a ghost restore happened, the new track ID is a
                # duplicate of the original person. Remove it from unique counts.
                dedup_tid = zone_owner_states[zone_name].pop("_ghost_dedup_tid", None)
                if dedup_tid is not None:
                    zone_unique_ids[zone_name].discard(dedup_tid)

            if SHOW_ROI:
                for zone_name, zone_cfg in EQUIPMENT_ZONES.items():
                    poly = zone_cfg["polygon"]
                    color = zone_cfg["color"]
                    x, y = poly[0]
                    owner_state = zone_owner_states[zone_name]
                    owner_id = owner_state["current_owner_id"]
                    occupied_txt = fmt_hhmmss(cumulative_zone_usage_sec(zone_name, zone_owner_states, zone_completed_session_times, now))
                    owner_txt = "None" if owner_id is None else f"ID {owner_id}"
                    session_txt = fmt_hhmmss(current_zone_owner_session_elapsed(owner_state, now))
                    valid_txt = "Y" if owner_state.get("is_valid_machine_usage") else "N"
                    multi_txt = " MULTI" if owner_state.get("multi_person_detected") else ""
                    owner_reason = "no_owner"
                    if owner_id is not None and owner_id in tracks:
                        owner_reason = _zone_usage_state(tracks[owner_id], zone_name).get("debug_reason", "no_proximity")
                    zone_lines = [
                        f"{zone_cfg['label']} occ:{occupied_txt} owner:{owner_txt}",
                        f"session:{session_txt}  valid:{valid_txt}{multi_txt}",
                        f"why:{owner_reason}",
                    ]
                    draw_stacked_text(
                        frame,
                        zone_lines,
                        int(x) + 10,
                        int(y),
                        int(y) + 8,
                        color,
                        font_scale=0.44,
                        thickness=2,
                        line_gap=18,
                        bg_color=(12, 12, 12),
                        bg_alpha=0.38,
                        padding=8,
                        border_color=color,
                    )

            zone_a_now = len(current_ids_in_zone(tracks, "equipment_a"))
            zone_a_unique = len(zone_unique_ids["equipment_a"])
            zone_b_now = len(current_ids_in_zone(tracks, "equipment_b"))
            zone_b_unique = len(zone_unique_ids["equipment_b"])
            out_of_zone_now = len(current_ids_out_of_zone(tracks))
            total_now_ids = zone_a_now + zone_b_now + out_of_zone_now
            crowd_status = crowd_status_from_count(total_now_ids)

            zone_a_text = f"Zone A: now={zone_a_now} unique={zone_a_unique}"
            zone_b_text = f"Zone B: now={zone_b_now} unique={zone_b_unique}"
            status_text = f"Status: {crowd_status} {total_now_ids}"

            draw_segment_bar(
                frame,
                [
                    (zone_a_text, EQUIPMENT_ZONES["equipment_a"]["color"]),
                    (zone_b_text, EQUIPMENT_ZONES["equipment_b"]["color"]),
                    (status_text, (0, 255, 255)),
                ],
                x=12,
                y=2,
                font_scale=0.48,
                thickness=2,
                gap=18,
                padding_x=12,
                padding_y=8,
                bg_color=(12, 12, 12),
                bg_alpha=0.42,
                border_color=(70, 70, 70),
            )

            bottom_overlay_lines = ["Zone Summary"]
            for zone_name, zone_cfg in EQUIPMENT_ZONES.items():
                owner_state = zone_owner_states[zone_name]
                owner_id = owner_state["current_owner_id"]
                owner_txt = "None" if owner_id is None else f"ID {owner_id}"
                owner_session_txt = fmt_hhmmss(current_zone_owner_session_elapsed(owner_state, now))
                occupied_txt = fmt_hhmmss(cumulative_zone_usage_sec(zone_name, zone_owner_states, zone_completed_session_times, now))
                valid_txt = "yes" if owner_state.get("is_valid_machine_usage") else "no"
                multi_txt = "yes" if owner_state.get("multi_person_detected") else "no"
                last_done_txt = fmt_hhmmss(owner_state.get("last_completed_session_duration_sec", 0.0))
                start_txt = current_zone_session_start_time_text(owner_state).split(" ")[-1] if current_zone_session_start_time_text(owner_state) else "—"
                bottom_overlay_lines.append(
                    f"{zone_cfg['label']} owner={owner_txt} session={owner_session_txt} occ={occupied_txt}"
                )
                bottom_overlay_lines.append(
                    f"valid_use={valid_txt} multi_person={multi_txt} start={start_txt} last_done={last_done_txt}"
                )

            summary_sizes = [
                cv2.getTextSize(line, cv2.FONT_HERSHEY_SIMPLEX, 0.46, 2)[0]
                for line in bottom_overlay_lines
            ]
            summary_max_w = max((w for w, _ in summary_sizes), default=0)
            summary_text_h = max((h for _, h in summary_sizes), default=0)
            summary_panel_w = summary_max_w + 20
            summary_panel_h = summary_text_h + (max(0, len(bottom_overlay_lines) - 1) * 18) + 20
            summary_panel_x = max(6, frame.shape[1] - summary_panel_w - 8)
            summary_panel_y = max(3, frame.shape[0] - summary_panel_h - 4)
            draw_text_panel(
                frame,
                bottom_overlay_lines,
                summary_panel_x,
                summary_panel_y,
                (245, 245, 245),
                font_scale=0.46,
                thickness=2,
                line_gap=18,
                padding=10,
                bg_color=(12, 12, 12),
                bg_alpha=0.42,
                border_color=(70, 70, 70),
            )

            inf_ms = (inf_t1 - inf_t0) * 1000.0
            fps_inst = (1000.0 / inf_ms) if inf_ms > 0 else 0.0

            fps_now = time.perf_counter()
            fps_win_frames += 1
            fps_elapsed = fps_now - fps_win_start
            if fps_elapsed >= FPS_AVG_WINDOW_SEC:
                fps_avg = fps_win_frames / fps_elapsed
                fps_win_start = fps_now
                fps_win_frames = 0

            perf_line = f"FPS(inst): {fps_inst:.1f} | FPS(avg {FPS_AVG_WINDOW_SEC:.0f}s): {fps_avg:.1f} | inf_ms: {inf_ms:.1f}"
            perf_size = cv2.getTextSize(perf_line, cv2.FONT_HERSHEY_SIMPLEX, 0.60, 2)[0]
            perf_panel_y = max(12, frame.shape[0] - perf_size[1] - 24)
            draw_text_panel(
                frame,
                [perf_line],
                12,
                perf_panel_y,
                (0, 255, 0),
                font_scale=0.60,
                thickness=2,
                line_gap=18,
                padding=10,
                bg_color=(12, 12, 12),
                bg_alpha=0.42,
                border_color=(70, 70, 70),
            )

            post_t1 = time.perf_counter()

            disp_t0 = time.perf_counter()
            key = 0
            if not HEADLESS:
                cv2.imshow(WINDOW_NAME, frame)
                key = cv2.waitKey(1) & 0xFF
            disp_t1 = time.perf_counter()

            loop_wall_t1 = time.perf_counter()
            loop_cpu_t1 = time.process_time()

            frame_i += 1
            if frame_i > WARMUP_FRAMES:
                t_cap_ms.append((cap_t1 - cap_t0) * 1000.0)
                t_inf_ms.append((inf_t1 - inf_t0) * 1000.0)
                t_post_ms.append((post_t1 - post_t0) * 1000.0)
                t_disp_ms.append((disp_t1 - disp_t0) * 1000.0)
                t_loop_ms.append((loop_wall_t1 - loop_wall_t0) * 1000.0)
                t_cpu_ms.append((loop_cpu_t1 - loop_cpu_t0) * 1000.0)

            if now - last_print_t >= PRINT_EVERY_SEC:
                last_print_t = now

                zone_parts = []
                for zone_name, zone_cfg in EQUIPMENT_ZONES.items():
                    ids_now = current_ids_in_zone(tracks, zone_name)
                    owner_state = zone_owner_states[zone_name]
                    owner_id = owner_state["current_owner_id"]
                    owner_txt = "None" if owner_id is None else f"ID{owner_id}"
                    session_txt = fmt_hhmmss(current_zone_owner_session_elapsed(owner_state, now))
                    zone_parts.append(
                        f"{zone_cfg['label']}[now={len(ids_now)}, unique={len(zone_unique_ids[zone_name])}, owner={owner_txt}, session={session_txt}, valid={owner_state.get('is_valid_machine_usage', False)}, multi={owner_state.get('multi_person_detected', False)}]"
                    )
                print(" | ".join(zone_parts), flush=True)

                topT = sorted(tracks.items(), key=lambda kv: live_total_dwell(kv[1], now), reverse=True)[:PRINT_TOP_N]
                top_str = " | ".join([f"ID{tid}:{fmt_hhmmss(live_total_dwell(t, now))}" for tid, t in topT])
                if top_str:
                    print(f"Top total dwell: {top_str}", flush=True)

                if t_loop_ms:
                    avg_loop = statistics.mean(t_loop_ms)
                    fps_est = 1000.0 / avg_loop if avg_loop > 0 else 0.0
                    avg_cpu = statistics.mean(t_cpu_ms) if t_cpu_ms else 0.0
                    cpu_util_est = (avg_cpu / avg_loop * 100.0) if avg_loop > 0 else 0.0
                    verdict = "STABLE" if _pctl(t_loop_ms, 99) <= 3 * avg_loop else "JITTERY"

                    print(
                        f"[Perf] FPS~{fps_est:.2f} | loop {_stage_stats_ms(t_loop_ms)} | "
                        f"cap {_stage_stats_ms(t_cap_ms)} | inf {_stage_stats_ms(t_inf_ms)} | "
                        f"post {_stage_stats_ms(t_post_ms)} | disp {_stage_stats_ms(t_disp_ms)} | "
                        f"cpu(ms) {_stage_stats_ms(t_cpu_ms)} | cpu_util~{cpu_util_est:.1f}% | {verdict}",
                        flush=True,
                    )
                else:
                    fps_est = 0.0

                cpu_pct = proc.cpu_percent(interval=None)
                rss_mb = proc.memory_info().rss / (1024.0 * 1024.0)
                maxrss_mb = _maxrss_mb()
                ctx = proc.num_ctx_switches()
                d_vol = ctx.voluntary - prev_ctx.voluntary
                d_invol = ctx.involuntary - prev_ctx.involuntary
                prev_ctx = ctx
                threads = proc.num_threads()

                cpu_pct_samples.append(cpu_pct)
                rss_mb_samples.append(rss_mb)

                print(
                    f"[Res] CPU%={cpu_pct:.1f} | RSS={rss_mb:.1f} MB | MaxRSS={maxrss_mb:.1f} MB | "
                    f"ctx(+vol/+invol)={d_vol}/{d_invol} | threads={threads}",
                    flush=True,
                )

                if csv_w and t_loop_ms:
                    csv_w.writerow([
                        now,
                        f"{fps_est:.3f}",
                        crowd_status,
                        total_now_ids,
                        f"{statistics.mean(t_loop_ms):.3f}",
                        f"{_pctl(t_loop_ms,95):.3f}",
                        f"{_pctl(t_loop_ms,99):.3f}",
                        f"{statistics.mean(t_cap_ms):.3f}",
                        f"{statistics.mean(t_inf_ms):.3f}",
                        f"{statistics.mean(t_post_ms):.3f}",
                        f"{statistics.mean(t_disp_ms):.3f}",
                        f"{cpu_pct:.1f}",
                        f"{rss_mb:.1f}",
                        f"{maxrss_mb:.1f}",
                        f"{d_vol}",
                        f"{d_invol}",
                        f"{threads}",
                        datetime.fromtimestamp(now).isoformat(),
                    ])
                    csv_f.flush()

                # ── MQTT payload build & publish ──────────────────────────
                zone_a_ids = sorted(current_ids_in_zone(tracks, "equipment_a"))
                zone_b_ids = sorted(current_ids_in_zone(tracks, "equipment_b"))

                zone_a_timer_sec = cumulative_zone_usage_sec("equipment_a", zone_owner_states, zone_completed_session_times, now)
                zone_b_timer_sec = cumulative_zone_usage_sec("equipment_b", zone_owner_states, zone_completed_session_times, now)

                zone_a_owner_state = zone_owner_states["equipment_a"]
                zone_b_owner_state = zone_owner_states["equipment_b"]
                zone_a_owner_id = zone_a_owner_state["current_owner_id"]
                zone_b_owner_id = zone_b_owner_state["current_owner_id"]
                zone_a_current_session_sec = current_zone_owner_session_elapsed(zone_a_owner_state, now)
                zone_b_current_session_sec = current_zone_owner_session_elapsed(zone_b_owner_state, now)
                zone_a_avg_session_sec = compute_zone_avg_session_duration_sec("equipment_a", zone_completed_session_times)
                zone_b_avg_session_sec = compute_zone_avg_session_duration_sec("equipment_b", zone_completed_session_times)

                zone_a_estimated_waiting_time_sec = compute_zone_estimated_waiting_time_sec(
                    "equipment_a", zone_a_owner_id, zone_completed_session_times, zone_owner_states, now
                )
                zone_b_estimated_waiting_time_sec = compute_zone_estimated_waiting_time_sec(
                    "equipment_b", zone_b_owner_id, zone_completed_session_times, zone_owner_states, now
                )

                mqtt_payload = {
                    "node_id": "rpi1",
                    "ts": round(now, 3),
                    "timestamp": datetime.fromtimestamp(now).strftime("%Y-%m-%d %H:%M:%S"),
                    "fps_est": round(fps_est, 3),
                    "inf_avg_ms": round(statistics.mean(t_inf_ms), 3) if t_inf_ms else 0.0,
                    "loop_avg_ms": round(statistics.mean(t_loop_ms), 3) if t_loop_ms else 0.0,
                    "loop_p95_ms": round(_pctl(t_loop_ms, 95), 3) if t_loop_ms else 0.0,
                    "loop_p99_ms": round(_pctl(t_loop_ms, 99), 3) if t_loop_ms else 0.0,
                    "cap_avg_ms": round(statistics.mean(t_cap_ms), 3) if t_cap_ms else 0.0,
                    "post_avg_ms": round(statistics.mean(t_post_ms), 3) if t_post_ms else 0.0,
                    "disp_avg_ms": round(statistics.mean(t_disp_ms), 3) if t_disp_ms else 0.0,
                    "cpu_pct": round(cpu_pct, 3),
                    "rss_mb": round(rss_mb, 3),
                    "total_now_ids": int(total_now_ids),
                    "crowd_status": crowd_status,
                    "zones": {
                        "equipment_a": {
                            "people_now": len(zone_a_ids),
                            "unique_ids_seen_in_zone": len(zone_unique_ids["equipment_a"]),
                            "tracked_ids": "|".join(str(tid) for tid in zone_a_ids) if zone_a_ids else "—",
                            "zone_timer_sec": round(zone_a_timer_sec, 3),
                            "zone_timer_hhmmss": fmt_hhmmss(zone_a_timer_sec),
                            "zone_occupied_duration_sec": round(zone_a_timer_sec, 3),
                            "zone_occupied_duration_hhmmss": fmt_hhmmss(zone_a_timer_sec),
                            "current_owner_id": zone_a_owner_id,
                            "current_session_start_time": current_zone_session_start_time_text(zone_a_owner_state),
                            "current_session_duration_sec": round(zone_a_current_session_sec, 3),
                            "is_valid_machine_usage": bool(zone_a_owner_state.get("is_valid_machine_usage", False)),
                            "multi_person_detected": bool(zone_a_owner_state.get("multi_person_detected", False)),
                            "last_completed_session_duration_sec": round(zone_a_owner_state.get("last_completed_session_duration_sec", 0.0), 3),
                            "completed_sessions_count": int(zone_a_owner_state.get("completed_sessions_count", 0)),
                            "avg_completed_session_duration_sec": round(zone_a_avg_session_sec, 3),
                            "avg_individual_usage_time_sec": round(zone_a_avg_session_sec, 3),
                            "current_longest_usage_time_sec": round(zone_a_current_session_sec, 3),
                            "estimated_waiting_time_sec": round(zone_a_estimated_waiting_time_sec, 3),
                        },
                        "equipment_b": {
                            "people_now": len(zone_b_ids),
                            "unique_ids_seen_in_zone": len(zone_unique_ids["equipment_b"]),
                            "tracked_ids": "|".join(str(tid) for tid in zone_b_ids) if zone_b_ids else "—",
                            "zone_timer_sec": round(zone_b_timer_sec, 3),
                            "zone_timer_hhmmss": fmt_hhmmss(zone_b_timer_sec),
                            "zone_occupied_duration_sec": round(zone_b_timer_sec, 3),
                            "zone_occupied_duration_hhmmss": fmt_hhmmss(zone_b_timer_sec),
                            "current_owner_id": zone_b_owner_id,
                            "current_session_start_time": current_zone_session_start_time_text(zone_b_owner_state),
                            "current_session_duration_sec": round(zone_b_current_session_sec, 3),
                            "is_valid_machine_usage": bool(zone_b_owner_state.get("is_valid_machine_usage", False)),
                            "multi_person_detected": bool(zone_b_owner_state.get("multi_person_detected", False)),
                            "last_completed_session_duration_sec": round(zone_b_owner_state.get("last_completed_session_duration_sec", 0.0), 3),
                            "completed_sessions_count": int(zone_b_owner_state.get("completed_sessions_count", 0)),
                            "avg_completed_session_duration_sec": round(zone_b_avg_session_sec, 3),
                            "avg_individual_usage_time_sec": round(zone_b_avg_session_sec, 3),
                            "current_longest_usage_time_sec": round(zone_b_current_session_sec, 3),
                            "estimated_waiting_time_sec": round(zone_b_estimated_waiting_time_sec, 3),
                        },
                    },
                }
                try:
                    mqtt_publish(mqtt_payload)
                    print(f"[MQTT] Published OK ts={mqtt_payload['timestamp']}", flush=True)
                except Exception as mqtt_err:
                    print(f"[MQTT] PUBLISH ERROR: {mqtt_err}", flush=True)

            if key == ord('q'):
                print("Quit requested.", flush=True)
                break

            if key == ord('r'):
                SHOW_ROI = not SHOW_ROI
                print(f"SHOW_ROI toggled: {SHOW_ROI}", flush=True)

    except KeyboardInterrupt:
        print("Interrupted.", flush=True)
    except Exception as e:
        import traceback
        print(f"\n{'='*60}", flush=True)
        print(f"[FATAL] Unhandled exception in main loop:", flush=True)
        traceback.print_exc()
        print(f"{'='*60}\n", flush=True)
    finally:
        print("Cleaning up...", flush=True)
        if cap:
            cap.release()
        if raw_video_writer:
            raw_video_writer.release()
        if csv_f:
            csv_f.close()
        if zone_csv_f:
            zone_csv_f.close()
        if not HEADLESS:
            cv2.destroyAllWindows()
        global mqtt_client
        if mqtt_client is not None:
            try:
                mqtt_client.loop_stop()
                mqtt_client.disconnect()
            except Exception:
                pass
        print("Done.", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Gym equipment zone tracker (v3-simplified)")
    parser.add_argument("--val", action="store_true", help="Run validation on DETECT model")
    args = parser.parse_args()

    if args.val:
        run_validation()
    else:
        main()
