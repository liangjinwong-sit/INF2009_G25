"""
GymPulse Dashboard  –  Flask + MQTT + SQLite backend
====================================================
Keeps the same dashboard UI and API shape as your CSV prototype,
but now listens for live MQTT payloads from RPi1 and stores history
into local SQLite on RPi3.

This version also auto-migrates older SQLite schemas by adding any
missing columns to the snapshots table at startup.
"""

from flask import Flask, jsonify, render_template_string, request
import json
import os
import sqlite3
import time
from threading import Lock, Thread
from datetime import datetime
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import paho.mqtt.client as mqtt

app = Flask(__name__)


def env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}

def load_simple_env_file(path: str):
    """Load KEY=VALUE pairs from a local env file if present.
    Existing environment variables win.
    """
    if not os.path.exists(path):
        return
    try:
        with open(path, 'r', encoding='utf-8') as f:
            for raw_line in f:
                line = raw_line.strip()
                if not line or line.startswith('#') or '=' not in line:
                    continue
                key, value = line.split('=', 1)
                key = key.strip()
                value = value.strip().strip("""'""").strip('"')
                if key:
                    os.environ.setdefault(key, value)
    except Exception as e:
        print(f'Env file load warning: {e}', flush=True)


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
load_simple_env_file(os.path.join(BASE_DIR, 'telegram.env'))


# ─── Config ────────────────────────────────────────────────────────────────────
MQTT_BROKER = os.getenv("MQTT_BROKER", "localhost")   # app.py runs on RPi3 with local Mosquitto
MQTT_PORT   = safe_int(os.getenv("MQTT_PORT", 1883), 1883) if "safe_int" in globals() else int(os.getenv("MQTT_PORT", "1883"))
MQTT_TOPIC  = os.getenv("MQTT_TOPIC", "gympulse/rpi1/vision/state")

DB_PATH = os.getenv("GYMPULSE_DB_PATH", "/home/jared/Desktop/Project/gympulse.db")

DEMO_MODE = env_bool("DEMO_MODE", False)

# Telegram alert config (RPi3 side)
# Use environment variables on the Pi instead of hardcoding the bot token in app.py.
TELEGRAM_ENABLED = env_bool("TELEGRAM_ENABLED", False)
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
TELEGRAM_ALERT_COOLDOWN_SEC = int(float(os.getenv("TELEGRAM_ALERT_COOLDOWN_SEC", "300")))
TELEGRAM_TEST_ENDPOINT_ENABLED = env_bool("TELEGRAM_TEST_ENDPOINT_ENABLED", True)
TELEGRAM_COMMANDS_ENABLED = env_bool("TELEGRAM_COMMANDS_ENABLED", True)
TELEGRAM_POLL_TIMEOUT_SEC = max(1, int(float(os.getenv("TELEGRAM_POLL_TIMEOUT_SEC", "25"))))
TELEGRAM_POLL_BOOT_SKIP_OLD = env_bool("TELEGRAM_POLL_BOOT_SKIP_OLD", True)

HISTORY_MINUTES = int(float(os.getenv("HISTORY_MINUTES", "15")))   # how many minutes of history to show in charts
CHART_THIN_SEC  = int(float(os.getenv("CHART_THIN_SEC", "5")))     # thin chart data: 1 point every N seconds
# ───────────────────────────────────────────────────────────────────────────────

latest_lock = Lock()
latest_state = {
    "profile": {},
    "zones": {
        "equipment_a": {},
        "equipment_b": {},
    }
}

telegram_alert_lock = Lock()
telegram_alert_state = {
    "last_crowd_status": "LOW",
    "last_sent_ts": 0.0,
}

telegram_bot_lock = Lock()
telegram_bot_state = {
    "next_update_offset": None,
}


# ─── Pure helpers ──────────────────────────────────────────────────────────────
def safe_float(v, d=0.0):
    try:
        return float(v)
    except Exception:
        return d


def safe_int(v, d=0):
    try:
        return int(float(v))
    except Exception:
        return d


def crowd_status_from_count(n: int) -> str:
    """Match vision script: <=2 LOW, <=5 MID, 6+ HIGH."""
    if n <= 2:
        return "LOW"
    if n <= 5:
        return "MID"
    return "HIGH"


def compute_freshness(ts_text: str) -> dict:
    """Return {text, level} where level is live|delay|stale|offline."""
    if not ts_text:
        return {"text": "No data", "level": "offline"}
    try:
        ts = datetime.strptime(ts_text, "%Y-%m-%d %H:%M:%S")
        age = (datetime.now() - ts).total_seconds()
        if age <= 2:
            return {"text": f"Live · {age:.0f}s ago", "level": "live"}
        if age <= 5:
            return {"text": f"Slight delay · {age:.0f}s", "level": "delay"}
        if age <= 30:
            return {"text": f"Stale · {age:.0f}s ago", "level": "stale"}
        return {"text": f"Offline · {age:.0f}s ago", "level": "offline"}
    except ValueError:
        return {"text": "Unknown", "level": "stale"}


def format_wait_seconds(sec: float) -> str:
    sec = max(0.0, safe_float(sec, 0.0))
    return f"{sec:.1f}s"


def _telegram_detail_to_text(detail) -> str:
    if isinstance(detail, (dict, list)):
        try:
            return json.dumps(detail, ensure_ascii=False)
        except Exception:
            return str(detail)
    return str(detail)


def telegram_api_post(method: str, params: dict, timeout_sec: float = 10.0):
    if not TELEGRAM_BOT_TOKEN:
        return False, "missing TELEGRAM_BOT_TOKEN"

    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/{method}"
        data = urlencode(params or {}).encode("utf-8")
        req = Request(url, data=data, method="POST")
        with urlopen(req, timeout=max(5.0, float(timeout_sec))) as resp:
            body = resp.read().decode("utf-8", errors="ignore")
        try:
            parsed = json.loads(body)
        except Exception:
            parsed = body
        ok = isinstance(parsed, dict) and bool(parsed.get("ok"))
        return ok, parsed
    except HTTPError as e:
        body = e.read().decode("utf-8", errors="ignore")
        print(f"Telegram {method} HTTP error:", body, flush=True)
        try:
            return False, json.loads(body)
        except Exception:
            return False, body
    except URLError as e:
        print(f"Telegram {method} URL error:", e, flush=True)
        return False, str(e)
    except Exception as e:
        print(f"Telegram {method} error:", e, flush=True)
        return False, str(e)


def send_telegram_message(text: str, chat_id=None):
    if not TELEGRAM_ENABLED:
        return False, "telegram disabled"
    if not TELEGRAM_BOT_TOKEN:
        return False, "missing TELEGRAM_BOT_TOKEN"

    target_chat_id = str(chat_id if chat_id is not None else TELEGRAM_CHAT_ID).strip()
    if not target_chat_id:
        return False, "missing TELEGRAM_CHAT_ID"

    ok, detail = telegram_api_post(
        "sendMessage",
        {
            "chat_id": target_chat_id,
            "text": text,
        },
        timeout_sec=10,
    )
    return ok, _telegram_detail_to_text(detail)


def build_telegram_status_message(title: str = "📊 GymPulse Current Status") -> str:
    profile = get_current_profile()
    zones = get_current_zones()
    za = zones.get("equipment_a", {})
    zb = zones.get("equipment_b", {})

    timestamp = profile.get("timestamp", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    total_people = safe_int(profile.get("total_now_ids", 0))
    crowd_status = (profile.get("crowd_status") or crowd_status_from_count(total_people)).upper()

    return (
        f"{title}\n"
        f"Crowd status: {crowd_status}\n"
        f"Total people: {total_people}\n"
        f"Zone A est. wait: {format_wait_seconds(za.get('estimated_waiting_time_sec', 0))}\n"
        f"Zone B est. wait: {format_wait_seconds(zb.get('estimated_waiting_time_sec', 0))}\n"
        f"Timestamp: {timestamp}"
    )


def build_telegram_test_message() -> str:
    return build_telegram_status_message("🧪 GymPulse Telegram Test")


def build_telegram_help_message() -> str:
    return (
        "🤖 GymPulse Bot Commands\n"
        "/status - show current gym status\n"
        "/now - same as /status\n"
        "/help - show this help"
    )


def telegram_chat_allowed(chat_id) -> bool:
    configured_chat_id = str(TELEGRAM_CHAT_ID).strip()
    if not configured_chat_id:
        return True
    return str(chat_id).strip() == configured_chat_id


def fetch_telegram_updates(offset=None, timeout_sec=None):
    if not TELEGRAM_ENABLED or not TELEGRAM_BOT_TOKEN:
        return []

    poll_timeout = TELEGRAM_POLL_TIMEOUT_SEC if timeout_sec is None else max(0, int(timeout_sec))
    params = {
        "timeout": poll_timeout,
        "allowed_updates": json.dumps(["message"]),
    }
    if offset is not None:
        params["offset"] = int(offset)

    ok, detail = telegram_api_post("getUpdates", params, timeout_sec=poll_timeout + 10)
    if not ok:
        print("Telegram getUpdates failed:", _telegram_detail_to_text(detail), flush=True)
        return []

    if isinstance(detail, dict) and isinstance(detail.get("result"), list):
        return detail["result"]
    return []


def handle_telegram_update(update: dict):
    message = update.get("message") or {}
    chat = message.get("chat") or {}
    chat_id = chat.get("id")
    text = (message.get("text") or "").strip()

    if not text or chat_id is None:
        return

    if not telegram_chat_allowed(chat_id):
        print(f"Ignoring Telegram message from unauthorized chat: {chat_id}", flush=True)
        return

    first_token = text.split()[0].strip().lower()
    if first_token.startswith("/") and "@" in first_token:
        first_token = first_token.split("@", 1)[0]

    if first_token in {"/status", "/now", "status", "now"}:
        ok, detail = send_telegram_message(build_telegram_status_message(), chat_id=chat_id)
        if not ok:
            print("Telegram status reply not sent:", detail, flush=True)
    elif first_token in {"/help", "/start", "help", "start"}:
        ok, detail = send_telegram_message(build_telegram_help_message(), chat_id=chat_id)
        if not ok:
            print("Telegram help reply not sent:", detail, flush=True)


def telegram_command_loop():
    next_offset = None

    if TELEGRAM_POLL_BOOT_SKIP_OLD:
        startup_updates = fetch_telegram_updates(timeout_sec=0)
        if startup_updates:
            next_offset = max(int(u.get("update_id", 0)) for u in startup_updates) + 1
            with telegram_bot_lock:
                telegram_bot_state["next_update_offset"] = next_offset

    while True:
        try:
            with telegram_bot_lock:
                saved_offset = telegram_bot_state.get("next_update_offset")
            if saved_offset is not None:
                next_offset = saved_offset

            updates = fetch_telegram_updates(offset=next_offset, timeout_sec=TELEGRAM_POLL_TIMEOUT_SEC)
            for update in updates:
                update_id = int(update.get("update_id", 0))
                next_offset = update_id + 1
                with telegram_bot_lock:
                    telegram_bot_state["next_update_offset"] = next_offset
                handle_telegram_update(update)
        except Exception as e:
            print("Telegram command loop error:", e, flush=True)
            time.sleep(2)


def start_telegram_command_listener():
    if not TELEGRAM_ENABLED:
        print("Telegram command listener disabled: TELEGRAM_ENABLED is false", flush=True)
        return None
    if not TELEGRAM_COMMANDS_ENABLED:
        print("Telegram command listener disabled: TELEGRAM_COMMANDS_ENABLED is false", flush=True)
        return None
    if not TELEGRAM_BOT_TOKEN:
        print("Telegram command listener disabled: missing bot token", flush=True)
        return None

    t = Thread(target=telegram_command_loop, name="telegram-command-loop", daemon=True)
    t.start()
    print("Telegram command listener started", flush=True)
    return t


def maybe_send_telegram_alert(payload: dict):
    zones = payload.get("zones", {})
    za = zones.get("equipment_a", {})
    zb = zones.get("equipment_b", {})

    total_people = safe_int(payload.get("total_now_ids", 0))
    crowd_status = (payload.get("crowd_status") or crowd_status_from_count(total_people)).upper()
    now_ts = time.time()

    with telegram_alert_lock:
        prev_status = telegram_alert_state["last_crowd_status"]
        last_sent_ts = telegram_alert_state["last_sent_ts"]
        telegram_alert_state["last_crowd_status"] = crowd_status

    if crowd_status != "HIGH":
        return

    if prev_status == "HIGH":
        return

    if (now_ts - last_sent_ts) < TELEGRAM_ALERT_COOLDOWN_SEC:
        return

    message = (
        "🚨 GymPulse Alert\n"
        f"Crowd status: HIGH\n"
        f"Total people: {total_people}\n"
        f"Zone A est. wait: {format_wait_seconds(za.get('estimated_waiting_time_sec', 0))}\n"
        f"Zone B est. wait: {format_wait_seconds(zb.get('estimated_waiting_time_sec', 0))}\n"
        f"Timestamp: {payload.get('timestamp', '—')}"
    )

    ok, detail = send_telegram_message(message)
    if ok:
        with telegram_alert_lock:
            telegram_alert_state["last_sent_ts"] = now_ts
    else:
        print("Telegram alert not sent:", detail, flush=True)


# ─── DB init / migration / save ────────────────────────────────────────────────
def _table_columns(conn, table_name: str):
    rows = conn.execute(f"PRAGMA table_info({table_name})").fetchall()
    return {row[1] for row in rows}


def _ensure_column(conn, table_name: str, col_name: str, col_type: str):
    cols = _table_columns(conn, table_name)
    if col_name not in cols:
        conn.execute(f"ALTER TABLE {table_name} ADD COLUMN {col_name} {col_type}")


def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")

    conn.execute("""
        CREATE TABLE IF NOT EXISTS snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts REAL,
            timestamp TEXT,
            crowd_status TEXT,
            total_now_ids INTEGER,

            fps_est REAL,
            inf_avg_ms REAL,
            loop_avg_ms REAL,
            loop_p95_ms REAL,
            loop_p99_ms REAL,
            cap_avg_ms REAL,
            post_avg_ms REAL,
            disp_avg_ms REAL,
            cpu_pct REAL,
            rss_mb REAL,

            zone_a_people_now INTEGER,
            zone_a_unique_ids INTEGER,
            zone_a_tracked_ids TEXT,
            zone_a_timer_sec REAL,
            zone_a_timer_hhmmss TEXT,
            zone_a_zone_occupied_duration_sec REAL,
            zone_a_zone_occupied_duration_hhmmss TEXT,
            zone_a_current_owner_id TEXT,
            zone_a_current_session_start_time TEXT,
            zone_a_current_session_duration_sec REAL,
            zone_a_is_valid_machine_usage INTEGER,
            zone_a_multi_person_detected INTEGER,
            zone_a_last_completed_session_duration_sec REAL,
            zone_a_completed_sessions_count INTEGER,
            zone_a_avg_completed_session_duration_sec REAL,
            zone_a_avg_individual_usage_time_sec REAL,
            zone_a_current_longest_usage_time_sec REAL,
            zone_a_estimated_waiting_time_sec REAL,

            zone_b_people_now INTEGER,
            zone_b_unique_ids INTEGER,
            zone_b_tracked_ids TEXT,
            zone_b_timer_sec REAL,
            zone_b_timer_hhmmss TEXT,
            zone_b_zone_occupied_duration_sec REAL,
            zone_b_zone_occupied_duration_hhmmss TEXT,
            zone_b_current_owner_id TEXT,
            zone_b_current_session_start_time TEXT,
            zone_b_current_session_duration_sec REAL,
            zone_b_is_valid_machine_usage INTEGER,
            zone_b_multi_person_detected INTEGER,
            zone_b_last_completed_session_duration_sec REAL,
            zone_b_completed_sessions_count INTEGER,
            zone_b_avg_completed_session_duration_sec REAL,
            zone_b_avg_individual_usage_time_sec REAL,
            zone_b_current_longest_usage_time_sec REAL,
            zone_b_estimated_waiting_time_sec REAL,

            raw_json TEXT
        )
    """)

    # Auto-upgrade older DB schemas
    required_cols = {
        "ts": "REAL",
        "timestamp": "TEXT",
        "crowd_status": "TEXT",
        "total_now_ids": "INTEGER",

        "fps_est": "REAL",
        "inf_avg_ms": "REAL",
        "loop_avg_ms": "REAL",
        "loop_p95_ms": "REAL",
        "loop_p99_ms": "REAL",
        "cap_avg_ms": "REAL",
        "post_avg_ms": "REAL",
        "disp_avg_ms": "REAL",
        "cpu_pct": "REAL",
        "rss_mb": "REAL",

        "zone_a_people_now": "INTEGER",
        "zone_a_unique_ids": "INTEGER",
        "zone_a_tracked_ids": "TEXT",
        "zone_a_timer_sec": "REAL",
        "zone_a_timer_hhmmss": "TEXT",
        "zone_a_zone_occupied_duration_sec": "REAL",
        "zone_a_zone_occupied_duration_hhmmss": "TEXT",
        "zone_a_current_owner_id": "TEXT",
        "zone_a_current_session_start_time": "TEXT",
        "zone_a_current_session_duration_sec": "REAL",
        "zone_a_is_valid_machine_usage": "INTEGER",
        "zone_a_multi_person_detected": "INTEGER",
        "zone_a_last_completed_session_duration_sec": "REAL",
        "zone_a_completed_sessions_count": "INTEGER",
        "zone_a_avg_completed_session_duration_sec": "REAL",
        "zone_a_avg_individual_usage_time_sec": "REAL",
        "zone_a_current_longest_usage_time_sec": "REAL",
        "zone_a_estimated_waiting_time_sec": "REAL",

        "zone_b_people_now": "INTEGER",
        "zone_b_unique_ids": "INTEGER",
        "zone_b_tracked_ids": "TEXT",
        "zone_b_timer_sec": "REAL",
        "zone_b_timer_hhmmss": "TEXT",
        "zone_b_zone_occupied_duration_sec": "REAL",
        "zone_b_zone_occupied_duration_hhmmss": "TEXT",
        "zone_b_current_owner_id": "TEXT",
        "zone_b_current_session_start_time": "TEXT",
        "zone_b_current_session_duration_sec": "REAL",
        "zone_b_is_valid_machine_usage": "INTEGER",
        "zone_b_multi_person_detected": "INTEGER",
        "zone_b_last_completed_session_duration_sec": "REAL",
        "zone_b_completed_sessions_count": "INTEGER",
        "zone_b_avg_completed_session_duration_sec": "REAL",
        "zone_b_avg_individual_usage_time_sec": "REAL",
        "zone_b_current_longest_usage_time_sec": "REAL",
        "zone_b_estimated_waiting_time_sec": "REAL",

        "raw_json": "TEXT",
    }

    for col_name, col_type in required_cols.items():
        _ensure_column(conn, "snapshots", col_name, col_type)

    conn.commit()
    conn.close()


def save_snapshot(payload: dict):
    zones = payload.get("zones", {})
    za = zones.get("equipment_a", {})
    zb = zones.get("equipment_b", {})

    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        INSERT INTO snapshots (
            ts, timestamp, crowd_status, total_now_ids,
            fps_est, inf_avg_ms, loop_avg_ms, loop_p95_ms, loop_p99_ms,
            cap_avg_ms, post_avg_ms, disp_avg_ms, cpu_pct, rss_mb,
            zone_a_people_now, zone_a_unique_ids, zone_a_tracked_ids,
            zone_a_timer_sec, zone_a_timer_hhmmss,
            zone_a_zone_occupied_duration_sec, zone_a_zone_occupied_duration_hhmmss,
            zone_a_current_owner_id, zone_a_current_session_start_time, zone_a_current_session_duration_sec,
            zone_a_is_valid_machine_usage, zone_a_multi_person_detected,
            zone_a_last_completed_session_duration_sec, zone_a_completed_sessions_count,
            zone_a_avg_completed_session_duration_sec,
            zone_a_avg_individual_usage_time_sec, zone_a_current_longest_usage_time_sec,
            zone_a_estimated_waiting_time_sec,
            zone_b_people_now, zone_b_unique_ids, zone_b_tracked_ids,
            zone_b_timer_sec, zone_b_timer_hhmmss,
            zone_b_zone_occupied_duration_sec, zone_b_zone_occupied_duration_hhmmss,
            zone_b_current_owner_id, zone_b_current_session_start_time, zone_b_current_session_duration_sec,
            zone_b_is_valid_machine_usage, zone_b_multi_person_detected,
            zone_b_last_completed_session_duration_sec, zone_b_completed_sessions_count,
            zone_b_avg_completed_session_duration_sec,
            zone_b_avg_individual_usage_time_sec, zone_b_current_longest_usage_time_sec,
            zone_b_estimated_waiting_time_sec,
            raw_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        safe_float(payload.get("ts", 0)),
        payload.get("timestamp", ""),
        payload.get("crowd_status", ""),
        safe_int(payload.get("total_now_ids", 0)),

        safe_float(payload.get("fps_est", 0)),
        safe_float(payload.get("inf_avg_ms", 0)),
        safe_float(payload.get("loop_avg_ms", 0)),
        safe_float(payload.get("loop_p95_ms", 0)),
        safe_float(payload.get("loop_p99_ms", 0)),
        safe_float(payload.get("cap_avg_ms", 0)),
        safe_float(payload.get("post_avg_ms", 0)),
        safe_float(payload.get("disp_avg_ms", 0)),
        safe_float(payload.get("cpu_pct", 0)),
        safe_float(payload.get("rss_mb", 0)),

        safe_int(za.get("people_now", 0)),
        safe_int(za.get("unique_ids_seen_in_zone", 0)),
        za.get("tracked_ids", "—"),
        safe_float(za.get("zone_timer_sec", 0)),
        za.get("zone_timer_hhmmss", "00:00:00"),
        safe_float(za.get("zone_occupied_duration_sec", za.get("zone_timer_sec", 0))),
        za.get("zone_occupied_duration_hhmmss", za.get("zone_timer_hhmmss", "00:00:00")),
        "" if za.get("current_owner_id") in (None, "") else str(za.get("current_owner_id")),
        za.get("current_session_start_time", ""),
        safe_float(za.get("current_session_duration_sec", 0)),
        1 if bool(za.get("is_valid_machine_usage", False)) else 0,
        1 if bool(za.get("multi_person_detected", False)) else 0,
        safe_float(za.get("last_completed_session_duration_sec", 0)),
        safe_int(za.get("completed_sessions_count", 0)),
        safe_float(za.get("avg_completed_session_duration_sec", za.get("avg_individual_usage_time_sec", 0))),
        safe_float(za.get("avg_individual_usage_time_sec", za.get("avg_completed_session_duration_sec", 0))),
        safe_float(za.get("current_longest_usage_time_sec", za.get("current_session_duration_sec", 0))),
        safe_float(za.get("estimated_waiting_time_sec", 0)),

        safe_int(zb.get("people_now", 0)),
        safe_int(zb.get("unique_ids_seen_in_zone", 0)),
        zb.get("tracked_ids", "—"),
        safe_float(zb.get("zone_timer_sec", 0)),
        zb.get("zone_timer_hhmmss", "00:00:00"),
        safe_float(zb.get("zone_occupied_duration_sec", zb.get("zone_timer_sec", 0))),
        zb.get("zone_occupied_duration_hhmmss", zb.get("zone_timer_hhmmss", "00:00:00")),
        "" if zb.get("current_owner_id") in (None, "") else str(zb.get("current_owner_id")),
        zb.get("current_session_start_time", ""),
        safe_float(zb.get("current_session_duration_sec", 0)),
        1 if bool(zb.get("is_valid_machine_usage", False)) else 0,
        1 if bool(zb.get("multi_person_detected", False)) else 0,
        safe_float(zb.get("last_completed_session_duration_sec", 0)),
        safe_int(zb.get("completed_sessions_count", 0)),
        safe_float(zb.get("avg_completed_session_duration_sec", zb.get("avg_individual_usage_time_sec", 0))),
        safe_float(zb.get("avg_individual_usage_time_sec", zb.get("avg_completed_session_duration_sec", 0))),
        safe_float(zb.get("current_longest_usage_time_sec", zb.get("current_session_duration_sec", 0))),
        safe_float(zb.get("estimated_waiting_time_sec", 0)),

        json.dumps(payload),
    ))
    conn.commit()
    conn.close()


# ─── MQTT ──────────────────────────────────────────────────────────────────────
def on_connect(client, userdata, flags, rc):
    print("MQTT connected with rc =", rc, flush=True)
    client.subscribe(MQTT_TOPIC)


def on_message(client, userdata, msg):
    try:
        payload = json.loads(msg.payload.decode("utf-8"))
        zones = payload.get("zones", {})
        za = zones.get("equipment_a", {})
        zb = zones.get("equipment_b", {})

        with latest_lock:
            latest_state["profile"] = {
                "timestamp":     payload.get("timestamp", "—"),
                "fps_est":       str(payload.get("fps_est", 0)),
                "inf_avg_ms":    str(payload.get("inf_avg_ms", 0)),
                "loop_avg_ms":   str(payload.get("loop_avg_ms", 0)),
                "loop_p95_ms":   str(payload.get("loop_p95_ms", 0)),
                "loop_p99_ms":   str(payload.get("loop_p99_ms", 0)),
                "cap_avg_ms":    str(payload.get("cap_avg_ms", 0)),
                "post_avg_ms":   str(payload.get("post_avg_ms", 0)),
                "disp_avg_ms":   str(payload.get("disp_avg_ms", 0)),
                "cpu_pct":       str(payload.get("cpu_pct", 0)),
                "rss_mb":        str(payload.get("rss_mb", 0)),
                "total_now_ids": str(payload.get("total_now_ids", 0)),
                "crowd_status":  payload.get("crowd_status", "LOW"),
            }
            latest_state["zones"] = {
                "equipment_a": {
                    "people_now": za.get("people_now", 0),
                    "unique_ids_seen_in_zone": za.get("unique_ids_seen_in_zone", 0),
                    "tracked_ids": za.get("tracked_ids", "—"),
                    "zone_timer_hhmmss": za.get("zone_timer_hhmmss", "00:00:00"),
                    "zone_timer_sec": za.get("zone_timer_sec", 0),
                    "zone_occupied_duration_sec": za.get("zone_occupied_duration_sec", za.get("zone_timer_sec", 0)),
                    "zone_occupied_duration_hhmmss": za.get("zone_occupied_duration_hhmmss", za.get("zone_timer_hhmmss", "00:00:00")),
                    "current_owner_id": za.get("current_owner_id", ""),
                    "current_session_start_time": za.get("current_session_start_time", ""),
                    "current_session_duration_sec": za.get("current_session_duration_sec", za.get("current_longest_usage_time_sec", 0)),
                    "is_valid_machine_usage": za.get("is_valid_machine_usage", False),
                    "multi_person_detected": za.get("multi_person_detected", False),
                    "last_completed_session_duration_sec": za.get("last_completed_session_duration_sec", 0),
                    "completed_sessions_count": za.get("completed_sessions_count", 0),
                    "avg_completed_session_duration_sec": za.get("avg_completed_session_duration_sec", za.get("avg_individual_usage_time_sec", 0)),
                    "avg_individual_usage_time_sec": za.get("avg_individual_usage_time_sec", za.get("avg_completed_session_duration_sec", 0)),
                    "current_longest_usage_time_sec": za.get("current_longest_usage_time_sec", za.get("current_session_duration_sec", 0)),
                    "estimated_waiting_time_sec": za.get("estimated_waiting_time_sec", 0),
                },
                "equipment_b": {
                    "people_now": zb.get("people_now", 0),
                    "unique_ids_seen_in_zone": zb.get("unique_ids_seen_in_zone", 0),
                    "tracked_ids": zb.get("tracked_ids", "—"),
                    "zone_timer_hhmmss": zb.get("zone_timer_hhmmss", "00:00:00"),
                    "zone_timer_sec": zb.get("zone_timer_sec", 0),
                    "zone_occupied_duration_sec": zb.get("zone_occupied_duration_sec", zb.get("zone_timer_sec", 0)),
                    "zone_occupied_duration_hhmmss": zb.get("zone_occupied_duration_hhmmss", zb.get("zone_timer_hhmmss", "00:00:00")),
                    "current_owner_id": zb.get("current_owner_id", ""),
                    "current_session_start_time": zb.get("current_session_start_time", ""),
                    "current_session_duration_sec": zb.get("current_session_duration_sec", zb.get("current_longest_usage_time_sec", 0)),
                    "is_valid_machine_usage": zb.get("is_valid_machine_usage", False),
                    "multi_person_detected": zb.get("multi_person_detected", False),
                    "last_completed_session_duration_sec": zb.get("last_completed_session_duration_sec", 0),
                    "completed_sessions_count": zb.get("completed_sessions_count", 0),
                    "avg_completed_session_duration_sec": zb.get("avg_completed_session_duration_sec", zb.get("avg_individual_usage_time_sec", 0)),
                    "avg_individual_usage_time_sec": zb.get("avg_individual_usage_time_sec", zb.get("avg_completed_session_duration_sec", 0)),
                    "current_longest_usage_time_sec": zb.get("current_longest_usage_time_sec", zb.get("current_session_duration_sec", 0)),
                    "estimated_waiting_time_sec": zb.get("estimated_waiting_time_sec", 0),
                },
            }

        save_snapshot(payload)
        maybe_send_telegram_alert(payload)
    except Exception as e:
        print("MQTT parse error:", e, flush=True)


def start_mqtt():
    client = mqtt.Client(client_id="gympulse-rpi3-dashboard")
    client.on_connect = on_connect
    client.on_message = on_message
    client.reconnect_delay_set(min_delay=1, max_delay=10)
    client.connect(MQTT_BROKER, MQTT_PORT, 60)
    client.loop_start()
    return client


# ─── Data accessors ────────────────────────────────────────────────────────────
def get_current_profile() -> dict:
    with latest_lock:
        return dict(latest_state["profile"])


def get_current_zones() -> dict:
    with latest_lock:
        return {
            "equipment_a": dict(latest_state["zones"].get("equipment_a", {})),
            "equipment_b": dict(latest_state["zones"].get("equipment_b", {})),
        }


def get_history(minutes: int = 15) -> dict:
    thin_sec = max(1, int(CHART_THIN_SEC))

    if minutes == 0:
        # Strict today: 00:00:00 to 23:59:59 local time
        today_start = time.mktime(datetime.now().replace(
            hour=0, minute=0, second=0, microsecond=0).timetuple())
        today_end   = today_start + 86400
        where  = "WHERE ts >= ? AND ts < ?"
        params = (today_start, today_end)
    else:
        cutoff = time.time() - (minutes * 60)
        where  = "WHERE ts >= ?"
        params = (cutoff,)

    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(f"""
            SELECT
                ts,
                timestamp,
                total_now_ids,
                crowd_status,
                zone_a_people_now,
                zone_b_people_now,
                zone_a_estimated_waiting_time_sec,
                zone_b_estimated_waiting_time_sec,
                zone_a_current_session_duration_sec,
                zone_b_current_session_duration_sec,
                zone_a_completed_sessions_count,
                zone_b_completed_sessions_count
            FROM snapshots
            {where}
            ORDER BY ts ASC
        """, params).fetchall()
        conn.close()
    except sqlite3.OperationalError as e:
        print("History query error:", e, flush=True)
        return {
            "labels": [], "za": [], "zb": [], "total": [],
            "za_wait": [], "zb_wait": [], "crowd": [],
            "za_longest": [], "zb_longest": [],
        }

    if not rows:
        return {
            "labels": [], "za": [], "zb": [], "total": [],
            "za_wait": [], "zb_wait": [], "crowd": [],
            "za_longest": [], "zb_longest": [],
        }

    labels, za, zb, total_arr, crowd_arr, za_wait, zb_wait = [], [], [], [], [], [], []
    za_longest, zb_longest = [], []
    za_sess, zb_sess = [], []
    last_keep_ts = None

    for row in rows:
        ts_val = safe_float(row["ts"], 0)
        if last_keep_ts is not None and (ts_val - last_keep_ts) < thin_sec:
            continue
        last_keep_ts = ts_val

        ts_full = row["timestamp"] or ""
        label = ts_full.split(" ")[-1] if " " in ts_full else ts_full

        labels.append(label)
        za.append(safe_int(row["zone_a_people_now"], 0))
        zb.append(safe_int(row["zone_b_people_now"], 0))
        total_arr.append(safe_int(row["total_now_ids"], 0))
        crowd_arr.append(row["crowd_status"] or crowd_status_from_count(total_arr[-1]))
        za_wait.append(round(safe_float(row["zone_a_estimated_waiting_time_sec"], 0), 1))
        zb_wait.append(round(safe_float(row["zone_b_estimated_waiting_time_sec"], 0), 1))
        za_longest.append(round(safe_float(row["zone_a_current_session_duration_sec"], 0), 1))
        zb_longest.append(round(safe_float(row["zone_b_current_session_duration_sec"], 0), 1))
        za_sess.append(safe_int(row["zone_a_completed_sessions_count"], 0))
        zb_sess.append(safe_int(row["zone_b_completed_sessions_count"], 0))

    return {
        "labels": labels, "za": za, "zb": zb, "total": total_arr,
        "crowd": crowd_arr, "za_wait": za_wait, "zb_wait": zb_wait,
        "za_longest": za_longest, "zb_longest": zb_longest,
        "za_sess": za_sess, "zb_sess": zb_sess,
    }



def get_daily_sessions() -> dict:
    """
    Count completed owner sessions per zone today and compute today's average duration.
    Uses the cumulative completed_sessions_count fields emitted by the vision script.
    """
    today_start = time.mktime(datetime.now().replace(
        hour=0, minute=0, second=0, microsecond=0).timetuple())

    def extract_sessions(rows, count_col: str, dur_col: str):
        sessions = []
        prev_count = None
        for row in rows:
            count_now = safe_int(row[count_col], 0)
            dur_now = safe_float(row[dur_col], 0)
            if prev_count is None:
                prev_count = count_now
                continue
            if count_now > prev_count:
                sessions.extend([dur_now] * max(1, count_now - prev_count))
            prev_count = count_now
        return sessions

    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        rows = conn.execute("""
            SELECT
                ts,
                zone_a_completed_sessions_count,
                zone_a_last_completed_session_duration_sec,
                zone_b_completed_sessions_count,
                zone_b_last_completed_session_duration_sec
            FROM snapshots
            WHERE ts >= ?
            ORDER BY ts ASC
        """, (today_start,)).fetchall()
        conn.close()

        za_sessions_list = extract_sessions(rows, "zone_a_completed_sessions_count", "zone_a_last_completed_session_duration_sec")
        zb_sessions_list = extract_sessions(rows, "zone_b_completed_sessions_count", "zone_b_last_completed_session_duration_sec")

        za_sessions = len(za_sessions_list)
        zb_sessions = len(zb_sessions_list)
        za_avg = round(sum(za_sessions_list) / za_sessions, 1) if za_sessions else 0.0
        zb_avg = round(sum(zb_sessions_list) / zb_sessions, 1) if zb_sessions else 0.0
    except sqlite3.OperationalError as e:
        print("Daily sessions query error:", e, flush=True)
        za_sessions, zb_sessions, za_avg, zb_avg = 0, 0, 0.0, 0.0

    return {"za": za_sessions, "zb": zb_sessions, "za_avg": za_avg, "zb_avg": zb_avg}


def get_peak_hours(minutes: int = -1, day: int = None) -> dict:
    """
    Return avg zone occupancy grouped by hour-of-day (0-23).
    minutes=-1 → all-time (used when day is specified)
    minutes=0  → today only (00:00–23:59 local time)
    minutes>0  → last N minutes
    day        → 0=Mon … 6=Sun (SQLite strftime '%w': 0=Sun,1=Mon…6=Sat)
    """
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row

        # Convert our 0=Mon…6=Sun to SQLite's 0=Sun…6=Sat
        if day is not None:
            sqlite_dow = (day + 1) % 7
            where  = "WHERE timestamp IS NOT NULL AND timestamp != '' AND CAST(strftime('%w', timestamp) AS INTEGER) = ?"
            params = (sqlite_dow,)
        elif minutes == 0:
            where  = "WHERE date(timestamp) = date('now', 'localtime')"
            params = ()
        elif minutes > 0:
            cutoff = time.time() - minutes * 60
            where  = "WHERE ts >= ?"
            params = (cutoff,)
        else:
            where  = "WHERE timestamp IS NOT NULL AND timestamp != ''"
            params = ()

        rows = conn.execute(f"""
            SELECT
                CAST(strftime('%H', timestamp) AS INTEGER) AS hour,
                SUM(zone_a_people_now) AS za_sum,
                SUM(zone_b_people_now) AS zb_sum,
                COUNT(*) AS samples
            FROM snapshots
            {where}
            GROUP BY hour
            ORDER BY hour
        """, params).fetchall()
        conn.close()
    except sqlite3.OperationalError as e:
        print("Peak hours query error:", e, flush=True)
        return {"za": [0]*24, "zb": [0]*24, "samples": [0]*24, "max": 1}

    za      = [0.0] * 24
    zb      = [0.0] * 24
    samples = [0]   * 24

    for row in rows:
        h = int(row["hour"])
        if 0 <= h <= 23:
            n = int(row["samples"]) or 1
            za[h]      = round(float(row["za_sum"]) / n, 2)
            zb[h]      = round(float(row["zb_sum"]) / n, 2)
            samples[h] = int(row["samples"])

    max_val = max(max(za), max(zb), 1.0)
    return {"za": za, "zb": zb, "samples": samples, "max": round(max_val, 2)}


# ─── HTML template ─────────────────────────────────────────────────────────────
HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>GymPulse · Live Dashboard</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
:root{
  --bg:#080b10;--s1:#0d1320;--s2:#111928;--border:#19253a;
  --text:#d8e6f5;--muted:#4a5d74;--dim:#1e2d42;
  --green:#00e5a0;--blue:#00b8ff;--amber:#f59e0b;--red:#ef4444;
  --gd:rgba(0,229,160,.08);--bd:rgba(0,184,255,.08);
  --zone-a:#00c8ff;--zone-b:#ffc800;
  --zad:rgba(0,200,255,.08);--zbd:rgba(255,200,0,.08);
  --ad:rgba(245,158,11,.10);--rd:rgba(239,68,68,.10);
  --sans:system-ui,-apple-system,"Segoe UI",Roboto,Arial,sans-serif;
  --mono:ui-monospace,"SFMono-Regular",Menlo,Consolas,monospace;
  --r:14px;
}
html{font-size:15px}
body{background:var(--bg);color:var(--text);font-family:var(--sans);
  min-height:100vh;overflow-x:hidden;padding-bottom:56px}
::-webkit-scrollbar{width:5px;height:5px}
::-webkit-scrollbar-track{background:var(--bg)}
::-webkit-scrollbar-thumb{background:var(--dim);border-radius:3px}

/* Header */
header{
  position:sticky;top:0;z-index:200;
  background:rgba(8,11,16,.9);backdrop-filter:blur(14px);
  border-bottom:1px solid var(--border);
  display:flex;align-items:center;justify-content:space-between;
  padding:13px 28px;
}
.brand{display:flex;align-items:center;gap:10px}
.brand-logo{
  width:36px;height:36px;border-radius:9px;background:var(--green);
  display:flex;align-items:center;justify-content:center;
  font-weight:900;font-size:14px;color:#000;letter-spacing:-.5px;flex-shrink:0
}
.brand-text{font-size:1.1rem;font-weight:800;letter-spacing:-.4px}
.brand-text em{color:var(--green);font-style:normal}
.hdr-right{display:flex;align-items:center;gap:14px}
.live-pill{
  display:flex;align-items:center;gap:6px;
  background:var(--gd);border:1px solid rgba(0,229,160,.22);
  border-radius:20px;padding:5px 12px;
  font-family:var(--mono);font-size:.68rem;color:var(--green)
}
@keyframes blink{0%,100%{opacity:1;transform:scale(1)}50%{opacity:.3;transform:scale(.6)}}
.demo-tag{
  font-family:var(--mono);font-size:.62rem;padding:4px 10px;border-radius:20px;
  background:var(--ad);border:1px solid rgba(245,158,11,.3);color:var(--amber);display:none
}
.hdr-time{font-family:var(--mono);font-size:.7rem;color:var(--muted)}

/* Page */
.page{max-width:1340px;margin:0 auto;padding:22px 28px 0}

/* Section label */
.sec{
  font-size:.62rem;font-weight:700;letter-spacing:.16em;text-transform:uppercase;
  color:var(--muted);margin:26px 0 11px;display:flex;align-items:center;gap:8px
}
.sec::after{content:'';flex:1;height:1px;background:var(--border)}

/* Grids */
.g2{display:grid;grid-template-columns:1fr 1fr;gap:13px}
.g-zones{display:grid;grid-template-columns:1fr 1fr;gap:13px}
.g5{display:grid;grid-template-columns:repeat(5,1fr);gap:13px}
@media(max-width:1100px){.g5{grid-template-columns:repeat(3,1fr)}}
@media(max-width:860px){.g-zones{grid-template-columns:1fr}.g5{grid-template-columns:1fr 1fr}}
@media(max-width:580px){.g2,.g-zones,.g5{grid-template-columns:1fr}.page{padding:13px}}

/* Base card */
.card{
  background:var(--s1);border:1px solid var(--border);border-radius:var(--r);
  padding:18px 20px;position:relative;overflow:hidden;transition:border-color .25s
}
.card:hover{border-color:#253550}
.card::before{
  content:'';position:absolute;inset:0;pointer-events:none;
  background:linear-gradient(140deg,rgba(255,255,255,.016) 0%,transparent 55%)
}
.clbl{font-size:.62rem;font-weight:700;letter-spacing:.11em;text-transform:uppercase;
  color:var(--muted);margin-bottom:9px}
.csub{font-family:var(--mono);font-size:.68rem;color:var(--muted);margin-top:8px;line-height:1.75}
.csub b{color:var(--text);font-weight:500}

/* Dot / text freshness states */
.cam-dot{width:8px;height:8px;border-radius:50%;flex-shrink:0;transition:background .4s}
.cam-dot.live  {background:var(--green);box-shadow:0 0 7px var(--green)}
.cam-dot.delay {background:var(--amber);box-shadow:0 0 7px var(--amber)}
.cam-dot.stale {background:var(--red)}
.cam-dot.offline{background:var(--dim)}
.cam-txt{font-family:var(--mono);font-size:.7rem;transition:color .4s}
.cam-txt.live  {color:var(--green)}
.cam-txt.delay {color:var(--amber)}
.cam-txt.stale {color:var(--red)}
.cam-txt.offline{color:var(--muted)}

/* Hero decision card */
.hero-card{
  background:var(--s1);border:1px solid var(--border);border-radius:var(--r);
  padding:24px 26px;position:relative;overflow:hidden;
}
.hero-card::before{
  content:'';position:absolute;inset:0;pointer-events:none;
  background:linear-gradient(140deg,rgba(255,255,255,.016) 0%,transparent 55%)
}
.hero-layout{display:grid;grid-template-columns:auto 1fr;gap:32px;align-items:center}
@media(max-width:700px){.hero-layout{grid-template-columns:1fr;gap:20px}}
.hero-status-col{display:flex;flex-direction:column;gap:11px;min-width:200px}
.hero-section-label{
  font-size:.58rem;font-weight:700;letter-spacing:.15em;text-transform:uppercase;color:var(--muted)
}
.gym-status-pill{
  display:inline-flex;align-items:center;gap:11px;
  padding:14px 30px;border-radius:50px;
  font-size:2rem;font-weight:900;letter-spacing:.04em;
  transition:all .5s cubic-bezier(.4,0,.2,1);white-space:nowrap
}
.gym-status-pill.quiet   {background:rgba(0,229,160,.10);border:2px solid rgba(0,229,160,.35);color:var(--green)}
.gym-status-pill.moderate{background:rgba(245,158,11,.10);border:2px solid rgba(245,158,11,.35);color:var(--amber)}
.gym-status-pill.busy    {background:rgba(239,68,68,.10);border:2px solid rgba(239,68,68,.35);color:var(--red);animation:high-pulse 1.8s ease-in-out infinite}
@keyframes high-pulse{0%,100%{box-shadow:0 0 0 0 rgba(239,68,68,0)}50%{box-shadow:0 0 0 9px rgba(239,68,68,.07)}}
.hero-meta{display:flex;align-items:center;gap:8px}
.hero-people{font-family:var(--mono);font-size:.72rem;color:var(--muted)}
.hero-people b{color:var(--text);font-size:.9rem;font-weight:700}
.hero-freshness{display:flex;align-items:center;gap:7px;font-family:var(--mono);font-size:.65rem}
.hero-stats-col{display:grid;grid-template-columns:repeat(3,1fr);gap:13px}
@media(max-width:900px){.hero-stats-col{grid-template-columns:repeat(2,1fr)}}
@media(max-width:580px){.hero-stats-col{grid-template-columns:1fr}}
.hero-stat{
  background:var(--s2);border:1px solid var(--border);border-radius:10px;
  padding:14px 16px;display:flex;flex-direction:column;gap:5px
}
.hero-stat-lbl{font-size:.57rem;font-weight:700;letter-spacing:.12em;text-transform:uppercase;color:var(--muted)}
.hero-stat-val{font-size:1rem;font-weight:700;color:var(--text);line-height:1.3}

/* Recommendation bar */
.rec-bar{
  border-radius:var(--r);padding:15px 22px;
  display:flex;align-items:center;gap:16px;
  margin-top:13px;transition:background .5s,border-color .5s;
  border:1px solid transparent;
}
.rec-bar.go    {background:rgba(0,229,160,.07);border-color:rgba(0,229,160,.22)}
.rec-bar.wait  {background:rgba(245,158,11,.07);border-color:rgba(245,158,11,.22)}
.rec-bar.later {background:rgba(239,68,68,.07);border-color:rgba(239,68,68,.22)}
.rec-bar.unknown{background:rgba(30,45,66,.5);border-color:var(--border)}
.rec-icon{font-size:1.65rem;flex-shrink:0;line-height:1}
.rec-content{flex:1;min-width:0}
.rec-label{font-size:1.05rem;font-weight:800;letter-spacing:-.2px}
.rec-label.go    {color:var(--green)}
.rec-label.wait  {color:var(--amber)}
.rec-label.later {color:var(--red)}
.rec-label.unknown{color:var(--muted)}
.rec-desc{font-family:var(--mono);font-size:.68rem;color:var(--muted);margin-top:3px}
.rec-badge{
  font-family:var(--mono);font-size:.72rem;font-weight:600;
  padding:5px 13px;border-radius:20px;background:rgba(255,255,255,.04);
  border:1px solid var(--border);color:var(--muted);white-space:nowrap;flex-shrink:0
}

/* Zone cards — resident-facing */
.zcard{
  background:var(--s1);border:1px solid var(--border);border-radius:var(--r);
  overflow:hidden;transition:border-color .3s
}
.zcard:hover{border-color:#253550}
.zcard.zone-in-use-a{border-color:rgba(0,200,255,.22)}
.zcard.zone-in-use-b{border-color:rgba(255,200,0,.22)}
.zcard-head{
  display:flex;align-items:center;justify-content:space-between;
  padding:15px 20px 13px;border-bottom:1px solid var(--border)
}
.zcard-title-row{display:flex;align-items:center;gap:9px}
.ztag{
  width:30px;height:30px;border-radius:8px;flex-shrink:0;
  display:flex;align-items:center;justify-content:center;font-weight:800;font-size:.78rem
}
.ztag.a{background:var(--zad);border:1px solid rgba(0,200,255,.25);color:var(--zone-a)}
.ztag.b{background:var(--zbd);border:1px solid rgba(255,200,0,.25);color:var(--zone-b)}
.zcard-title{font-size:.9rem;font-weight:700}
.zavail-badge{
  font-size:.65rem;font-weight:700;padding:5px 12px;border-radius:20px;
  letter-spacing:.04em;transition:all .3s;white-space:nowrap
}
.zavail-free   {background:rgba(0,229,160,.1);border:1px solid rgba(0,229,160,.3);color:var(--green)}
.zavail-inuse-a{background:var(--zad);border:1px solid rgba(0,200,255,.35);color:var(--zone-a);animation:zbadge-a 2s ease-in-out infinite}
.zavail-inuse-b{background:var(--zbd);border:1px solid rgba(255,200,0,.35);color:var(--zone-b);animation:zbadge-b 2s ease-in-out infinite}
.zavail-unknown{background:rgba(30,45,66,.6);border:1px solid var(--border);color:var(--muted)}
@keyframes zbadge-a{0%,100%{box-shadow:0 0 0 0 transparent}50%{box-shadow:0 0 0 4px rgba(0,200,255,.07)}}
@keyframes zbadge-b{0%,100%{box-shadow:0 0 0 0 transparent}50%{box-shadow:0 0 0 4px rgba(255,200,0,.07)}}
.zcard-body{padding:18px 20px 16px}
.zcard-primary{display:flex;align-items:center;gap:16px;margin-bottom:16px;flex-wrap:wrap}
.zcard-session-block{display:flex;flex-direction:column;gap:3px}
.zcard-session-lbl{font-size:.58rem;font-weight:700;letter-spacing:.1em;text-transform:uppercase;color:var(--muted)}
.zcard-session-timer{
  font-family:var(--mono);font-size:2.6rem;font-weight:800;letter-spacing:-2px;
  line-height:1;font-variant-numeric:tabular-nums;transition:color .4s
}
.zcard-session-timer.idle{color:var(--dim);font-size:2rem;letter-spacing:-1px}
.zcard-session-timer.a{color:var(--zone-a)}
.zcard-session-timer.b{color:var(--zone-b)}
.zcard-free-est{
  font-family:var(--mono);font-size:.72rem;font-weight:600;
  padding:4px 11px;border-radius:20px;
  background:rgba(0,229,160,.06);border:1px solid rgba(0,229,160,.15);color:var(--green)
}
.zcard-free-est.inuse{background:rgba(245,158,11,.07);border-color:rgba(245,158,11,.2);color:var(--amber)}
.zcard-divider{height:1px;background:var(--border);margin:0 0 13px}
.zcard-metrics{display:grid;grid-template-columns:1fr 1fr 1fr;gap:12px}
.zcard-metric{display:flex;flex-direction:column;gap:3px}
.zm-lbl{font-size:.57rem;font-weight:700;letter-spacing:.1em;text-transform:uppercase;color:var(--muted)}
.zm-val{font-family:var(--mono);font-size:.85rem;font-weight:600;color:var(--text)}
.zm-val.dim{color:var(--muted)}
.zcard-wait-row{
  margin-top:12px;padding:9px 13px;border-radius:8px;
  display:flex;align-items:center;justify-content:space-between;
  background:rgba(255,255,255,.025);border:1px solid var(--border)
}
.zwait-lbl{font-size:.58rem;font-weight:700;letter-spacing:.1em;text-transform:uppercase;color:var(--muted)}
.zwait-val{font-family:var(--mono);font-size:.85rem;font-weight:600;color:var(--text)}

/* Insight strip */
.insight-strip{display:flex;align-items:center;flex-wrap:wrap;gap:8px;margin-bottom:12px}
.insight-chip{
  font-family:var(--mono);font-size:.64rem;
  background:var(--s2);border:1px solid var(--border);
  border-radius:20px;padding:5px 12px;color:var(--muted);
  display:flex;align-items:center;gap:5px
}
.insight-chip .ic-dot{width:6px;height:6px;border-radius:50%;flex-shrink:0}

/* Best times traffic lights */
.besttime-card{
  background:var(--s1);border:1px solid var(--border);border-radius:var(--r);
  padding:18px 20px;margin-bottom:13px
}
.besttime-header{display:flex;align-items:baseline;justify-content:space-between;
  margin-bottom:14px;flex-wrap:wrap;gap:6px}
.besttime-title{font-size:.85rem;font-weight:700}
.besttime-sub{font-family:var(--mono);font-size:.62rem;color:var(--muted)}
.btime-row{display:flex;gap:5px;flex-wrap:wrap}
.btime-slot{
  flex:1;min-width:0;border-radius:10px;padding:11px 4px 9px;text-align:center;
  border:1px solid transparent;transition:transform .1s,box-shadow .1s;cursor:default
}
.btime-slot:hover{transform:scale(1.04);box-shadow:0 2px 12px rgba(0,0,0,.2)}
.bts-hour{font-family:var(--mono);font-size:.68rem;font-weight:700;margin-bottom:7px}
.bts-dot{width:12px;height:12px;border-radius:50%;margin:0 auto 6px}
.bts-label{font-size:.58rem;font-weight:700;letter-spacing:.04em}
.btime-slot.bts-quiet   {background:rgba(0,229,160,.07);border-color:rgba(0,229,160,.2)}
.btime-slot.bts-quiet   .bts-hour{color:var(--text)}
.btime-slot.bts-quiet   .bts-dot{background:var(--green)}
.btime-slot.bts-quiet   .bts-label{color:var(--green)}
.btime-slot.bts-moderate{background:rgba(245,158,11,.07);border-color:rgba(245,158,11,.2)}
.btime-slot.bts-moderate .bts-hour{color:var(--text)}
.btime-slot.bts-moderate .bts-dot{background:var(--amber)}
.btime-slot.bts-moderate .bts-label{color:var(--amber)}
.btime-slot.bts-busy    {background:rgba(239,68,68,.07);border-color:rgba(239,68,68,.2)}
.btime-slot.bts-busy    .bts-hour{color:var(--text)}
.btime-slot.bts-busy    .bts-dot{background:var(--red)}
.btime-slot.bts-busy    .bts-label{color:var(--red)}
.btime-slot.bts-nodata  {background:rgba(30,45,66,.4);border-color:var(--border)}
.btime-slot.bts-nodata  .bts-hour{color:var(--muted)}
.btime-slot.bts-nodata  .bts-dot{background:var(--dim)}
.btime-slot.bts-nodata  .bts-label{color:var(--dim)}
.btime-slot.bts-now     {box-shadow:inset 0 0 0 2px rgba(255,255,255,.07)}

/* Chart cards */
.cchart{
  background:var(--s1);border:1px solid var(--border);border-radius:var(--r);
  padding:18px 20px;overflow:hidden
}
.chead{display:flex;align-items:flex-start;justify-content:space-between;
  margin-bottom:14px;flex-wrap:wrap;gap:8px}
.ctitle{font-size:.85rem;font-weight:700}
.csub2{font-size:.62rem;color:var(--muted);margin-top:2px}
.cleg{display:flex;gap:12px}
.cleg-item{display:flex;align-items:center;gap:5px;font-size:.62rem;color:var(--muted)}
.cleg-dot{width:8px;height:8px;border-radius:50%;flex-shrink:0}
canvas.chrt{display:block;width:100%!important;height:155px!important}
.chart-note{margin-top:8px;font-family:var(--mono);font-size:.62rem;color:var(--muted)}

/* System health */
.syscard{
  background:var(--s1);border:1px solid var(--border);border-radius:var(--r);padding:15px 17px
}
.sysv{
  font-family:var(--mono);font-size:1.45rem;font-weight:500;
  letter-spacing:-.5px;line-height:1;margin:7px 0 5px;font-variant-numeric:tabular-nums
}
.sysv.g{color:var(--green)} .sysv.b{color:var(--blue)}
.sysv.a{color:var(--amber)} .sysv.p{color:#a855f7}
.sbar-wrap{margin-top:7px}
.sbar-lbls{display:flex;justify-content:space-between;
  font-family:var(--mono);font-size:.56rem;color:var(--dim);margin-bottom:4px}
.sbar{height:3px;background:var(--border);border-radius:2px;overflow:hidden}
.sfill{height:100%;border-radius:2px;transition:width .8s cubic-bezier(.4,0,.2,1)}
.fg{background:linear-gradient(90deg,var(--green),#00c090)}
.fb{background:linear-gradient(90deg,var(--blue),#0098d8)}
.fa{background:linear-gradient(90deg,var(--amber),#d97706)}
.fp{background:linear-gradient(90deg,#a855f7,#ec4899)}
canvas.spark{display:block;width:100%!important;height:26px!important;margin-top:5px}

/* Footer */
footer{text-align:center;font-family:var(--mono);font-size:.62rem;color:var(--dim);
  margin-top:44px;padding-top:18px;border-top:1px solid var(--border)}

/* Chart tooltip */
.chart-wrap{position:relative}
.chart-tooltip{
  display:none;position:absolute;pointer-events:none;
  background:var(--s2);border:1px solid var(--border);border-radius:6px;
  padding:6px 10px;font-family:var(--mono);font-size:.62rem;
  color:var(--text);line-height:1.7;white-space:nowrap;z-index:20;
  transform:translateX(-50%);transition:left .05s
}
.chart-tooltip.visible{display:block}
.chart-tooltip .tip-za{color:#00c8ff}
.chart-tooltip .tip-zb{color:#ffc800}

/* Heatmap */
.hmap-card{background:var(--s1);border:1px solid var(--border);border-radius:var(--r);padding:18px 20px}
.hmap-title-row{display:flex;align-items:baseline;justify-content:space-between;margin-bottom:14px;flex-wrap:wrap;gap:6px}
.hmap-title{font-size:.85rem;font-weight:700;margin-bottom:2px}
.hmap-sub{font-family:var(--mono);font-size:.62rem;color:var(--muted)}
.hmap-leg{display:flex;gap:14px}
.hmap-leg-item{display:flex;align-items:center;gap:5px;font-size:.62rem;color:var(--muted)}
.hmap-leg-swatch{width:10px;height:10px;border-radius:2px;flex-shrink:0}
.hmap-body{display:flex;flex-direction:column;gap:6px}
.hmap-row{display:flex;align-items:center;gap:6px}
.hmap-row-lbl{font-family:var(--mono);font-size:.62rem;color:var(--muted);width:44px;flex-shrink:0;text-align:right}
.hmap-cells{display:grid;grid-template-columns:repeat(24,1fr);gap:3px;flex:1}
.hmap-cell{height:28px;border-radius:4px;background:var(--dim);cursor:pointer;position:relative;transition:transform .1s}
.hmap-cell:hover{transform:scale(1.08);z-index:2}
.hmap-cell:hover .hmap-tip{display:block}
.hmap-tip{
  display:none;position:absolute;bottom:calc(100% + 5px);left:50%;transform:translateX(-50%);
  background:var(--s2);border:1px solid var(--border);border-radius:6px;
  padding:5px 8px;white-space:nowrap;z-index:10;pointer-events:none;
  font-family:var(--mono);font-size:.62rem;color:var(--text);line-height:1.6
}
.hmap-tip::after{
  content:'';position:absolute;top:100%;left:50%;transform:translateX(-50%);
  border:4px solid transparent;border-top-color:var(--border)
}
.hmap-hours{display:grid;grid-template-columns:repeat(24,1fr);gap:3px;margin-left:50px;margin-top:4px}
.hmap-hr-lbl{font-family:var(--mono);font-size:.55rem;color:var(--muted);text-align:center}
.hmap-hr-lbl.operating{color:var(--text);font-weight:700}
.hmap-empty{font-family:var(--mono);font-size:.72rem;color:var(--muted);text-align:center;padding:24px 0}
.hmap-cell.op-hours{outline:1px solid rgba(255,255,255,.13);outline-offset:-1px}

/* Timeframe selector */
.tf-bar{display:flex;align-items:center;justify-content:space-between;margin:26px 0 11px}
.tf-bar .sec{margin:0}
.tf-btns{display:flex;gap:4px}
.tf-btn{
  font-family:var(--mono);font-size:.62rem;font-weight:600;letter-spacing:.08em;
  text-transform:uppercase;padding:4px 11px;border-radius:20px;border:1px solid var(--border);
  background:none;color:var(--muted);cursor:pointer;transition:all .18s
}
.tf-btn:hover{color:var(--text);border-color:#253550}
.tf-btn.active{background:var(--dim);border-color:#253550;color:var(--text)}
.flash{animation:fl .3s ease-out}
@keyframes fl{0%{opacity:.25}100%{opacity:1}}

/* Tabs */
.tab-bar{display:flex;align-items:center;gap:4px;border-bottom:1px solid var(--border);margin-bottom:4px}
.tab-btn{
  padding:10px 20px;font-size:.75rem;font-weight:700;letter-spacing:.08em;
  text-transform:uppercase;color:var(--muted);background:none;border:none;
  border-bottom:2px solid transparent;cursor:pointer;
  font-family:var(--sans);transition:color .2s,border-color .2s;margin-bottom:-1px;
}
.tab-btn:hover{color:var(--text)}
.tab-btn.active{color:var(--green);border-bottom-color:var(--green)}
.tab-pane{display:none}
.tab-pane.active{display:block}

/* Hidden JS anchors */
.js-hidden{display:none!important}
</style>
</head>
<body>

<header>
  <div class="brand">
    <div class="brand-logo">GP</div>
    <div class="brand-text">Gym<em>Pulse</em></div>
  </div>
  <div class="hdr-right">
    <div class="demo-tag" id="demo_tag">&#9654; DEMO REPLAY</div>
    <div class="live-pill">
      <div class="cam-dot live" id="cam_dot"></div>
      <span class="cam-txt live" id="cam_txt">Connecting&#8230;</span>
    </div>
    <div class="hdr-time" id="hdr_clock">&#8212;</div>
  </div>
</header>

<div class="page">

<div class="tab-bar">
  <button class="tab-btn active" onclick="switchTab('dashboard',this)">&#128202; Dashboard</button>
  <button class="tab-btn" onclick="switchTab('syshealth',this)">&#9881;&#65039; System</button>
</div>

<!-- Hidden JS anchors — keep set() from crashing on removed telemetry fields -->
<div class="js-hidden">
  <span id="crowd_pill"></span><span id="crowd_icon"></span><span id="crowd_txt"></span>
  <span id="total_in_frame"></span><span id="total_zones"></span>
  <span id="za_big"></span><span id="zb_big"></span>
  <span id="za_timer"></span><span id="zb_timer"></span>
  <span id="za_owner"></span><span id="zb_owner"></span>
  <span id="za_session"></span><span id="zb_session"></span>
  <span id="za_session_start"></span><span id="zb_session_start"></span>
  <span id="za_valid"></span><span id="zb_valid"></span>
  <span id="za_wait"></span><span id="zb_wait"></span>
  <span id="za_ids"></span><span id="zb_ids"></span>
  <span id="za_unique"></span><span id="zb_unique"></span>
  <span id="za_multi"></span><span id="zb_multi"></span>
  <span id="za_last_completed"></span><span id="zb_last_completed"></span>
  <span id="badge_a"></span><span id="badge_b"></span>
  <span id="cam_fps"></span><span id="cam_inf"></span><span id="cam_ts"></span>
  <span id="za_avg_session"></span><span id="zb_avg_session"></span>
</div>

<!-- ====== TAB 1: Dashboard ====== -->
<div class="tab-pane active" id="tab-dashboard">

<!-- 1. Live Status Hero -->
<div class="sec" style="margin-top:18px">Live Status</div>
<div class="hero-card">
  <div class="hero-layout">

    <div class="hero-status-col">
      <div class="hero-section-label">Gym Status</div>
      <div class="gym-status-pill quiet" id="gym_status_pill">
        <span id="gym_status_icon">&#128994;</span>
        <span id="gym_status_txt">Quiet</span>
      </div>
      <div class="hero-freshness">
        <div class="cam-dot live" id="hero_cam_dot"></div>
        <span class="cam-txt live" id="hero_cam_txt">Connecting&#8230;</span>
      </div>
      <div class="hero-people"><b id="hero_total">0</b> people in gym</div>
    </div>

    <div class="hero-stats-col">
      <div class="hero-stat">
        <div class="hero-stat-lbl">Available Now</div>
        <div class="hero-stat-val" id="hero_avail">Checking&#8230;</div>
      </div>
      <div class="hero-stat">
        <div class="hero-stat-lbl">Estimated Wait</div>
        <div class="hero-stat-val" id="hero_wait">&#8212;</div>
      </div>
      <div class="hero-stat">
        <div class="hero-stat-lbl">Best Time Next</div>
        <div class="hero-stat-val" id="hero_best_next">Loading&#8230;</div>
      </div>
    </div>

  </div>
</div>

<!-- 2. Recommendation bar -->
<div class="rec-bar unknown" id="rec_bar">
  <div class="rec-icon" id="rec_icon">&#8987;</div>
  <div class="rec-content">
    <div class="rec-label unknown" id="rec_label">Waiting for data&#8230;</div>
    <div class="rec-desc" id="rec_desc">Connecting to sensors</div>
  </div>
  <div class="rec-badge" id="rec_total_badge">&#8212; people</div>
</div>

<!-- 3. Equipment Status -->
<div class="sec">Equipment Zones</div>
<div class="g-zones">

  <!-- Zone A -->
  <div class="zcard" id="zcard_a">
    <div class="zcard-head">
      <div class="zcard-title-row">
        <div class="ztag a">A</div>
        <div class="zcard-title">Equipment Zone A</div>
      </div>
      <div class="zavail-badge zavail-free" id="za_avail_badge">Available Now</div>
    </div>
    <div class="zcard-body">
      <div class="zcard-primary">
        <div class="zcard-session-block">
          <div class="zcard-session-lbl" id="za_session_label">Available</div>
          <div class="zcard-session-timer idle" id="za_session_timer">&#8212;</div>
        </div>
        <div class="zcard-free-est" id="za_free_est">No queue</div>
      </div>
      <div class="zcard-divider"></div>
      <div class="zcard-metrics">
        <div class="zcard-metric">
          <div class="zm-lbl">Avg Session</div>
          <div class="zm-val" id="za_avg_disp">&#8212;</div>
        </div>
        <div class="zcard-metric">
          <div class="zm-lbl">Last Used</div>
          <div class="zm-val" id="za_last_used">&#8212;</div>
        </div>
        <div class="zcard-metric">
          <div class="zm-lbl">Sessions Today</div>
          <div class="zm-val" id="za_sessions">&#8212;</div>
        </div>
      </div>
      <div class="zcard-wait-row">
        <span class="zwait-lbl">Est. Wait</span>
        <span class="zwait-val" id="za_wait_disp">No queue</span>
      </div>
    </div>
  </div>

  <!-- Zone B -->
  <div class="zcard" id="zcard_b">
    <div class="zcard-head">
      <div class="zcard-title-row">
        <div class="ztag b">B</div>
        <div class="zcard-title">Equipment Zone B</div>
      </div>
      <div class="zavail-badge zavail-free" id="zb_avail_badge">Available Now</div>
    </div>
    <div class="zcard-body">
      <div class="zcard-primary">
        <div class="zcard-session-block">
          <div class="zcard-session-lbl" id="zb_session_label">Available</div>
          <div class="zcard-session-timer idle" id="zb_session_timer">&#8212;</div>
        </div>
        <div class="zcard-free-est" id="zb_free_est">No queue</div>
      </div>
      <div class="zcard-divider"></div>
      <div class="zcard-metrics">
        <div class="zcard-metric">
          <div class="zm-lbl">Avg Session</div>
          <div class="zm-val" id="zb_avg_disp">&#8212;</div>
        </div>
        <div class="zcard-metric">
          <div class="zm-lbl">Last Used</div>
          <div class="zm-val" id="zb_last_used">&#8212;</div>
        </div>
        <div class="zcard-metric">
          <div class="zm-lbl">Sessions Today</div>
          <div class="zm-val" id="zb_sessions">&#8212;</div>
        </div>
      </div>
      <div class="zcard-wait-row">
        <span class="zwait-lbl">Est. Wait</span>
        <span class="zwait-val" id="zb_wait_disp">No queue</span>
      </div>
    </div>
  </div>

</div>

</div><!-- /.tab-pane #tab-dashboard -->

<!-- ====== TAB 2: System Health ====== -->
<div class="tab-pane" id="tab-syshealth">

<div class="sec">System</div>
<div class="g5">

  <div class="syscard">
    <div class="clbl">FPS</div>
    <div class="sysv g" id="sys_fps">&#8212;</div>
    <canvas class="spark" id="spark_fps"></canvas>
    <div class="csub">estimated</div>
  </div>

  <div class="syscard">
    <div class="clbl">Inference</div>
    <div class="sysv b" id="sys_inf">&#8212;</div>
    <div class="sbar-wrap">
      <div class="sbar-lbls"><span>0</span><span>200 ms</span></div>
      <div class="sbar"><div class="sfill fb" id="bar_inf" style="width:0%"></div></div>
    </div>
    <div class="csub">ms / frame</div>
  </div>

  <div class="syscard">
    <div class="clbl">CPU Usage</div>
    <div class="sysv a" id="sys_cpu">&#8212;</div>
    <div class="sbar-wrap">
      <div class="sbar-lbls"><span>0%</span><span>400% (Pi 5)</span></div>
      <div class="sbar"><div class="sfill fa" id="bar_cpu" style="width:0%"></div></div>
    </div>
    <div class="csub">multi-core</div>
  </div>

  <div class="syscard">
    <div class="clbl">RAM (RSS)</div>
    <div class="sysv p" id="sys_ram">&#8212;</div>
    <div class="sbar-wrap">
      <div class="sbar-lbls"><span>0</span><span>512 MB</span></div>
      <div class="sbar"><div class="sfill fp" id="bar_ram" style="width:0%"></div></div>
    </div>
    <div class="csub">MB resident set</div>
  </div>

  <div class="syscard">
    <div class="clbl">Loop avg / p95</div>
    <div class="sysv g" id="sys_loop">&#8212;</div>
    <div class="sbar-wrap">
      <div class="sbar-lbls"><span>0</span><span>500 ms</span></div>
      <div class="sbar"><div class="sfill fg" id="bar_loop" style="width:0%"></div></div>
    </div>
    <div class="csub">p95 <b><span id="sys_p95">&#8212;</span> ms</b></div>
  </div>

</div>

<!-- 4. Trends & History -->
<div class="tf-bar">
  <div class="sec" style="margin:0" id="history_sec_label">Trends &#183; last 15 min</div>
  <div style="display:flex;align-items:center;gap:8px">
    <div class="tf-btns" id="hist_tf_btns">
      <button class="tf-btn"        onclick="setHistTimeframe(1,   this)">1 min</button>
      <button class="tf-btn active" onclick="setHistTimeframe(15,  this)">15 min</button>
      <button class="tf-btn"        onclick="setHistTimeframe(60,  this)">1 hr</button>
      <button class="tf-btn"        onclick="setHistTimeframe(0,   this)">Today</button>
    </div>
    <button class="tf-btn" style="border-color:rgba(239,68,68,.3);color:var(--red)" onclick="clearAll()">Clear All</button>
  </div>
</div>

<!-- Insight strip computed from history data -->
<div class="insight-strip" id="insight_strip"></div>

<div style="margin-bottom:13px">
  <div class="cchart">
    <div class="chead">
      <div>
        <div class="ctitle">Current Session Duration</div>
        <div class="csub2">Elapsed time for active sessions (seconds)</div>
      </div>
      <div class="cleg">
        <div class="cleg-item"><div class="cleg-dot" style="background:#00c8ff"></div>Zone A</div>
        <div class="cleg-item"><div class="cleg-dot" style="background:#ffc800"></div>Zone B</div>
      </div>
    </div>
    <div class="chart-wrap">
      <canvas class="chrt" id="chart_longest"></canvas>
      <div class="chart-tooltip" id="tip_longest"></div>
    </div>
    <div class="chart-note">Resets when the session ends</div>
  </div>
</div>

<div class="g2">
  <div class="cchart">
    <div class="chead">
      <div>
        <div class="ctitle">Total Crowd</div>
        <div class="csub2">Current people count across all zones</div>
      </div>
      <div class="cleg">
        <div class="cleg-item"><div class="cleg-dot" style="background:#00e5a0"></div>Total</div>
      </div>
    </div>
    <div class="chart-wrap">
      <canvas class="chrt" id="chart_occ"></canvas>
      <div class="chart-tooltip" id="tip_occ"></div>
    </div>
    <div class="chart-note">Offline-safe chart rendering</div>
  </div>

  <div class="cchart">
    <div class="chead">
      <div>
        <div class="ctitle">Estimated Wait Time</div>
        <div class="csub2">Seconds estimated per waiting person</div>
      </div>
      <div class="cleg">
        <div class="cleg-item"><div class="cleg-dot" style="background:#00c8ff"></div>Zone A</div>
        <div class="cleg-item"><div class="cleg-dot" style="background:#ffc800"></div>Zone B</div>
      </div>
    </div>
    <div class="chart-wrap">
      <canvas class="chrt" id="chart_wait"></canvas>
      <div class="chart-tooltip" id="tip_wait"></div>
    </div>
    <div class="chart-note">No external Chart.js needed</div>
  </div>
</div>

<!-- 5. Peak Hours -->
<div class="tf-bar">
  <div class="sec" style="margin:0" id="hmap_sec_label">Peak Hours &#183; Today</div>
  <div style="display:flex;align-items:center;gap:8px">
    <div class="tf-btns" id="hmap_tf_btns">
      <button class="tf-btn" onclick="setHmapDay(0,this)">Mon</button>
      <button class="tf-btn"        onclick="setHmapDay(1,this)">Tue</button>
      <button class="tf-btn"        onclick="setHmapDay(2,this)">Wed</button>
      <button class="tf-btn"        onclick="setHmapDay(3,this)">Thu</button>
      <button class="tf-btn"        onclick="setHmapDay(4,this)">Fri</button>
      <button class="tf-btn"        onclick="setHmapDay(5,this)">Sat</button>
      <button class="tf-btn"        onclick="setHmapDay(6,this)">Sun</button>
    </div>
  </div>
</div>

<!-- Traffic-light best-times strip (primary) -->
<div class="besttime-card">
  <div class="besttime-header">
    <div class="besttime-title">Predicted Peak Hours &#8212; Next 10 Hours</div>
    <div class="besttime-sub" id="btime_sub">Based on today&#39;s pattern</div>
  </div>
  <div class="btime-row" id="btime_row">
    <div class="btime-slot bts-nodata" style="flex:1">
      <div class="bts-hour">&#8212;</div>
      <div class="bts-dot"></div>
      <div class="bts-label">No data</div>
    </div>
  </div>
</div>

<!-- Hourly heatmap (secondary detail) -->
<div class="hmap-card">
  <div class="hmap-title-row">
    <div>
      <div class="hmap-title">Avg occupancy by hour of day</div>
      <div class="hmap-sub" id="hmap_sub">Loading&#8230;</div>
    </div>
    <div class="hmap-leg">
      <div class="hmap-leg-item">
        <div class="hmap-leg-swatch" style="background:#00c8ff"></div>Zone A
      </div>
      <div class="hmap-leg-item">
        <div class="hmap-leg-swatch" style="background:#ffc800"></div>Zone B
      </div>
    </div>
  </div>
  <div class="hmap-body">
    <div class="hmap-row">
      <div class="hmap-row-lbl">Zone A</div>
      <div class="hmap-cells" id="hmap_za"></div>
    </div>
    <div class="hmap-row">
      <div class="hmap-row-lbl">Zone B</div>
      <div class="hmap-cells" id="hmap_zb"></div>
    </div>
  </div>
  <div class="hmap-hours" id="hmap_hours"></div>
</div>

</div><!-- /.tab-pane #tab-syshealth -->

</div><!-- /.page -->
<footer>GymPulse Prototype &#183; MQTT &#8594; Flask &#8594; Browser &#183; 1 s status poll &#183; 10 s history poll</footer>

<script>
const $ = id => document.getElementById(id);

function switchTab(name, btn) {
  document.querySelectorAll('.tab-pane').forEach(p => p.classList.remove('active'));
  document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
  document.getElementById('tab-' + name).classList.add('active');
  btn.classList.add('active');
  setTimeout(() => {
    redrawHistoryCharts();
    if (sparkBuf.fps) drawSpark("spark_fps", sparkBuf.fps, "#00e5a0");
    if (name === "syshealth") { pollPeak(); }
  }, 50);
}

function set(id, val) {
  const el = $(id);
  if (!el || el.textContent === String(val)) return;
  el.textContent = val;
  el.classList.remove("flash");
  void el.offsetWidth;
  el.classList.add("flash");
}

// ── Clock ────────────────────────────────────────────────────────────────
const tick = () => $("hdr_clock").textContent = new Date().toLocaleTimeString("en-GB");
tick(); setInterval(tick, 1000);

// ── Status maps ──────────────────────────────────────────────────────────
const CROWD_MAP = {
  LOW:  { cls: "quiet",    icon: "\u{1F7E2}", label: "Quiet",    recKey: "go"    },
  MID:  { cls: "moderate", icon: "\u{1F7E1}", label: "Moderate", recKey: "wait"  },
  HIGH: { cls: "busy",     icon: "\u{1F534}", label: "Busy",     recKey: "later" },
};

// ── Format helpers ───────────────────────────────────────────────────────
function fmtSecTimer(sec) {
  sec = Math.max(0, +sec || 0);
  if (sec === 0) return "\u2014";
  const m = Math.floor(sec / 60), s = Math.round(sec % 60);
  return m + ":" + String(s).padStart(2, "0");
}

function fmtWait(sec) {
  sec = Math.max(0, +sec || 0);
  if (sec < 5)  return "No queue";
  if (sec < 60) return "~" + Math.ceil(sec / 30) * 30 + " s";
  return "~" + Math.ceil(sec / 60) + " min";
}

function fmtAvgSession(sec) {
  sec = +sec || 0;
  if (sec <= 0) return "\u2014";
  if (sec < 60) return sec.toFixed(1) + " s";
  return (sec / 60).toFixed(1) + " min";
}

// ── Per-zone "last used" client-side tracker ─────────────────────────────
const zoneLastUsed = {
  za: { wasOccupied: false, freeAtMs: null },
  zb: { wasOccupied: false, freeAtMs: null },
};

function trackZoneOccupancy(pfx, isOcc) {
  const t = zoneLastUsed[pfx];
  if (isOcc) {
    t.wasOccupied = true;
    t.freeAtMs = null;           // reset; zone is active
  } else if (t.wasOccupied && t.freeAtMs === null) {
    t.freeAtMs = Date.now();     // just transitioned to free
  }
}

function fmtLastUsedLive(pfx, isOcc) {
  if (isOcc) return "Now";
  const t = zoneLastUsed[pfx];
  if (!t.freeAtMs) return "\u2014";          // never been occupied
  const s = (Date.now() - t.freeAtMs) / 1000;
  if (s < 5)   return "Just now";
  if (s < 60)  return Math.round(s) + " s ago";
  const m = Math.floor(s / 60);
  if (m < 60)  return m + " min ago";
  return Math.floor(m / 60) + " hr ago";
}

function fmtFreeEst(avgSec, currentSec, waitSec) {
  const wt = +waitSec || 0;
  const av = +avgSec  || 0;
  const cu = +currentSec || 0;
  if (wt > 60) return "~" + Math.ceil(wt / 60) + " min wait";
  if (wt > 5)  return "~" + Math.ceil(wt) + " s wait";
  if (av <= 0) return "\u2014";
  const rem = Math.max(0, av - cu);
  if (rem < 10) return "Freeing soon";
  if (rem < 60) return "Free in ~" + Math.ceil(rem / 30) * 30 + " s";
  return "Free in ~" + Math.ceil(rem / 60) + " min";
}

// ── Gym status pill ──────────────────────────────────────────────────────
function setCrowd(status) {
  const m = CROWD_MAP[status] || CROWD_MAP.LOW;
  const pill = $("gym_status_pill");
  if (pill) {
    pill.className = "gym-status-pill " + m.cls;
    $("gym_status_icon").textContent = m.icon;
    $("gym_status_txt").textContent  = m.label;
  }
  set("crowd_txt", status);
}

// ── Recommendation bar ───────────────────────────────────────────────────
function updateRecommendation(status, zaOcc, zbOcc, freshLevel, totalPeople) {
  const bar   = $("rec_bar");
  const lbl   = $("rec_label");
  const badge = $("rec_total_badge");
  if (badge) badge.textContent = totalPeople + " people";

  let recKey = "go", label = "", desc = "", cls = "go";

  if (freshLevel === "offline") {
    recKey = "unknown"; cls = "unknown";
    label  = "RPI 1 offline";
    desc   = "Data unavailable \u2014 check System Health";
  } else {
    const freeA = !zaOcc, freeB = !zbOcc;
    const freeZone = freeA ? "Zone A" : "Zone B";
    const busyZone = freeA ? "Zone B" : "Zone A";
    if (status === "HIGH") {
      recKey = "later"; cls = "later";
      label  = "Best Time Later";
      desc   = "Gym is busy \u2014 quietest upcoming window shown below";
    } else if (!freeA && !freeB) {
      recKey = "wait"; cls = "wait";
      label  = "Wait a Bit";
      desc   = "All zones in use \u2014 usually frees up within the avg session time";
    } else if (status === "MID") {
      recKey = "wait"; cls = "wait";
      label  = "Wait a Bit";
      desc   = (freeA && freeB ? "Both zones free" : freeZone + " is free") + " but gym is moderately busy";
    } else {
      recKey = "go"; cls = "go";
      label  = "Go Now";
      desc   = (freeA && freeB ? "Both zones available" : freeZone + " is free") + ", gym is quiet";
    }
  }

  if (bar) bar.className = "rec-bar " + cls;
  if (lbl) { lbl.className = "rec-label " + cls; lbl.textContent = label; }

  const icons = { go: "\u2713", wait: "\u23F1", later: "\u23F0", unknown: "\u231B" };
  set("rec_icon", icons[recKey] || icons.unknown);
  set("rec_desc", desc);
}

// ── Hero stats ───────────────────────────────────────────────────────────
function updateHeroStats(totalPeople, zaData, zbData) {
  set("hero_total", totalPeople);
  const zaOcc = (+zaData.people_now || 0) > 0;
  const zbOcc = (+zbData.people_now || 0) > 0;
  let av = (!zaOcc && !zbOcc) ? "Both zones free"
         : (!zaOcc)           ? "Zone A free \u00B7 Zone B in use"
         : (!zbOcc)           ? "Zone A in use \u00B7 Zone B free"
         :                      "All zones occupied";
  set("hero_avail", av);
  const maxW = Math.max(+zaData.estimated_waiting_time_sec || 0, +zbData.estimated_waiting_time_sec || 0);
  set("hero_wait", maxW < 5 ? "No queue" : fmtWait(maxW));
}

// ── Zone card update ─────────────────────────────────────────────────────
const sparkBuf = {};

function setZone(pfx, colorCls, data) {
  const n    = +data.people_now || 0;
  const zLtr = pfx.slice(-1);

  // Feed hidden anchors so original setters don't miss
  set(pfx + "_big",   n);
  set(pfx + "_timer", data.zone_occupied_duration_hhmmss || data.zone_timer_hhmmss || "00:00:00");
  const ownerId = (!data.current_owner_id && data.current_owner_id !== 0) ? "\u2014" : ("ID " + data.current_owner_id);
  set(pfx + "_owner", ownerId);
  set(pfx + "_session", (+data.current_session_duration_sec || 0).toFixed(1) + " s");
  set(pfx + "_session_start", (data.current_session_start_time || "").split(" ")[1] || "\u2014");
  set(pfx + "_valid",  data.is_valid_machine_usage ? "YES" : "NO");
  set(pfx + "_wait",   (+data.estimated_waiting_time_sec || 0).toFixed(1) + " s");
  set(pfx + "_ids",    data.tracked_ids  || "\u2014");
  set(pfx + "_unique", data.unique_ids_seen_in_zone || "0");
  set(pfx + "_multi",  data.multi_person_detected ? "YES" : "NO");
  const lc = +data.last_completed_session_duration_sec || 0;
  set(pfx + "_last_completed", lc > 0 ? lc.toFixed(1) + " s" : "\u2014");
  const ob = $("badge_" + zLtr);
  if (ob) {
    if (n > 0) { ob.textContent = n === 1 ? "1 PERSON" : n + " PEOPLE"; ob.className = "obadge oactive " + colorCls; }
    else       { ob.textContent = "EMPTY"; ob.className = "obadge oempty"; }
  }

  // New resident-facing card
  const isOcc   = n > 0;
  trackZoneOccupancy(pfx, isOcc);
  const avgSec  = +data.avg_completed_session_duration_sec || +data.avg_individual_usage_time_sec || 0;
  const curSec  = +data.current_session_duration_sec || 0;
  const waitSec = +data.estimated_waiting_time_sec   || 0;
  const tmrSec  = +data.zone_timer_sec || 0;

  const avBadge = $(pfx + "_avail_badge");
  const sesTimer = $(pfx + "_session_timer");
  const sesLabel = $(pfx + "_session_label");
  const freeEst  = $(pfx + "_free_est");
  const cardEl   = $("zcard_" + zLtr);

  if (avBadge) {
    if (isOcc) {
      avBadge.textContent = "In Use";
      avBadge.className   = "zavail-badge zavail-inuse-" + colorCls;
    } else {
      avBadge.textContent = "Available Now";
      avBadge.className   = "zavail-badge zavail-free";
    }
  }
  if (sesTimer) {
    if (isOcc && curSec > 0) {
      sesTimer.textContent = fmtSecTimer(curSec);
      sesTimer.className   = "zcard-session-timer " + colorCls;
    } else {
      sesTimer.textContent = "\u2014";
      sesTimer.className   = "zcard-session-timer idle";
    }
  }
  if (sesLabel) sesLabel.textContent = isOcc ? "Current session" : "Available";
  if (freeEst) {
    if (isOcc) {
      freeEst.textContent = fmtFreeEst(avgSec, curSec, waitSec);
      freeEst.className   = "zcard-free-est inuse";
    } else {
      freeEst.textContent = "No queue";
      freeEst.className   = "zcard-free-est";
    }
  }
  set(pfx + "_wait_disp", waitSec < 5 ? "No queue" : fmtWait(waitSec));
  set(pfx + "_last_used", fmtLastUsedLive(pfx, isOcc));
  if (cardEl) cardEl.className = "zcard" + (isOcc ? " zone-in-use-" + colorCls : "");
}

// ── Sparkline ────────────────────────────────────────────────────────────
function drawSpark(id, buf, color) {
  const cv = $(id); if (!cv) return;
  const dpr = window.devicePixelRatio || 1;
  const W = Math.max(1, cv.clientWidth * dpr), H = Math.max(1, cv.clientHeight * dpr);
  cv.width = W; cv.height = H;
  const ctx = cv.getContext("2d"); ctx.clearRect(0,0,W,H);
  if (buf.length < 2) return;
  const px=1,py=2,w=W-px*2,h=H-py*2, hi=Math.max(...buf)*1.2||1;
  const x=i=>px+(i/(buf.length-1))*w, y=v=>py+h-(v/hi)*h;
  const g=ctx.createLinearGradient(0,py,0,py+h);
  g.addColorStop(0,color+"44"); g.addColorStop(1,color+"00");
  ctx.beginPath(); ctx.moveTo(x(0),y(buf[0]));
  buf.forEach((v,i)=>{if(i)ctx.lineTo(x(i),y(v))});
  ctx.lineTo(x(buf.length-1),H); ctx.lineTo(x(0),H); ctx.closePath();
  ctx.fillStyle=g; ctx.fill();
  ctx.beginPath(); ctx.moveTo(x(0),y(buf[0]));
  buf.forEach((v,i)=>{if(i)ctx.lineTo(x(i),y(v))});
  ctx.strokeStyle=color; ctx.lineWidth=1.5*dpr; ctx.lineJoin="round"; ctx.lineCap="round"; ctx.stroke();
}

function hexToRgba(hex, alpha) {
  const r=hex.replace("#",""), f=r.length===3?r.split("").map(c=>c+c).join(""):r;
  const n=parseInt(f,16);
  return `rgba(${(n>>16)&255},${(n>>8)&255},${n&255},${alpha})`;
}

function drawLineSeries(ctx, pts, color, baseY, dpr) {
  if (pts.length<2){
    if(pts.length===1){ctx.beginPath();ctx.arc(pts[0].x,pts[0].y,2.5*dpr,0,Math.PI*2);ctx.fillStyle=color;ctx.fill();}
    return;
  }
  ctx.beginPath(); ctx.moveTo(pts[0].x,pts[0].y);
  for(let i=1;i<pts.length;i++) ctx.lineTo(pts[i].x,pts[i].y);
  ctx.strokeStyle=color; ctx.lineWidth=1.8*dpr; ctx.lineJoin="round"; ctx.lineCap="round"; ctx.stroke();
  ctx.beginPath(); ctx.moveTo(pts[0].x,pts[0].y);
  for(let i=1;i<pts.length;i++) ctx.lineTo(pts[i].x,pts[i].y);
  ctx.lineTo(pts[pts.length-1].x,baseY); ctx.lineTo(pts[0].x,baseY); ctx.closePath();
  ctx.fillStyle=hexToRgba(color,0.10); ctx.fill();
  for(const p of pts){ctx.beginPath();ctx.arc(p.x,p.y,1.8*dpr,0,Math.PI*2);ctx.fillStyle=color;ctx.fill();}
}

function drawSimpleChart(id, labels, d1, d2, c1, c2, decimals) {
  const cv=$(id); if(!cv) return;
  const dpr=window.devicePixelRatio||1;
  const W=Math.max(1,cv.clientWidth*dpr), H=Math.max(1,cv.clientHeight*dpr);
  cv.width=W; cv.height=H;
  const ctx=cv.getContext("2d"); ctx.clearRect(0,0,W,H);
  ctx.fillStyle="#0d1320"; ctx.fillRect(0,0,W,H);
  const pL=34*dpr,pR=10*dpr,pT=10*dpr,pB=22*dpr;
  const plotX=pL,plotY=pT,plotW=Math.max(10,W-pL-pR),plotH=Math.max(10,H-pT-pB),baseY=plotY+plotH;
  ctx.strokeStyle="#19253a"; ctx.lineWidth=1*dpr;
  ctx.beginPath(); ctx.rect(plotX,plotY,plotW,plotH); ctx.stroke();
  const count=Math.max(d1.length,d2.length,labels.length);
  if(count===0){
    ctx.fillStyle="#4a5d74"; ctx.font=`${11*dpr}px ui-monospace,monospace`;
    ctx.fillText("No data yet",plotX+8*dpr,plotY+20*dpr); return;
  }
  const maxVal=Math.max(1,...d1,...d2), yMax=Math.max(1,maxVal*1.15);
  ctx.font=`${10*dpr}px ui-monospace,monospace`; ctx.fillStyle="#4a5d74"; ctx.strokeStyle="#19253a55";
  for(let i=0;i<=4;i++){
    const frac=i/4, y=plotY+plotH-frac*plotH;
    ctx.beginPath(); ctx.moveTo(plotX,y); ctx.lineTo(plotX+plotW,y); ctx.stroke();
    const val=yMax*frac, txt=decimals>0?val.toFixed(decimals):Math.round(val).toString();
    ctx.fillText(txt,2*dpr,y+3*dpr);
  }
  const xStep=count>1?plotW/(count-1):0;
  const mkPts=arr=>arr.map((v,i)=>({x:plotX+(count>1?i*xStep:plotW/2),y:plotY+plotH-(Math.max(0,v)/yMax)*plotH}));
  drawLineSeries(ctx,mkPts(d1),c1,baseY,dpr);
  drawLineSeries(ctx,mkPts(d2),c2,baseY,dpr);
  const stride=Math.max(1,Math.ceil(count/6)); ctx.fillStyle="#4a5d74";
  for(let i=0;i<count;i+=stride){
    const x=plotX+(count>1?i*xStep:plotW/2);
    ctx.fillText((labels[i]||"").slice(0,8),x-12*dpr,H-6*dpr);
  }
  return {pts1:mkPts(d1),pts2:mkPts(d2),plotX,plotW,plotH,plotY,baseY,dpr,xStep,count};
}

function attachChartHover(canvasId, tipId, lk1, lk2, decimals, singleLabel) {
  const cv=$(canvasId), tip=$(tipId); if(!cv||!tip) return;
  cv.addEventListener("mousemove", e=>{
    const rect=cv.getBoundingClientRect(), dpr=window.devicePixelRatio||1;
    const mx=(e.clientX-rect.left)*dpr;
    const d1=historyCache[lk1]||[], d2=historyCache[lk2]||[];
    const count=Math.max(d1.length,d2.length);
    if(count===0){tip.classList.remove("visible");return;}
    const pL=34*dpr,pR=10*dpr,W=cv.width,plotW=Math.max(10,W-pL-pR);
    const xStep=count>1?plotW/(count-1):0;
    const i=Math.max(0,Math.min(count-1,xStep>0?Math.round((mx-pL)/xStep):0));
    const v1=d1[i]!==undefined?d1[i]:null, v2=d2[i]!==undefined?d2[i]:null;
    const lbl=historyCache.labels[i]||"";
    const fmt=v=>v===null?"\u2014":decimals>0?(+v).toFixed(decimals):(+v).toFixed(0);
    if(singleLabel){
      tip.innerHTML=`<span style="color:var(--muted)">${lbl}</span><br>`+
        `<span style="color:#00e5a0">${singleLabel}</span> ${fmt(v1)}`;
    } else {
      tip.innerHTML=`<span style="color:var(--muted)">${lbl}</span><br>`+
        `<span class="tip-za">Zone A</span> ${fmt(v1)}${decimals>0?" s":""}<br>`+
        `<span class="tip-zb">Zone B</span> ${fmt(v2)}${decimals>0?" s":""}`;
    }
    tip.style.left=((pL+i*xStep)/W*100).toFixed(1)+"%"; tip.style.top="6px";
    tip.classList.add("visible");
  });
  cv.addEventListener("mouseleave",()=>tip.classList.remove("visible"));
}

function attachAllChartHovers(){
  attachChartHover("chart_occ",    "tip_occ",    "total",     "",          0, "Total");
  attachChartHover("chart_wait",   "tip_wait",   "za_wait",   "zb_wait",   1);
  attachChartHover("chart_longest","tip_longest","za_longest","zb_longest",1);
}

const historyCache={labels:[],za:[],zb:[],za_wait:[],zb_wait:[],za_longest:[],zb_longest:[],total:[],za_sess:[],zb_sess:[]};

function redrawHistoryCharts(){
  drawSimpleChart("chart_occ",    historyCache.labels,historyCache.total,     [],                     "#00e5a0","#00e5a0",0);
  drawSimpleChart("chart_wait",   historyCache.labels,historyCache.za_wait,   historyCache.zb_wait,   "#00c8ff","#ffc800",1);
  drawSimpleChart("chart_longest",historyCache.labels,historyCache.za_longest,historyCache.zb_longest,"#00c8ff","#ffc800",1);
}

window.addEventListener("resize",()=>{
  redrawHistoryCharts();
  if(sparkBuf.fps) drawSpark("spark_fps",sparkBuf.fps,"#00e5a0");
});

// ── Insight strip ────────────────────────────────────────────────────────
function computeInsights(cache) {
  const strip=$("insight_strip"); if(!strip) return;
  const za=cache.za||[], zb=cache.zb||[], zaW=cache.za_wait||[], zbW=cache.zb_wait||[];
  const tot=cache.total||[], n=za.length;
  if(n<3){strip.innerHTML="";return;}
  const chips=[];
  // Peak crowd time
  const combined=za.map((v,i)=>v+(zb[i]||0));
  const peakVal=Math.max(...combined);
  if(peakVal>0){
    const pi=combined.indexOf(peakVal), pl=(cache.labels[pi]||"").slice(0,8);
    if(pl) chips.push({dot:"#f59e0b",text:"Crowd peaked at "+pl});
  }
  // Which zone had longer waits
  const avgZaW=zaW.length?zaW.reduce((a,b)=>a+b,0)/zaW.length:0;
  const avgZbW=zbW.length?zbW.reduce((a,b)=>a+b,0)/zbW.length:0;
  if(avgZaW>1||avgZbW>1){
    chips.push({dot:"#00c8ff",text:(avgZaW>=avgZbW?"Zone A":"Zone B")+" had the longer average wait"});
  }
  // Recent vs overall traffic
  if(n>5){
    const recent=combined.slice(-3), overall=combined;
    const rAvg=recent.reduce((a,b)=>a+b,0)/recent.length;
    const oAvg=overall.reduce((a,b)=>a+b,0)/overall.length;
    if(rAvg<oAvg*0.7) chips.push({dot:"#00e5a0",text:"Current traffic is below the session average"});
    else if(rAvg>oAvg*1.3) chips.push({dot:"#ef4444",text:"Current traffic is above the session average"});
  }
  if(!chips.length){strip.innerHTML="";return;}
  strip.innerHTML=chips.map(c=>
    `<div class="insight-chip"><div class="ic-dot" style="background:${c.dot}"></div>${c.text}</div>`
  ).join("");
}

// ── Best-times traffic lights ────────────────────────────────────────────
function updateBestTimes(data) {
  const row=$("btime_row"), sub=$("btime_sub"); if(!row) return;
  const nowH=new Date().getHours();
  const combined=data.za.map((v,i)=>v+(data.zb[i]||0));
  const hasData=data.samples.some(s=>s>0);
  if(!hasData){
    row.innerHTML=`<div class="btime-slot bts-nodata" style="flex:1"><div class="bts-hour">\u2014</div><div class="bts-dot"></div><div class="bts-label">No data</div></div>`;
    if(sub) sub.textContent="No snapshots in range";
    return;
  }
  const maxC=Math.max(...combined.filter(v=>v>0),1);
  // Find quietest upcoming hour (1-10 hr ahead)
  let bestH=-1, bestV=Infinity;
  for(let o=1;o<=10;o++){
    const h=(nowH+o)%24;
    if(data.samples[h]>0 && combined[h]<bestV){bestV=combined[h];bestH=h;}
  }
  if(bestH>=0){
    const hl=bestH===0?"12am":bestH<12?bestH+"am":bestH===12?"12pm":(bestH-12)+"pm";
    if(sub) sub.textContent="Predicted from historical data \u00B7 quietest: "+hl;
    const be=$("hero_best_next");
    if(be) be.textContent="Best window: ~"+hl;
  } else {
    if(sub) sub.textContent="Predicted from historical data";
    const be=$("hero_best_next");
    if(be) be.textContent="Not enough data yet";
  }
  // Render 10 slots: now + next 9
  let slots="";
  for(let o=0;o<10;o++){
    const h=(nowH+o)%24;
    const val=combined[h], sam=data.samples[h];
    const hr12=h===0?"12a":h<12?h+"a":h===12?"12p":(h-12)+"p";
    const isNow=o===0;
    let cls,label;
    if(sam===0){cls="bts-nodata";label="\u2014";}
    else{
      const r=val/maxC;
      if(r<0.35)      {cls="bts-quiet";   label="Quiet";}
      else if(r<0.70) {cls="bts-moderate";label="Moderate";}
      else            {cls="bts-busy";    label="Busy";}
    }
    if(isNow) cls+=" bts-now";
    slots+=`<div class="btime-slot ${cls}">
      <div class="bts-hour">${isNow?"Now":hr12}</div>
      <div class="bts-dot"></div>
      <div class="bts-label">${label}</div>
    </div>`;
  }
  row.innerHTML=slots;
}

// ── Status poll ───────────────────────────────────────────────────────────
async function pollStatus() {
  try {
    const res=await fetch("/api/status",{cache:"no-store"});
    const d=await res.json();
    const p=d.profile, z=d.zones, s=d.summary, fr=d.freshness;
    if(d.demo_mode) $("demo_tag").style.display="";

    // Header freshness pill
    const dotEl=$("cam_dot"), txtEl=$("cam_txt");
    if(dotEl) dotEl.className="cam-dot "+fr.level;
    if(txtEl){txtEl.className="cam-txt "+fr.level; txtEl.textContent=fr.text;}
    // Hero freshness
    const hdot=$("hero_cam_dot"), htxt=$("hero_cam_txt");
    if(hdot) hdot.className="cam-dot "+fr.level;
    if(htxt){htxt.className="cam-txt "+fr.level; htxt.textContent=fr.text;}

    const zaOcc=(+z.equipment_a.people_now||0)>0;
    const zbOcc=(+z.equipment_b.people_now||0)>0;

    setCrowd(s.crowd_status);
    updateHeroStats(s.total_people, z.equipment_a, z.equipment_b);
    updateRecommendation(s.crowd_status, zaOcc, zbOcc, fr.level, s.total_people);
    setZone("za","a",z.equipment_a);
    setZone("zb","b",z.equipment_b);

    // System Health
    const fps=parseFloat(p.fps_est)||0;
    const inf=parseFloat(p.inf_avg_ms||0), cpu=parseFloat(p.cpu_pct||0);
    const ram=parseFloat(p.rss_mb||0),    loop=parseFloat(p.loop_avg_ms||0);
    set("sys_fps", fps.toFixed(2));
    set("sys_inf", inf.toFixed(1)+" ms");
    set("sys_cpu", cpu.toFixed(1)+" %");
    set("sys_ram", ram.toFixed(0)+" MB");
    set("sys_loop",loop.toFixed(1)+" ms");
    set("sys_p95", parseFloat(p.loop_p95_ms||0).toFixed(1));
    $("bar_inf").style.width =Math.min(inf /200*100,100)+"%";
    $("bar_cpu").style.width =Math.min(cpu /400*100,100)+"%";
    $("bar_ram").style.width =Math.min(ram /512*100,100)+"%";
    $("bar_loop").style.width=Math.min(loop/500*100,100)+"%";
    if(!sparkBuf.fps) sparkBuf.fps=[];
    sparkBuf.fps.push(fps); if(sparkBuf.fps.length>60) sparkBuf.fps.shift();
    drawSpark("spark_fps",sparkBuf.fps,"#00e5a0");
  } catch(e){console.error("Status error:",e);}
}

// ── History poll ─────────────────────────────────────────────────────────
let currentHistoryMinutes=15, historyInterval=null;

async function pollHistory(minutes) {
  if(minutes===undefined) minutes=currentHistoryMinutes;
  try {
    const res=await fetch("/api/history?minutes="+minutes,{cache:"no-store"});
    const d=await res.json();
    historyCache.labels    =d.labels    ||[];
    historyCache.za        =d.za        ||[];
    historyCache.zb        =d.zb        ||[];
    historyCache.za_wait   =d.za_wait   ||[];
    historyCache.zb_wait   =d.zb_wait   ||[];
    historyCache.za_longest=d.za_longest||[];
    historyCache.zb_longest=d.zb_longest||[];
    historyCache.total     =d.total     ||[];
    historyCache.za_sess   =d.za_sess   ||[];
    historyCache.zb_sess   =d.zb_sess   ||[];
    redrawHistoryCharts();
    computeInsights(historyCache);
  } catch(e){console.error("History error:",e);}
}

function setHistTimeframe(minutes, btn) {
  currentHistoryMinutes=minutes;
  document.querySelectorAll("#hist_tf_btns .tf-btn").forEach(b=>b.classList.remove("active"));
  btn.classList.add("active");
  const lbl=minutes===0?"Today":minutes===1?"last 1 min":minutes===60?"last 1 hr":"last 15 min";
  const se=$("history_sec_label"); if(se) se.textContent="Trends \u00B7 "+lbl;
  clearInterval(historyInterval);
  historyInterval=setInterval(()=>pollHistory(), minutes<=15?10000:30000);
  pollHistory(minutes);
}

// ── Daily sessions poll ──────────────────────────────────────────────────
async function pollDaily() {
  try {
    const res=await fetch("/api/daily",{cache:"no-store"});
    const d=await res.json();
    set("za_sessions",d.za??"&#8212;");
    set("zb_sessions",d.zb??"&#8212;");
    set("za_avg_session",(d.za_avg||0)>0?((d.za_avg>=60)?Math.floor(d.za_avg/60)+"m "+Math.round(d.za_avg%60)+"s":d.za_avg.toFixed(1)+"s"):"&#8212;");
    set("zb_avg_session",(d.zb_avg||0)>0?((d.zb_avg>=60)?Math.floor(d.zb_avg/60)+"m "+Math.round(d.zb_avg%60)+"s":d.zb_avg.toFixed(1)+"s"):"&#8212;");
    set("za_avg_disp", fmtAvgSession(d.za_avg||0));
    set("zb_avg_disp", fmtAvgSession(d.zb_avg||0));
  } catch(e){console.error("Daily error:",e);}
}

// ── Peak hours ───────────────────────────────────────────────────────────
// JS: Sun=0,Mon=1...Sat=6 → remap to Mon=0...Sun=6
const _jsDay = new Date().getDay();
const _todayHmapDay = _jsDay === 0 ? 6 : _jsDay - 1;
let currentHmapDay = _todayHmapDay, hmapInterval = null;
const DAY_NAMES=["Monday","Tuesday","Wednesday","Thursday","Friday","Saturday","Sunday"];

function setHmapDay(day, btn) {
  currentHmapDay=day;
  document.querySelectorAll("#hmap_tf_btns .tf-btn").forEach(b=>b.classList.remove("active"));
  btn.classList.add("active");
  const se=$("hmap_sec_label"); if(se) se.textContent="Peak Hours \u00B7 "+DAY_NAMES[day];
  clearInterval(hmapInterval);
  hmapInterval=setInterval(()=>pollPeak(), 60000);
  pollPeak();
}

async function clearAll() {
  try {
    const res = await fetch("/api/clear_history", {method:"POST"});
    const d = await res.json();
    if(!res.ok || !d.ok) throw new Error((d && d.error) || "Clear failed");
    await pollHistory(currentHistoryMinutes);
    await pollPeak();
    await pollDaily();
  } catch(e){
    console.error("Clear error:",e);
  }
}

function drawHeatmap(data, day) {
  const zaEl=$("hmap_za"),zbEl=$("hmap_zb"),hrEl=$("hmap_hours"),subEl=$("hmap_sub");
  if(!zaEl||!zbEl) return;
  updateBestTimes(data);
  const maxVal=data.max||1, hasData=data.samples.some(s=>s>0);
  if(!hasData){
    zaEl.innerHTML=zbEl.innerHTML=`<div class="hmap-empty" style="grid-column:1/-1">No data for this day yet</div>`;
    if(subEl) subEl.textContent="No snapshots for "+DAY_NAMES[day||0];
    return;
  }
  const total=data.samples.reduce((a,b)=>a+b,0);
  if(subEl) subEl.textContent=`Based on ${total.toLocaleString()} snapshots \u00B7 ${DAY_NAMES[day||0]}`;

  // Operating hours: 6 am – 11 pm every day
  const opStart=6, opEnd=24;

  // Build hour labels (always, allow re-render for operating highlight)
  if(hrEl){
    hrEl.innerHTML="";
    for(let h=0;h<24;h++){
      const lbl=document.createElement("div"); lbl.className="hmap-hr-lbl";
      if(h>=opStart&&h<opEnd) lbl.classList.add("operating");
      lbl.textContent=h===0?"12a":h<12?h+"a":h===12?"12p":(h-12)+"p";
      hrEl.appendChild(lbl);
    }
  }

  function buildRow(container,values,hexColor){
    container.innerHTML="";
    for(let h=0;h<24;h++){
      const v=values[h]||0, norm=Math.min(v/maxVal,1);
      const r=parseInt(hexColor.slice(1,3),16),g=parseInt(hexColor.slice(3,5),16),b=parseInt(hexColor.slice(5,7),16);
      const alpha=norm<0.04?0.06:0.12+norm*0.82;
      const cell=document.createElement("div"); cell.className="hmap-cell";
      if(h>=opStart&&h<opEnd) cell.classList.add("op-hours");
      cell.style.background=norm<0.02?"var(--dim)":`rgba(${r},${g},${b},${alpha.toFixed(2)})`;
      const hr12=h===0?"12am":h<12?h+"am":h===12?"12pm":(h-12)+"pm";
      const tip=document.createElement("div"); tip.className="hmap-tip";
      tip.innerHTML=`${hr12}<br>avg ${v.toFixed(2)} people<br>${data.samples[h]} samples`;
      cell.appendChild(tip); container.appendChild(cell);
    }
  }
  buildRow(zaEl,data.za,"#00c8ff");
  buildRow(zbEl,data.zb,"#ffc800");
}

async function pollPeak(day) {
  if(day===undefined) day=currentHmapDay;
  try {
    const res=await fetch("/api/peak?day="+day,{cache:"no-store"});
    const d=await res.json();
    drawHeatmap(d,day);
  } catch(e){console.error("Peak hours error:",e);}
}

// ── "Last Used" 1-second tick (independent of API poll) ──────────────────
setInterval(()=>{
  for(const pfx of ["za","zb"]){
    const t=zoneLastUsed[pfx];
    if(t.freeAtMs){
      set(pfx+"_last_used", fmtLastUsedLive(pfx, false));
    }
  }
},1000);

// ── Boot ──────────────────────────────────────────────────────────────────
// Mark today's Peak Hours button active and set heading before first poll
(function initHmapDay(){
  const btns = document.querySelectorAll("#hmap_tf_btns .tf-btn");
  if(btns[_todayHmapDay]) btns[_todayHmapDay].classList.add("active");
  const se = $("hmap_sec_label");
  if(se) se.textContent = "Peak Hours \u00B7 " + DAY_NAMES[_todayHmapDay];
})();
pollStatus();
pollHistory();
pollDaily();
pollPeak();
setInterval(pollStatus, 1000);
historyInterval=setInterval(()=>pollHistory(), 10000);
setInterval(pollDaily, 60000);
hmapInterval=setInterval(()=>pollPeak(), 60000);
attachAllChartHovers();
</script>
</body>
</html>
"""


# ─── Routes ────────────────────────────────────────────────────────────────────
@app.route("/")
def index():
    return render_template_string(HTML)


@app.route("/api/status")
def api_status():
    profile = get_current_profile()
    zones = get_current_zones()

    za = safe_int(zones["equipment_a"].get("people_now", 0))
    zb = safe_int(zones["equipment_b"].get("people_now", 0))

    total_people = safe_int(profile.get("total_now_ids", za + zb))
    crowd_status = profile.get("crowd_status") or crowd_status_from_count(total_people)

    return jsonify({
        "demo_mode": DEMO_MODE,
        "freshness": compute_freshness(profile.get("timestamp", "")),
        "profile": {
            "timestamp":   profile.get("timestamp", "—"),
            "fps_est":     profile.get("fps_est", "0"),
            "inf_avg_ms":  profile.get("inf_avg_ms", "0"),
            "loop_avg_ms": profile.get("loop_avg_ms", "0"),
            "loop_p95_ms": profile.get("loop_p95_ms", "0"),
            "loop_p99_ms": profile.get("loop_p99_ms", "0"),
            "cap_avg_ms":  profile.get("cap_avg_ms", "0"),
            "post_avg_ms": profile.get("post_avg_ms", "0"),
            "disp_avg_ms": profile.get("disp_avg_ms", "0"),
            "cpu_pct":     profile.get("cpu_pct", "0"),
            "rss_mb":      profile.get("rss_mb", "0"),
        },
        "zones": {
            "equipment_a": {
                "people_now":                        zones["equipment_a"].get("people_now", "0"),
                "unique_ids_seen_in_zone":          zones["equipment_a"].get("unique_ids_seen_in_zone", "0"),
                "tracked_ids":                      zones["equipment_a"].get("tracked_ids", "—"),
                "zone_timer_hhmmss":                zones["equipment_a"].get("zone_timer_hhmmss", "00:00:00"),
                "zone_timer_sec":                   zones["equipment_a"].get("zone_timer_sec", "0"),
                "zone_occupied_duration_sec":       zones["equipment_a"].get("zone_occupied_duration_sec", "0"),
                "zone_occupied_duration_hhmmss":    zones["equipment_a"].get("zone_occupied_duration_hhmmss", "00:00:00"),
                "current_owner_id":                 zones["equipment_a"].get("current_owner_id", ""),
                "current_session_start_time":       zones["equipment_a"].get("current_session_start_time", ""),
                "current_session_duration_sec":     zones["equipment_a"].get("current_session_duration_sec", "0"),
                "is_valid_machine_usage":           zones["equipment_a"].get("is_valid_machine_usage", False),
                "multi_person_detected":            zones["equipment_a"].get("multi_person_detected", False),
                "last_completed_session_duration_sec": zones["equipment_a"].get("last_completed_session_duration_sec", "0"),
                "completed_sessions_count":         zones["equipment_a"].get("completed_sessions_count", "0"),
                "avg_completed_session_duration_sec": zones["equipment_a"].get("avg_completed_session_duration_sec", "0"),
                "avg_individual_usage_time_sec":    zones["equipment_a"].get("avg_individual_usage_time_sec", "0"),
                "current_longest_usage_time_sec":   zones["equipment_a"].get("current_longest_usage_time_sec", "0"),
                "estimated_waiting_time_sec":       zones["equipment_a"].get("estimated_waiting_time_sec", "0"),
            },
            "equipment_b": {
                "people_now":                        zones["equipment_b"].get("people_now", "0"),
                "unique_ids_seen_in_zone":          zones["equipment_b"].get("unique_ids_seen_in_zone", "0"),
                "tracked_ids":                      zones["equipment_b"].get("tracked_ids", "—"),
                "zone_timer_hhmmss":                zones["equipment_b"].get("zone_timer_hhmmss", "00:00:00"),
                "zone_timer_sec":                   zones["equipment_b"].get("zone_timer_sec", "0"),
                "zone_occupied_duration_sec":       zones["equipment_b"].get("zone_occupied_duration_sec", "0"),
                "zone_occupied_duration_hhmmss":    zones["equipment_b"].get("zone_occupied_duration_hhmmss", "00:00:00"),
                "current_owner_id":                 zones["equipment_b"].get("current_owner_id", ""),
                "current_session_start_time":       zones["equipment_b"].get("current_session_start_time", ""),
                "current_session_duration_sec":     zones["equipment_b"].get("current_session_duration_sec", "0"),
                "is_valid_machine_usage":           zones["equipment_b"].get("is_valid_machine_usage", False),
                "multi_person_detected":            zones["equipment_b"].get("multi_person_detected", False),
                "last_completed_session_duration_sec": zones["equipment_b"].get("last_completed_session_duration_sec", "0"),
                "completed_sessions_count":         zones["equipment_b"].get("completed_sessions_count", "0"),
                "avg_completed_session_duration_sec": zones["equipment_b"].get("avg_completed_session_duration_sec", "0"),
                "avg_individual_usage_time_sec":    zones["equipment_b"].get("avg_individual_usage_time_sec", "0"),
                "current_longest_usage_time_sec":   zones["equipment_b"].get("current_longest_usage_time_sec", "0"),
                "estimated_waiting_time_sec":       zones["equipment_b"].get("estimated_waiting_time_sec", "0"),
            },
        },
        "summary": {
            "crowd_status": crowd_status,
            "total_people": total_people,
            "total_zones": za + zb,
        },
    })


@app.route("/api/test_telegram")
def api_test_telegram():
    if not TELEGRAM_TEST_ENDPOINT_ENABLED:
        return jsonify({"ok": False, "error": "test endpoint disabled"}), 403

    msg = build_telegram_test_message()
    ok, detail = send_telegram_message(msg)
    status_code = 200 if ok else 500
    return jsonify({
        "ok": ok,
        "telegram_enabled": TELEGRAM_ENABLED,
        "chat_id_configured": bool(TELEGRAM_CHAT_ID),
        "bot_token_configured": bool(TELEGRAM_BOT_TOKEN),
        "detail": detail[:500] if isinstance(detail, str) else str(detail),
        "preview": msg,
        "config_source_hint": "Uses shell environment first, then local telegram.env beside app.py",
    }), status_code


@app.route("/api/history")
def api_history():
    minutes = int(request.args.get("minutes", HISTORY_MINUTES))
    return jsonify(get_history(minutes))


@app.route("/api/daily")
def api_daily():
    return jsonify(get_daily_sessions())


@app.route("/api/peak")
def api_peak():
    minutes = int(request.args.get("minutes", -1))
    day_raw = request.args.get("day", None)
    day = int(day_raw) if day_raw is not None else None
    return jsonify(get_peak_hours(minutes, day=day))


@app.route("/api/clear_history", methods=["POST"])
def api_clear_history():
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.execute("DELETE FROM snapshots")
        deleted = cursor.rowcount if cursor.rowcount is not None else 0
        conn.commit()
        conn.close()
        return jsonify({"ok": True, "deleted": deleted})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


if __name__ == "__main__":
    init_db()
    mqtt_client = start_mqtt()
    telegram_thread = start_telegram_command_listener()
    print(f"Telegram enabled: {TELEGRAM_ENABLED}", flush=True)
    print(f"Telegram chat configured: {bool(TELEGRAM_CHAT_ID)}", flush=True)
    print(f"Telegram bot token configured: {bool(TELEGRAM_BOT_TOKEN)}", flush=True)
    print(f"Telegram commands enabled: {TELEGRAM_COMMANDS_ENABLED}", flush=True)
    app.run(host="0.0.0.0", port=5000, debug=False)
