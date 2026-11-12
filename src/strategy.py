"""
strategy.py
===========
The rules from the spec, with no broker, network or GUI in here, so every
rule can be tested offline (see test_logic.py).

Settled rules:
  * reference = the 09:15 one-minute candle of the chosen CE and PE
  * contract  = premium (09:15 close) in [min, max], closest to target (500)
  * H, L  -> +1000 -> Day High = (H + sqrt H) - 1000, Day Low = (L - sqrt L) - 1000
  * Fib from Day Low (0) to Day High (1), only the configured levels
  * BUY when a tick crosses UP through a level >= 0.5 AND the last completed
    1-minute candle is green. Price already above a level must come back
    below it and cross again.
  * SL = broken level - 10 (from the level, not the fill)
  * target = next higher level. Reaching it while it is also a fresh break
    (green candle) exits the old trade and opens a new one at that level.
  * 4.618: no target, SL only, runs to square-off
  * downward breaks never trade; CE and PE are fully independent
"""

import math
from dataclasses import dataclass
from typing import Optional

from config import FIB_LEVELS, HL_ADJUSTMENT, MIN_TRADE_LEVEL


# ----------------------------------------------------------------------
# math
# ----------------------------------------------------------------------
def day_high(h: float, adj: float = HL_ADJUSTMENT) -> float:
    x = h + adj
    return x + math.sqrt(x) - adj


def day_low(l: float, adj: float = HL_ADJUSTMENT) -> float:
    x = l + adj
    return x - math.sqrt(x) - adj


def fib_prices(dh: float, dl: float, levels=FIB_LEVELS) -> list:
    """[(level, price)] ascending. 0 = Day Low, 1 = Day High."""
    rng = dh - dl
    return [(lvl, dl + lvl * rng) for lvl in levels]


def round_down_tick(x: float, tick: float = 0.05) -> float:
    return round(math.floor(round(x / tick, 6)) * tick, 2)


def pick_contract(cands: list, lo: float, hi: float, target: float):
    """cands: [{'premium': float, ...}]. Premium inside [lo, hi], closest to target."""
    ok = [c for c in cands if c.get("premium") and lo <= c["premium"] <= hi]
    if not ok:
        return None
    return min(ok, key=lambda c: (abs(c["premium"] - target), c["premium"]))


# ----------------------------------------------------------------------
# candles
# ----------------------------------------------------------------------
@dataclass
class Candle:
    minute: object          # datetime truncated to the minute
    open: float
    high: float
    low: float
    close: float

    @property
    def green(self) -> bool:
        return self.close > self.open


class CandleBuilder:
    """1-minute candles from ticks. last_closed is the candle before the
    minute of the most recent tick."""

    def __init__(self):
        self.current: Optional[Candle] = None
        self.last_closed: Optional[Candle] = None

    def seed(self, candle: Candle) -> None:
        self.last_closed = candle

    def update(self, minute, price: float) -> None:
        c = self.current
        if c is None or minute > c.minute:
            if c is not None:
                self.last_closed = c
            self.current = Candle(minute, price, price, price, price)
        elif minute == c.minute:
            c.high = max(c.high, price)
            c.low = min(c.low, price)
            c.close = price
        # ticks for an older minute (late packets) are ignored

    def prev_green(self) -> bool:
        return bool(self.last_closed and self.last_closed.green)


# ----------------------------------------------------------------------
# per-side state machine
# ----------------------------------------------------------------------
@dataclass
class Position:
    level: float
    level_price: float
    sl: float
    target_level: Optional[float]
    target_price: Optional[float]


class SideStrategy:
    def __init__(self, side: str, prices: list, sl_points: float = 10.0,
                 tick: float = 0.05, min_level: float = MIN_TRADE_LEVEL):
        self.side = side
        self.prices = prices                     # [(level, price)] ascending
        self.sl_points = sl_points
        self.tick = tick
        self.min_level = min_level
        self.position: Optional[Position] = None
        self.prev_ltp: Optional[float] = None

    # -- helpers --------------------------------------------------------
    def crossed_up(self, prev: float, ltp: float) -> list:
        """Eligible levels a move from prev to ltp crossed upward."""
        return [(lvl, p) for lvl, p in self.prices
                if lvl >= self.min_level and prev <= p < ltp]

    def _next_level(self, level: float):
        for lvl, p in self.prices:
            if lvl > level:
                return lvl, p
        return None, None

    def _open(self, level: float, level_price: float) -> Position:
        tl, tp = self._next_level(level)
        self.position = Position(
            level=level,
            level_price=level_price,
            sl=round_down_tick(level_price - self.sl_points, self.tick),
            target_level=tl,
            target_price=tp,
        )
        return self.position

    # -- main entry -----------------------------------------------------
    def on_tick(self, ltp: float, prev_green: bool, entries_allowed: bool) -> list:
        """Returns a list of actions:
           {'kind': 'ENTER'|'EXIT'|'ROLL', 'reason', 'price', 'position', 'old'}"""
        prev, self.prev_ltp = self.prev_ltp, ltp
        if prev is None:
            return []                            # need a previous tick to see a cross

        pos = self.position
        if pos is not None:
            if ltp <= pos.sl:
                self.position = None
                return [{"kind": "EXIT", "reason": "SL", "price": ltp, "old": pos}]

            if pos.target_price is not None and ltp >= pos.target_price:
                crossed = self.crossed_up(prev, ltp)
                if crossed and prev_green and entries_allowed:
                    lvl, p = crossed[-1]
                    new = self._open(lvl, p)
                    return [{"kind": "ROLL", "reason": "TARGET", "price": ltp,
                             "old": pos, "position": new}]
                self.position = None
                return [{"kind": "EXIT", "reason": "TARGET", "price": ltp, "old": pos}]
            return []

        if not entries_allowed:
            return []
        crossed = self.crossed_up(prev, ltp)
        if crossed and prev_green:
            lvl, p = crossed[-1]
            return [{"kind": "ENTER", "reason": f"BREAK {lvl:g}", "price": ltp,
                     "position": self._open(lvl, p)}]
        return []

    def force_exit(self, reason: str, ltp: float) -> list:
        pos, self.position = self.position, None
        if pos is None:
            return []
        return [{"kind": "EXIT", "reason": reason, "price": ltp, "old": pos}]
