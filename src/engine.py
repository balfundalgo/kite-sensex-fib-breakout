"""
engine.py
=========
The daily flow.

  start      -> load BFO instruments, nearest SENSEX expiry, spot, candidate strikes
  pre-09:15  -> stream candidates, build 1-min candles from ticks
  09:16      -> pick CE/PE whose 09:15 close is in range and closest to 500;
                confirm the 09:15 OHLC from Kite historical data
  levels     -> Day High / Day Low / Fib per side
  trading    -> every tick goes through SideStrategy (CE and PE independent)
  risk       -> daily rupee loss lock, emergency stop
  15:15      -> square off everything
"""

import threading
import time
from datetime import datetime, timedelta

from kiteconnect import KiteTicker

import broker
import config
from broker import OrderRouter, Trade
from logger import logger, trade_row
from strategy import Candle, CandleBuilder, SideStrategy, day_high, day_low, fib_prices, pick_contract

SIDES = ("CE", "PE")


def _naive_now():
    return config.now_ist().replace(tzinfo=None)


def _minute(dt):
    return dt.replace(second=0, microsecond=0)


class SideData:
    def __init__(self, side):
        self.side = side
        self.symbol = ""
        self.token = None
        self.premium_915 = None
        self.ref = None              # Candle for 09:15
        self.day_high = None
        self.day_low = None
        self.levels = []             # [(level, price)]
        self.ltp = None
        self.candles = CandleBuilder()
        self.strategy = None
        self.trade = None            # open Trade


class Engine:
    def __init__(self, status_cb=None):
        self.status_cb = status_cb or (lambda snap: None)
        self.s = dict(config.SETTINGS)
        self.lock = threading.RLock()
        self.status = "IDLE"
        self.running = False
        self.halted = False          # emergency stop: no new entries
        self.locked = False          # daily loss lock
        self.squared_off = False
        self.thread = None
        self.kws = None
        self.feed_live = False
        self.ticks = 0
        self.expiry = None
        self.lot_size = 0
        self.tick_size = 0.05
        self.spot = None
        self.sides = {s: SideData(s) for s in SIDES}
        self.trades = []             # all Trades today
        self.trade_seq = 0
        self.router = None
        self._cand = {}              # token -> instrument row (pre-selection)
        self._cand_candles = {}      # token -> CandleBuilder (pre-selection)
        self._sub_tokens = []
        self._sub_mode = KiteTicker.MODE_LTP
        self._last_poll = 0.0

    # ==================================================================
    # control
    # ==================================================================
    def start(self):
        self.running = True
        self.thread = threading.Thread(target=self._run, daemon=True, name="engine")
        self.thread.start()

    def stop(self):
        self.running = False
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
            if self.locked:
                self.locked = False
                logger.warning("Loss lock released manually. It re-locks if the "
                               "day's P/L is still beyond the limit; raise the limit to continue.")
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
            logger.info("Mode %s | data account %s (%s) | order accounts: %s",
                        self.s["mode"], self.data.name, self.data.user_id,
                        ", ".join(f"{x.name} x{x.lots} lot" for x in sessions))

            self._load_instruments()

            ref_start = config.today_at(self.s["ref_candle_time"]).replace(tzinfo=None)
            ref_end = ref_start + timedelta(minutes=1)
            sq = config.today_at(self.s["squareoff_time"]).replace(tzinfo=None)

            if _naive_now() >= sq:
                raise RuntimeError(f"Started after square-off time {self.s['squareoff_time']}; nothing to do today.")

            if _naive_now() < ref_start:
                self._pre_open_stream(ref_start, ref_end)
                selected = self._select_from_ticks(ref_start)
            else:
                self._set_status("WAITING 09:16")
                self._wait_until(ref_end + timedelta(seconds=2))
                selected = {}
            if not self.running:
                return

            for side in SIDES:
                if side not in selected:
                    logger.info("%s: selecting from historical 09:15 candles", side)
                    pick = self._select_from_history(side, ref_start)
                    if pick:
                        selected[side] = pick
            if not selected:
                raise RuntimeError("No CE or PE found with a 09:15 close inside the premium range.")

            for side, row in selected.items():
                self._setup_side(side, row, ref_start)

            self.router = OrderRouter(self.s, self.lot_size, self.tick_size, self._on_trade_closed)
            self._start_trading_feed()
            self._set_status("TRADING")

            while self.running:
                now = _naive_now()
                if not self.squared_off and now >= sq:
                    logger.info("15:15 square-off")
                    with self.lock:
                        self.squared_off = True
                        self._close_all("SQUAREOFF")
                    self._set_status("SQUARED OFF")
                self._risk_check()
                if self.s["mode"] == "LIVE" and time.time() - self._last_poll > 2:
                    self._last_poll = time.time()
                    with self.lock:
                        open_trades = [sd.trade for sd in self.sides.values() if sd.trade]
                    if open_trades:
                        self.router.poll_broker_stops(self._on_broker_stop, open_trades)
                self._emit()
                time.sleep(0.5)
        except Exception as e:
            logger.exception("Engine error: %s", e)
            self._set_status("ERROR")
            self.running = False
        finally:
            self._emit()

    # ==================================================================
    # setup
    # ==================================================================
    def _load_instruments(self):
        kite = self.data.kite
        idx = self.s["index"]
        logger.info("Loading BFO instruments...")
        rows = [r for r in kite.instruments("BFO")
                if r.get("name") == idx and r.get("instrument_type") in ("CE", "PE")]
        today = _naive_now().date()
        expiries = sorted({r["expiry"] for r in rows if r["expiry"] and r["expiry"] >= today})
        if not expiries:
            raise RuntimeError(f"No {idx} option expiries found on BFO")
        self.expiry = expiries[0]
        rows = [r for r in rows if r["expiry"] == self.expiry]
        self.lot_size = int(rows[0]["lot_size"])
        self.tick_size = float(rows[0].get("tick_size") or 0.05)

        q = kite.ltp(["BSE:SENSEX"])
        self.spot = q["BSE:SENSEX"]["last_price"]
        win = float(self.s["strike_window"])
        self._cand = {r["instrument_token"]: r for r in rows
                      if abs(float(r["strike"]) - self.spot) <= win}
        logger.info("%s expiry %s | lot %d | spot %.2f | %d candidate contracts within +/-%g",
                    idx, self.expiry, self.lot_size, self.spot, len(self._cand), win)

    def _pre_open_stream(self, ref_start, ref_end):
        self._set_status("WAITING 09:15")
        self._cand_candles = {t: CandleBuilder() for t in self._cand}
        self._connect_feed(list(self._cand), KiteTicker.MODE_LTP)
        logger.info("Streaming %d candidates; reference candle %s-%s",
                    len(self._cand), ref_start.strftime("%H:%M"), ref_end.strftime("%H:%M"))
        self._wait_until(ref_end + timedelta(seconds=1.5))

    def _select_from_ticks(self, ref_start):
        self._set_status("SELECTING")
        picked = {}
        for side in SIDES:
            cands = []
            for tok, row in self._cand.items():
                if row["instrument_type"] != side:
                    continue
                cb = self._cand_candles.get(tok)
                c = None
                if cb:
                    for x in (cb.current, cb.last_closed):
                        if x and x.minute == ref_start:
                            c = x
                            break
                if c:
                    cands.append({"premium": c.close, "row": row})
            pick = pick_contract(cands, float(self.s["premium_min"]),
                                 float(self.s["premium_max"]), float(self.s["premium_target"]))
            if pick:
                picked[side] = pick["row"]
                logger.info("%s selected from ticks: %s (09:15 close %.2f)",
                            side, pick["row"]["tradingsymbol"], pick["premium"])
            else:
                logger.warning("%s: no contract in range from tick candles", side)
        return picked

    def _hist_candle(self, token, ref_start, tries=10):
        kite = self.data.kite
        for _ in range(tries):
            try:
                data = kite.historical_data(token, ref_start, ref_start + timedelta(seconds=59), "minute")
                for d in data:
                    dt = d["date"].replace(tzinfo=None) if hasattr(d["date"], "tzinfo") else d["date"]
                    if _minute(dt) == ref_start:
                        return Candle(ref_start, float(d["open"]), float(d["high"]),
                                      float(d["low"]), float(d["close"]))
            except Exception as e:
                logger.warning("historical_data %s: %s", token, e)
            time.sleep(1.5)
        return None

    def _select_from_history(self, side, ref_start):
        kite = self.data.kite
        rows = [r for r in self._cand.values() if r["instrument_type"] == side]
        keys = [f"BFO:{r['tradingsymbol']}" for r in rows]
        try:
            ltps = kite.ltp(keys)
        except Exception as e:
            logger.error("ltp for shortlist failed: %s", e)
            return None
        lo, hi = float(self.s["premium_min"]), float(self.s["premium_max"])
        short = [r for r in rows
                 if lo * 0.5 <= ltps.get(f"BFO:{r['tradingsymbol']}", {}).get("last_price", 0) <= hi * 2]
        short.sort(key=lambda r: abs(ltps[f"BFO:{r['tradingsymbol']}"]["last_price"]
                                     - float(self.s["premium_target"])))
        cands = []
        for r in short[:12]:
            c = self._hist_candle(r["instrument_token"], ref_start, tries=2)
            if c:
                cands.append({"premium": c.close, "row": r})
            time.sleep(0.35)          # historical API: ~3 requests / second
        pick = pick_contract(cands, lo, hi, float(self.s["premium_target"]))
        if pick:
            logger.info("%s selected from history: %s (09:15 close %.2f)",
                        side, pick["row"]["tradingsymbol"], pick["premium"])
            return pick["row"]
        logger.warning("%s: no contract with 09:15 close in %.0f-%.0f", side, lo, hi)
        return None

    def _setup_side(self, side, row, ref_start):
        sd = self.sides[side]
        sd.symbol = row["tradingsymbol"]
        sd.token = row["instrument_token"]
        ref = self._hist_candle(sd.token, ref_start)
        if ref is None:
            cb = self._cand_candles.get(sd.token)
            for x in ((cb.current, cb.last_closed) if cb else ()):
                if x and x.minute == ref_start:
                    ref = x
            if ref is None:
                raise RuntimeError(f"{side}: could not get the 09:15 candle for {sd.symbol}")
            logger.warning("%s: using tick-built 09:15 candle (historical unavailable)", side)
        sd.ref = ref
        sd.premium_915 = ref.close
        sd.day_high = day_high(ref.high)
        sd.day_low = day_low(ref.low)
        sd.levels = fib_prices(sd.day_high, sd.day_low)
        sd.candles = CandleBuilder()
        sd.candles.seed(ref)
        sd.strategy = SideStrategy(side, sd.levels, float(self.s["sl_points"]), self.tick_size)
        logger.info("%s %s | 09:15 O %.2f H %.2f L %.2f C %.2f | Day High %.2f Day Low %.2f",
                    side, sd.symbol, ref.open, ref.high, ref.low, ref.close, sd.day_high, sd.day_low)
        logger.info("%s levels: %s", side,
                    "  ".join(f"{lvl:g}={p:.2f}" for lvl, p in sd.levels))

    # ==================================================================
    # feed
    # ==================================================================
    def _connect_feed(self, tokens, mode):
        self._sub_tokens = list(tokens)
        self._sub_mode = mode
        if self.kws is not None:
            try:
                self.kws.subscribe(self._sub_tokens)
                self.kws.set_mode(mode, self._sub_tokens)
            except Exception as e:
                logger.warning("resubscribe failed: %s", e)
            return
        self.kws = KiteTicker(self.data.cfg["api_key"].strip(), self.data.kite.access_token)

        def on_connect(ws, resp):
            self.feed_live = True
            ws.subscribe(self._sub_tokens)
            ws.set_mode(self._sub_mode, self._sub_tokens)
            logger.info("Feed connected, %d instruments", len(self._sub_tokens))

        def on_close(ws, code, reason):
            self.feed_live = False
            if self.running:
                logger.warning("Feed closed: %s %s", code, reason)

        def on_error(ws, code, reason):
            self.feed_live = False
            logger.warning("Feed error: %s %s", code, reason)

        def on_reconnect(ws, n):
            logger.warning("Feed reconnecting, attempt %d", n)

        def on_noreconnect(ws):
            logger.error("Feed could not reconnect")

        self.kws.on_connect = on_connect
        self.kws.on_ticks = lambda ws, ticks: self._on_ticks(ticks)
        self.kws.on_close = on_close
        self.kws.on_error = on_error
        self.kws.on_reconnect = on_reconnect
        self.kws.on_noreconnect = on_noreconnect
        self.kws.connect(threaded=True)

    def _start_trading_feed(self):
        keep = [sd.token for sd in self.sides.values() if sd.token]
        drop = [t for t in self._sub_tokens if t not in keep]
        if self.kws is not None and drop:
            try:
                self.kws.unsubscribe(drop)
            except Exception:
                pass
        self._cand_candles = {}
        self._connect_feed(keep, KiteTicker.MODE_LTP)

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
        m = _minute(now)
        with self.lock:
            for t in ticks:
                self.ticks += 1
                tok = t["instrument_token"]
                ltp = t.get("last_price")
                if not ltp:
                    continue
                cb = self._cand_candles.get(tok)
                if cb is not None:
                    cb.update(m, ltp)
                for sd in self.sides.values():
                    if sd.token == tok and sd.strategy is not None:
                        sd.ltp = ltp
                        sd.candles.update(m, ltp)
                        self._step(sd, ltp, now)

    # ==================================================================
    # trading
    # ==================================================================
    def _entries_allowed(self):
        return self.running and not (self.halted or self.locked or self.squared_off)

    def _step(self, sd, ltp, now):
        actions = sd.strategy.on_tick(ltp, sd.candles.prev_green(), self._entries_allowed())
        for a in actions:
            if a["kind"] == "ENTER":
                self._open_trade(sd, a["position"], ltp, now)
            elif a["kind"] == "EXIT":
                self._close_trade(sd, a["reason"], ltp, now)
            elif a["kind"] == "ROLL":
                self._roll_trade(sd, a["position"], ltp, now)

    def _new_trade(self, sd, pos, ltp, now):
        self.trade_seq += 1
        t = Trade(id=self.trade_seq, side=sd.side, symbol=sd.symbol, level=pos.level,
                  level_price=pos.level_price, sl=pos.sl, target_level=pos.target_level,
                  target_price=pos.target_price, entry_time=now.strftime("%H:%M:%S"),
                  signal_price=ltp)
        self.trades.append(t)
        return t

    def _tgt(self, pos):
        return f"{pos.target_price:.2f} ({pos.target_level:g})" if pos.target_price else "NONE (run to 15:15)"

    def _open_trade(self, sd, pos, ltp, now):
        t = self._new_trade(sd, pos, ltp, now)
        sd.trade = t
        logger.info("%s BUY %s @ %.2f | break %g (%.2f) | SL %.2f | target %s",
                    sd.side, sd.symbol, ltp, pos.level, pos.level_price, pos.sl, self._tgt(pos))
        self.router.enter(t, ltp)

    def _close_trade(self, sd, reason, ltp, now):
        t = sd.trade
        sd.trade = None
        if t is None:
            return
        t.reason = reason
        t.exit_time = now.strftime("%H:%M:%S")
        t.exit_signal = ltp
        logger.info("%s EXIT %s @ %.2f (%s)", sd.side, sd.symbol, ltp, reason)
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
                    sd.side, old.target_level, ltp, pos.level, pos.sl, self._tgt(pos))
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
            if sd.strategy is not None and sd.trade is not None:
                sd.strategy.force_exit(reason, sd.ltp or 0)
                self._close_trade(sd, reason, sd.ltp or sd.trade.signal_price, now)

    def _on_broker_stop(self, trade):
        with self.lock:
            sd = self.sides[trade.side]
            if sd.trade is trade:
                logger.info("%s stop filled at the broker for %s", trade.side, trade.symbol)
                sd.strategy.position = None
                sd.trade = None
                trade.reason = "SL (broker)"
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
        with self.lock:
            if self.locked:
                return
            r, u = self.day_pnl()
            limit = float(config.SETTINGS.get("max_daily_loss", self.s["max_daily_loss"]))
            if limit > 0 and (r + u) <= -limit:
                self.locked = True
                logger.warning("DAILY LOSS LIMIT HIT: P/L %.2f <= -%.2f. Entries LOCKED.", r + u, limit)
                if config.SETTINGS.get("close_on_lock", self.s["close_on_lock"]):
                    self._close_all("LOSS LOCK")
                self.status = "LOCKED"

    # ==================================================================
    # status
    # ==================================================================
    def _wait_until(self, dt):
        while self.running and _naive_now() < dt:
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
            r, u = self.day_pnl()
            sides = {}
            for side, sd in self.sides.items():
                pos = sd.strategy.position if sd.strategy else None
                sides[side] = {
                    "symbol": sd.symbol, "ltp": sd.ltp, "ref": sd.ref,
                    "day_high": sd.day_high, "day_low": sd.day_low, "levels": sd.levels,
                    "state": ("IN TRADE" if pos else ("WATCHING" if sd.strategy else "—")),
                    "pos_level": pos.level if pos else None,
                    "entry": sd.trade.entry_price if sd.trade else None,
                    "sl": pos.sl if pos else None,
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
            return {
                "status": "LOCKED" if self.locked else self.status,
                "mode": self.s["mode"], "halted": self.halted, "locked": self.locked,
                "feed": self.feed_live, "ticks": self.ticks, "expiry": self.expiry,
                "lot_size": self.lot_size, "spot": self.spot,
                "realized": r, "unrealized": u, "pnl": r + u,
                "max_loss": float(config.SETTINGS.get("max_daily_loss", 0)),
                "sides": sides, "trades": trades,
            }
