"""
restfeed.py
===========
1-minute candles from Kite's REST historical API instead of from ticks.
Same approach as the SENSEX VWAP ladder build:

    REST       candle OHLC (the 09:15 reference candle and every 1-min candle
               used for the "preceding candle is green" check)
    WebSocket  LTP only — level crosses, stop-loss, target, P/L

Rules carried over from the VWAP feed (each learned from a live failure):
  * the minute still forming is dropped before anything sees it
  * nothing before today's 09:15 ever reaches the strategy
  * each poll runs with a hard deadline so a hung HTTP call cannot blind
    the strategy; it is abandoned and the next poll carries on
  * every bar records how late it arrived; slow bars and a feed that has
    stopped delivering are logged loudly instead of failing silently

v1.2 — candles were being accepted 0.2-3 s after the minute closed and never
looked at again. A candle served that early can still be missing the last
trades of the minute. Now:
  * SETTLE: a candle is accepted only `settle` seconds after its minute closes
  * VERIFY: every poll re-reads the last `verify_minutes` candles; if Zerodha
    has changed one since we accepted it, on_revise() is called with the old
    and new values, so the engine corrects it and the log shows exactly how
    often and how late revisions happen
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


def drop_forming(candles: List[Candle], now: datetime, settle: float = 0.0) -> List[Candle]:
    """Keep only minutes that closed at least `settle` seconds ago."""
    cut = timedelta(minutes=1, seconds=settle)
    return [c for c in candles if c.minute + cut <= now]


def same_ohlc(a: Candle, b: Candle) -> bool:
    return (abs(a.open - b.open) < 1e-6 and abs(a.high - b.high) < 1e-6
            and abs(a.low - b.low) < 1e-6 and abs(a.close - b.close) < 1e-6)


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
    Polls the 1-minute endpoint for each leg.
      on_bar(side, candle, lag, replay)        a newly accepted candle, in order
      on_revise(side, old, new, age_seconds)   an accepted candle that changed
    """

    def __init__(self, kite, legs: Dict[str, int], session_start: datetime,
                 on_bar: Callable, now_fn: Callable, on_revise: Optional[Callable] = None,
                 poll: float = 2.0, settle: float = 5.0, verify_minutes: int = 5,
                 hard_deadline: float = 10.0, blind_seconds: float = 180.0,
                 late_warn: float = 20.0):
        self.kite = kite
        self.legs = dict(legs)                  # {"CE": token, "PE": token}
        self.session_start = session_start      # today 09:15, naive IST
        self.on_bar = on_bar
        self.on_revise = on_revise
        self.now = now_fn
        self.poll = poll
        self.settle = settle
        self.verify_minutes = verify_minutes
        self.hard_deadline = hard_deadline
        self.blind_seconds = blind_seconds
        self.late_warn = late_warn

        self._last: Dict[str, datetime] = {}           # last minute accepted per leg
        self._seen: Dict[str, Dict[datetime, Candle]] = {s: {} for s in self.legs}
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="restpoll")
        self.latencies: List[float] = []
        self.polls = 0
        self.errors = 0
        self.abandoned = 0
        self.revisions = 0
        self.revision_ages: List[float] = []
        self.last_bar_at = time.time()
        self.last_bar_minute: Optional[datetime] = None
        self._blind_warned = 0.0
        self._late_warned = 0.0
        self._suppressed = 0

    # -- lifecycle ---------------------------------------------------------
    def remember(self, side: str, c: Candle):
        """A candle already handed to the engine (reference / backfill):
        mark it accepted so it is not emitted again, but keep verifying it."""
        self._seen[side][c.minute] = c
        if self._last.get(side) is None or c.minute > self._last[side]:
            self._last[side] = c.minute

    def start(self):
        self._thread = threading.Thread(target=self._loop, daemon=True, name="restfeed")
        self._thread.start()
        logger.info("REST candle feed started: 1-minute bars, poll %.1fs, settle %.0fs, "
                    "re-verify last %d min", self.poll, self.settle, self.verify_minutes)

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
        frm = max(self.session_start, now - timedelta(minutes=self.verify_minutes + 3))
        if self._last.get(side) is None:
            frm = self.session_start
        candles = fetch_1m(self.kite, token, frm, now)
        self.polls += 1
        now = self.now()
        closed = drop_forming(candles, now)            # every minute that has ended
        seen = self._seen[side]
        last = self._last.get(side)
        verify_from = now - timedelta(minutes=self.verify_minutes + 1)

        for c in sorted(closed, key=lambda x: x.minute):
            if c.minute < self.session_start:
                continue                               # never yesterday's candles
            close_at = c.minute + timedelta(minutes=1)

            old = seen.get(c.minute)
            if old is not None:
                # already accepted: re-verify recent ones
                if c.minute >= verify_from and not same_ohlc(old, c):
                    age = (now - close_at).total_seconds()
                    seen[c.minute] = c
                    self.revisions += 1
                    self.revision_ages.append(age)
                    if self.on_revise:
                        self.on_revise(side, old, c, age)
                continue

            if last is not None and c.minute <= last:
                continue                               # older than what we accepted, skip
            if (now - close_at).total_seconds() < self.settle:
                break                                  # not settled yet; later minutes neither

            lag = (now - close_at).total_seconds()
            self.latencies.append(lag)
            if len(self.latencies) > 2000:
                self.latencies = self.latencies[-1000:]
            self._warn_late(side, c, lag)
            seen[c.minute] = c
            self._last[side] = c.minute
            last = c.minute
            self.last_bar_at = time.time()
            self.last_bar_minute = c.minute
            self.on_bar(side, c, lag, False)

        # keep memory bounded
        if len(seen) > 400:
            for k in sorted(seen)[:-200]:
                seen.pop(k, None)

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
            "revisions": self.revisions,
            "max_revision_age": max(self.revision_ages) if self.revision_ages else None,
            "last_bar": self.last_bar_minute,
            "blind": (time.time() - self.last_bar_at) >= self.blind_seconds,
        }
