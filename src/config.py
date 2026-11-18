"""
config.py
=========
Paths, default settings, settings persistence and IST time helpers.

Everything the user can change lives in settings.json next to the EXE
(or the repo root when running from source). Secrets never leave that file.
"""

import copy
import json
import sys
from datetime import datetime, time as dtime
from pathlib import Path
from zoneinfo import ZoneInfo

APP_NAME = "SENSEX Fib Breakout"
APP_VERSION = "1.5.0"
BROKER = "Zerodha"

IST = ZoneInfo("Asia/Kolkata")


# ----------------------------------------------------------------------
# paths
# ----------------------------------------------------------------------
def _base_dir() -> Path:
    if getattr(sys, "frozen", False):          # PyInstaller EXE
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent  # repo root


BASE_DIR = _base_dir()
SETTINGS_FILE = BASE_DIR / "settings.json"
TOKEN_DIR = BASE_DIR / "tokens"
LOG_DIR = BASE_DIR / "logs"
TOKEN_DIR.mkdir(exist_ok=True)
LOG_DIR.mkdir(exist_ok=True)


# ----------------------------------------------------------------------
# strategy constants (from the spec — not user-editable)
# ----------------------------------------------------------------------
FIB_LEVELS = [0.0, 0.2, 0.5, 0.8, 1.0, 1.309, 1.618,
              2.118, 2.618, 3.118, 3.618, 4.118, 4.618]
MIN_TRADE_LEVEL = 0.5          # NO TRADE BELOW 0.5
HL_ADJUSTMENT = 1000.0         # +1000 before the formulas, -1000 after


# ----------------------------------------------------------------------
# defaults
# ----------------------------------------------------------------------
DEFAULT_SETTINGS = {
    "mode": "PAPER",                 # PAPER | LIVE
    "index": "SENSEX",               # Bank Nifty parked for now

    # contract selection
    "premium_min": 400.0,
    "premium_max": 600.0,
    "premium_target": 500.0,         # pick the premium closest to this
    "strike_window": 4000,           # +/- points around spot to scan
    "selection_shortlist": 6,        # strikes per side whose 09:15 candle is pulled from REST

    # timing
    "ref_candle_time": "09:15",      # the 1-minute reference candle
    "squareoff_time": "15:15",

    # candles (REST only) — WebSocket is LTP only
    "rest_poll_seconds": 2.0,        # how often the full session (09:15 -> now) is re-read
    "bar_grace_seconds": 15.0,       # no candle for the previous minute after this -> use the last one
    "candle_settle_seconds": 6.0,    # accept a candle only this long after its minute closes
                                     # (18 Nov test: Zerodha's candle final by +6 s at the latest;
                                     #  any later correction is still caught and applied)

    # risk / orders
    "sl_points": 10.0,               # SL measured from the broken level
    "product": "MIS",                # MIS | NRML
    "sl_order_type": "SL",           # SL (stop-limit) | SL-M
    "sl_limit_buffer": 5.0,          # limit = trigger - buffer (SL type)
    "market_protection": -1,         # -1 = Kite auto market protection
    "roll_mode": "HOLD",             # HOLD = keep position, move SL | REORDER = sell + buy
    "max_daily_loss": 10000.0,       # rupees, per day, all accounts
    "close_on_lock": True,           # square off when the loss lock trips

    # login callback
    "redirect_host": "127.0.0.1",
    "redirect_port": 8765,

    # accounts: [{name, api_key, api_secret, lots, enabled}]
    "accounts": [],

    "settings_version": 3,
}


def _migrate(stored: dict) -> dict:
    """One-time upgrades of an older settings.json. Runs once per file;
    after that the user's own choices stand."""
    if int(stored.get("settings_version", 1)) < 2:
        # v1.4.1: 5 s was too short (Zerodha corrects closes up to ~13 s late)
        if float(stored.get("candle_settle_seconds", 15.0)) < 15.0:
            stored["candle_settle_seconds"] = 15.0
        stored["settings_version"] = 2
    if int(stored.get("settings_version", 1)) < 3:
        # v1.5: measured live on 18 Nov - final by +6 s; 15 s waited too long
        stored["candle_settle_seconds"] = 6.0
        stored["settings_version"] = 3
    return stored


def load_settings() -> dict:
    data = copy.deepcopy(DEFAULT_SETTINGS)
    if SETTINGS_FILE.exists():
        try:
            stored = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
            before = int(stored.get("settings_version", 1))
            stored = _migrate(stored)
            data.update(stored)
            if before < data["settings_version"]:
                save_settings(data)
        except (json.JSONDecodeError, OSError, ValueError, TypeError):
            pass
    return data


def save_settings(data: dict) -> None:
    SETTINGS_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")


SETTINGS = load_settings()


# ----------------------------------------------------------------------
# time helpers
# ----------------------------------------------------------------------
def now_ist() -> datetime:
    return datetime.now(IST)


def parse_hhmm(text: str) -> dtime:
    h, m = text.strip().split(":")
    return dtime(int(h), int(m))


def today_at(hhmm: str) -> datetime:
    t = parse_hhmm(hhmm)
    n = now_ist()
    return n.replace(hour=t.hour, minute=t.minute, second=0, microsecond=0)
