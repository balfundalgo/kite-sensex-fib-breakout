"""
test_logic.py
=============
Offline checks of every rule. No broker, no network, no GUI.

    python3 src/test_logic.py

CI runs this on Ubuntu before building the EXE; the build only happens
if everything passes.
"""

import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from strategy import (Candle, CandleBuilder, SideStrategy, day_high, day_low,  # noqa: E402
                      fib_prices, pick_contract, round_down_tick)

PASSED = 0
FAILED = 0


def check(name, cond):
    global PASSED, FAILED
    if cond:
        PASSED += 1
    else:
        FAILED += 1
        print(f"FAIL  {name}")


def near(a, b, tol=0.01):
    return a is not None and b is not None and abs(a - b) <= tol


# ---------------------------------------------------------------- math
check("CE day high", near(day_high(525), 564.05))
check("CE day low", near(day_low(400), 362.58))
check("PE day high", near(day_high(540), 579.24))
check("PE day low", near(day_low(410), 372.45))

ce = fib_prices(day_high(525), day_low(400))
lv = dict(ce)
check("13 levels only", len(ce) == 13)
check("level 0 = day low", near(lv[0.0], 362.58))
check("level 1 = day high", near(lv[1.0], 564.05))
check("level 0.5", near(lv[0.5], 463.32))
check("level 0.8", near(lv[0.8], 523.76))
check("level 1.309", near(lv[1.309], 626.30))
check("level 4.618", near(lv[4.618], 1292.97))
check("ascending", all(ce[i][1] < ce[i + 1][1] for i in range(12)))

check("round down tick", round_down_tick(453.317) == 453.30)
check("round exact tick", round_down_tick(453.35) == 453.35)

# ---------------------------------------------------------------- selection
cands = [{"sym": "A", "premium": 380}, {"sym": "B", "premium": 455},
         {"sym": "C", "premium": 520}, {"sym": "D", "premium": 610}]
check("closest to 500 inside range", pick_contract(cands, 400, 600, 500)["sym"] == "C")
check("none in range", pick_contract([{"premium": 300}], 400, 600, 500) is None)
check("boundary inclusive", pick_contract([{"sym": "X", "premium": 400}], 400, 600, 500)["sym"] == "X")


# ---------------------------------------------------------------- candles
t0 = datetime(2026, 11, 12, 9, 16)
cb = CandleBuilder()
cb.seed(Candle(t0 - timedelta(minutes=1), 500, 525, 400, 510))
check("seeded 09:15 green", cb.prev_green())
cb.update(t0, 510)
cb.update(t0, 505)
cb.update(t0 + timedelta(minutes=1), 506)
check("09:16 red becomes last closed", not cb.prev_green())
check("09:16 close", cb.last_closed.close == 505)
cb.update(t0 + timedelta(minutes=1), 520)
cb.update(t0 + timedelta(minutes=2), 521)
check("09:17 green", cb.prev_green())


# ---------------------------------------------------------------- state machine
def fresh():
    return SideStrategy("CE", fib_prices(day_high(525), day_low(400)), sl_points=10)


L05, L08, L1, L1309 = lv[0.5], lv[0.8], lv[1.0], lv[1.309]

s = fresh()
check("first tick never trades", s.on_tick(470, True, True) == [])
check("already above 0.5 -> no trade", s.on_tick(472, True, True) == [])

s = fresh()
s.on_tick(400, True, True)
check("cross of 0.2 -> no trade", s.on_tick(410, True, True) == [])
check("break below 0.5 level ignored", s.position is None)

s = fresh()
s.on_tick(460, True, True)
check("cross 0.5 with RED candle -> no trade", s.on_tick(465, False, True) == [])

s = fresh()
s.on_tick(460, True, True)
a = s.on_tick(465, True, True)
check("cross 0.5 green -> ENTER", len(a) == 1 and a[0]["kind"] == "ENTER")
p = s.position
check("entry level 0.5", p.level == 0.5)
check("SL from level", near(p.sl, round_down_tick(L05 - 10)))
check("target is 0.8", p.target_level == 0.8 and near(p.target_price, L08))

check("downward move, no action", s.on_tick(460, True, True) == [])
a = s.on_tick(L05 - 10.5, True, True)
check("SL hit -> EXIT", a and a[0]["kind"] == "EXIT" and a[0]["reason"] == "SL")
check("flat after SL", s.position is None)

a = s.on_tick(470, True, True)
check("re-entry after SL on fresh cross", a and a[0]["kind"] == "ENTER")

# roll at 0.8
s = fresh()
s.on_tick(460, True, True)
s.on_tick(465, True, True)
a = s.on_tick(L08 + 0.5, True, True)
check("0.8 reached + green -> ROLL", a and a[0]["kind"] == "ROLL")
check("rolled to 0.8", s.position.level == 0.8 and s.position.target_level == 1.0)
check("new SL from 0.8", near(s.position.sl, round_down_tick(L08 - 10)))

# target reached with red candle -> plain exit
s = fresh()
s.on_tick(460, True, True)
s.on_tick(465, True, True)
a = s.on_tick(L08 + 0.5, False, True)
check("0.8 reached + red -> EXIT TARGET", a and a[0]["kind"] == "EXIT" and a[0]["reason"] == "TARGET")
check("flat after target", s.position is None)

# gap through several levels -> highest crossed
s = fresh()
s.on_tick(460, True, True)
a = s.on_tick(L1 + 1, True, True)
check("gap -> entry at highest crossed (1.0)", a and s.position.level == 1.0)
check("target 1.309", s.position.target_level == 1.309)

# entries blocked (lock / after square-off)
s = fresh()
s.on_tick(460, True, True)
check("entries blocked", s.on_tick(465, True, False) == [])

# top level: no target
s = fresh()
top = lv[4.618]
s.on_tick(top - 1, True, True)
a = s.on_tick(top + 1, True, True)
check("4.618 entry", a and s.position.level == 4.618)
check("4.618 has no target", s.position.target_price is None)
check("4.618 runs, no exit higher", s.on_tick(top + 200, True, True) == [])
a = s.force_exit("SQUAREOFF", top + 150)
check("square-off exit", a and a[0]["reason"] == "SQUAREOFF" and s.position is None)

# PE independent of CE
ce_s, pe_s = fresh(), SideStrategy("PE", fib_prices(day_high(540), day_low(410)))
ce_s.on_tick(460, True, True)
ce_s.on_tick(465, True, True)
check("PE untouched by CE trade", pe_s.position is None and ce_s.position is not None)


print(f"\n{PASSED} passed, {FAILED} failed")
sys.exit(1 if FAILED else 0)
