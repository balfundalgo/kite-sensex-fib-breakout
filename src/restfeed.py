"""
restfeed.py
===========
1-minute candles from Kite's REST historical API instead of from ticks.
Same approach as the SENSEX VWAP ladder build:

    REST       candle OHLC (the 09:15 reference candle and every 1-min candle
               used for the "preceding candle is green" check)
    WebSocket  LTP only — level crosses, stop-loss, target, P/L

Why: a candle built from ticks misses any trade that happens between two
ticks, so its high/low/close can differ from the exchange candle. The REST
candle is the exchange's own.

Rules carried over from the VWAP feed (each learned from a live failure):
  * the minute still forming is dropped before anything sees it
  * nothing before today's 09:15 ever reaches the strategy
  * each poll runs with a hard deadline so a hung HTTP call cannot blind
    the strategy; it is abandoned and the next poll carries on
  * every bar records how late it arrived; slow bars and a feed that has
    stopped delivering are logged loudly instead of failing silently
"""

import threading
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from datetime import datetime, timedelta
from typing import Callable, Dict, List, Optional

import config
from logger import logger
from strategy import Candle


def to_ist_naive(dt) -> datetime:
    """Kite returns tz-aware IST datetimes; the engine works in naive IST."""
    if getattr(dt, "tzinfo", None) is not None:
        return dt.astimezone(config.IST).replace(tzinfo=None)
    return dt


def drop_forming(candles: List[Candle], now: datetime) -> List[Candle]:
    """Remove the minute that is still being built (close time in the future)."""
    return [c for c in candles if c.minute + timedelta(minutes=1) <= now]


def fetch_1m(kite, token: int, frm: datetime, to: datetime) -> List[Candle]:
    """One REST call: 1-minute candles for token between frm and to (naive IST)."""
    rows = kite.historical_data(token, frm, to, "minute")
    out = []
    for d in rows or []:
        m = to_ist_naive(d["date"]).replace(second=0, microsecond=0)
        out.append(Candle(m, float(d["open"]), float(d["high"]),
                          float(d["low"]), float(d["close"])))
    return out


class RestCandleFeed:
    """
    Polls the 1-minute endpoint for each leg and calls on_bar(side, candle, lag)
    once for every newly completed candle, in order.
    """

    def __init__(self, kite, legs: Dict[str, int], session_start: datetime,
                 on_bar: Callable, now_fn: Callable, poll: float = 2.0,
                 hard_deadline: float = 10.0, blind_seconds: float = 180.0,
                 late_warn: float = 20.0):
        self.kite = kite
        self.legs = dict(legs)                  # {"CE": token, "PE": token}
        self.session_start = session_start      # today 09:15, naive IST
        self.on_bar = on_bar
        self.now = now_fn
        self.poll = poll
        self.hard_deadline = hard_deadline
        self.blind_seconds = blind_seconds
        self.late_warn = late_warn

        self._last: Dict[str, datetime] = {}    # last minute emitted per leg
        self._polled = set()                    # legs that completed a first poll
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="restpoll")
        self.latencies: List[float] = []
        self.polls = 0
        self.errors = 0
        self.abandoned = 0
        self.last_bar_at = time.time()
        self.last_bar_minute: Optional[datetime] = None
        self._blind_warned = 0.0
        self._late_warned = 0.0
        self._suppressed = 0

    # -- lifecycle ---------------------------------------------------------
    def prime(self, side: str, upto: datetime):
        """Everything at or before `upto` is already handled (e.g. the 09:15 candle)."""
        self._last[side] = max(self._last.get(side, upto), upto)

    def start(self):
        self._thread = threading.Thread(target=self._loop, daemon=True, name="restfeed")
        self._thread.start()
        logger.info("REST candle feed started: 1-minute bars, polling every %.1fs", self.poll)

    def stop(self):
        self._stop.set()
        try:
            self._pool.shutdown(wait=False)
        except Exception:
            pass

    # -- internals ---------------------------------------------------------
    def _loop(self):
        while not self._stop.is_set():
            for side, token in self.legs.items():
                if self._stop.is_set():
                    break
                try:
                    fut = self._pool.submit(self._poll_leg, side, token)
                    fut.result(timeout=self.hard_deadline)
                except FutureTimeout:
                    self.abandoned += 1
                    self.errors += 1
                    logger.error("[%s] REST poll exceeded %.0fs and was abandoned; carrying on",
                                 side, self.hard_deadline)
                except Exception as e:
                    self.errors += 1
                    logger.error("[%s] REST poll error: %s", side, e)
            self._check_blind()
            self._stop.wait(self.poll)

    def _poll_leg(self, side: str, token: int):
        now = self.now()
        candles = fetch_1m(self.kite, token, self.session_start, now)
        self.polls += 1
        candles = drop_forming(candles, self.now())
        last = self._last.get(side)
        replay = side not in self._polled       # first poll = backlog, not live bars
        self._polled.add(side)
        for c in sorted(candles, key=lambda x: x.minute):
            if c.minute < self.session_start:
                continue                       # never yesterday's candles
            if last is not None and c.minute <= last:
                continue
            closed_at = c.minute + timedelta(minutes=1)
            lag = (self.now() - closed_at).total_seconds()
            if not replay:
                self.latencies.append(lag)
                if len(self.latencies) > 2000:
                    self.latencies = self.latencies[-1000:]
                self._warn_late(side, c, lag)
            self._last[side] = c.minute
            last = c.minute
            self.last_bar_at = time.time()
            self.last_bar_minute = c.minute
            self.on_bar(side, c, lag, replay)

    def _warn_late(self, side, c, lag):
        if lag <= self.late_warn:
            return
        now = time.time()
        if now - self._late_warned > 60:
            extra = f" ({self._suppressed} similar suppressed)" if self._suppressed else ""
            logger.warning("[%s] candle %s arrived %.0fs late; entries in that minute "
                           "wait for it%s", side, c.minute.strftime("%H:%M"), lag, extra)
            self._late_warned = now
            self._suppressed = 0
        else:
            self._suppressed += 1

    def _check_blind(self):
        quiet = time.time() - self.last_bar_at
        if quiet < self.blind_seconds:
            return
        now = time.time()
        if now - self._blind_warned > 120:
            self._blind_warned = now
            logger.error("NO CANDLES FOR %.0f MINUTES: entries cannot be confirmed. "
                         "Check token, network or historical-data access.", quiet / 60)

    # -- reporting -----------------------------------------------------------
    def health(self) -> dict:
        lat = self.latencies[-200:]
        return {
            "polls": self.polls, "errors": self.errors, "abandoned": self.abandoned,
            "bars": len(self.latencies),
            "avg_lag": (sum(lat) / len(lat)) if lat else None,
            "max_lag": max(lat) if lat else None,
            "last_bar": self.last_bar_minute,
            "blind": (time.time() - self.last_bar_at) >= self.blind_seconds,
        }
