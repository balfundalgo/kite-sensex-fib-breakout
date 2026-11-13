"""
logger.py
=========
One app log per day plus two CSV trails:

  logs/app_YYYY-MM-DD.log     everything the engine does
  logs/trades_YYYY-MM-DD.csv  one row per strategy trade (entry -> exit)
  logs/orders_YYYY-MM-DD.csv  one row per broker order, per account
  logs/candles_YYYY-MM-DD.csv every REST candle used (and every revision),
                              for checking against the Kite chart
"""

import csv
import logging
import threading
from datetime import datetime

import config

_gui_sinks = []
_csv_lock = threading.Lock()


class _GuiHandler(logging.Handler):
    def emit(self, record):
        line = self.format(record)
        for sink in list(_gui_sinks):
            try:
                sink(line)
            except Exception:
                pass


def add_gui_sink(fn):
    _gui_sinks.append(fn)


def _today() -> str:
    return config.now_ist().strftime("%Y-%m-%d")


def setup() -> logging.Logger:
    log = logging.getLogger("fib")
    if log.handlers:
        return log
    log.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s  %(levelname)-7s %(message)s", "%H:%M:%S")

    fh = logging.FileHandler(config.LOG_DIR / f"app_{_today()}.log", encoding="utf-8")
    fh.setFormatter(logging.Formatter("%(asctime)s  %(levelname)-7s %(message)s"))
    log.addHandler(fh)

    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    log.addHandler(sh)

    gh = _GuiHandler()
    gh.setFormatter(fmt)
    log.addHandler(gh)

    logging.getLogger("kiteconnect").setLevel(logging.WARNING)
    return log


logger = setup()


TRADE_FIELDS = ["date", "side", "symbol", "level", "level_price", "entry_time",
                "entry_price", "sl", "target", "exit_time", "exit_price",
                "reason", "qty", "pnl", "mode"]

ORDER_FIELDS = ["time", "account", "broker", "symbol", "txn", "order_type",
                "level", "qty", "price", "trigger", "entry", "sl", "target",
                "order_id", "result", "mode"]


CANDLE_FIELDS = ["time", "side", "symbol", "minute", "open", "high", "low", "close",
                 "colour", "event", "lag_s", "previous"]


def _append(prefix: str, fields: list, row: dict) -> None:
    path = config.LOG_DIR / f"{prefix}_{_today()}.csv"
    with _csv_lock:
        new = not path.exists()
        with open(path, "a", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
            if new:
                w.writeheader()
            w.writerow(row)


def trade_row(row: dict) -> None:
    row.setdefault("date", _today())
    _append("trades", TRADE_FIELDS, row)


def order_row(row: dict) -> None:
    row.setdefault("time", datetime.now().strftime("%H:%M:%S"))
    row.setdefault("broker", config.BROKER)
    _append("orders", ORDER_FIELDS, row)


def candle_row(row: dict) -> None:
    row.setdefault("time", datetime.now().strftime("%H:%M:%S"))
    _append("candles", CANDLE_FIELDS, row)
