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

Breakeven (v1.6):
  Once price has covered `breakeven_pct` of the way from the entry price to
  the target (entry 500, target 600, 30 % -> at 530), the SL moves up to the
  entry price. Once per trade; never moves the SL down; not used on 4.618
  (no target).

Candles vs ticks (v1.1):
  Candle OHLC comes ONLY from Kite's REST historical API (exact exchange
  candles). Ticks are used only for what REST cannot see inside a minute:
  the level cross, SL, target and P/L. REST lags a few seconds, so in the
  first seconds of a minute the preceding candle may not be known yet. A
  cross in that window is held as PENDING and decided the moment the candle
  arrives: green and price still above the level -> BUY, otherwise skipped.
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


def round_up_tick(x: float, tick: float = 0.05) -> float:
    return round(math.ceil(round(x / tick, 6)) * tick, 2)


def pick_contract(cands: list, lo: float, hi: float, target: float):
    """cands: [{'premium': float, ...}]. Premium inside [lo, hi], closest to target."""
    ok = [c for c in cands if c.get("premium") and lo <= c["premium"] <= hi]
    if not ok:
        return None
    return min(ok, key=lambda c: (abs(c["premium"] - target), c["premium"]))


def walk_to_target(n: int, start: int, close_at, target: float, cheaper: int,
                   max_fetch: int = 20) -> dict:
    """
    Find the strikes whose 09:15 close sits either side of `target`.

    Strikes are indexed 0..n-1 in strike order. `cheaper` is the index step
    in which the premium falls: +1 for CE (higher strike, cheaper), -1 for PE.
    close_at(i) returns that strike's 09:15 close, or None if it has none.

    Starting at `start` (the strike whose CURRENT price is nearest the
    target), step towards the target until the 09:15 close crosses it, then
    check one strike beyond. Needed because after a late start the current
    price says nothing about 09:15 (18 Nov, started 14:37: every CE strike
    near 500 now had a 09:15 close of 600-976, the right one was never read).

    Returns {index: close} for every strike read (close may be None).
    """
    got = {}

    def get(i):
        if 0 <= i < n and i not in got and len(got) < max_fetch:
            got[i] = close_at(i)
        return got.get(i)

    i, direction = start, None
    while 0 <= i < n and len(got) < max_fetch:
        c = get(i)
        if c is None:
            if i not in got:
                break                              # fetch limit reached
            i += direction or cheaper              # no candle: keep going the same way
            continue
        step = cheaper if c > target else -cheaper
        if direction is not None and step != direction:
            get(i + direction)                     # crossed: one more beyond, for safety
            break
        direction = step
        i += step
    return got


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
    breakeven: bool = False      # SL already moved to entry


@dataclass
class Pending:
    level: float
    level_price: float
    minute: object           # minute in which the cross happened


class SideStrategy:
    """prev_green passed in by the engine is True / False when the preceding
    1-minute REST candle is known, or None while it has not arrived yet."""

    def __init__(self, side: str, prices: list, sl_points: float = 10.0,
                 tick: float = 0.05, min_level: float = MIN_TRADE_LEVEL):
        self.side = side
        self.prices = prices                     # [(level, price)] ascending
        self.sl_points = sl_points
        self.tick = tick
        self.min_level = min_level
        self.position: Optional[Position] = None
        self.pending: Optional[Pending] = None
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

    def _set_pending(self, level, price, minute):
        if self.pending is None or self.pending.minute != minute or level > self.pending.level:
            self.pending = Pending(level, price, minute)

    # -- main entry -----------------------------------------------------
    def on_tick(self, ltp: float, prev_green: Optional[bool], entries_allowed: bool,
                minute=None) -> list:
        """Returns actions: {'kind': 'ENTER'|'EXIT'|'ROLL'|'PENDING'|'SKIP', ...}"""
        prev, self.prev_ltp = self.prev_ltp, ltp
        if prev is None:
            return []                            # need a previous tick to see a cross

        pos = self.position
        if pos is not None:
            if ltp <= pos.sl:
                self.position = None
                self.pending = None
                return [{"kind": "EXIT", "reason": "BREAKEVEN SL" if pos.breakeven else "SL",
                         "price": ltp, "old": pos}]

            if pos.target_price is not None and ltp >= pos.target_price:
                crossed = self.crossed_up(prev, ltp)
                if crossed and entries_allowed:
                    lvl, p = crossed[-1]
                    if prev_green is True:
                        new = self._open(lvl, p)
                        return [{"kind": "ROLL", "reason": "TARGET", "price": ltp,
                                 "old": pos, "position": new}]
                    if prev_green is None:
                        # book the target now, decide the new entry when the candle lands
                        self.position = None
                        self._set_pending(lvl, p, minute)
                        return [{"kind": "EXIT", "reason": "TARGET", "price": ltp, "old": pos},
                                {"kind": "PENDING", "level": lvl, "price": ltp}]
                self.position = None
                return [{"kind": "EXIT", "reason": "TARGET", "price": ltp, "old": pos}]
            return []

        if not entries_allowed:
            self.pending = None
            return []

        actions = []
        crossed = self.crossed_up(prev, ltp)
        if crossed:
            lvl, p = crossed[-1]
            if prev_green is True:
                self.pending = None
                return [{"kind": "ENTER", "reason": f"BREAK {lvl:g}", "price": ltp,
                         "position": self._open(lvl, p)}]
            if prev_green is None:
                self._set_pending(lvl, p, minute)
                actions.append({"kind": "PENDING", "level": lvl, "price": ltp})
                return actions
            # red: no trade on this cross
        if self.pending is not None and prev_green is not None:
            actions += self.resolve_pending(prev_green, ltp, entries_allowed, minute)
        return actions

    def resolve_pending(self, prev_green: Optional[bool], ltp: Optional[float],
                        entries_allowed: bool, minute=None) -> list:
        """Decide a held cross once the preceding candle is known."""
        pd = self.pending
        if pd is None or self.position is not None:
            self.pending = None
            return []
        if minute is not None and pd.minute is not None and minute != pd.minute:
            self.pending = None
            return [{"kind": "SKIP", "level": pd.level,
                     "reason": "candle did not arrive within the minute"}]
        if prev_green is None:
            return []
        self.pending = None
        if not prev_green:
            return [{"kind": "SKIP", "level": pd.level, "reason": "preceding candle red"}]
        if not entries_allowed:
            return [{"kind": "SKIP", "level": pd.level, "reason": "entries blocked"}]
        if ltp is None or ltp <= pd.level_price:
            return [{"kind": "SKIP", "level": pd.level,
                     "reason": "price fell back below the level before the candle arrived"}]
        return [{"kind": "ENTER", "reason": f"BREAK {pd.level:g} (confirmed)", "price": ltp,
                 "position": self._open(pd.level, pd.level_price)}]

    def check_breakeven(self, ltp: float, entry: Optional[float], pct: float):
        """Move the SL to the entry price once price has covered `pct` % of the
        way from entry to target. Returns a BREAKEVEN action, or None."""
        pos = self.position
        if (pos is None or pos.breakeven or pos.target_price is None or not pct
                or pct <= 0 or entry is None or ltp is None):
            return None
        span = pos.target_price - entry
        if span <= 0:
            return None                          # filled at/above the target: nothing to protect
        trigger = entry + span * pct / 100.0
        if ltp < trigger:
            return None
        pos.breakeven = True
        new_sl = round_up_tick(entry, self.tick)
        if new_sl <= pos.sl:
            return None                          # never move the SL down
        old = pos.sl
        pos.sl = new_sl
        return {"kind": "BREAKEVEN", "sl": new_sl, "old_sl": old, "trigger": trigger,
                "entry": entry, "price": ltp}

    def force_exit(self, reason: str, ltp: float) -> list:
        self.pending = None
        pos, self.position = self.position, None
        if pos is None:
            return []
        return [{"kind": "EXIT", "reason": reason, "price": ltp, "old": pos}]
