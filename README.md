# SENSEX Fib Breakout — Zerodha Kite

SENSEX options breakout + Fibonacci strategy on the Zerodha Kite Connect API.
Implements *SENSEX & BANK NIFTY Automated Options Breakout + Fibonacci Strategy — UPDATED*
(SENSEX only in v1; Bank Nifty parked).

## The day

| Time | What happens |
|---|---|
| before 09:15 | Login, load BFO instruments, nearest SENSEX expiry, stream candidate strikes |
| 09:16 | Pick one CE and one PE whose 09:15 1-min close is in ₹400–₹600, closest to ₹500. Confirm the 09:15 OHLC from Kite historical data |
| levels | H, L → +1000 → Day High = (H + √H) − 1000, Day Low = (L − √L) − 1000 → Fib 0 (Day Low) to 1 (Day High) |
| trading | BUY when a tick crosses UP through a level ≥ 0.5 and the last completed 1-min candle is green |
| exits | SL = broken level − ₹10. Target = next level; reaching it on a green candle opens the next trade at that level. 4.618 has no target |
| risk | Daily rupee loss limit across all accounts → LOCKED. Emergency stop closes everything |
| 15:15 | Square-off |

CE and PE are fully independent. No trade below 0.5, no trade on downward breaks.

## Running from source (Mac)

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python src/test_logic.py     # offline rule checks
python src/main.py
```

1. **Accounts** tab: add name, API key, API secret, lots → *Add / Update* → *Login selected*.
2. **Settings** tab: check values, keep **PAPER** for testing → *Save settings*.
3. **Start**.

Redirect URL on developers.kite.trade must be exactly `http://127.0.0.1:8765/`.

## Files

| | |
|---|---|
| `src/strategy.py` | All rules, pure Python, no broker |
| `src/engine.py` | Daily flow, selection, ticks, risk, square-off |
| `src/broker.py` | Account sessions, paper/live order router, stop handling |
| `src/kite_auth.py` | Daily Kite login with local redirect catcher |
| `src/gui.py` | CustomTkinter dashboard |
| `src/test_logic.py` | Offline tests (CI runs these before building) |

Logs (next to the EXE): `logs/app_*.log`, `logs/trades_*.csv`, `logs/orders_*.csv`.
`settings.json` and `tokens/` hold secrets and are git-ignored.

## Notes

* LIVE orders: MARKET buy (Kite auto market protection) → SELL stop at level − 10.
  Default stop type is **SL** (stop-limit, limit = trigger − buffer) because Zerodha
  generally rejects SL-M on options; switch in Settings if your account allows SL-M.
* **Roll mode HOLD** (default): when a target level is also a fresh break, the old trade
  is booked and the position is carried into the new one by moving the stop — same
  result as sell + buy without two extra orders. **REORDER** really sells and buys.
* Since 1 April 2026 API order placement needs a static IP registered in the Kite
  developer console. Run LIVE from that machine/VPS.

## Build

Every push to `main` runs the tests on Ubuntu, then builds `SENSEX_Fib_Breakout.exe`
on Windows. Download it from the Actions run page.
