"""
engine.py
=========
The daily flow, for SENSEX, BANK NIFTY or both (setting "index").

  start      -> per index: load its instruments (SENSEX: BFO, Bank Nifty: NFO),
                nearest expiry, spot, candidate strikes
  ref + 1min -> per index: read the reference candle of the strikes from REST,
                pick the CE / PE whose close is in range and closest to the target
  levels     -> Day High / Day Low / Fib per contract from that REST candle
  trading    -> REST feed delivers every completed 1-min candle (OHLC);
                WebSocket delivers LTP for crosses, SL, target and P/L
  risk       -> daily rupee loss lock, daily rupee profit target, breakeven
                stop, emergency stop
  15:15      -> square off everything

Every contract is a "side", keyed "SENSEX CE", "SENSEX PE", "BANKNIFTY CE",
"BANKNIFTY PE". Each is fully independent; P/L, loss lock and profit target
cover all of them together, across all accounts.

Data split (same as the SENSEX VWAP ladder):
    REST       all candle OHLC
    WebSocket  LTP only

v1.6: Bank Nifty and BOTH. Max daily profit (closed + open P/L) squares off
everything and stops entries. Breakeven: once price covers breakeven_pct of
entry -> target, the stop moves to the entry price.

v1.5.1: strike selection reads the 09:15 candles themselves. It starts at
the strike whose current price is nearest 500 and walks strike by strike
until the 09:15 close crosses 500 (walk_to_target). v1.5.0 only read the 6
strikes nearest 500 NOW, which is right at 09:16 but wrong after a late
start (18 Nov 14:37: no CE found, the correct strike was never read).

v1.4.1: Zerodha corrects a candle's close a few seconds after the minute
ends. Candles, including the reference candles used for strike selection,
are accepted only after the settle delay. A later correction of the
reference open/close updates the displayed reference; levels change only if
the high or low changes.

v1.4: every poll reads the whole session from 09:15:00 (as the VWAP ladder
does). Any later revision by Zerodha is applied and logged. Every candle
used is written to logs/candles_<date>.csv for checking.
"""

import threading
import time
from datetime import timedelta

from kiteconnect import KiteTicker

import broker
import config
from broker import OrderRouter, Trade
from logger import candle_row, logger, trade_row
from restfeed import RestCandleFeed, fetch_closed, fetch_minute
from strategy import SideStrategy, day_high, day_low, fib_prices, pick_contract, walk_to_target

SIDES = ("CE", "PE")


def _naive_now():
    return config.now_ist().replace(tzinfo=None)


def _minute(dt):
    return dt.replace(second=0, microsecond=0)


class IndexData:
    """Instruments and contract facts for one index (SENSEX or BANKNIFTY)."""

    def __init__(self, name):
        spec = config.INDICES[name]
        self.name = name
        self.label = spec["label"]
        self.exchange = spec["exchange"]
        self.inst_name = spec["name"]
        self.spot_key = spec["spot"]
        self.expiry = None
        self.lot_size = 0
        self.tick_size = 0.05
        self.spot = None
        self.cand = {}               # token -> instrument row
        self.ref_start = None        # reference candle minute (naive IST)
        self.selected = False


class SideData:
    def __init__(self, index, side):
        self.index = index           # "SENSEX" | "BANKNIFTY"
        self.side = side             # "CE" | "PE"
        self.key = f"{index} {side}"
        self.symbol = ""
        self.token = None
        self.exchange = config.INDICES[index]["exchange"]
        self.lot_size = 0
        self.tick_size = 0.05
        self.ref = None              # REST reference Candle
        self.day_high = None
        self.day_low = None
        self.levels = []             # [(level, price)]
        self.ltp = None
        self.bars = {}               # minute -> REST Candle (today)
        self.last_bar = None         # most recent REST Candle
        self.strategy = None
        self.trade = None            # open Trade
        self._fallback_logged = None


class Engine:
    def __init__(self, status_cb=None):
        self.status_cb = status_cb or (lambda snap: None)
        self.s = dict(config.SETTINGS)
        self.lock = threading.RLock()
        self.status = "IDLE"
        self.running = False
        self.halted = False          # emergency stop: no new entries
        self.locked = False          # daily loss lock
        self.profit_locked = False   # daily profit target reached
        self.squared_off = False
        self.thread = None
        self.kws = None
        self.feed = None             # RestCandleFeed
        self.feed_live = False
        self.ticks = 0
        self.index_names = config.active_indices(self.s)
        self.indices = {n: IndexData(n) for n in self.index_names}
        self.sides = {}
        for n in self.index_names:
            for side in SIDES:
                sd = SideData(n, side)
                self.sides[sd.key] = sd
        self.trades = []             # all Trades today
        self.trade_seq = 0
        self.router = None
        self._sub_tokens = []
        self._last_poll = 0.0
        self.session_start = None

    # ==================================================================
    # control
    # ==================================================================
    def start(self):
        self.running = True
        self.thread = threading.Thread(target=self._run, daemon=True, name="engine")
        self.thread.start()

    def stop(self):
        self.running = False
        if self.feed:
            self.feed.stop()
        self._close_feed()
        self._set_status("STOPPED")
        logger.info("Engine stopped. Open positions (if any) are NOT closed by Stop.")

    def emergency_stop(self):
        logger.warning("EMERGENCY STOP: entries blocked, closing all strategy positions")
        with self.lock:
            self.halted = True
            self._close_all("EMERGENCY")
        self._set_status("HALTED")

    def unlock(self):
        with self.lock:
            if self.locked or self.profit_locked:
                self.locked = False
                self.profit_locked = False
                logger.warning("Lock released manually. It locks again if the day's P/L is "
                               "still beyond the loss limit or the profit target; change "
                               "the limit to continue.")
                self._set_status("TRADING")

    def resume_entries(self):
        with self.lock:
            self.halted = False
        logger.info("Entries resumed")

    # ==================================================================
    # main thread
    # ==================================================================
    def _run(self):
        try:
            self._set_status("STARTING")
            sessions = broker.active_sessions()
            if not sessions:
                raise RuntimeError("No enabled account is logged in. Log in on the Accounts tab first.")
            self.data = sessions[0]
            logger.info("Mode %s | index %s | data account %s (%s) | order accounts: %s",
                        self.s["mode"], " + ".join(self.index_names), self.data.name,
                        self.data.user_id,
                        ", ".join(f"{x.name} x{x.lots} lot" for x in sessions))
            logger.info("Candles: REST historical API only | WebSocket: LTP for "
                        "crosses, stop-loss, target and P/L")
            be = float(self.s.get("breakeven_pct", 0) or 0)
            mp = float(self.s.get("max_daily_profit", 0) or 0)
            logger.info("Breakeven: %s | Max daily profit: %s | Max daily loss: %s",
                        f"SL to entry at {be:g}% of the way to target" if be > 0 else "off",
                        f"{mp:,.0f}" if mp > 0 else "off",
                        f"{float(self.s.get('max_daily_loss', 0)):,.0f}"
                        if float(self.s.get("max_daily_loss", 0)) > 0 else "off")

            for ix in self.indices.values():
                self._load_instruments(ix)
                ix.ref_start = config.today_at(
                    config.index_setting(self.s, ix.name, "ref_candle_time")).replace(tzinfo=None)

            # one REST session for every leg, from the earliest reference candle
            self.session_start = min(ix.ref_start for ix in self.indices.values())
            sq = config.today_at(self.s["squareoff_time"]).replace(tzinfo=None)
            if _naive_now() >= sq:
                raise RuntimeError(f"Started after square-off time {self.s['squareoff_time']}; nothing to do today.")

            self.router = OrderRouter(self.s, self._on_trade_closed)
            settle = float(self.s.get("candle_settle_seconds", 6))

            # each index starts trading as soon as its own reference candle is in
            for ix in sorted(self.indices.values(), key=lambda x: x.ref_start):
                ref_end = ix.ref_start + timedelta(minutes=1)
                if _naive_now() < ref_end:
                    if not self.feed:
                        self._set_status(f"WAITING {ref_end.strftime('%H:%M')}")
                    logger.info("%s: waiting for the %s candle to close (selection at %s)",
                                ix.label, ix.ref_start.strftime("%H:%M"), ref_end.strftime("%H:%M"))
                self._wait_until(ref_end + timedelta(seconds=settle + 1), quiet=bool(self.feed))
                if not self.running:
                    return
                if not self.feed:
                    self._set_status("SELECTING")
                picked = 0
                for side in SIDES:
                    pick = self._select_side(ix, side)
                    if pick:
                        self._setup_side(self.sides[f"{ix.name} {side}"], ix, *pick)
                        picked += 1
                if not picked:
                    logger.error("%s: no CE or PE with a %s close inside the premium range; "
                                 "%s is not traded today", ix.label,
                                 ix.ref_start.strftime("%H:%M"), ix.label)
                    continue
                ix.selected = True
                self._go_live(ix)
                self._set_status("TRADING")

            if not any(ix.selected for ix in self.indices.values()):
                raise RuntimeError("No contract selected for any index.")

            while self.running:
                now = _naive_now()
                if not self.squared_off and now >= sq:
                    logger.info("%s square-off", self.s["squareoff_time"])
                    with self.lock:
                        self.squared_off = True
                        self._close_all("SQUAREOFF")
                    self._set_status("SQUARED OFF")
                self._risk_check()
                self._check_pending(now)
                self._poll_stops()
                self._emit()
                time.sleep(0.5)
        except Exception as e:
            logger.exception("Engine error: %s", e)
            self._set_status("ERROR")
            self.running = False
        finally:
            self._emit()

    def _poll_stops(self):
        if self.s["mode"] == "LIVE" and self.router and time.time() - self._last_poll > 2:
            self._last_poll = time.time()
            with self.lock:
                open_trades = [sd.trade for sd in self.sides.values() if sd.trade]
            if open_trades:
                self.router.poll_broker_stops(self._on_broker_stop, open_trades)

    # ==================================================================
    # setup
    # ==================================================================
    def _load_instruments(self, ix: IndexData):
        kite = self.data.kite
        logger.info("%s: loading %s instruments...", ix.label, ix.exchange)
        rows = [r for r in kite.instruments(ix.exchange)
                if r.get("name") == ix.inst_name and r.get("instrument_type") in SIDES]
        today = _naive_now().date()
        expiries = sorted({r["expiry"] for r in rows if r["expiry"] and r["expiry"] >= today})
        if not expiries:
            raise RuntimeError(f"No {ix.inst_name} option expiries found on {ix.exchange}")
        ix.expiry = expiries[0]
        rows = [r for r in rows if r["expiry"] == ix.expiry]
        ix.lot_size = int(rows[0]["lot_size"])
        ix.tick_size = float(rows[0].get("tick_size") or 0.05)

        try:
            q = kite.ltp([ix.spot_key])
            ix.spot = q[ix.spot_key]["last_price"]
        except Exception as e:
            ix.spot = None
            logger.warning("%s: spot %s not available (%s); scanning every strike",
                           ix.label, ix.spot_key, e)
        win = float(config.index_setting(self.s, ix.name, "strike_window"))
        ix.cand = {r["instrument_token"]: r for r in rows
                   if ix.spot is None or abs(float(r["strike"]) - ix.spot) <= win}
        logger.info("%s expiry %s | lot %d | spot %s | %d candidate contracts within +/-%g",
                    ix.label, ix.expiry, ix.lot_size,
                    f"{ix.spot:.2f}" if ix.spot else "n/a", len(ix.cand), win)

    def _ref_candle(self, token, ref_start):
        """The reference candle from REST, or None if not published yet."""
        try:
            return fetch_minute(self.data.kite, token, ref_start)
        except Exception as e:
            logger.warning("historical_data %s: %s", token, e)
        return None

    def _select_side(self, ix: IndexData, side):
        """
        Choose on the reference-candle close, read from REST. Start at the
        strike whose current LTP is nearest the target and walk strike by
        strike until the close crosses the target (walk_to_target), so a late
        start finds the same contract as an on-time start. A strike whose
        candle is not published yet is retried a few times.
        """
        kite = self.data.kite
        key = f"{ix.name} {side}"
        rows = sorted((r for r in ix.cand.values() if r["instrument_type"] == side),
                      key=lambda r: float(r["strike"]))
        lo = float(config.index_setting(self.s, ix.name, "premium_min"))
        hi = float(config.index_setting(self.s, ix.name, "premium_max"))
        target = float(config.index_setting(self.s, ix.name, "premium_target"))
        if not rows:
            logger.warning("%s: no contracts in the strike window", key)
            return None
        ltps = {}
        names = [f"{ix.exchange}:{r['tradingsymbol']}" for r in rows]
        for i in range(0, len(names), 500):              # Kite: up to 1000 per call
            ltps.update(kite.ltp(names[i:i + 500]))
        now_px = [ltps.get(n, {}).get("last_price") or 0 for n in names]
        start = min(range(len(rows)), key=lambda i: abs(now_px[i] - target))
        cheaper = 1 if side == "CE" else -1           # direction in which the premium falls

        got = {}

        def close_at(i):
            r = rows[i]
            for attempt in range(4):                  # not published yet: retry briefly
                if not self.running:
                    return None
                c = self._ref_candle(r["instrument_token"], ix.ref_start)
                time.sleep(0.35)                      # historical API: ~3 requests / second
                if c:
                    got[i] = (r, c)
                    return c.close
                time.sleep(1.0)
            logger.warning("%s: no %s candle for %s", key, ix.ref_start.strftime("%H:%M"),
                           r["tradingsymbol"])
            return None

        walk_to_target(len(rows), start, close_at, target, cheaper,
                       max_fetch=int(self.s.get("selection_shortlist", 20)))

        cands = [{"premium": c.close, "row": r, "candle": c} for r, c in got.values()]
        for x in sorted(cands, key=lambda x: abs(x["premium"] - target)):
            logger.info("  %s %-26s %s close %.2f", key, x["row"]["tradingsymbol"],
                        ix.ref_start.strftime("%H:%M"), x["premium"])
        pick = pick_contract(cands, lo, hi, target)
        if not pick:
            logger.warning("%s: no contract with %s close in %.0f-%.0f",
                           key, ix.ref_start.strftime("%H:%M"), lo, hi)
            return None
        logger.info("%s selected: %s (REST %s close %.2f)", key, pick["row"]["tradingsymbol"],
                    ix.ref_start.strftime("%H:%M"), pick["premium"])
        return pick["row"], pick["candle"]

    def _setup_side(self, sd, ix, row, ref):
        sd.symbol = row["tradingsymbol"]
        sd.token = row["instrument_token"]
        sd.lot_size = ix.lot_size
        sd.tick_size = ix.tick_size
        sd.bars = {ref.minute: ref}
        sd.last_bar = ref
        self._apply_ref(sd, ref)
        self._candle_log(sd, ref, "REFERENCE")

    def _apply_ref(self, sd, ref):
        """Levels from the reference candle. Also used if Zerodha revises that
        candle before this side has traded."""
        sd.ref = ref
        sd.day_high = day_high(ref.high)
        sd.day_low = day_low(ref.low)
        sd.levels = fib_prices(sd.day_high, sd.day_low)
        prev_ltp = sd.strategy.prev_ltp if sd.strategy else None
        sd.strategy = SideStrategy(sd.key, sd.levels, float(self.s["sl_points"]), sd.tick_size)
        sd.strategy.prev_ltp = prev_ltp
        logger.info("%s %s | %s O %.2f H %.2f L %.2f C %.2f | Day High %.2f Day Low %.2f",
                    sd.key, sd.symbol, ref.minute.strftime("%H:%M"), ref.open, ref.high,
                    ref.low, ref.close, sd.day_high, sd.day_low)
        logger.info("%s levels: %s", sd.key,
                    "  ".join(f"{lvl:g}={p:.2f}" for lvl, p in sd.levels))

    def _candle_log(self, sd, c, event, lag=None, previous=None):
        candle_row({
            "side": sd.key, "symbol": sd.symbol, "minute": c.minute.strftime("%H:%M"),
            "open": f"{c.open:.2f}", "high": f"{c.high:.2f}", "low": f"{c.low:.2f}",
            "close": f"{c.close:.2f}", "colour": "GREEN" if c.green else "red",
            "event": event, "lag_s": f"{lag:.1f}" if lag is not None else "",
            "previous": (f"O {previous.open:.2f} H {previous.high:.2f} "
                         f"L {previous.low:.2f} C {previous.close:.2f}") if previous else "",
        })

    # ==================================================================
    # REST candles + WebSocket for one index
    # ==================================================================
    def _go_live(self, ix: IndexData):
        """Start (or extend) the REST candle feed and the WebSocket with this
        index's selected contracts."""
        sds = [sd for sd in self.sides.values() if sd.index == ix.name and sd.token]
        settle = float(self.s.get("candle_settle_seconds", 6))
        if self.feed is None:
            self.feed = RestCandleFeed(
                self.data.kite, {}, self.session_start, on_bar=self._on_rest_bar,
                on_revise=self._on_rest_revise, now_fn=_naive_now,
                poll=float(self.s.get("rest_poll_seconds", 2.0)), settle=settle)
            started = False
        else:
            started = True
        for sd in sds:
            self.feed.add_leg(sd.key, sd.token)
            self.feed.remember(sd.key, sd.ref)
            # Late start: load every settled candle since the session start
            # BEFORE trading, so the colour check never uses a stale candle.
            if _naive_now() > ix.ref_start + timedelta(minutes=2):
                try:
                    hist = fetch_closed(self.data.kite, sd.token, self.session_start, _naive_now())
                    n = 0
                    with self.lock:
                        for c in hist:
                            if c.minute > ix.ref_start:
                                sd.bars[c.minute] = c
                                sd.last_bar = c
                                self.feed.remember(sd.key, c)
                                n += 1
                    logger.info("[%s] backfilled %d REST candles since %s", sd.key, n,
                                ix.ref_start.strftime("%H:%M"))
                except Exception as e:
                    logger.error("[%s] backfill failed: %s", sd.key, e)
        if not started:
            self.feed.start()
        self._subscribe([sd.token for sd in sds])

    def _on_rest_bar(self, key, c, lag, replay):
        with self.lock:
            sd = self.sides[key]
            sd.bars[c.minute] = c
            if sd.last_bar is None or c.minute > sd.last_bar.minute:
                sd.last_bar = c
            if not replay:
                logger.info("[%s] %s O %.2f H %.2f L %.2f C %.2f %s  (+%.1fs)",
                            key, c.minute.strftime("%H:%M"), c.open, c.high, c.low, c.close,
                            "GREEN" if c.green else "red", lag)
                self._candle_log(sd, c, "NEW", lag)
            self._resolve(sd, _naive_now())

    def _on_rest_revise(self, key, old, new, age):
        with self.lock:
            sd = self.sides[key]
            sd.bars[new.minute] = new
            if sd.last_bar is not None and sd.last_bar.minute == new.minute:
                sd.last_bar = new
            flip = old.green != new.green
            logger.warning("[%s] %s candle REVISED by Zerodha %.0fs after close: "
                           "O %.2f H %.2f L %.2f C %.2f -> O %.2f H %.2f L %.2f C %.2f%s",
                           key, new.minute.strftime("%H:%M"), age,
                           old.open, old.high, old.low, old.close,
                           new.open, new.high, new.low, new.close,
                           "  (COLOUR CHANGED)" if flip else "")
            self._candle_log(sd, new, "REVISED", age, previous=old)
            if sd.ref is not None and new.minute == sd.ref.minute:
                if new.high == sd.ref.high and new.low == sd.ref.low:
                    # open/close only: levels come from H/L, so nothing to
                    # recompute - just show the corrected candle
                    sd.ref = new
                    logger.info("[%s] reference open/close corrected; high/low unchanged, "
                                "levels unaffected", key)
                    return
                traded = any(t.side == key for t in self.trades)
                if not traded and sd.strategy and sd.strategy.position is None:
                    logger.warning("[%s] reference high/low revised before any trade: "
                                   "recomputing Day High / Day Low / levels", key)
                    self._apply_ref(sd, new)
                else:
                    logger.warning("[%s] reference high/low revised after trading started; "
                                   "levels kept as they were", key)

    def prev_green(self, sd, now):
        """Colour of the 1-min candle before `now`'s minute, from REST.
        None while that candle has not arrived yet. If it still has not
        arrived after bar_grace seconds (no trades in that minute), the
        latest REST candle before this minute is used."""
        m = _minute(now)
        c = sd.bars.get(m - timedelta(minutes=1))
        if c is not None:
            return c.green
        grace = float(self.s.get("bar_grace_seconds", 15))
        if (now - m).total_seconds() >= grace:
            # only a recent candle may stand in (no trades last minute);
            # never something from minutes or hours ago
            older = [k for k in sd.bars if m - timedelta(minutes=3) <= k < m]
            if older:
                if sd._fallback_logged != m:
                    sd._fallback_logged = m
                    logger.warning("[%s] no REST candle for %s after %.0fs; using %s",
                                   sd.key, (m - timedelta(minutes=1)).strftime("%H:%M"),
                                   grace, max(older).strftime("%H:%M"))
                return sd.bars[max(older)].green
        return None

    def _resolve(self, sd, now):
        if sd.strategy is None or sd.strategy.pending is None:
            return
        acts = sd.strategy.resolve_pending(self.prev_green(sd, now), sd.ltp,
                                           self._entries_allowed(), _minute(now))
        self._apply(sd, acts, sd.ltp, now)

    def _check_pending(self, now):
        with self.lock:
            for sd in self.sides.values():
                self._resolve(sd, now)

    # ==================================================================
    # WebSocket (LTP only)
    # ==================================================================
    def _subscribe(self, tokens):
        tokens = [t for t in tokens if t and t not in self._sub_tokens]
        if not tokens:
            return
        self._sub_tokens.extend(tokens)
        if self.kws is None:
            self._connect_feed()
            return
        if self.feed_live:
            try:
                self.kws.subscribe(tokens)
                self.kws.set_mode(self.kws.MODE_LTP, tokens)
                logger.info("WebSocket: added %d instruments (%d total)", len(tokens),
                            len(self._sub_tokens))
            except Exception as e:
                logger.error("WebSocket subscribe failed: %s", e)
        # if not connected right now, on_connect subscribes the full list

    def _connect_feed(self):
        self.kws = KiteTicker(self.data.cfg["api_key"].strip(), self.data.kite.access_token)

        def on_connect(ws, resp):
            self.feed_live = True
            ws.subscribe(list(self._sub_tokens))
            ws.set_mode(ws.MODE_LTP, list(self._sub_tokens))
            logger.info("WebSocket connected (LTP), %d instruments", len(self._sub_tokens))

        def on_close(ws, code, reason):
            self.feed_live = False
            if self.running:
                logger.warning("WebSocket closed: %s %s", code, reason)

        def on_error(ws, code, reason):
            self.feed_live = False
            logger.warning("WebSocket error: %s %s", code, reason)

        def on_reconnect(ws, n):
            logger.warning("WebSocket reconnecting, attempt %d", n)

        def on_noreconnect(ws):
            logger.error("WebSocket could not reconnect")

        self.kws.on_connect = on_connect
        self.kws.on_ticks = lambda ws, ticks: self._on_ticks(ticks)
        self.kws.on_close = on_close
        self.kws.on_error = on_error
        self.kws.on_reconnect = on_reconnect
        self.kws.on_noreconnect = on_noreconnect
        self.kws.connect(threaded=True)

    def _close_feed(self):
        if self.kws is not None:
            try:
                self.kws.close(1000, "stop")   # never kws.stop(): the reactor cannot restart
            except Exception:
                pass
            self.kws = None
        self.feed_live = False

    def _on_ticks(self, ticks):
        now = _naive_now()
        with self.lock:
            for t in ticks:
                self.ticks += 1
                ltp = t.get("last_price")
                if not ltp:
                    continue
                for sd in self.sides.values():
                    if sd.token == t["instrument_token"] and sd.strategy is not None:
                        sd.ltp = ltp
                        self._step(sd, ltp, now)

    # ==================================================================
    # trading
    # ==================================================================
    def _entries_allowed(self):
        return self.running and not (self.halted or self.locked or self.profit_locked
                                     or self.squared_off)

    def _step(self, sd, ltp, now):
        acts = sd.strategy.on_tick(ltp, self.prev_green(sd, now), self._entries_allowed(),
                                   _minute(now))
        self._apply(sd, acts, ltp, now)
        self._breakeven(sd, ltp)

    def _breakeven(self, sd, ltp):
        t = sd.trade
        if t is None or t.status != "OPEN" or not t.filled or sd.strategy.position is None:
            return
        pct = float(config.SETTINGS.get("breakeven_pct", self.s.get("breakeven_pct", 0)) or 0)
        act = sd.strategy.check_breakeven(ltp, t.entry_price, pct)
        if act is None:
            return
        t.sl = act["sl"]
        logger.info("%s BREAKEVEN %s: LTP %.2f covered %g%% of %.2f -> %.2f; SL %.2f -> %.2f (entry)",
                    sd.key, sd.symbol, ltp, pct, act["entry"], sd.strategy.position.target_price,
                    act["old_sl"], act["sl"])
        self.router.move_sl(t, "BREAKEVEN")

    def _apply(self, sd, actions, ltp, now):
        for a in actions:
            k = a["kind"]
            if k == "ENTER":
                self._open_trade(sd, a["position"], ltp, now)
            elif k == "EXIT":
                self._close_trade(sd, a["reason"], ltp, now)
            elif k == "ROLL":
                self._roll_trade(sd, a["position"], ltp, now)
            elif k == "PENDING":
                logger.info("%s cross of %g @ %.2f: waiting for the %s REST candle to confirm green",
                            sd.key, a["level"], a["price"],
                            (_minute(now) - timedelta(minutes=1)).strftime("%H:%M"))
            elif k == "SKIP":
                logger.info("%s cross of %g skipped: %s", sd.key, a["level"], a["reason"])

    def _new_trade(self, sd, pos, ltp, now):
        self.trade_seq += 1
        t = Trade(id=self.trade_seq, side=sd.key, symbol=sd.symbol, level=pos.level,
                  level_price=pos.level_price, sl=pos.sl, target_level=pos.target_level,
                  target_price=pos.target_price, entry_time=now.strftime("%H:%M:%S"),
                  signal_price=ltp, exchange=sd.exchange, lot_size=sd.lot_size,
                  tick=sd.tick_size)
        self.trades.append(t)
        return t

    def _tgt(self, pos):
        return f"{pos.target_price:.2f} ({pos.target_level:g})" if pos.target_price else "NONE (run to square-off)"

    def _open_trade(self, sd, pos, ltp, now):
        t = self._new_trade(sd, pos, ltp, now)
        sd.trade = t
        logger.info("%s BUY %s @ %.2f | break %g (%.2f) | SL %.2f | target %s",
                    sd.key, sd.symbol, ltp, pos.level, pos.level_price, pos.sl, self._tgt(pos))
        self.router.enter(t, ltp)

    def _close_trade(self, sd, reason, ltp, now):
        t = sd.trade
        sd.trade = None
        if t is None:
            return
        t.reason = reason
        t.exit_time = now.strftime("%H:%M:%S")
        t.exit_signal = ltp
        logger.info("%s EXIT %s @ %.2f (%s)", sd.key, sd.symbol, ltp, reason)
        self.router.exit(t, ltp)

    def _roll_trade(self, sd, pos, ltp, now):
        old = sd.trade
        if old is None:
            self._open_trade(sd, pos, ltp, now)
            return
        old.reason = "TARGET"
        old.exit_time = now.strftime("%H:%M:%S")
        old.exit_signal = ltp
        logger.info("%s TARGET %g hit @ %.2f -> new trade at level %g, SL %.2f, target %s",
                    sd.key, old.target_level, ltp, pos.level, pos.sl, self._tgt(pos))
        if self.s.get("roll_mode", "HOLD") == "REORDER":
            sd.trade = None
            self.router.exit(old, ltp)
            self._open_trade(sd, pos, ltp, now)
        else:
            new = self._new_trade(sd, pos, ltp, now)
            sd.trade = new
            self.router.roll_hold(old, new, ltp)

    def _close_all(self, reason):
        now = _naive_now()
        for sd in self.sides.values():
            if sd.strategy is not None:
                sd.strategy.pending = None
            if sd.strategy is not None and sd.trade is not None:
                sd.strategy.force_exit(reason, sd.ltp or 0)
                self._close_trade(sd, reason, sd.ltp or sd.trade.signal_price, now)

    def _on_broker_stop(self, trade):
        with self.lock:
            sd = self.sides[trade.side]
            if sd.trade is trade:
                logger.info("%s stop filled at the broker for %s", trade.side, trade.symbol)
                was_be = sd.strategy.position is not None and sd.strategy.position.breakeven
                sd.strategy.position = None
                sd.trade = None
                trade.reason = "BREAKEVEN SL (broker)" if was_be else "SL (broker)"
                trade.exit_time = _naive_now().strftime("%H:%M:%S")
                trade.exit_signal = trade.sl
                self.router._finish(trade)

    def _on_trade_closed(self, t: Trade):
        pnl = t.pnl(None)
        trade_row({
            "side": t.side, "symbol": t.symbol, "level": f"{t.level:g}",
            "level_price": f"{t.level_price:.2f}", "entry_time": t.entry_time,
            "entry_price": f"{t.entry_price:.2f}", "sl": f"{t.sl:.2f}",
            "target": f"{t.target_price:.2f}" if t.target_price else "NONE",
            "exit_time": t.exit_time or "",
            "exit_price": f"{t.exit_price:.2f}" if t.exit_price is not None else "",
            "reason": t.reason, "qty": t.qty, "pnl": f"{pnl:.2f}", "mode": self.s["mode"],
        })
        logger.info("Trade #%d %s %s closed (%s) P/L %+.2f", t.id, t.side, t.symbol, t.reason, pnl)

    # ==================================================================
    # risk
    # ==================================================================
    def day_pnl(self):
        realized = unrealized = 0.0
        for t in self.trades:
            ltp = self.sides[t.side].ltp
            if t.status == "CLOSED":
                realized += t.pnl(None)
            else:
                unrealized += t.pnl(ltp)
        return realized, unrealized

    def _risk_check(self):
        """Loss lock and profit target, on closed + open P/L of every index
        and account together. Limits are read live, so a change in Settings
        applies at once."""
        with self.lock:
            if self.locked or self.profit_locked:
                return
            r, u = self.day_pnl()
            total = r + u
            loss = float(config.SETTINGS.get("max_daily_loss", self.s.get("max_daily_loss", 0)) or 0)
            if loss > 0 and total <= -loss:
                self.locked = True
                logger.warning("DAILY LOSS LIMIT HIT: P/L %.2f <= -%.2f. Entries LOCKED.", total, loss)
                if config.SETTINGS.get("close_on_lock", self.s["close_on_lock"]):
                    self._close_all("LOSS LOCK")
                self.status = "LOCKED"
                return
            profit = float(config.SETTINGS.get("max_daily_profit",
                                               self.s.get("max_daily_profit", 0)) or 0)
            if profit > 0 and total >= profit:
                self.profit_locked = True
                logger.warning("MAX DAILY PROFIT REACHED: P/L %.2f >= %.2f (closed %.2f + open %.2f). "
                               "Squaring off everything; no new entries today.", total, profit, r, u)
                self._close_all("PROFIT TARGET")
                self.status = "PROFIT DONE"

    # ==================================================================
    # status
    # ==================================================================
    def _wait_until(self, dt, quiet=False):
        while self.running and _naive_now() < dt:
            if quiet:
                # other indices are already trading while this one waits
                self._risk_check()
                self._check_pending(_naive_now())
                self._poll_stops()
            self._emit()
            time.sleep(0.25)

    def _set_status(self, st):
        self.status = st
        self._emit()

    def _emit(self):
        try:
            self.status_cb(self.snapshot())
        except Exception as e:
            logger.debug("status cb: %s", e)

    def snapshot(self):
        with self.lock:
            now = _naive_now()
            r, u = self.day_pnl()
            sides = {}
            for key, sd in self.sides.items():
                st = sd.strategy
                pos = st.position if st else None
                pend = st.pending if st else None
                pg = self.prev_green(sd, now) if st else None
                if pos:
                    state = "IN TRADE"
                elif pend:
                    state = f"PENDING {pend.level:g}"
                elif st:
                    state = "WATCHING"
                else:
                    state = "—"
                lb = sd.last_bar
                sides[key] = {
                    "index": sd.index, "side": sd.side,
                    "symbol": sd.symbol, "ltp": sd.ltp, "ref": sd.ref,
                    "day_high": sd.day_high, "day_low": sd.day_low, "levels": sd.levels,
                    "state": state,
                    "prev_candle": (None if not st else
                                    "waiting" if pg is None else "GREEN" if pg else "RED"),
                    "last_bar": (f"{lb.minute.strftime('%H:%M')}  O {lb.open:.2f} H {lb.high:.2f} "
                                 f"L {lb.low:.2f} C {lb.close:.2f}" if lb else None),
                    "pos_level": pos.level if pos else None,
                    "entry": sd.trade.entry_price if sd.trade else None,
                    "sl": pos.sl if pos else None,
                    "breakeven": bool(pos and pos.breakeven),
                    "target": pos.target_price if pos else None,
                    "target_level": pos.target_level if pos else None,
                    "pnl": sd.trade.pnl(sd.ltp) if sd.trade else None,
                }
            trades = [{
                "id": t.id, "side": t.side, "symbol": t.symbol, "level": t.level,
                "entry_time": t.entry_time, "entry": t.entry_price, "sl": t.sl,
                "target": t.target_price, "exit_time": t.exit_time, "exit": t.exit_price,
                "reason": t.reason, "qty": t.qty, "status": t.status,
                "pnl": t.pnl(self.sides[t.side].ltp),
            } for t in self.trades]
            idx = {n: {"label": ix.label, "expiry": ix.expiry, "lot_size": ix.lot_size,
                       "spot": ix.spot} for n, ix in self.indices.items()}
            if self.locked:
                status = "LOCKED"
            elif self.profit_locked:
                status = "PROFIT DONE"
            else:
                status = self.status
            return {
                "status": status,
                "mode": self.s["mode"], "halted": self.halted, "locked": self.locked,
                "profit_locked": self.profit_locked,
                "feed": self.feed_live, "ticks": self.ticks, "indices": idx,
                "realized": r, "unrealized": u, "pnl": r + u,
                "max_loss": float(config.SETTINGS.get("max_daily_loss", 0) or 0),
                "max_profit": float(config.SETTINGS.get("max_daily_profit", 0) or 0),
                "rest": self.feed.health() if self.feed else None,
                "sides": sides, "trades": trades,
            }
