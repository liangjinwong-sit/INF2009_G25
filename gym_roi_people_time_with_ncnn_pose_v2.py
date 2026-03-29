"""
INF2009 Lab 5-style instrumentation for your Gym ROI YOLO loop.

Updated for two equipment zones:
  - Replaces the single ROI with 2 named equipment areas.
  - Tracks per-ID dwell time for each equipment area.
  - Removes the old total ROI occupied timer.
  - Saves per-zone enter/exit/final records to a separate CSV.

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

from collections import deque
import statistics

# for posture angles
import math
from collections import Counter


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
SAVE_RAW_VIDEO = True
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

# MQTT publish (RPi1 -> RPi3)
MQTT_ENABLE = True
# MQTT_BROKER = "172.20.10.4"   # replace with your RPi3 IP
# MQTT_BROKER = "192.168.1.2"   # replace with your RPi3 IP
MQTT_BROKER = "172.20.10.3"   # replace with your RPi3 IP
# MQTT_BROKER = "10.42.0.1"   # replace with your RPi3 IP
MQTT_PORT = 1883
MQTT_TOPIC = "gympulse/rpi1/vision/state"

# ----------------------------
# per-person confidence smoothing
# ----------------------------
CONF_EMA_BETA = 0.8   # 0.8 old + 0.2 new

# ----------------------------
# Pose path on Pi 5
# ----------------------------
POSE_MODEL_PATH = "/home/keithpi5/Desktop/Project/yolo11n-pose_ncnn_model"
POSE_EVERY_N = 6
POSE_IMG_SIZE = 320          # if you want more accuracy: 320 (slower)
KP_MIN_CONF = 0.25            # FIX 2: lowered from 0.35 — lets partially-occluded joints survive

POSTURE_EMA_BETA = 0.8

# ----------------------------
# motion-first gating
# ----------------------------
MOTION_HIST_LEN = 6           # FIX 4: increased from 4 — smooths out brief motion spikes
MOTION_EMA_BETA = 0.55
MOTION_SPEED_THR = 0.25      # FIX 4: raised from 0.18 — small fidgets/exercise motion won't trigger "moving"
MOTION_STILL_GATE = 0.60
MOTION_MOVING_GATE = 0.52

# Speed tweak: ByteTrack is usually faster than BoT-SORT
TRACKER_CFG = "bytetrack.yaml"
#TRACKER_CFG = "/home/keithpi5/Desktop/Project/bytetrack_gym.yaml"

# ----------------------------
# accuracy knobs
# ----------------------------
POSE_MARGIN = 0.25            # FIX 3: expanded from 0.18 — captures more body context for side-angle poses
POSE_MIN_KP_COUNT = 5         # FIX 2: lowered from 8 — push-up down / side-on poses often show ~5-6 joints
POSE_MIN_MEAN_KP_CONF = 0.35  # FIX 2: lowered from 0.45 — allows classification with moderate-confidence kps
STATE_VOTE_LEN = 7            # per-ID vote smoothing window
POSTURE_MIN_TOP_SCORE = 0.55  # if top posture score too low, keep previous state
OWNER_STILL_GRACE_SEC = 2.0   # keep current equipment owner briefly if still-state flickers
OWNER_USAGE_GRACE_SEC = 2.0   # keep current equipment owner briefly if pose-use gate flickers
MACHINE_USAGE_STABLE_CHECKS = 2
POSE_STALE_SEC = 2.5

# Practical machine-use gate tuning knobs
MACHINE_SITTING_SCORE_THR = 0.42
MACHINE_BODY_CENTER_MAX_ZONE_FRAC = 0.46
MACHINE_HANDLE_X_FRAC = 0.36
MACHINE_HANDLE_Y_ABOVE_FRAC = 0.42
MACHINE_HANDLE_Y_BELOW_FRAC = 0.24
MACHINE_ARM_SUPPORT_X_FRAC = 0.42
MACHINE_ARM_SUPPORT_Y_ABOVE_FRAC = 0.42
MACHINE_ARM_SUPPORT_Y_BELOW_FRAC = 0.28
MACHINE_SEATED_CENTER_X_FRAC = 0.30
MACHINE_SEATED_CENTER_Y_MIN_FRAC = 0.38
MACHINE_SEATED_CENTER_Y_MAX_FRAC = 0.90
MACHINE_WRIST_SHOULDER_Y_ALLOW_FRAC = 0.22

POSE_SPECIAL_STATES = set()
POSE_FOLLOW_STATES = set()

MACHINE_KNEE_BENT_THR = 155.0
MACHINE_TORSO_MAX_DEG = 70.0
MACHINE_HIP_KNEE_Y_ALLOW_FRAC = 0.18
MACHINE_REACH_X_EXTRA_FRAC = 0.16
MACHINE_REACH_Y_ABOVE_FRAC = 0.20
MACHINE_REACH_Y_BELOW_FRAC = 0.22
MACHINE_ARMUP_SEATED_SCORE_THR = 0.42

mqtt_client = None


def mqtt_setup():
    global mqtt_client
    if not MQTT_ENABLE:
        return
    mqtt_client = mqtt.Client(client_id="gympulse-rpi1-vision")
    mqtt_client.connect(MQTT_BROKER, MQTT_PORT, 60)
    mqtt_client.loop_start()


def mqtt_publish(payload: dict):
    if not MQTT_ENABLE or mqtt_client is None:
        return
    try:
        mqtt_client.publish(MQTT_TOPIC, json.dumps(payload), qos=0, retain=True)
    except Exception as e:
        print("[MQTT] publish failed:", e, flush=True)


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
        "owner_invalid_usage_since_t": None,
        "is_valid_machine_usage": False,
        "multi_person_detected": False,
        "current_session_start_t": None,
        "current_session_accumulated_sec": 0.0,
        "current_session_running": False,
        "current_session_last_resume_t": None,
        "last_completed_session_duration_sec": 0.0,
        "completed_sessions_count": 0,
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
        "last_pose_update_f": -999999,
        "last_pose_update_t": 0.0,
        "pose_good_kp_count": 0,
        "pose_mean_kp_conf": 0.0,
        "posture_label": "unknown",
        "posture_score": 0.0,
        "body_center_dist_norm": 999.0,
        "handle_contacts": 0,
        "raw_valid": False,
        "stable_valid": False,
        "debug_reason": "no_pose",
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
    zone_usage["last_pose_update_f"] = frame_i
    zone_usage["last_pose_update_t"] = float(now)
    zone_usage["pose_good_kp_count"] = 0
    zone_usage["pose_mean_kp_conf"] = 0.0
    zone_usage["posture_label"] = "unknown"
    zone_usage["posture_score"] = 0.0
    zone_usage["body_center_dist_norm"] = 999.0
    zone_usage["handle_contacts"] = 0
    zone_usage["debug_reason"] = reason
    _update_zone_usage_stability(zone_usage, False)
    return zone_usage


def _mean_visible_point(kps_xy, kps_cf, idxs):
    pts = []
    for idx in idxs:
        if kps_cf is not None and idx < len(kps_cf) and kps_cf[idx] >= KP_MIN_CONF:
            pts.append(kps_xy[idx])
    if not pts:
        return None
    return np.mean(np.asarray(pts, dtype=np.float32), axis=0)


def _point_in_rect(pt, rect) -> bool:
    x1, y1, x2, y2 = rect
    return (float(pt[0]) >= float(x1) and float(pt[0]) <= float(x2) and
            float(pt[1]) >= float(y1) and float(pt[1]) <= float(y2))


def _equipment_handle_rect(zone_name: str, support: bool = False):
    x, y, w, h = _zone_bounds(zone_name)
    cx, cy = EQUIPMENT_ZONES[zone_name]["equipment_center"]

    if support:
        rx = float(w) * MACHINE_ARM_SUPPORT_X_FRAC
        ry_up = float(h) * MACHINE_ARM_SUPPORT_Y_ABOVE_FRAC
        ry_dn = float(h) * MACHINE_ARM_SUPPORT_Y_BELOW_FRAC
    else:
        rx = float(w) * MACHINE_HANDLE_X_FRAC
        ry_up = float(h) * MACHINE_HANDLE_Y_ABOVE_FRAC
        ry_dn = float(h) * MACHINE_HANDLE_Y_BELOW_FRAC

    return (
        float(cx) - rx,
        float(cy) - ry_up,
        float(cx) + rx,
        float(cy) + ry_dn,
    )



def _seated_machine_position_ok(track: dict, zone_name: str, body_center) -> bool:
    if body_center is None:
        body_center = _track_box_center(track)
    if body_center is None:
        return False

    x, y, w, h = _zone_bounds(zone_name)
    zone_cx = float(x) + (float(w) * 0.5)

    dx_norm = abs(float(body_center[0]) - zone_cx) / max(1.0, float(w))
    y_norm = (float(body_center[1]) - float(y)) / max(1.0, float(h))

    return (
        dx_norm <= MACHINE_SEATED_CENTER_X_FRAC
        and MACHINE_SEATED_CENTER_Y_MIN_FRAC <= y_norm <= MACHINE_SEATED_CENTER_Y_MAX_FRAC
    )


def _latpulldown_like_pose(track: dict, zone_name: str, kps_xy, kps_cf, posture_scores: dict, body_center) -> bool:
    if kps_xy is None or kps_cf is None:
        return False

    seated_pos_ok = _seated_machine_position_ok(track, zone_name, body_center)
    if not seated_pos_ok:
        return False

    shoulders_mid = _mean_visible_point(kps_xy, kps_cf, [5, 6])
    wrists_high = 0
    elbows_high = 0

    if shoulders_mid is not None:
        _, bbox_h = _track_bbox_wh(track)
        y_allow = max(12.0, float(bbox_h) * MACHINE_WRIST_SHOULDER_Y_ALLOW_FRAC)

        for idx in (9, 10):
            if idx < len(kps_cf) and kps_cf[idx] >= KP_MIN_CONF and float(kps_xy[idx][1]) <= float(shoulders_mid[1]) + y_allow:
                wrists_high += 1
        for idx in (7, 8):
            if idx < len(kps_cf) and kps_cf[idx] >= KP_MIN_CONF and float(kps_xy[idx][1]) <= float(shoulders_mid[1]) + y_allow:
                elbows_high += 1

    top_posture = "unknown"
    top_score = 0.0
    if posture_scores:
        top_posture, top_score = max(posture_scores.items(), key=lambda kv: float(kv[1]))

    sitting_score = float(posture_scores.get("sitting", 0.0)) if posture_scores else 0.0
    arms_up_score = float(posture_scores.get("arms_up", 0.0)) if posture_scores else 0.0

    return bool(
        seated_pos_ok
        and (wrists_high >= 1 or elbows_high >= 2)
        and (
            arms_up_score >= 0.38
            or top_posture == "arms_up"
            or sitting_score >= 0.34
            or float(top_score) >= 0.40
        )
    )


def _machine_seated_pose_ok(track: dict, zone_name: str, kps_xy, kps_cf, posture_scores: dict, body_center):
    if kps_xy is None or kps_cf is None:
        return False

    seated_pos_ok = _seated_machine_position_ok(track, zone_name, body_center)
    if not seated_pos_ok:
        return False

    shoulders_mid = _mean_visible_point(kps_xy, kps_cf, [5, 6])
    hips_mid = _mean_visible_point(kps_xy, kps_cf, [11, 12])
    knees_mid = _mean_visible_point(kps_xy, kps_cf, [13, 14])

    _, bbox_h = _track_bbox_wh(track)
    bbox_h = max(1.0, float(bbox_h))

    torso_angle = None
    if shoulders_mid is not None and hips_mid is not None:
        dx = abs(float(shoulders_mid[0]) - float(hips_mid[0]))
        dy = abs(float(shoulders_mid[1]) - float(hips_mid[1]))
        torso_angle = math.degrees(math.atan2(dx, dy + 1e-6))

    knee_angles = []
    for hip_i, knee_i, ankle_i in ((11, 13, 15), (12, 14, 16)):
        if (hip_i < len(kps_cf) and knee_i < len(kps_cf) and ankle_i < len(kps_cf)
                and kps_cf[hip_i] >= KP_MIN_CONF and kps_cf[knee_i] >= KP_MIN_CONF and kps_cf[ankle_i] >= KP_MIN_CONF):
            knee_angles.append(_angle_deg(kps_xy[hip_i], kps_xy[knee_i], kps_xy[ankle_i]))

    knee_bent = bool(knee_angles and (min(float(v) for v in knee_angles) <= MACHINE_KNEE_BENT_THR))

    hip_near_knee_level = False
    if hips_mid is not None and knees_mid is not None:
        hip_near_knee_level = float(hips_mid[1]) >= (float(knees_mid[1]) - (bbox_h * MACHINE_HIP_KNEE_Y_ALLOW_FRAC))

    top_posture = "unknown"
    top_score = 0.0
    if posture_scores:
        top_posture, top_score = max(posture_scores.items(), key=lambda kv: float(kv[1]))

    arms_up_seated_like = bool(
        top_posture == "arms_up"
        and float(top_score) >= MACHINE_ARMUP_SEATED_SCORE_THR
        and (torso_angle is None or float(torso_angle) <= MACHINE_TORSO_MAX_DEG)
    )

    return bool(
        seated_pos_ok
        and (torso_angle is None or float(torso_angle) <= MACHINE_TORSO_MAX_DEG)
        and (knee_bent or hip_near_knee_level or arms_up_seated_like)
    )


def _forward_machine_reach_pose(track: dict, zone_name: str, kps_xy, kps_cf, body_center) -> bool:
    if kps_xy is None or kps_cf is None:
        return False

    seated_pos_ok = _seated_machine_position_ok(track, zone_name, body_center)
    if not seated_pos_ok:
        return False

    shoulders_mid = _mean_visible_point(kps_xy, kps_cf, [5, 6])
    hips_mid = _mean_visible_point(kps_xy, kps_cf, [11, 12])
    _, bbox_h = _track_bbox_wh(track)
    bbox_h = max(1.0, float(bbox_h))

    eq_center = EQUIPMENT_ZONES[zone_name]["equipment_center"]
    visible_arm = False

    for shoulder_i, elbow_i, wrist_i in ((5, 7, 9), (6, 8, 10)):
        if shoulder_i >= len(kps_cf) or elbow_i >= len(kps_cf) or wrist_i >= len(kps_cf):
            continue
        if kps_cf[shoulder_i] < KP_MIN_CONF or kps_cf[wrist_i] < KP_MIN_CONF:
            continue

        shoulder = kps_xy[shoulder_i]
        wrist = kps_xy[wrist_i]
        elbow_bent = False
        if kps_cf[elbow_i] >= KP_MIN_CONF:
            elbow_ang = _angle_deg(kps_xy[shoulder_i], kps_xy[elbow_i], kps_xy[wrist_i])
            elbow_bent = 35.0 <= float(elbow_ang) <= 175.0

        reach_toward_equipment = abs(float(wrist[0]) - float(eq_center[0])) <= (abs(float(shoulder[0]) - float(eq_center[0])) + (bbox_h * MACHINE_REACH_X_EXTRA_FRAC))

        y_min = float(shoulder[1]) - (bbox_h * MACHINE_REACH_Y_ABOVE_FRAC)
        if hips_mid is not None:
            y_max = float(hips_mid[1]) + (bbox_h * MACHINE_REACH_Y_BELOW_FRAC)
        elif shoulders_mid is not None:
            y_max = float(shoulders_mid[1]) + (bbox_h * 0.55)
        else:
            y_max = float(shoulder[1]) + (bbox_h * 0.55)

        reach_band_ok = y_min <= float(wrist[1]) <= y_max

        if elbow_bent and reach_toward_equipment and reach_band_ok:
            visible_arm = True
            break

    return visible_arm


def classify_valid_machine_usage(track: dict, zone_name: str, kps_xy, kps_cf, posture_scores: dict, pose_conf: float):
    """Simplified presence-based validation (v3-simplified).

    For a condo-gym crowd detector the only question is:
        'Is someone physically at this equipment station?'

    Validity = person's body centre is close to the equipment centre.
    We still record the best posture label for the overlay, but it no
    longer gates the timer.  Seated / handle / arms checks are removed.
    """
    zone_usage = _zone_usage_state(track, zone_name)
    bbox_center = _track_box_center(track)
    x, y, w, h = _zone_bounds(zone_name)

    # ---- keypoint quality (kept for overlay info only) ----
    good_confs = []
    if kps_cf is not None:
        good_confs = [float(v) for v in kps_cf if float(v) >= KP_MIN_CONF]
    good_kp_count = len(good_confs)
    mean_kp_conf = (sum(good_confs) / len(good_confs)) if good_confs else 0.0

    top_posture = "unknown"
    top_score = 0.0
    if posture_scores:
        top_posture, top_score = max(posture_scores.items(), key=lambda kv: float(kv[1]))

    # ---- body-centre proximity (the only real gate) ----
    body_center = _mean_visible_point(kps_xy, kps_cf, [5, 6, 11, 12]) if kps_xy is not None else None
    if body_center is None:
        body_center = bbox_center

    eq_center = EQUIPMENT_ZONES[zone_name]["equipment_center"]
    body_dist_norm = 999.0
    if body_center is not None:
        body_dist_norm = _distance(body_center, eq_center) / max(1.0, float(min(w, h)))
    body_close = body_dist_norm <= MACHINE_BODY_CENTER_MAX_ZONE_FRAC

    # ---- simple validity: person is near the equipment ----
    raw_valid = bool(body_close)

    zone_usage["pose_good_kp_count"] = int(good_kp_count)
    zone_usage["pose_mean_kp_conf"] = float(mean_kp_conf)
    zone_usage["posture_label"] = top_posture
    zone_usage["posture_score"] = float(top_score)
    zone_usage["body_center_dist_norm"] = float(body_dist_norm)
    zone_usage["handle_contacts"] = 0
    zone_usage["debug_reason"] = "ok" if raw_valid else "body_far"

    stable_valid = _update_zone_usage_stability(zone_usage, raw_valid)
    return {
        "raw_valid": raw_valid,
        "stable_valid": stable_valid,
        "top_posture": top_posture,
        "top_score": float(top_score),
        "good_kp_count": int(good_kp_count),
        "mean_kp_conf": float(mean_kp_conf),
        "body_center_dist_norm": float(body_dist_norm),
        "handle_contacts": 0,
    }


def maybe_update_track_pose_usage(frame, pose_model, track: dict, zone_name: str, now: float, frame_i: int):
    zone_usage = _zone_usage_state(track, zone_name)

    if (frame_i - int(zone_usage.get("last_pose_update_f", -999999))) < POSE_EVERY_N:
        return zone_usage

    zone_usage["last_pose_update_f"] = int(frame_i)
    zone_usage["last_pose_update_t"] = float(now)

    if pose_model is None:
        return _set_zone_usage_invalid(track, zone_name, frame_i, now, "pose_model_missing")

    bbox = track.get("current_bbox")
    if bbox is None:
        return _set_zone_usage_invalid(track, zone_name, frame_i, now, "missing_bbox")

    x1, y1, x2, y2 = bbox
    crop, crop_origin = _clamp_crop(frame, x1, y1, x2, y2, margin_ratio=POSE_MARGIN)
    if crop is None or crop.size == 0:
        return _set_zone_usage_invalid(track, zone_name, frame_i, now, "bad_crop")

    try:
        pose_res = pose_model(
            crop,
            imgsz=POSE_IMG_SIZE,
            conf=0.20,
            verbose=False
        )[0]
    except Exception as e:
        print(f"[POSE] inference failed for zone={zone_name}: {e}", flush=True)
        return _set_zone_usage_invalid(track, zone_name, frame_i, now, "pose_infer_fail")

    if pose_res.keypoints is None:
        return _set_zone_usage_invalid(track, zone_name, frame_i, now, "no_keypoints")

    try:
        kps_xy_all = pose_res.keypoints.xy.cpu().numpy()
    except Exception:
        return _set_zone_usage_invalid(track, zone_name, frame_i, now, "bad_keypoints_xy")

    if kps_xy_all is None or len(kps_xy_all) == 0:
        return _set_zone_usage_invalid(track, zone_name, frame_i, now, "empty_keypoints")

    kps_cf_all = None
    try:
        if pose_res.keypoints.conf is not None:
            kps_cf_all = pose_res.keypoints.conf.cpu().numpy()
    except Exception:
        kps_cf_all = None

    best_idx = 0
    if pose_res.boxes is not None and len(pose_res.boxes) > 0:
        try:
            pose_boxes = pose_res.boxes.xyxy.cpu().numpy()
            crop_center = np.array([crop.shape[1] * 0.5, crop.shape[0] * 0.5], dtype=np.float32)
            best_score = None
            for i, pb in enumerate(pose_boxes):
                bx1, by1, bx2, by2 = pb
                pc = np.array([(bx1 + bx2) * 0.5, (by1 + by2) * 0.5], dtype=np.float32)
                area = max(1.0, float(max(0.0, bx2 - bx1) * max(0.0, by2 - by1)))
                dist = float(np.linalg.norm(pc - crop_center))
                score = dist - 0.0001 * area
                if best_score is None or score < best_score:
                    best_score = score
                    best_idx = i
        except Exception:
            best_idx = 0

    kps_xy = np.asarray(kps_xy_all[best_idx], dtype=np.float32)
    if kps_cf_all is not None and len(kps_cf_all) > best_idx:
        kps_cf = np.asarray(kps_cf_all[best_idx], dtype=np.float32)
    else:
        kps_cf = np.ones((kps_xy.shape[0],), dtype=np.float32)

    origin_x, origin_y = crop_origin
    kps_xy_global = kps_xy.copy()
    kps_xy_global[:, 0] += float(origin_x)
    kps_xy_global[:, 1] += float(origin_y)

    posture_scores_raw, posture_conf = classify_posture_from_kps(
        kps_xy,
        kps_cf,
        bbox_wh=_track_bbox_wh(track),
    )
    track["posture_scores"] = ema_update_scores(track["posture_scores"], posture_scores_raw, POSTURE_EMA_BETA)
    track["posture_conf"] = float(posture_conf)

    classify_valid_machine_usage(
        track,
        zone_name,
        kps_xy_global,
        kps_cf,
        track["posture_scores"],
        posture_conf,
    )
    return zone_usage


def is_owner_eligible_track(track: dict, zone_name: str, now: float) -> bool:
    if track is None:
        return False
    if _track_is_stale(track, now):
        return False
    if not track["zones"][zone_name]["inside"]:
        return False
    # FIX 1: Accept both "still" and "moving" so that exercises like push-ups
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
        # FIX 1b: Prefer "still" people over "moving" ones (sort key 0 vs 1)
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


def _release_zone_owner(owner_state: dict, now: float, completed_session_times: list = None):
    _pause_zone_owner_session(owner_state, now)
    _finalize_zone_owner_session(owner_state, now, completed_session_times)
    owner_state["current_owner_id"] = None
    owner_state["owner_since_t"] = None
    owner_state["owner_not_still_since_t"] = None
    owner_state["owner_invalid_usage_since_t"] = None
    owner_state["is_valid_machine_usage"] = False


def get_zone_owner_pose_valid(track: dict, zone_name: str, now: float) -> bool:
    zone_usage = _zone_usage_state(track, zone_name)
    last_pose_update_t = float(zone_usage.get("last_pose_update_t", 0.0))
    if (float(now) - last_pose_update_t) > POSE_STALE_SEC:
        return False
    return bool(zone_usage.get("stable_valid", False))


def update_zone_owner_state(owner_state: dict, tracks: dict, zone_name: str, now: float, completed_session_times: list = None):
    next_candidate_id, eligible_ids, multi_person_detected = select_zone_owner_candidate(tracks, zone_name, now)
    owner_state["multi_person_detected"] = bool(multi_person_detected)

    owner_id = owner_state.get("current_owner_id")
    owner_track = tracks.get(owner_id) if owner_id is not None else None

    if owner_id is not None and owner_track is not None:
        if (not owner_track["zones"][zone_name]["inside"]) or _track_is_stale(owner_track, now):
            _release_zone_owner(owner_state, now, completed_session_times)
        else:
            # Simplified: any owner who is inside the zone is "active",
            # regardless of still/moving state.  For a crowd detector we
            # only care that someone is present at the equipment.
            owner_active = True   # already checked "inside" above

            if owner_active:
                owner_state["owner_not_still_since_t"] = None
            else:
                if owner_state.get("owner_not_still_since_t") is None:
                    owner_state["owner_not_still_since_t"] = float(now)
                _pause_zone_owner_session(owner_state, now)
                owner_state["is_valid_machine_usage"] = False
                if (float(now) - float(owner_state["owner_not_still_since_t"])) > OWNER_STILL_GRACE_SEC:
                    _release_zone_owner(owner_state, now, completed_session_times)

            if owner_state.get("current_owner_id") is not None and owner_active:
                usage_valid = get_zone_owner_pose_valid(owner_track, zone_name, now)
                owner_state["is_valid_machine_usage"] = bool(usage_valid)

                if usage_valid:
                    owner_state["owner_invalid_usage_since_t"] = None
                    _start_or_resume_zone_owner_session(owner_state, now)
                    return owner_state["current_owner_id"]

                _pause_zone_owner_session(owner_state, now)
                if owner_state.get("owner_invalid_usage_since_t") is None:
                    owner_state["owner_invalid_usage_since_t"] = float(now)
                if (float(now) - float(owner_state["owner_invalid_usage_since_t"])) <= OWNER_USAGE_GRACE_SEC:
                    return owner_state["current_owner_id"]

                _release_zone_owner(owner_state, now, completed_session_times)

    if owner_state.get("current_owner_id") is None and next_candidate_id is not None:
        owner_state["current_owner_id"] = int(next_candidate_id)
        owner_state["owner_since_t"] = float(now)
        owner_state["owner_not_still_since_t"] = None
        owner_state["owner_invalid_usage_since_t"] = None
        owner_state["is_valid_machine_usage"] = False

        new_owner_track = tracks.get(next_candidate_id)
        if new_owner_track is not None and get_zone_owner_pose_valid(new_owner_track, zone_name, now):
            owner_state["is_valid_machine_usage"] = True
            _start_or_resume_zone_owner_session(owner_state, now)

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
        "avg_completed_session_duration_sec",
        "current_longest_usage_time_sec",
        "estimated_waiting_time_sec",
    ]


def write_zone_snapshot_rows(zone_csv_w, now: float, tracks: dict, zone_unique_ids: dict, zone_live_timers: dict, zone_owner_states: dict, zone_completed_session_times: dict):
    if zone_csv_w is None:
        return

    timestamp_txt = datetime.fromtimestamp(now).strftime("%Y-%m-%d %H:%M:%S")
    for zone_name in EQUIPMENT_ZONES:
        ids_now = sorted(current_ids_in_zone(tracks, zone_name))
        occupied_sec = cumulative_zone_usage_sec(zone_name, zone_owner_states, zone_completed_session_times, now)
        owner_state = zone_owner_states[zone_name]
        owner_id = owner_state["current_owner_id"]
        current_session_sec = current_zone_owner_session_elapsed(owner_state, now)
        avg_completed = compute_zone_avg_session_duration_sec(zone_name, zone_completed_session_times)
        estimated_waiting = compute_zone_estimated_waiting_time_sec(
            zone_name, owner_id, zone_completed_session_times, zone_owner_states, now
        )
        zone_csv_w.writerow([
            f"{float(now):.3f}",
            timestamp_txt,
            zone_name,
            len(ids_now),
            len(zone_unique_ids[zone_name]),
            "|".join(str(tid) for tid in ids_now),
            f"{float(occupied_sec):.3f}",
            fmt_hhmmss(occupied_sec),
            f"{float(occupied_sec):.3f}",
            fmt_hhmmss(occupied_sec),
            "" if owner_id is None else str(owner_id),
            current_zone_session_start_time_text(owner_state),
            f"{current_session_sec:.3f}",
            int(bool(owner_state.get("is_valid_machine_usage", False))),
            int(bool(owner_state.get("multi_person_detected", False))),
            f"{float(owner_state.get('last_completed_session_duration_sec', 0.0)):.3f}",
            int(owner_state.get("completed_sessions_count", 0)),
            f"{float(avg_completed):.3f}",
            f"{current_session_sec:.3f}",
            f"{estimated_waiting:.3f}",
        ])


def open_zone_record(zone_csv_path: str, zone_records: list, open_zone_records: dict,
                     now, zone_name, tid, current_people, unique_people, state, conf_ema):
    return


def close_zone_record(zone_csv_path: str, zone_records: list, open_zone_records: dict,
                      now, close_reason, zone_name, tid, dwell_sec,
                      current_people, unique_people, state, conf_ema):
    return


# ----------------------------
# pose helper functions
# ----------------------------
def _clamp_crop(frame, x1, y1, x2, y2, margin_ratio: float = 0.0):
    """
    Crop with optional margin around bbox.
    margin_ratio=0.18 expands bbox by ~18% on each side.
    """
    h, w = frame.shape[:2]
    bw = float(x2 - x1)
    bh = float(y2 - y1)
    mx = margin_ratio * bw
    my = margin_ratio * bh

    x1 = int(x1 - mx); x2 = int(x2 + mx)
    y1 = int(y1 - my); y2 = int(y2 + my)

    x1 = max(0, min(w - 1, x1)); x2 = max(0, min(w, x2))
    y1 = max(0, min(h - 1, y1)); y2 = max(0, min(h, y2))
    if x2 <= x1 or y2 <= y1:
        return None, None
    return frame[y1:y2, x1:x2], (x1, y1)


def _angle_deg(a, b, c):
    bax, bay = a[0] - b[0], a[1] - b[1]
    bcx, bcy = c[0] - b[0], c[1] - b[1]
    dot = bax * bcx + bay * bcy
    na = math.hypot(bax, bay) + 1e-6
    nc = math.hypot(bcx, bcy) + 1e-6
    cosv = max(-1.0, min(1.0, dot / (na * nc)))
    return math.degrees(math.acos(cosv))


def ema_update_scores(old_scores: dict, new_scores: dict, beta: float):
    out = {}
    for k, nv in new_scores.items():
        ov = float(old_scores.get(k, nv))
        out[k] = beta * ov + (1.0 - beta) * float(nv)
    return out

def classify_posture_from_kps(kps_xy, kps_cf, bbox_wh):
    """
    Rule-based posture / action classifier from 2D keypoints.

    Returns soft scores over:
      standing / sitting / lying / plank / pushup / squat / lunge / arms_up / using_phone

    The rules are intentionally geometry-first:
      - horizontal body -> lying / plank / pushup
      - upright body -> standing / sitting / squat / lunge / arms_up / using_phone

    Notes:
      - pushup is inferred as a pushup-like frame from plank geometry plus bent elbows.
        Full repetition detection still benefits from temporal smoothing outside this function.
      - using_phone is only a heuristic based on wrist-near-face / chest + bent-elbow cues.
    """
    w, h = bbox_wh
    w = float(max(1.0, w))
    h = float(max(1.0, h))
    aspect = w / h

    # COCO indices
    NOSE, L_EYE, R_EYE, L_EAR, R_EAR = 0, 1, 2, 3, 4
    L_SH, R_SH = 5, 6
    L_EL, R_EL = 7, 8
    L_WR, R_WR = 9, 10
    L_HIP, R_HIP = 11, 12
    L_KNE, R_KNE = 13, 14
    L_ANK, R_ANK = 15, 16

    LABELS = ["standing", "sitting", "lying", "plank", "pushup", "squat", "lunge", "arms_up", "using_phone"]

    def zero_scores():
        return {k: 0.0 for k in LABELS}

    def ok(i):
        return (kps_cf is not None) and (i < len(kps_cf)) and (float(kps_cf[i]) >= KP_MIN_CONF)

    def pt(i):
        return np.asarray(kps_xy[i], dtype=np.float32)

    def mid(i, j):
        return (pt(i) + pt(j)) * 0.5

    def ang(a, b, c):
        return _angle_deg(pt(a), pt(b), pt(c))

    def clamp01(v):
        return max(0.0, min(1.0, float(v)))

    def mean_non_none(vals):
        xs = [float(v) for v in vals if v is not None]
        return (sum(xs) / len(xs)) if xs else None

    def norm_dist(a, b):
        return float(np.linalg.norm(np.asarray(a, dtype=np.float32) - np.asarray(b, dtype=np.float32)) / h)

    used = []

    # ----------------------------
    # Core body geometry
    # ----------------------------
    shoulders_mid = hips_mid = knees_mid = ankles_mid = None
    torso_angle = None  # 0 = vertical, 90 = horizontal

    if ok(L_SH) and ok(R_SH):
        shoulders_mid = mid(L_SH, R_SH)
        used += [kps_cf[L_SH], kps_cf[R_SH]]
    if ok(L_HIP) and ok(R_HIP):
        hips_mid = mid(L_HIP, R_HIP)
        used += [kps_cf[L_HIP], kps_cf[R_HIP]]
    if ok(L_KNE) and ok(R_KNE):
        knees_mid = mid(L_KNE, R_KNE)
        used += [kps_cf[L_KNE], kps_cf[R_KNE]]
    elif ok(L_KNE):
        knees_mid = pt(L_KNE)      # fallback: use single visible knee
        used += [kps_cf[L_KNE]]
    elif ok(R_KNE):
        knees_mid = pt(R_KNE)      # fallback: use single visible knee
        used += [kps_cf[R_KNE]]
    if ok(L_ANK) and ok(R_ANK):
        ankles_mid = mid(L_ANK, R_ANK)
        used += [kps_cf[L_ANK], kps_cf[R_ANK]]

    if shoulders_mid is not None and hips_mid is not None:
        dx = abs(float(shoulders_mid[0] - hips_mid[0]))
        dy = abs(float(shoulders_mid[1] - hips_mid[1]))
        torso_angle = math.degrees(math.atan2(dx, dy + 1e-6))

    kneeL = kneeR = None
    if ok(L_HIP) and ok(L_KNE) and ok(L_ANK):
        kneeL = ang(L_HIP, L_KNE, L_ANK)
        used += [kps_cf[L_KNE], kps_cf[L_ANK]]
    if ok(R_HIP) and ok(R_KNE) and ok(R_ANK):
        kneeR = ang(R_HIP, R_KNE, R_ANK)
        used += [kps_cf[R_KNE], kps_cf[R_ANK]]

    elbL = elbR = None
    if ok(L_SH) and ok(L_EL) and ok(L_WR):
        elbL = ang(L_SH, L_EL, L_WR)
        used += [kps_cf[L_EL], kps_cf[L_WR]]
    if ok(R_SH) and ok(R_EL) and ok(R_WR):
        elbR = ang(R_SH, R_EL, R_WR)
        used += [kps_cf[R_EL], kps_cf[R_WR]]

    knee_mean = mean_non_none([kneeL, kneeR])
    elbow_mean = mean_non_none([elbL, elbR])

    knee_asym = None
    if kneeL is not None and kneeR is not None:
        knee_asym = abs(float(kneeL) - float(kneeR))

    # Straightness / horizontal support helpers
    body_line_err = None
    if shoulders_mid is not None and hips_mid is not None and ankles_mid is not None:
        body_line_err = abs(float(shoulders_mid[1] - hips_mid[1])) + abs(float(hips_mid[1] - ankles_mid[1]))
        body_line_err /= h

    # hip_low: hips are at roughly knee level or below — key seated cue.
    # Uses single-knee fallback so gym-machine occlusion doesn't break this.
    # Margin widened to 0.15 * h to catch natural seated postures on chairs/benches.
    hip_low = False
    if hips_mid is not None and knees_mid is not None:
        hip_low = float(hips_mid[1]) >= (float(knees_mid[1]) - 0.15 * h)

    # Compact torso cue: seated people have a shorter shoulder-to-hip span
    # relative to bbox height than standing people.
    torso_compact = False
    if shoulders_mid is not None and hips_mid is not None:
        torso_len_norm = abs(float(shoulders_mid[1]) - float(hips_mid[1])) / h
        torso_compact = torso_len_norm < 0.32

    upright_like = (torso_angle is not None and torso_angle < 35.0)
    seated_upright_like = (torso_angle is not None and torso_angle < 40.0)
    horizontal_like = ((torso_angle is not None and torso_angle > 65.0) or (aspect > 1.35))

    # ----------------------------
    # Wrist / face / chest helpers
    # ----------------------------
    face_pts = []
    for i in (NOSE, L_EYE, R_EYE, L_EAR, R_EAR):
        if ok(i):
            face_pts.append(pt(i))
            used.append(kps_cf[i])
    face_center = np.mean(np.asarray(face_pts, dtype=np.float32), axis=0) if face_pts else None

    chest_center = None
    if shoulders_mid is not None and hips_mid is not None:
        chest_center = (shoulders_mid * 0.65) + (hips_mid * 0.35)
    elif shoulders_mid is not None:
        chest_center = shoulders_mid.copy()

    wrists_above_shoulders = 0
    wrists_above_head = 0
    wrist_near_face = False
    wrist_near_chest = False
    bent_arm_near_face = False

    for wrist_i, shoulder_i, elbow_ang in ((L_WR, L_SH, elbL), (R_WR, R_SH, elbR)):
        if ok(wrist_i):
            wrist = pt(wrist_i)

            if ok(shoulder_i):
                shoulder = pt(shoulder_i)
                if float(wrist[1]) < float(shoulder[1]) - 0.10 * h:
                    wrists_above_shoulders += 1
                if float(wrist[1]) < float(shoulder[1]) - 0.28 * h:
                    wrists_above_head += 1

            if face_center is not None:
                d_face = norm_dist(wrist, face_center)
                if d_face < 0.18:
                    wrist_near_face = True
                if d_face < 0.22 and elbow_ang is not None and 35.0 < float(elbow_ang) < 145.0:
                    bent_arm_near_face = True

            if chest_center is not None:
                d_chest = norm_dist(wrist, chest_center)
                if d_chest < 0.18:
                    wrist_near_chest = True

    arms_up_strong = wrists_above_shoulders >= 2
    phone_like = bool((upright_like or (knee_mean is not None and knee_mean < 145.0)) and not arms_up_strong and (bent_arm_near_face or wrist_near_chest))

    # ----------------------------
    # Rule scores
    # ----------------------------
    scores = zero_scores()

    # HORIZONTAL BRANCH
    if horizontal_like:
        pushup_score = 0.0
        plank_score = 0.0
        lying_score = 0.0

        # Geometry
        if torso_angle is not None:
            pushup_score += 2.0 if torso_angle > 68.0 else 1.0
            plank_score += 2.0 if torso_angle > 68.0 else 1.0
            lying_score += 2.0 if torso_angle > 70.0 else 1.0

        if aspect > 1.35:
            lying_score += 1.5
            plank_score += 0.5
            pushup_score += 0.5

        if body_line_err is not None:
            if body_line_err < 0.18:
                plank_score += 2.0
                pushup_score += 1.5
            elif body_line_err < 0.28:
                plank_score += 1.0
                pushup_score += 0.8
            else:
                lying_score += 1.0

        if knee_mean is not None:
            if knee_mean > 150.0:
                plank_score += 1.5
                pushup_score += 1.2
            elif knee_mean > 138.0:
                plank_score += 0.6
                pushup_score += 0.5
            else:
                lying_score += 0.8

        if elbow_mean is not None:
            if elbow_mean < 135.0:
                pushup_score += 2.6
            elif elbow_mean > 155.0:
                plank_score += 1.8
            else:
                lying_score += 0.6

        if face_center is not None and shoulders_mid is not None and hips_mid is not None:
            # face higher than chest in image is less lying-like for this camera view
            if float(face_center[1]) < float(shoulders_mid[1]) + 0.08 * h:
                plank_score += 0.3
                pushup_score += 0.3

        # Convert to soft distribution
        total = max(pushup_score + plank_score + lying_score, 1e-6)
        scores["pushup"] = pushup_score / total
        scores["plank"] = plank_score / total
        scores["lying"] = lying_score / total

    else:
        # UPRIGHT / SEATED BRANCH

        # Arms-up
        arms_up_score = 0.0
        if wrists_above_shoulders >= 2:
            arms_up_score += 3.0
        elif wrists_above_shoulders == 1:
            arms_up_score += 1.0
        if wrists_above_head >= 1:
            arms_up_score += 1.0
        if upright_like:
            arms_up_score += 0.5

        # Using phone
        using_phone_score = 0.0
        if phone_like:
            using_phone_score += 3.0
        if wrist_near_face:
            using_phone_score += 1.0
        if wrist_near_chest:
            using_phone_score += 0.8
        if elbow_mean is not None and 35.0 < elbow_mean < 145.0:
            using_phone_score += 0.6
        if knee_mean is not None and knee_mean < 145.0:
            using_phone_score += 0.4  # seated phone
        if arms_up_strong:
            using_phone_score -= 1.0

        # Lunge
        lunge_score = 0.0
        if seated_upright_like:
            lunge_score += 0.5
        if kneeL is not None and kneeR is not None:
            if min(kneeL, kneeR) < 125.0 and max(kneeL, kneeR) > 145.0:
                lunge_score += 3.0
            elif min(kneeL, kneeR) < 135.0 and max(kneeL, kneeR) > 150.0:
                lunge_score += 2.0
            if knee_asym is not None and knee_asym > 25.0:
                lunge_score += 1.5
        if ok(L_ANK) and ok(R_ANK):
            ankle_dx_norm = abs(float(pt(L_ANK)[0] - pt(R_ANK)[0])) / h
            if ankle_dx_norm > 0.18:
                lunge_score += 0.8

        # Squat
        squat_score = 0.0
        if torso_angle is not None and torso_angle < 40.0:
            squat_score += 1.0
        if kneeL is not None and kneeR is not None:
            if kneeL < 130.0 and kneeR < 130.0:
                squat_score += 3.0
            elif kneeL < 140.0 and kneeR < 140.0:
                squat_score += 1.8
            if knee_asym is not None and knee_asym < 20.0:
                squat_score += 1.0
        if hip_low:
            squat_score += 1.0

        # Sitting
        # KEY FIX: hip_low is now the dominant cue for sitting (+2.5).
        # Previously it was only +1.2, and required BOTH knees to score well.
        # Now a single visible bent knee is enough to contribute, and hip_low
        # alone can push sitting above standing when the torso is upright.
        sitting_score = 0.0
        if torso_angle is not None and torso_angle < 42.0:
            sitting_score += 1.2
        if hip_low:
            sitting_score += 2.5   # strongest single cue for seated posture
        # Both knees clearly bent: big bonus
        if kneeL is not None and kneeR is not None:
            if 65.0 < kneeL < 140.0 and 65.0 < kneeR < 140.0:
                sitting_score += 2.0
            elif knee_mean is not None and knee_mean < 145.0:
                sitting_score += 1.0
        # Single visible knee bent: still reward it (gym machines often occlude one leg)
        elif kneeL is not None and 65.0 < kneeL < 140.0:
            sitting_score += 1.5
        elif kneeR is not None and 65.0 < kneeR < 140.0:
            sitting_score += 1.5
        # Compact torso: standing people have a taller visible torso span
        if torso_compact:
            sitting_score += 0.8
        if knee_asym is not None and knee_asym < 22.0:
            sitting_score += 0.4

        # Standing
        # KEY FIX: penalise standing when hip_low is True (was only +0.8 for not hip_low).
        # Also requires BOTH knees clearly straight to earn the big knee bonus.
        standing_score = 0.0
        if torso_angle is not None and torso_angle < 25.0:
            standing_score += 2.0
        elif torso_angle is not None and torso_angle < 35.0:
            standing_score += 1.0
        if kneeL is not None and kneeR is not None:
            if kneeL > 150.0 and kneeR > 150.0:
                standing_score += 2.5
            elif knee_mean is not None and knee_mean > 145.0:
                standing_score += 1.2
        if not hip_low:
            standing_score += 1.2   # high hips support standing
        else:
            standing_score -= 1.5   # low hips strongly oppose standing
        if arms_up_strong:
            standing_score += 0.4
        if phone_like:
            standing_score += 0.2
        standing_score = max(0.0, standing_score)

        # Resolve competing lower-body classes with geometry-first preference
        if arms_up_score >= 3.0:
            scores["arms_up"] = 0.80
            scores["standing"] = 0.15 if standing_score >= sitting_score else 0.05
            scores["sitting"] = 0.10 if sitting_score > standing_score else 0.05
        elif using_phone_score >= 3.0:
            if knee_mean is not None and knee_mean < 145.0:
                scores["using_phone"] = 0.78
                scores["sitting"] = 0.15
                scores["standing"] = 0.07
            else:
                scores["using_phone"] = 0.82
                scores["standing"] = 0.13
                scores["sitting"] = 0.05
        elif lunge_score >= max(squat_score + 0.4, sitting_score + 0.8, 3.2):
            scores["lunge"] = 0.85
            scores["standing"] = 0.10
            scores["sitting"] = 0.05
        elif squat_score >= max(sitting_score + 0.6, lunge_score + 0.2, 3.0):
            scores["squat"] = 0.85
            scores["sitting"] = 0.10
            scores["standing"] = 0.05
        elif sitting_score >= max(standing_score - 0.3, 2.0):
            # KEY FIX: lowered threshold from (standing + 0.2, 2.6) to (standing - 0.3, 2.0).
            # Sitting no longer needs to clearly beat standing — it just needs to be credible.
            scores["sitting"] = 0.72
            scores["standing"] = 0.23
            scores["lying"] = 0.05
        else:
            scores["standing"] = 0.85
            scores["sitting"] = 0.10
            scores["lying"] = 0.05

    # Safety fallback
    total_prob = float(sum(scores.values()))
    if total_prob <= 1e-6:
        scores = zero_scores()
        scores["standing"] = 0.85
        scores["sitting"] = 0.10
        scores["lying"] = 0.05
        total_prob = 1.0

    # Normalize cleanly
    for k in list(scores.keys()):
        scores[k] = clamp01(scores[k] / total_prob)

    conf = float(sum(float(v) for v in used) / len(used)) if used else 0.3
    return scores, conf

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
    track["motion_scores"] = ema_update_scores(track["motion_scores"], new_scores, MOTION_EMA_BETA)
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
    pose_model = None
    try:
        pose_model = YOLO(POSE_MODEL_PATH, task="pose")
        print(f"Pose model loaded: {POSE_MODEL_PATH}")
    except Exception as e:
        pose_model = None
        print(f"[POSE] disabled: could not load pose model: {e}", flush=True)

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
                    hx1, hy1, hx2, hy2 = _equipment_handle_rect(zone_name, support=False)
                    cv2.rectangle(frame, (int(hx1), int(hy1)), (int(hx2), int(hy2)), color, 1)

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
                            "posture_scores": {
                                "standing": 0.20, "sitting": 0.15, "lying": 0.15,
                                "plank": 0.10, "pushup": 0.10, "squat": 0.10, "lunge": 0.10,
                                "arms_up": 0.05, "using_phone": 0.05
                            },
                            "posture_conf": 0.0,
                            "zone_usage": {zn: _make_track_zone_usage_state() for zn in EQUIPMENT_ZONES},
                            "motion_hist": deque(maxlen=MOTION_HIST_LEN),
                            "motion_scores": {"moving": 0.5, "still": 0.5},
                            "motion_speed": 0.0,
                            "state_hist": deque(maxlen=STATE_VOTE_LEN),
                            "state_final": "still",
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
                    if t["state_hist"]:
                        t["state_final"] = Counter(t["state_hist"]).most_common(1)[0][0]
                    state = t["state_final"]

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
                        posture_txt = zone_usage.get("posture_label", "unknown")
                        valid_txt = "Y" if zone_usage.get("stable_valid", False) else "N"
                        cv2.rectangle(frame, (x1, y1), (x2, y2), zone_color, 2)
                        reason_txt = str(zone_usage.get("debug_reason", ""))
                        if len(reason_txt) > 24:
                            reason_txt = reason_txt[:24]
                        track_lines = [
                            f"ID {tid} {zone_label} {session_txt}",
                            f"conf:{conf_txt}  state:{state}",
                            f"pose:{posture_txt}  use:{valid_txt}",
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
                next_candidate_id, _, _ = select_zone_owner_candidate(tracks, zone_name, now)

                pose_target_ids = []
                for target_id in (current_owner_id, next_candidate_id):
                    if target_id is None:
                        continue
                    if target_id in tracks and target_id not in pose_target_ids:
                        pose_target_ids.append(target_id)

                for target_id in pose_target_ids:
                    maybe_update_track_pose_usage(
                        frame,
                        pose_model,
                        tracks[target_id],
                        zone_name,
                        now,
                        frame_i,
                    )

                update_zone_owner_state(
                    zone_owner_states[zone_name],
                    tracks,
                    zone_name,
                    now,
                    zone_completed_session_times[zone_name]
                )

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
                        owner_reason = _zone_usage_state(tracks[owner_id], zone_name).get("debug_reason", "no_pose")
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
                        f"{statistics.mean(t_cap_ms):.3f}" if t_cap_ms else "0",
                        f"{statistics.mean(t_inf_ms):.3f}" if t_inf_ms else "0",
                        f"{statistics.mean(t_post_ms):.3f}" if t_post_ms else "0",
                        f"{statistics.mean(t_disp_ms):.3f}" if t_disp_ms else "0",
                        f"{cpu_pct:.3f}",
                        f"{rss_mb:.3f}",
                        f"{maxrss_mb:.3f}",
                        int(ctx.voluntary),
                        int(ctx.involuntary),
                        threads,
                        datetime.fromtimestamp(now).strftime("%Y-%m-%d %H:%M:%S"),
                    ])
                    csv_f.flush()

                write_zone_snapshot_rows(
                    zone_csv_w,
                    now,
                    tracks,
                    zone_unique_ids,
                    zone_live_timers,
                    zone_owner_states,
                    zone_completed_session_times
                )
                if zone_csv_f:
                    zone_csv_f.flush()

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

                payload = {
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
                            "people_now": zone_a_now,
                            "unique_ids_seen_in_zone": zone_a_unique,
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
                            "estimated_waiting_time_sec": round(zone_a_estimated_waiting_time_sec, 3)
                        },
                        "equipment_b": {
                            "people_now": zone_b_now,
                            "unique_ids_seen_in_zone": zone_b_unique,
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
                            "estimated_waiting_time_sec": round(zone_b_estimated_waiting_time_sec, 3)
                        }
                    }
                }

                mqtt_publish(payload)

            if key == ord("r"):
                SHOW_ROI = not SHOW_ROI
            elif key == ord("q"):
                break

    finally:
        final_owner_now = time.time()
        for zone_name in EQUIPMENT_ZONES:
            owner_state = zone_owner_states[zone_name]
            if owner_state["current_owner_id"] is not None:
                _release_zone_owner(owner_state, final_owner_now, zone_completed_session_times[zone_name])

        if zone_csv_path is not None and open_zone_records:
            final_now = time.time()
            for tid, zone_name in list(open_zone_records.keys()):
                t = tracks.get(tid)

                if zone_name == OUT_OF_ZONE_LABEL:
                    dwell_sec = live_open_record_elapsed(zone_records, open_zone_records, tid, OUT_OF_ZONE_LABEL, final_now)
                    current_people = len(current_ids_out_of_zone(tracks))
                    unique_people = len(out_of_zone_unique_ids)
                else:
                    dwell_sec = live_zone_dwell(t, zone_name, final_now) if t is not None else 0.0
                    current_people = len(current_ids_in_zone(tracks, zone_name))
                    unique_people = len(zone_unique_ids[zone_name])

                state = t.get("state_final", "unknown") if t is not None else "unknown"
                conf_ema = t.get("conf_ema", 0.0) if t is not None else 0.0
                close_zone_record(zone_csv_path, zone_records, open_zone_records,
                                  final_now, CSV_CLOSE_CLOSED, zone_name, tid, dwell_sec,
                                  current_people, unique_people, state, conf_ema)

        cap.release()
        if raw_video_writer is not None:
            raw_video_writer.release()
            if raw_video_path is not None:
                print(f"Raw video saved: {raw_video_path}")
        if not HEADLESS:
            cv2.destroyAllWindows()
        if csv_f:
            csv_f.close()
        if zone_csv_f:
            zone_csv_f.close()

        global mqtt_client
        if mqtt_client is not None:
            try:
                mqtt_client.loop_stop()
                mqtt_client.disconnect()
            except Exception:
                pass

        if t_loop_ms:
            avg_loop = statistics.mean(t_loop_ms)
            fps_est = 1000.0 / avg_loop if avg_loop > 0 else 0.0
            print("\n===== FINAL SUMMARY (rolling window) =====")
            print(f"FPS~{fps_est:.2f}")
            print(f"loop {_stage_stats_ms(t_loop_ms)}")
            print(f"cap  {_stage_stats_ms(t_cap_ms)}")
            print(f"inf  {_stage_stats_ms(t_inf_ms)}")
            print(f"post {_stage_stats_ms(t_post_ms)}")
            print(f"disp {_stage_stats_ms(t_disp_ms)}")
            print(f"cpu  {_stage_stats_ms(t_cpu_ms)}")
            for zone_name, zone_cfg in EQUIPMENT_ZONES.items():
                print(f"{zone_cfg['label']} unique IDs={len(zone_unique_ids[zone_name])}")
            if cpu_pct_samples:
                print(f"CPU% avg/p95={statistics.mean(cpu_pct_samples):.1f}/{_pctl(cpu_pct_samples,95):.1f}")
            if rss_mb_samples:
                print(f"RSS(MB) avg/p95={statistics.mean(rss_mb_samples):.1f}/{_pctl(rss_mb_samples,95):.1f}")
            print(f"Peak RSS (ru_maxrss)={_maxrss_mb():.1f} MB")
            print("Profile CSV:", CSV_PATH if LOG_CSV else "(disabled)")
            print("Zone CSV:", ZONE_CSV_PATH if LOG_CSV else "(disabled)")
            print("========================================\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Gym equipment-zone tracking + motion-first state logic + NCNN YOLO pose with profiling + eval.")
    sub = parser.add_subparsers(dest="mode", required=False)

    sub.add_parser("run", help="Run webcam equipment-zone tracking pipeline (default).")

    p_val = sub.add_parser("val", help="Run mAP validation for the NCNN DETECT model.")
    p_val.add_argument("--data", default="coco128.yaml", help="Dataset YAML (e.g., coco128.yaml).")
    p_val.add_argument("--imgsz", type=int, default=IMG_SIZE, help="Image size for validation.")
    p_val.add_argument("--device", default="cpu", help="Device string for Ultralytics val() (e.g., cpu, 0).")

    args = parser.parse_args()

    if args.mode == "val":
        run_validation(data=args.data, imgsz=args.imgsz, device=args.device)
    else:
        main()