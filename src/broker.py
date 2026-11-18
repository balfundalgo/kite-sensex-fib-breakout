"""
broker.py
=========
Account sessions and order routing.

Every strategy trade fans out to one "leg" per enabled, logged-in Kite
account (qty = that account's lots x lot size).

PAPER: fills at the signal LTP, nothing is sent to Zerodha.
LIVE : MARKET buy -> wait for fill -> protective SELL stop at (level - 10).
       v1.6: each trade carries its own exchange (BFO for SENSEX, NFO for
       Bank Nifty), lot size and tick size, so one router serves both.
       Exits cancel the stop first; if the stop already filled, nothing else
       is sent (no double sell). All broker calls run on one worker thread so
       ticks are never blocked and orders stay in sequence.
"""

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Optional

import config
import kite_auth
from logger import logger, order_row

ORDER_TAG = "BFFIB"
DONE_STATES = ("COMPLETE", "REJECTED", "CANCELLED")


# ----------------------------------------------------------------------
# accounts
# ----------------------------------------------------------------------
class AccountSession:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.kite = None
        self.user_id = None
        self.error = ""

    @property
    def name(self):
        return self.cfg["name"]

    @property
    def enabled(self):
        return bool(self.cfg.get("enabled", True))

    @property
    def lots(self):
        return max(1, int(self.cfg.get("lots", 1)))

    @property
    def logged_in(self):
        return self.kite is not None

    def login(self, force=False):
        s = config.SETTINGS
        try:
            self.kite, profile = kite_auth.login(
                self.cfg, s["redirect_host"], int(s["redirect_port"]), force=force)
            self.user_id = profile["user_id"]
            self.error = ""
        except Exception as e:
            self.kite = None
            self.user_id = None
            self.error = str(e)
            logger.error("%s: login failed: %s", self.name, e)
            raise


SESSIONS: dict = {}          # name -> AccountSession


def sync_sessions(account_cfgs: list) -> None:
    """Keep SESSIONS in step with the accounts list in settings."""
    names = {a["name"] for a in account_cfgs}
    for n in list(SESSIONS):
        if n not in names:
            SESSIONS.pop(n)
    for a in account_cfgs:
        if a["name"] in SESSIONS:
            sess = SESSIONS[a["name"]]
            if (sess.cfg.get("api_key") != a.get("api_key")
                    or sess.cfg.get("api_secret") != a.get("api_secret")):
                sess.kite = None          # credentials changed -> login again
            sess.cfg = a
        else:
            SESSIONS[a["name"]] = AccountSession(a)


def active_sessions() -> list:
    return [s for s in SESSIONS.values() if s.enabled and s.logged_in]


# ----------------------------------------------------------------------
# trade bookkeeping
# ----------------------------------------------------------------------
@dataclass
class Leg:
    account: str
    qty: int
    entry_price: Optional[float] = None
    exit_price: Optional[float] = None
    buy_order_id: Optional[str] = None
    sl_order_id: Optional[str] = None
    closed: bool = False
    failed: bool = False


@dataclass
class Trade:
    id: int
    side: str                     # "SENSEX CE", "BANKNIFTY PE", ... (index + side)
    symbol: str
    level: float
    level_price: float
    sl: float
    target_level: Optional[float]
    target_price: Optional[float]
    entry_time: str
    signal_price: float
    legs: list = field(default_factory=list)
    exit_time: Optional[str] = None
    exit_signal: Optional[float] = None
    reason: str = ""
    status: str = "OPEN"          # OPEN | CLOSING | CLOSED
    exchange: str = "BFO"         # BFO (SENSEX) | NFO (BANKNIFTY)
    lot_size: int = 0
    tick: float = 0.05

    @property
    def filled(self):
        """At least one account has a confirmed entry price."""
        return any(l.entry_price is not None for l in self.legs if not l.failed)

    @property
    def qty(self):
        return sum(l.qty for l in self.legs if not l.failed)

    @property
    def entry_price(self):
        legs = [l for l in self.legs if l.entry_price is not None]
        if not legs:
            return self.signal_price
        q = sum(l.qty for l in legs)
        return sum(l.entry_price * l.qty for l in legs) / q

    @property
    def exit_price(self):
        legs = [l for l in self.legs if l.exit_price is not None]
        if not legs:
            return self.exit_signal
        q = sum(l.qty for l in legs)
        return sum(l.exit_price * l.qty for l in legs) / q

    def pnl(self, ltp: Optional[float]) -> float:
        total = 0.0
        for l in self.legs:
            if l.failed or l.entry_price is None:
                continue
            px = l.exit_price if l.exit_price is not None else ltp
            if px is None:
                continue
            total += (px - l.entry_price) * l.qty
        return total

    @property
    def all_legs_closed(self):
        return all(l.closed or l.failed for l in self.legs)


# ----------------------------------------------------------------------
# router
# ----------------------------------------------------------------------
def _round_tick(x, tick=0.05):
    return round(round(x / tick) * tick, 2)


class OrderRouter:
    def __init__(self, settings: dict, on_trade_closed):
        self.s = settings
        self.live = settings["mode"] == "LIVE"
        self.on_trade_closed = on_trade_closed     # callback(trade)
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="orders")
        self._poll_pending = threading.Event()

    # -- public ---------------------------------------------------------
    def build_legs(self, trade: Trade) -> None:
        trade.legs = [Leg(account=s.name, qty=s.lots * trade.lot_size)
                      for s in active_sessions()]

    def enter(self, trade: Trade, ltp: float) -> None:
        self.build_legs(trade)
        if not self.live:
            for l in trade.legs:
                l.entry_price = ltp
                self._log(trade, l, "BUY", "MARKET", ltp, None, "PAPER FILL")
            return
        self.pool.submit(self._live_enter, trade)

    def exit(self, trade: Trade, ltp: float) -> None:
        trade.status = "CLOSING"
        if not self.live:
            for l in trade.legs:
                if not l.failed:
                    l.exit_price = ltp
                    l.closed = True
                    self._log(trade, l, "SELL", "MARKET", ltp, None, f"PAPER {trade.reason}")
            self._finish(trade)
            return
        self.pool.submit(self._live_exit, trade)

    def roll_hold(self, old: Trade, new: Trade, ltp: float) -> None:
        """Book the old trade at ltp and carry its position into the new one;
        only the protective stop moves. Economically identical to sell+buy,
        without the extra two orders."""
        old.status = "CLOSING"
        new.legs = []
        for l in old.legs:
            nl = Leg(account=l.account, qty=l.qty, failed=l.failed,
                     sl_order_id=l.sl_order_id)
            if not l.failed:
                l.exit_price = ltp
                l.closed = True
                nl.entry_price = ltp
            new.legs.append(nl)
            self._log(old, l, "ROLL", "-", ltp, None,
                      f"ROLL -> {new.level:g} (position held)")
        self._finish(old)
        if self.live:
            self.pool.submit(self._live_move_sl, new, old)

    def move_sl(self, trade: Trade, why: str = "") -> None:
        """Move the protective stop to trade.sl (e.g. breakeven)."""
        if not self.live:
            for l in trade.legs:
                if not l.failed and not l.closed:
                    self._log(trade, l, "SELL", "STOP", None, trade.sl,
                              f"PAPER STOP MOVED {why}".strip())
            return
        self.pool.submit(self._live_move_sl, trade)

    def poll_broker_stops(self, on_stop_filled, open_trades: list) -> None:
        """LIVE: detect protective stops that filled at the broker."""
        if not self.live or self._poll_pending.is_set():
            return
        self._poll_pending.set()
        self.pool.submit(self._poll, on_stop_filled, list(open_trades))

    def shutdown(self):
        self.pool.shutdown(wait=False, cancel_futures=False)

    # -- live internals ---------------------------------------------------
    def _kite(self, account):
        sess = SESSIONS.get(account)
        return sess.kite if sess else None

    def _place(self, kite, trade, txn, qty, order_type, price=None, trigger=None):
        params = dict(
            variety=kite.VARIETY_REGULAR,
            exchange=trade.exchange,
            tradingsymbol=trade.symbol,
            transaction_type=txn,
            quantity=qty,
            product=self.s["product"],
            order_type=order_type,
            validity=kite.VALIDITY_DAY,
            tag=ORDER_TAG,
        )
        if price is not None:
            params["price"] = price
        if trigger is not None:
            params["trigger_price"] = trigger
        if order_type in (kite.ORDER_TYPE_MARKET, kite.ORDER_TYPE_SLM):
            mp = self.s.get("market_protection")
            if mp not in (None, 0, "0", ""):
                params["market_protection"] = int(mp)
        return kite.place_order(**params)

    @staticmethod
    def _wait(kite, order_id, timeout=10.0):
        end = time.time() + timeout
        last = {}
        while time.time() < end:
            try:
                hist = kite.order_history(order_id)
                if hist:
                    last = hist[-1]
                    if last.get("status") in DONE_STATES:
                        break
            except Exception as e:
                logger.warning("order_history %s: %s", order_id, e)
            time.sleep(0.4)
        return (last.get("status", "UNKNOWN"), last.get("average_price"),
                last.get("status_message") or "")

    def _stop_params(self, trade):
        tick = trade.tick or 0.05
        trigger = _round_tick(trade.sl, tick)
        if self.s["sl_order_type"] == "SL-M":
            return "SL-M", None, trigger
        limit = max(tick, _round_tick(trade.sl - float(self.s["sl_limit_buffer"]), tick))
        return "SL", limit, trigger

    def _place_stop(self, kite, trade, leg):
        otype, limit, trigger = self._stop_params(trade)
        kite_type = kite.ORDER_TYPE_SLM if otype == "SL-M" else kite.ORDER_TYPE_SL
        try:
            leg.sl_order_id = self._place(kite, trade, kite.TRANSACTION_TYPE_SELL,
                                          leg.qty, kite_type, price=limit, trigger=trigger)
            self._log(trade, leg, "SELL", otype, limit, trigger, "STOP PLACED", leg.sl_order_id)
        except Exception as e:
            self._log(trade, leg, "SELL", otype, limit, trigger, f"STOP FAILED: {e}")
            logger.error("%s %s: stop order failed: %s", leg.account, trade.symbol, e)

    def _live_enter(self, trade: Trade):
        for leg in trade.legs:
            kite = self._kite(leg.account)
            if kite is None:
                leg.failed = True
                continue
            try:
                oid = self._place(kite, trade, kite.TRANSACTION_TYPE_BUY,
                                  leg.qty, kite.ORDER_TYPE_MARKET)
                leg.buy_order_id = oid
            except Exception as e:
                leg.failed = True
                self._log(trade, leg, "BUY", "MARKET", None, None, f"REJECTED: {e}")
                logger.error("%s %s: BUY failed: %s", leg.account, trade.symbol, e)
                continue
            status, avg, msg = self._wait(kite, oid)
            if status != "COMPLETE":
                leg.failed = True
                self._log(trade, leg, "BUY", "MARKET", avg, None, f"{status} {msg}", oid)
                logger.error("%s %s: BUY %s %s", leg.account, trade.symbol, status, msg)
                continue
            leg.entry_price = float(avg)
            self._log(trade, leg, "BUY", "MARKET", avg, None, "FILLED", oid)
            if trade.status == "OPEN":
                self._place_stop(kite, trade, leg)

    def _live_move_sl(self, trade: Trade, old: Optional[Trade] = None):
        if old is not None:
            # runs after the old entry task, so its fills/failures are final now
            for nl, ol in zip(trade.legs, old.legs):
                nl.failed = ol.failed
                nl.sl_order_id = ol.sl_order_id
        otype, limit, trigger = self._stop_params(trade)
        for leg in trade.legs:
            if leg.failed or leg.closed:
                continue
            kite = self._kite(leg.account)
            if kite is None:
                continue
            if not leg.sl_order_id:
                self._place_stop(kite, trade, leg)
                continue
            try:
                kw = dict(variety=kite.VARIETY_REGULAR, order_id=leg.sl_order_id,
                          trigger_price=trigger)
                if limit is not None:
                    kw["price"] = limit
                kite.modify_order(**kw)
                self._log(trade, leg, "SELL", otype, limit, trigger, "STOP MOVED", leg.sl_order_id)
            except Exception as e:
                logger.error("%s: stop modify failed (%s), placing a new stop", leg.account, e)
                self._place_stop(kite, trade, leg)

    def _live_exit(self, trade: Trade):
        for leg in trade.legs:
            if leg.failed or leg.closed:
                continue
            kite = self._kite(leg.account)
            if kite is None or leg.entry_price is None:
                leg.closed = True
                continue
            if leg.sl_order_id:
                status, avg, _ = self._wait(kite, leg.sl_order_id, timeout=0.1)
                if status == "COMPLETE":
                    leg.exit_price = float(avg)
                    leg.closed = True
                    self._log(trade, leg, "SELL", "STOP", avg, None, "STOP FILLED", leg.sl_order_id)
                    continue
                try:
                    kite.cancel_order(variety=kite.VARIETY_REGULAR, order_id=leg.sl_order_id)
                except Exception as e:
                    status, avg, _ = self._wait(kite, leg.sl_order_id, timeout=1.0)
                    if status == "COMPLETE":
                        leg.exit_price = float(avg)
                        leg.closed = True
                        self._log(trade, leg, "SELL", "STOP", avg, None, "STOP FILLED", leg.sl_order_id)
                        continue
                    logger.warning("%s: stop cancel failed: %s", leg.account, e)
            try:
                oid = self._place(kite, trade, kite.TRANSACTION_TYPE_SELL,
                                  leg.qty, kite.ORDER_TYPE_MARKET)
                status, avg, msg = self._wait(kite, oid)
                if status == "COMPLETE":
                    leg.exit_price = float(avg)
                    self._log(trade, leg, "SELL", "MARKET", avg, None, f"FILLED {trade.reason}", oid)
                else:
                    self._log(trade, leg, "SELL", "MARKET", avg, None, f"{status} {msg}", oid)
                    logger.error("%s %s: EXIT %s %s - CHECK POSITION MANUALLY",
                                 leg.account, trade.symbol, status, msg)
            except Exception as e:
                self._log(trade, leg, "SELL", "MARKET", None, None, f"EXIT FAILED: {e}")
                logger.error("%s %s: EXIT failed: %s - CHECK POSITION MANUALLY",
                             leg.account, trade.symbol, e)
            leg.closed = True
        self._finish(trade)

    def _poll(self, on_stop_filled, trades):
        try:
            by_acct = {}
            for t in trades:
                for leg in t.legs:
                    if leg.sl_order_id and not leg.closed and not leg.failed:
                        by_acct.setdefault(leg.account, []).append((t, leg))
            for acct, pairs in by_acct.items():
                kite = self._kite(acct)
                if kite is None:
                    continue
                try:
                    orders = {o["order_id"]: o for o in kite.orders()}
                except Exception as e:
                    logger.warning("%s: orders() failed: %s", acct, e)
                    continue
                for t, leg in pairs:
                    o = orders.get(leg.sl_order_id)
                    if o and o.get("status") == "COMPLETE":
                        leg.exit_price = float(o.get("average_price") or t.sl)
                        leg.closed = True
                        self._log(t, leg, "SELL", "STOP", leg.exit_price, None,
                                  "STOP FILLED AT BROKER", leg.sl_order_id)
            for t in trades:
                stopped = any(l.sl_order_id and l.closed and not l.failed for l in t.legs)
                if t.status == "OPEN" and stopped and t.all_legs_closed:
                    on_stop_filled(t)
        finally:
            self._poll_pending.clear()

    # -- shared -----------------------------------------------------------
    def _finish(self, trade: Trade):
        trade.status = "CLOSED"
        try:
            self.on_trade_closed(trade)
        except Exception as e:
            logger.error("trade close callback: %s", e)

    def _log(self, trade, leg, txn, otype, price, trigger, result, oid=""):
        order_row({
            "account": leg.account, "symbol": trade.symbol, "txn": txn,
            "order_type": otype, "level": f"{trade.level:g}", "qty": leg.qty,
            "price": f"{price:.2f}" if isinstance(price, (int, float)) else "",
            "trigger": f"{trigger:.2f}" if isinstance(trigger, (int, float)) else "",
            "entry": f"{trade.level_price:.2f}", "sl": f"{trade.sl:.2f}",
            "target": f"{trade.target_price:.2f}" if trade.target_price else "NONE",
            "order_id": oid or "", "result": result,
            "mode": "LIVE" if self.live else "PAPER",
        })
