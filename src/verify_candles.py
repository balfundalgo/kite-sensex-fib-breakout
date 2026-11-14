"""
verify_candles.py
=================
Checks every candle the app used during a session against the FINAL candle
Zerodha's historical API returns now (after the day is over).

    python3 src/verify_candles.py logs/candles_2026-11-13.csv
    python3 src/verify_candles.py logs/app_2026-11-13.log

Works with the v1.2 candles CSV or a v1.1/v1.2 app log.

What the result means:
  * every candle matches  -> the app read exactly what Zerodha serves; if the
    Kite chart still looks different, the chart is drawing something else
    (reload the chart page: the live candle on screen is built in the browser
    from ticks)
  * some candles differ   -> those were read before Zerodha finished them;
    the "read +Ns" column shows how soon after the close that happened, which
    tells us the settle delay actually needed

Login uses the first account in settings.json (repo root, or pass
--settings PATH to the one next to the EXE). It opens the normal Kite login.
"""

import argparse
import csv
import json
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import config  # noqa: E402
import kite_auth  # noqa: E402
from restfeed import fetch_1m  # noqa: E402

LOG_BAR = re.compile(r"\[(CE|PE)\] (\d\d:\d\d) O ([\d.]+) H ([\d.]+) L ([\d.]+) C ([\d.]+) "
                     r"\w+\s+\(\+([\d.]+)s\)")
LOG_REF = re.compile(r"^\S+ \S+\s+INFO\s+(CE|PE) (SENSEX\S+) \| 09:15 O ([\d.]+) H ([\d.]+) "
                     r"L ([\d.]+) C ([\d.]+)")
LOG_SEL = re.compile(r"(CE|PE) selected: (\S+)")
TOL = 0.001


def parse_log(path: Path):
    """[(side, symbol, 'HH:MM', o, h, l, c, lag_or_None, event)]"""
    rows, symbol = [], {}
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        m = LOG_SEL.search(line)
        if m:
            symbol[m.group(1)] = m.group(2)
            continue
        m = LOG_REF.search(line)
        if m:
            side, sym = m.group(1), m.group(2)
            symbol[side] = sym
            rows.append((side, sym, "09:15", *map(float, m.group(3, 4, 5, 6)), None, "REFERENCE"))
            continue
        m = LOG_BAR.search(line)
        if m and m.group(1) in symbol:
            side = m.group(1)
            rows.append((side, symbol[side], m.group(2), *map(float, m.group(3, 4, 5, 6)),
                         float(m.group(7)), "NEW"))
    return rows


def parse_csv(path: Path):
    rows = []
    with open(path, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            lag = float(r["lag_s"]) if r.get("lag_s") else None
            rows.append((r["side"], r["symbol"], r["minute"], float(r["open"]), float(r["high"]),
                         float(r["low"]), float(r["close"]), lag, r["event"]))
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("file", help="logs/candles_YYYY-MM-DD.csv or logs/app_YYYY-MM-DD.log")
    ap.add_argument("--settings", help="settings.json to take the Kite account from")
    args = ap.parse_args()

    path = Path(args.file)
    day = re.search(r"(\d{4}-\d{2}-\d{2})", path.name)
    if not day:
        sys.exit("File name must contain the date, e.g. candles_2026-11-13.csv")
    day = datetime.strptime(day.group(1), "%Y-%m-%d")
    rows = parse_csv(path) if path.suffix.lower() == ".csv" else parse_log(path)
    if not rows:
        sys.exit("No candles found in that file.")

    settings = config.SETTINGS
    if args.settings:
        settings = {**config.DEFAULT_SETTINGS,
                    **json.loads(Path(args.settings).read_text(encoding="utf-8"))}
    accounts = [a for a in settings.get("accounts", []) if a.get("enabled", True)]
    if accounts:
        account = accounts[0]
    else:
        print("No account in settings.json - enter the Kite app credentials:")
        account = {"name": "verify", "api_key": input("API key: ").strip(),
                   "api_secret": input("API secret: ").strip()}
    kite, _ = kite_auth.login(account, settings["redirect_host"], int(settings["redirect_port"]))

    symbols = sorted({r[1] for r in rows})
    tokens = {i["tradingsymbol"]: i["instrument_token"] for i in kite.instruments("BFO")
              if i["tradingsymbol"] in symbols}
    final = {}
    for sym in symbols:
        if sym not in tokens:
            print(f"{sym}: not in today's instrument list (expired?) - skipped")
            continue
        for c in fetch_1m(kite, tokens[sym], day.replace(hour=9, minute=15),
                          day.replace(hour=15, minute=30)):
            final[(sym, c.minute.strftime("%H:%M"))] = c

    # keep the last version the app used for each minute (a REVISED row supersedes NEW)
    used = {}
    for r in rows:
        used[(r[1], r[2])] = r

    print(f"\n{'side':4} {'min':5} {'event':9} {'read':>7}   {'app O/H/L/C':33}   "
          f"{'final O/H/L/C':33}  result")
    ok = bad = missing = 0
    early = []
    for key in sorted(used, key=lambda k: (k[1], k[0])):
        side, sym, mn, o, h, l, c, lag, ev = used[key]
        f = final.get((sym, mn))
        app = f"{o:8.2f}{h:8.2f}{l:8.2f}{c:8.2f}"
        if f is None:
            missing += 1
            print(f"{side:4} {mn:5} {ev:9} {'':>7}   {app:33}   {'(no final candle)':33}")
            continue
        fin = f"{f.open:8.2f}{f.high:8.2f}{f.low:8.2f}{f.close:8.2f}"
        same = all(abs(a - b) < TOL for a, b in ((o, f.open), (h, f.high), (l, f.low), (c, f.close)))
        read = f"+{lag:.1f}s" if lag is not None else ""
        if same:
            ok += 1
            res = "OK"
        else:
            bad += 1
            diffs = [n for n, a, b in (("O", o, f.open), ("H", h, f.high), ("L", l, f.low),
                                        ("C", c, f.close)) if abs(a - b) >= TOL]
            colour = "  COLOUR DIFFERS" if (c > o) != (f.close > f.open) else ""
            res = "DIFF " + ",".join(diffs) + colour
            if lag is not None:
                early.append(lag)
        print(f"{side:4} {mn:5} {ev:9} {read:>7}   {app:33}   {fin:33}  {res}")

    print(f"\n{ok} match, {bad} differ, {missing} without a final candle")
    if bad and early:
        print(f"Differing candles were read {min(early):.1f}-{max(early):.1f}s after their close.")
    if bad == 0 and ok:
        print("Every candle the app used equals Zerodha's final candle. "
              "Compare the chart only after reloading it.")


if __name__ == "__main__":
    main()
