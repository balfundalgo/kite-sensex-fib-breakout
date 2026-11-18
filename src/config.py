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
APP_VERSION = "1.6.0"
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
# indices (Kite instrument lists)
#   SENSEX options:     exchange BFO, name SENSEX,    spot BSE:SENSEX
#   BANK NIFTY options: exchange NFO, name BANKNIFTY, spot NSE:NIFTY BANK
# Contract-selection settings for Bank Nifty carry the "bn_" prefix
# (bn_premium_min, ...); SENSEX keeps the original keys.
# ----------------------------------------------------------------------
INDICES = {
    "SENSEX":    {"exchange": "BFO", "name": "SENSEX",    "spot": "BSE:SENSEX",
                  "prefix": "",    "label": "SENSEX"},
    "BANKNIFTY": {"exchange": "NFO", "name": "BANKNIFTY", "spot": "NSE:NIFTY BANK",
                  "prefix": "bn_", "label": "BANK NIFTY"},
}
INDEX_CHOICES = ["SENSEX", "BANKNIFTY", "BOTH"]
PER_INDEX_KEYS = ("premium_min", "premium_max", "premium_target", "strike_window",
                  "ref_candle_time")


def active_indices(settings: dict) -> list:
    choice = str(settings.get("index", "SENSEX")).upper()
    return list(INDICES) if choice == "BOTH" else [choice if choice in INDICES else "SENSEX"]


def index_setting(settings: dict, index: str, key: str):
    """Per-index value: SENSEX uses 'premium_min', Bank Nifty 'bn_premium_min'."""
    return settings[INDICES[index]["prefix"] + key]


# ----------------------------------------------------------------------
# defaults
# ----------------------------------------------------------------------
DEFAULT_SETTINGS = {
    "mode": "PAPER",                 # PAPER | LIVE
    "index": "SENSEX",               # SENSEX | BANKNIFTY | BOTH

    # contract selection
    "premium_min": 400.0,
    "premium_max": 600.0,
    "premium_target": 500.0,         # pick the premium closest to this
    "strike_window": 4000,           # +/- points around spot to scan
    "selection_shortlist": 20,       # most strikes per side whose 09:15 candle is read while
                                     # walking to the one nearest 500

    # Bank Nifty contract selection (NFO); the spec leaves these configurable
    "bn_premium_min": 400.0,
    "bn_premium_max": 600.0,
    "bn_premium_target": 500.0,
    "bn_strike_window": 4000,
    "bn_ref_candle_time": "09:15",

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
    "max_daily_profit": 0.0,         # rupees, per day, all accounts, closed + open; 0 = off.
                                     # When reached: everything squared off, no new entries
    "breakeven_pct": 30.0,           # move SL to entry once price has covered this % of
                                     # entry -> target (500 entry, 600 target, 30 -> at 530);
                                     # 0 = off. Not used on 4.618 (no target)

    # login callback
    "redirect_host": "127.0.0.1",
    "redirect_port": 8765,

    # accounts: [{name, api_key, api_secret, lots, enabled}]
    "accounts": [],

    "settings_version": 5,
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
    if int(stored.get("settings_version", 1)) < 4:
        # v1.5.1: strikes are now walked one by one; 6 is too few after a late start
        if int(stored.get("selection_shortlist", 20)) < 20:
            stored["selection_shortlist"] = 20
        stored["settings_version"] = 4
    if int(stored.get("settings_version", 1)) < 5:
        # v1.6: Bank Nifty / both, max profit, breakeven - new keys come from
        # the defaults; nothing existing changes
        stored["settings_version"] = 5
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
