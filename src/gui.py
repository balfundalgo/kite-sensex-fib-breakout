"""
gui.py
======
Dashboard for the SENSEX Fib Breakout trader.

Tabs: Dashboard (CE / PE panels, Fib ladders, P/L, trades), Accounts
(Kite logins), Settings, Log. The engine runs on its own threads; the GUI
only reads snapshots and log lines from queues, every 300 ms.
"""

import queue
import threading
import tkinter as tk
from tkinter import messagebox, ttk

import customtkinter as ctk

import broker
import config
import logger as logmod
from engine import Engine
from logger import logger

BG = "#0f1115"
CARD = "#171a21"
CARD2 = "#1d212a"
LINE = "#2a2f3a"
TEXT = "#e6e8ec"
SUB = "#8b93a3"
ACCENT = "#4f8cff"
OK = "#2ecc71"
WARN = "#ff5c5c"
AMBER = "#f5a623"
CE_C = "#2ecc71"
PE_C = "#ff6b6b"
MUTED = "#3a4050"

ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("blue")


def fmt(x, nd=2, dash="—"):
    return f"{x:,.{nd}f}" if isinstance(x, (int, float)) else dash


def fmt_pnl(x):
    return f"{x:+,.2f}" if isinstance(x, (int, float)) else "—"


class SidePanel(ctk.CTkFrame):
    FIELDS = [("Contract", "symbol"), ("09:15 O/H/L/C (REST)", "ref"), ("Day High", "day_high"),
              ("Day Low", "day_low"), ("Last candle (REST)", "last_bar"), ("LTP (WebSocket)", "ltp"),
              ("State", "state"), ("Prev candle", "prev_candle"), ("Entry", "entry"),
              ("Stop-loss", "sl"), ("Target", "target"), ("Open P/L", "pnl")]

    def __init__(self, master, side, color):
        super().__init__(master, fg_color=CARD, corner_radius=12)
        self.side = side
        head = ctk.CTkFrame(self, fg_color="transparent")
        head.pack(fill="x", padx=14, pady=(12, 4))
        ctk.CTkLabel(head, text=f"● {side}", text_color=color,
                     font=ctk.CTkFont(size=18, weight="bold")).pack(side="left")
        self.badge = ctk.CTkLabel(head, text=" — ", fg_color=MUTED, corner_radius=6,
                                  font=ctk.CTkFont(size=12, weight="bold"))
        self.badge.pack(side="right")

        grid = ctk.CTkFrame(self, fg_color="transparent")
        grid.pack(fill="x", padx=14)
        self.vals = {}
        for i, (label, key) in enumerate(self.FIELDS):
            r, c = i % 6, (i // 6) * 2
            ctk.CTkLabel(grid, text=label, text_color=SUB, anchor="w").grid(
                row=r, column=c, sticky="w", pady=0, padx=(0 if c == 0 else 24, 8))
            v = ctk.CTkLabel(grid, text="—", text_color=TEXT, anchor="e",
                             font=ctk.CTkFont(size=13, weight="bold"))
            v.grid(row=r, column=c + 1, sticky="e", pady=0)
            self.vals[key] = v
        grid.columnconfigure(1, weight=1)
        grid.columnconfigure(3, weight=1)

        ctk.CTkLabel(self, text="Fibonacci levels  (trades only at 0.5 and above)",
                     text_color=SUB, anchor="w").pack(fill="x", padx=14, pady=(6, 2))
        wrap = tk.Frame(self, bg=CARD)
        wrap.pack(fill="both", expand=True, padx=14, pady=(0, 12))
        self.tree = ttk.Treeview(wrap, columns=("lvl", "price", "mark"), show="headings",
                                 height=13, style="Dark.Treeview")
        for c, w, t in (("lvl", 70, "Level"), ("price", 100, "Price"), ("mark", 170, "")):
            self.tree.heading(c, text=t)
            self.tree.column(c, width=w, anchor="center")
        self.tree.tag_configure("notrade", foreground="#5d6577")
        self.tree.tag_configure("trade", foreground=TEXT)
        self.tree.tag_configure("entry", background="#1f3b2b", foreground="#bff5d2")
        self.tree.tag_configure("target", background="#1d2c4a", foreground="#c7dbff")
        self.tree.tag_configure("ltp", background="#3a3220", foreground="#ffe0a3")
        self.tree.pack(fill="both", expand=True)
        self._levels_sig = None

    def render(self, d):
        ref = d.get("ref")
        values = {
            "symbol": d.get("symbol") or "—",
            "ltp": fmt(d.get("ltp")),
            "ref": (f"{ref.open:.2f} / {ref.high:.2f} / {ref.low:.2f} / {ref.close:.2f}"
                    if ref else "—"),
            "day_high": fmt(d.get("day_high")),
            "day_low": fmt(d.get("day_low")),
            "state": d.get("state") or "—",
            "prev_candle": d.get("prev_candle") or "—",
            "last_bar": d.get("last_bar") or "—",
            "entry": (f"{fmt(d.get('entry'))}  @ lvl {d['pos_level']:g}"
                      if d.get("pos_level") is not None else "—"),
            "sl": fmt(d.get("sl")),
            "target": (f"{fmt(d.get('target'))}  ({d['target_level']:g})"
                       if d.get("target") else ("NONE → 15:15" if d.get("pos_level") else "—")),
            "pnl": fmt_pnl(d.get("pnl")),
        }
        for k, v in values.items():
            self.vals[k].configure(text=v)
        pnl = d.get("pnl")
        self.vals["pnl"].configure(text_color=(OK if (pnl or 0) > 0 else WARN if (pnl or 0) < 0 else TEXT))
        pc = d.get("prev_candle")
        self.vals["prev_candle"].configure(
            text_color=OK if pc == "GREEN" else WARN if pc == "RED" else AMBER if pc == "waiting" else TEXT)
        st = d.get("state") or ""
        self.badge.configure(text=f" {st} ",
                             fg_color=(OK if st == "IN TRADE" else AMBER if st.startswith("PENDING")
                                       else ACCENT if st == "WATCHING" else MUTED))

        levels = d.get("levels") or []
        ltp = d.get("ltp")
        lvl_in = d.get("pos_level")
        tgt_lvl = d.get("target_level")
        # LTP sits between the highest level below it and the next one
        below = None
        if ltp is not None:
            for lvl, p in levels:
                if p <= ltp:
                    below = lvl
        sig = (tuple(levels), below, lvl_in, tgt_lvl)
        if sig == self._levels_sig:
            return
        self._levels_sig = sig
        self.tree.delete(*self.tree.get_children())
        for lvl, p in reversed(levels):
            mark, tag = "", ("trade" if lvl >= config.MIN_TRADE_LEVEL else "notrade")
            if lvl < config.MIN_TRADE_LEVEL:
                mark = "no trade"
            if lvl == tgt_lvl:
                mark, tag = "TARGET", "target"
            if lvl == lvl_in:
                mark, tag = "ENTRY LEVEL", "entry"
            if lvl == below and lvl != lvl_in:
                mark = (mark + "  ◀ LTP above").strip()
                if tag not in ("entry", "target"):
                    tag = "ltp"
            self.tree.insert("", "end", values=(f"{lvl:g}", f"{p:.2f}", mark), tags=(tag,))


class App(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title(f"{config.APP_NAME} v{config.APP_VERSION} — Balfund")
        self.geometry("1500x960")
        self.minsize(1200, 760)
        self.configure(fg_color=BG)

        self.engine = None
        self.snap = None
        self.logq = queue.Queue()
        self.snapq = queue.Queue(maxsize=5)
        logmod.add_gui_sink(self.logq.put)
        broker.sync_sessions(config.SETTINGS["accounts"])

        self._style_tree()
        self._build_header()
        self.tabs = ctk.CTkTabview(self, fg_color=BG, segmented_button_selected_color=ACCENT)
        self.tabs.pack(fill="both", expand=True, padx=12, pady=(0, 12))
        self._build_dashboard(self.tabs.add("Dashboard"))
        self._build_accounts(self.tabs.add("Accounts"))
        self._build_settings(self.tabs.add("Settings"))
        self._build_log(self.tabs.add("Log"))

        self._refresh_accounts()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(300, self._pump)
        self.after(250, self._tick_clock)
        logger.info("%s v%s ready. Mode: %s", config.APP_NAME, config.APP_VERSION,
                    config.SETTINGS["mode"])

    # ------------------------------------------------------------ styling
    def _style_tree(self):
        st = ttk.Style()
        st.theme_use("default")
        st.configure("Dark.Treeview", background=CARD2, fieldbackground=CARD2,
                     foreground=TEXT, rowheight=21, borderwidth=0, font=("Segoe UI", 10))
        st.configure("Dark.Treeview.Heading", background=CARD, foreground=SUB,
                     relief="flat", font=("Segoe UI", 10, "bold"))
        st.map("Dark.Treeview", background=[("selected", "#2b3550")])

    # ------------------------------------------------------------ header
    def _build_header(self):
        h = ctk.CTkFrame(self, fg_color=CARD, corner_radius=0, height=64)
        h.pack(fill="x")
        left = ctk.CTkFrame(h, fg_color="transparent")
        left.pack(side="left", padx=16, pady=10)
        ctk.CTkLabel(left, text="SENSEX FIB BREAKOUT", font=ctk.CTkFont(size=20, weight="bold"),
                     text_color=TEXT).pack(side="left")
        ctk.CTkLabel(left, text="  Zerodha Kite · 09:15 candle · CE / PE",
                     text_color=SUB).pack(side="left")
        self.lbl_mode = ctk.CTkLabel(left, text="", corner_radius=6,
                                     font=ctk.CTkFont(size=12, weight="bold"))
        self.lbl_mode.pack(side="left", padx=(14, 6))
        self.lbl_status = ctk.CTkLabel(left, text=" IDLE ", fg_color=MUTED, corner_radius=6,
                                       font=ctk.CTkFont(size=12, weight="bold"))
        self.lbl_status.pack(side="left", padx=6)

        right = ctk.CTkFrame(h, fg_color="transparent")
        right.pack(side="right", padx=16)
        self.lbl_clock = ctk.CTkLabel(right, text="", font=ctk.CTkFont(size=18, weight="bold"))
        self.lbl_clock.pack(side="right", padx=(14, 0))
        self.btn_unlock = ctk.CTkButton(right, text="Unlock", width=80, fg_color=MUTED,
                                        command=self._unlock)
        self.btn_unlock.pack(side="right", padx=4)
        self.btn_resume = ctk.CTkButton(right, text="Resume entries", width=120, fg_color=MUTED,
                                        command=self._resume)
        self.btn_resume.pack(side="right", padx=4)
        self.btn_emerg = ctk.CTkButton(right, text="EMERGENCY STOP", width=150, fg_color=WARN,
                                       hover_color="#d84343", command=self._emergency)
        self.btn_emerg.pack(side="right", padx=4)
        self.btn_stop = ctk.CTkButton(right, text="Stop", width=80, fg_color=MUTED,
                                      state="disabled", command=self._stop)
        self.btn_stop.pack(side="right", padx=4)
        self.btn_start = ctk.CTkButton(right, text="Start", width=90, fg_color=OK,
                                       hover_color="#27ae60", text_color="#0b1a10",
                                       command=self._start)
        self.btn_start.pack(side="right", padx=4)

        strip = ctk.CTkFrame(self, fg_color=BG, corner_radius=0)
        strip.pack(fill="x", padx=16, pady=(4, 0))
        self.lbl_feed = ctk.CTkLabel(strip, text="WS: offline   |   REST candles: —",
                                     text_color=SUB, anchor="w")
        self.lbl_feed.pack(side="left")
        self._paint_mode()

    def _paint_mode(self):
        m = config.SETTINGS["mode"]
        self.lbl_mode.configure(text=f" {m} ", fg_color=WARN if m == "LIVE" else AMBER,
                                text_color="#111")

    # ------------------------------------------------------------ dashboard
    def _stat(self, parent, title):
        f = ctk.CTkFrame(parent, fg_color=CARD, corner_radius=12)
        ctk.CTkLabel(f, text=title, text_color=SUB).pack(anchor="w", padx=14, pady=(10, 0))
        v = ctk.CTkLabel(f, text="—", font=ctk.CTkFont(size=20, weight="bold"), text_color=TEXT)
        v.pack(anchor="w", padx=14, pady=(0, 10))
        return f, v

    def _build_dashboard(self, tab):
        stats = ctk.CTkFrame(tab, fg_color="transparent")
        stats.pack(fill="x", pady=(4, 8))
        self.stat = {}
        for key, title in (("pnl", "Day P/L"), ("realized", "Realized"),
                           ("unrealized", "Unrealized"), ("maxloss", "Loss lock at"),
                           ("contract", "Expiry · Lot"), ("spot", "SENSEX")):
            f, v = self._stat(stats, title)
            f.pack(side="left", fill="x", expand=True, padx=4)
            self.stat[key] = v

        mid = ctk.CTkFrame(tab, fg_color="transparent")
        mid.pack(fill="both", expand=True)
        self.panel_ce = SidePanel(mid, "CE", CE_C)
        self.panel_ce.pack(side="left", fill="both", expand=True, padx=4)
        self.panel_pe = SidePanel(mid, "PE", PE_C)
        self.panel_pe.pack(side="left", fill="both", expand=True, padx=4)

        bot = ctk.CTkFrame(tab, fg_color=CARD, corner_radius=12)
        bot.pack(fill="both", expand=False, pady=(8, 0), padx=4)
        ctk.CTkLabel(bot, text="Trades today", text_color=SUB, anchor="w").pack(
            fill="x", padx=14, pady=(8, 2))
        cols = [("id", "#", 40), ("side", "Side", 50), ("symbol", "Symbol", 190),
                ("level", "Level", 60), ("et", "Entry time", 90), ("entry", "Entry", 90),
                ("sl", "SL", 80), ("target", "Target", 90), ("xt", "Exit time", 90),
                ("exit", "Exit", 90), ("reason", "Reason", 110), ("qty", "Qty", 60),
                ("pnl", "P/L", 100)]
        wrap = tk.Frame(bot, bg=CARD)
        wrap.pack(fill="both", expand=True, padx=14, pady=(0, 12))
        self.trades = ttk.Treeview(wrap, columns=[c[0] for c in cols], show="headings",
                                   height=6, style="Dark.Treeview")
        for c, t, w in cols:
            self.trades.heading(c, text=t)
            self.trades.column(c, width=w, anchor="center")
        self.trades.tag_configure("profit", foreground="#7ee2a8")
        self.trades.tag_configure("loss", foreground="#ff9a9a")
        self.trades.tag_configure("open", foreground="#ffe0a3")
        sb = ttk.Scrollbar(wrap, orient="vertical", command=self.trades.yview)
        self.trades.configure(yscrollcommand=sb.set)
        self.trades.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")

    # ------------------------------------------------------------ accounts
    def _build_accounts(self, tab):
        top = ctk.CTkFrame(tab, fg_color=CARD, corner_radius=12)
        top.pack(fill="both", expand=True, pady=4)
        ctk.CTkLabel(top, text="Kite accounts — orders go to every enabled, logged-in account. "
                               "Market data comes from the first one.",
                     text_color=SUB, anchor="w").pack(fill="x", padx=14, pady=(10, 4))
        wrap = tk.Frame(top, bg=CARD)
        wrap.pack(fill="both", expand=True, padx=14, pady=4)
        self.acc_tree = ttk.Treeview(wrap, columns=("name", "lots", "enabled", "status"),
                                     show="headings", height=8, style="Dark.Treeview")
        for c, t, w in (("name", "Name", 200), ("lots", "Lots", 80),
                        ("enabled", "Enabled", 90), ("status", "Status", 420)):
            self.acc_tree.heading(c, text=t)
            self.acc_tree.column(c, width=w, anchor="center")
        self.acc_tree.pack(fill="both", expand=True)
        self.acc_tree.bind("<<TreeviewSelect>>", self._acc_select)

        form = ctk.CTkFrame(tab, fg_color=CARD, corner_radius=12)
        form.pack(fill="x", pady=8)
        self.acc_vars = {
            "name": tk.StringVar(), "api_key": tk.StringVar(), "api_secret": tk.StringVar(),
            "lots": tk.StringVar(value="1"), "enabled": tk.BooleanVar(value=True),
        }
        row = ctk.CTkFrame(form, fg_color="transparent")
        row.pack(fill="x", padx=14, pady=(12, 4))
        for label, key, w, show in (("Name", "name", 160, ""), ("API key", "api_key", 220, ""),
                                    ("API secret", "api_secret", 260, "•"), ("Lots", "lots", 70, "")):
            ctk.CTkLabel(row, text=label, text_color=SUB).pack(side="left", padx=(0, 6))
            ctk.CTkEntry(row, textvariable=self.acc_vars[key], width=w, show=show).pack(
                side="left", padx=(0, 14))
        ctk.CTkCheckBox(row, text="Enabled", variable=self.acc_vars["enabled"]).pack(side="left")

        btns = ctk.CTkFrame(form, fg_color="transparent")
        btns.pack(fill="x", padx=14, pady=(4, 12))
        ctk.CTkButton(btns, text="Add / Update", width=120, command=self._acc_save).pack(side="left", padx=4)
        ctk.CTkButton(btns, text="Remove", width=90, fg_color=MUTED, command=self._acc_remove).pack(side="left", padx=4)
        ctk.CTkButton(btns, text="Login selected", width=130, fg_color=OK, text_color="#0b1a10",
                      command=lambda: self._acc_login(False)).pack(side="left", padx=(24, 4))
        ctk.CTkButton(btns, text="Login all", width=100, fg_color=OK, text_color="#0b1a10",
                      command=self._acc_login_all).pack(side="left", padx=4)
        ctk.CTkButton(btns, text="Force fresh login", width=140, fg_color=MUTED,
                      command=lambda: self._acc_login(True)).pack(side="left", padx=4)
        port = config.SETTINGS["redirect_port"]
        host = config.SETTINGS["redirect_host"]
        ctk.CTkLabel(form, text=f"Redirect URL on developers.kite.trade must be exactly  "
                                f"http://{host}:{port}/   ·   Login is once per day; tokens are cached.",
                     text_color=SUB, anchor="w").pack(fill="x", padx=14, pady=(0, 12))

    def _refresh_accounts(self):
        broker.sync_sessions(config.SETTINGS["accounts"])
        self.acc_tree.delete(*self.acc_tree.get_children())
        for a in config.SETTINGS["accounts"]:
            sess = broker.SESSIONS.get(a["name"])
            if sess and sess.logged_in:
                status = f"LOGGED IN · {sess.user_id}"
            elif sess and sess.error:
                status = f"ERROR · {sess.error[:60]}"
            else:
                status = "not logged in"
            self.acc_tree.insert("", "end", iid=a["name"], values=(
                a["name"], a.get("lots", 1), "yes" if a.get("enabled", True) else "no", status))

    def _acc_select(self, _=None):
        sel = self.acc_tree.selection()
        if not sel:
            return
        for a in config.SETTINGS["accounts"]:
            if a["name"] == sel[0]:
                for k in ("name", "api_key", "api_secret"):
                    self.acc_vars[k].set(a.get(k, ""))
                self.acc_vars["lots"].set(str(a.get("lots", 1)))
                self.acc_vars["enabled"].set(bool(a.get("enabled", True)))

    def _acc_save(self):
        name = self.acc_vars["name"].get().strip()
        if not name:
            messagebox.showerror("Account", "Name is required")
            return
        try:
            lots = int(self.acc_vars["lots"].get())
            if lots < 1:
                raise ValueError
        except ValueError:
            messagebox.showerror("Account", "Lots must be a whole number ≥ 1")
            return
        rec = {"name": name, "api_key": self.acc_vars["api_key"].get().strip(),
               "api_secret": self.acc_vars["api_secret"].get().strip(),
               "lots": lots, "enabled": bool(self.acc_vars["enabled"].get())}
        accts = [a for a in config.SETTINGS["accounts"] if a["name"] != name]
        accts.append(rec)
        config.SETTINGS["accounts"] = accts
        config.save_settings(config.SETTINGS)
        self._refresh_accounts()
        logger.info("Account %s saved (%d lot%s, %s)", name, lots, "" if lots == 1 else "s",
                    "enabled" if rec["enabled"] else "disabled")

    def _acc_remove(self):
        sel = self.acc_tree.selection()
        if not sel or not messagebox.askyesno("Remove", f"Remove account {sel[0]}?"):
            return
        config.SETTINGS["accounts"] = [a for a in config.SETTINGS["accounts"] if a["name"] != sel[0]]
        config.save_settings(config.SETTINGS)
        self._refresh_accounts()

    def _acc_login(self, force):
        sel = self.acc_tree.selection()
        if not sel:
            messagebox.showinfo("Login", "Select an account first")
            return
        self._login_names([sel[0]], force)

    def _acc_login_all(self):
        self._login_names([a["name"] for a in config.SETTINGS["accounts"] if a.get("enabled", True)], False)

    def _login_names(self, names, force):
        def work():
            for n in names:
                sess = broker.SESSIONS.get(n)
                if sess:
                    try:
                        sess.login(force=force)
                    except Exception:
                        pass
            self.after(0, self._refresh_accounts)
        threading.Thread(target=work, daemon=True).start()

    # ------------------------------------------------------------ settings
    def _build_settings(self, tab):
        box = ctk.CTkScrollableFrame(tab, fg_color=CARD, corner_radius=12)
        box.pack(fill="both", expand=True, pady=4)
        s = config.SETTINGS
        self.set_vars = {}

        def section(title):
            ctk.CTkLabel(box, text=title, font=ctk.CTkFont(size=15, weight="bold"),
                         text_color=ACCENT, anchor="w").pack(fill="x", padx=16, pady=(14, 4))

        def field(label, key, hint="", kind="entry", options=None):
            r = ctk.CTkFrame(box, fg_color="transparent")
            r.pack(fill="x", padx=16, pady=3)
            ctk.CTkLabel(r, text=label, width=260, anchor="w", text_color=TEXT).pack(side="left")
            if kind == "bool":
                v = tk.BooleanVar(value=bool(s[key]))
                ctk.CTkSwitch(r, text="", variable=v).pack(side="left")
            elif kind == "option":
                v = tk.StringVar(value=str(s[key]))
                ctk.CTkSegmentedButton(r, values=options, variable=v).pack(side="left")
            else:
                v = tk.StringVar(value=str(s[key]))
                ctk.CTkEntry(r, textvariable=v, width=160).pack(side="left")
            if hint:
                ctk.CTkLabel(r, text=hint, text_color=SUB, anchor="w").pack(side="left", padx=12)
            self.set_vars[key] = (v, kind)

        section("Mode")
        field("Trading mode", "mode", "PAPER simulates fills · LIVE sends real orders",
              kind="option", options=["PAPER", "LIVE"])
        section("Contract selection")
        field("Premium min (₹)", "premium_min")
        field("Premium max (₹)", "premium_max")
        field("Pick premium closest to (₹)", "premium_target", "09:15 candle close is compared")
        field("Strike window (± points from spot)", "strike_window")
        field("Selection shortlist (strikes per side)", "selection_shortlist",
              "closest by LTP; their 09:15 candle is pulled from REST")
        section("Timing")
        field("Reference candle (HH:MM)", "ref_candle_time", "1-minute candle")
        field("Square-off (HH:MM)", "squareoff_time")
        section("Candles (REST)  ·  WebSocket is used only for LTP")
        field("REST poll (seconds)", "rest_poll_seconds", "completed 1-min candles")
        field("Candle settle (seconds)", "candle_settle_seconds",
              "accept a candle only this long after its minute closes")
        field("Re-verify candles (minutes)", "candle_verify_minutes",
              "revisions by Zerodha within this window are applied and logged")
        field("Candle grace (seconds)", "bar_grace_seconds",
              "no candle for the previous minute after this -> use the last one")
        section("Orders & risk")
        field("Stop-loss (₹ below broken level)", "sl_points")
        field("Product", "product", kind="option", options=["MIS", "NRML"])
        field("Stop order type", "sl_order_type",
              "SL = stop-limit (Zerodha blocks SL-M on most options)", kind="option",
              options=["SL", "SL-M"])
        field("SL limit buffer (₹)", "sl_limit_buffer", "limit = trigger − buffer")
        field("Market protection", "market_protection", "-1 = Kite automatic, 1–100 = %")
        field("At target break", "roll_mode",
              "HOLD keeps the position and moves the stop · REORDER sells and buys again",
              kind="option", options=["HOLD", "REORDER"])
        field("Max daily loss (₹, all accounts)", "max_daily_loss", "0 = off")
        field("Close positions when loss lock trips", "close_on_lock", kind="bool")
        section("Kite login")
        field("Redirect host", "redirect_host")
        field("Redirect port", "redirect_port")

        ctk.CTkButton(box, text="Save settings", width=160, command=self._save_settings).pack(
            anchor="w", padx=16, pady=16)

    def _save_settings(self):
        s = config.SETTINGS
        new = {}
        numeric = {"premium_min", "premium_max", "premium_target", "strike_window",
                   "sl_points", "sl_limit_buffer", "max_daily_loss",
                   "rest_poll_seconds", "bar_grace_seconds", "candle_settle_seconds"}
        ints = {"redirect_port", "market_protection", "selection_shortlist",
                "candle_verify_minutes"}
        try:
            for key, (v, kind) in self.set_vars.items():
                val = v.get()
                if key in numeric:
                    val = float(val)
                elif key in ints:
                    val = int(float(val))
                elif key in ("ref_candle_time", "squareoff_time"):
                    config.parse_hhmm(val)
                new[key] = val
        except Exception as e:
            messagebox.showerror("Settings", f"Invalid value: {e}")
            return
        if new["premium_min"] >= new["premium_max"]:
            messagebox.showerror("Settings", "Premium min must be below premium max")
            return
        if new["mode"] == "LIVE" and s["mode"] != "LIVE":
            if not messagebox.askyesno("LIVE mode", "LIVE sends real orders to every enabled "
                                                    "account. Orders need your registered static IP. Continue?"):
                return
        s.update(new)
        config.save_settings(s)
        self._paint_mode()
        logger.info("Settings saved (mode %s). Strategy settings apply on the next Start; "
                    "the loss limit applies immediately.", s["mode"])

    # ------------------------------------------------------------ log
    def _build_log(self, tab):
        self.txt = ctk.CTkTextbox(tab, fg_color=CARD, font=ctk.CTkFont(family="Consolas", size=12))
        self.txt.pack(fill="both", expand=True, pady=4)
        ctk.CTkLabel(tab, text=f"Logs folder: {config.LOG_DIR}", text_color=SUB,
                     anchor="w").pack(fill="x", pady=(4, 0))

    # ------------------------------------------------------------ controls
    def _start(self):
        if self.engine and self.engine.running:
            return
        if not broker.active_sessions():
            messagebox.showwarning("Start", "Log in to at least one enabled account first (Accounts tab).")
            return
        if config.SETTINGS["mode"] == "LIVE" and not messagebox.askyesno(
                "LIVE", "Start in LIVE mode? Real orders will be placed."):
            return
        self.engine = Engine(status_cb=self._on_snap)
        self.engine.start()
        self.btn_start.configure(state="disabled")
        self.btn_stop.configure(state="normal")
        self.tabs.set("Dashboard")

    def _stop(self):
        if self.engine:
            open_pos = any(t["status"] != "CLOSED" for t in (self.snap or {}).get("trades", []))
            if open_pos and not messagebox.askyesno(
                    "Stop", "There is an open position. Stop does NOT close it "
                            "(use EMERGENCY STOP for that). Stop anyway?"):
                return
            self.engine.stop()
        self.btn_start.configure(state="normal")
        self.btn_stop.configure(state="disabled")

    def _emergency(self):
        if self.engine and messagebox.askyesno("Emergency stop",
                                               "Block new entries and close all strategy positions now?"):
            self.engine.emergency_stop()

    def _resume(self):
        if self.engine:
            self.engine.resume_entries()

    def _unlock(self):
        if self.engine and messagebox.askyesno("Unlock", "Release the daily loss lock?"):
            self.engine.unlock()

    def _on_close(self):
        if self.engine and self.engine.running:
            if not messagebox.askyesno("Exit", "Engine is running. Exit anyway? "
                                               "Open positions are not closed."):
                return
            self.engine.stop()
        self.destroy()

    # ------------------------------------------------------------ rendering
    def _on_snap(self, snap):
        try:
            self.snapq.put_nowait(snap)
        except queue.Full:
            pass

    def _pump(self):
        lines = []
        while not self.logq.empty():
            lines.append(self.logq.get_nowait())
        if lines:
            self.txt.insert("end", "\n".join(lines) + "\n")
            self.txt.see("end")
        snap = None
        while not self.snapq.empty():
            snap = self.snapq.get_nowait()
        if snap:
            self.snap = snap
            self._render(snap)
        self.after(300, self._pump)

    def _tick_clock(self):
        self.lbl_clock.configure(text=config.now_ist().strftime("%H:%M:%S IST"))
        self.after(250, self._tick_clock)

    def _render(self, s):
        st = s["status"]
        color = {"TRADING": OK, "LOCKED": WARN, "HALTED": WARN, "ERROR": WARN,
                 "SQUARED OFF": ACCENT, "STOPPED": MUTED}.get(st, AMBER)
        self.lbl_status.configure(text=f" {st} ", fg_color=color, text_color="#111")
        rest = s.get("rest")
        if rest and rest.get("avg_lag") is not None:
            rtxt = (f"REST candles: lag {rest['avg_lag']:.1f}s avg / {rest['max_lag']:.1f}s max"
                    + f" · revisions {rest['revisions']}"
                    + (f" (latest {rest['max_revision_age']:.0f}s after close)"
                       if rest.get("max_revision_age") is not None else "")
                    + (f" · {rest['errors']} err" if rest["errors"] else ""))
        elif rest:
            rtxt = "REST candles: waiting for first bar"
        else:
            rtxt = "REST candles: —"
        if rest and rest.get("blind"):
            rtxt = "REST candles: NO DATA"
        self.lbl_feed.configure(
            text=f"WS: {'live' if s['feed'] else 'offline'} · {s['ticks']:,} ticks   |   {rtxt}",
            text_color=(WARN if rest and rest.get("blind") else OK if s["feed"] else SUB))
        if st in ("STOPPED", "ERROR"):
            self.btn_start.configure(state="normal")
            self.btn_stop.configure(state="disabled")

        pnl = s["pnl"]
        self.stat["pnl"].configure(text=fmt_pnl(pnl), text_color=OK if pnl > 0 else WARN if pnl < 0 else TEXT)
        self.stat["realized"].configure(text=fmt_pnl(s["realized"]))
        self.stat["unrealized"].configure(text=fmt_pnl(s["unrealized"]))
        self.stat["maxloss"].configure(text=(f"-{s['max_loss']:,.0f}" if s["max_loss"] else "off")
                                       + ("  LOCKED" if s["locked"] else ""),
                                       text_color=WARN if s["locked"] else TEXT)
        self.stat["contract"].configure(text=f"{s['expiry'] or '—'} · {s['lot_size'] or '—'}")
        self.stat["spot"].configure(text=fmt(s["spot"]))

        self.panel_ce.render(s["sides"]["CE"])
        self.panel_pe.render(s["sides"]["PE"])

        self.trades.delete(*self.trades.get_children())
        for t in reversed(s["trades"]):
            tag = "open" if t["status"] != "CLOSED" else ("profit" if t["pnl"] > 0 else "loss")
            self.trades.insert("", "end", tags=(tag,), values=(
                t["id"], t["side"], t["symbol"], f"{t['level']:g}", t["entry_time"],
                fmt(t["entry"]), fmt(t["sl"]), fmt(t["target"], dash="NONE"),
                t["exit_time"] or "—", fmt(t["exit"]), t["reason"] or t["status"],
                t["qty"], fmt_pnl(t["pnl"])))


def run():
    App().mainloop()


if __name__ == "__main__":
    run()
