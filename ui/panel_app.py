"""
ui/panel_app.py
================
Main window for the panel: a Notebook with "General" (live status overview,
same data the old ui/tui.py showed) and "Configuración" (ui/config_panel.py).

Everything here goes through shared/api_client.py — no direct DB access.
"""

import tkinter as tk
from tkinter import ttk
from datetime import datetime

from loguru import logger

from shared.api_client import ApiClient, ApiError
from shared.paths import BASE_DIR as _BASE_DIR
from ui.async_utils import run_async_retrying
from ui.config_panel import ConfigTab
from ui.totp_tool import TotpToolTab


class PanelApp:

    def __init__(self, config: dict, open_config: bool = False):
        self.config = config
        self.api    = ApiClient(config)

        self.root = tk.Tk()
        self.root.title("ATANA Agents — Panel")
        self.root.minsize(960, 720)
        self.root.geometry("1100x780")

        self._setup_style()

        self.notebook = ttk.Notebook(self.root)
        self.notebook.pack(fill="both", expand=True)

        self.overview_tab = OverviewTab(self.notebook, self.api)
        self.totp_tab     = TotpToolTab(self.notebook, self.api)
        self.config_tab   = ConfigTab(self.notebook, self.api)

        self.notebook.add(self.overview_tab, text="  General  ")
        self.notebook.add(self.totp_tab,     text="  TOTP  ")
        self.notebook.add(self.config_tab,   text="  Configuración  ")

        if open_config:
            self.notebook.select(self.config_tab)

    def _setup_style(self):
        style = ttk.Style()
        available = style.theme_names()
        if "aqua" in available:
            style.theme_use("aqua")
        elif "vista" in available:
            style.theme_use("vista")
        elif "clam" in available:
            style.theme_use("clam")

        style.configure("Treeview", rowheight=30, font=("Segoe UI", 10))
        style.configure("Treeview.Heading", font=("Segoe UI", 10, "bold"))

    def run(self):
        self.overview_tab.start_auto_refresh()
        self.root.mainloop()


class OverviewTab(ttk.Frame):
    """Live agent status — port of the old ui/tui.py DashboardWindow, now
    reading everything from the local API instead of SQL Server directly."""

    def __init__(self, parent, api: ApiClient):
        super().__init__(parent)
        self.api = api
        self._selected_provider: str | None = None
        self._build_ui()

    # ── Layout ─────────────────────────────────────────────────────────────

    def _build_ui(self):
        header = tk.Frame(self, bg="#1a1a2e", pady=8)
        header.pack(fill="x")

        tk.Label(
            header, text="ATANA Agents", font=("Segoe UI", 14, "bold"),
            fg="white", bg="#1a1a2e",
        ).pack(side="left", padx=16)

        tk.Button(
            header, text="⟳  Refresh", font=("Segoe UI", 10),
            bg="#2d2d5e", fg="white", relief="flat", cursor="hand2",
            padx=12, pady=4, command=self._refresh,
        ).pack(side="right", padx=12)

        self.lbl_last = tk.Label(header, text="", font=("Segoe UI", 9), fg="#8888aa", bg="#1a1a2e")
        self.lbl_last.pack(side="right", padx=4)

        summary = tk.Frame(self, bg="#f0f0f0", pady=7, padx=16)
        summary.pack(fill="x")

        self.lbl_ok     = tk.Label(summary, text="✔  OK: 0",            fg="#2f9e44", bg="#f0f0f0", font=("Segoe UI", 10, "bold"))
        self.lbl_interv = tk.Label(summary, text="⚠  Intervention: 0",  fg="#f08c00", bg="#f0f0f0", font=("Segoe UI", 10, "bold"))
        self.lbl_err    = tk.Label(summary, text="✖  Error: 0",         fg="#e03131", bg="#f0f0f0", font=("Segoe UI", 10, "bold"))
        self.lbl_run    = tk.Label(summary, text="",                    fg="#1971c2", bg="#f0f0f0", font=("Segoe UI", 10, "bold"))
        for lbl in (self.lbl_ok, self.lbl_interv, self.lbl_err, self.lbl_run):
            lbl.pack(side="left", padx=14)

        self.interv_outer = tk.Frame(self, bg="#fff3cd", bd=1, relief="solid")

        tk.Label(
            self.interv_outer, text="⚠  Intervention required — click Play to authorize",
            font=("Segoe UI", 11, "bold"), fg="#856404", bg="#fff3cd", pady=6, padx=12,
        ).pack(fill="x", anchor="w")

        interv_canvas_frame = tk.Frame(self.interv_outer, bg="#fff3cd")
        interv_canvas_frame.pack(fill="x", padx=8, pady=(0, 8))

        self._interv_canvas = tk.Canvas(interv_canvas_frame, bg="#fff3cd", highlightthickness=0, bd=0)
        interv_vsb = ttk.Scrollbar(interv_canvas_frame, orient="vertical", command=self._interv_canvas.yview)
        self._interv_canvas.configure(yscrollcommand=interv_vsb.set)
        self._interv_canvas.pack(side="left", fill="x", expand=True)
        interv_vsb.pack(side="right", fill="y")

        self.interv_rows = tk.Frame(self._interv_canvas, bg="#fff3cd")
        self._interv_canvas_window = self._interv_canvas.create_window((0, 0), window=self.interv_rows, anchor="nw")

        self._interv_canvas.bind(
            "<Configure>",
            lambda e: self._interv_canvas.itemconfig(self._interv_canvas_window, width=e.width),
        )
        self.interv_rows.bind(
            "<Configure>",
            lambda e: self._interv_canvas.configure(
                scrollregion=self._interv_canvas.bbox("all"),
                height=min(self.interv_rows.winfo_reqheight(), 220),
            ),
        )

        table_frame = tk.LabelFrame(self, text="Agent status", font=("Segoe UI", 10, "bold"), padx=4, pady=4)
        table_frame.pack(fill="both", expand=True, padx=12, pady=(8, 4))

        cols = ("st", "agent", "result", "files", "last_run", "next_run", "ver")
        self.tree = ttk.Treeview(table_frame, columns=cols, show="headings", height=9, selectmode="browse")

        self.tree.heading("st",       text="")
        self.tree.heading("agent",    text="Agent")
        self.tree.heading("result",   text="Last result")
        self.tree.heading("files",    text="Files today")
        self.tree.heading("last_run", text="Last run")
        self.tree.heading("next_run", text="Next run")
        self.tree.heading("ver",      text="Version")

        self.tree.column("st",       width=30,  anchor="center", stretch=False)
        self.tree.column("agent",    width=130, anchor="w",      stretch=False)
        self.tree.column("result",   width=260, anchor="w")
        self.tree.column("files",    width=90,  anchor="center", stretch=False)
        self.tree.column("last_run", width=120, anchor="center", stretch=False)
        self.tree.column("next_run", width=120, anchor="center", stretch=False)
        self.tree.column("ver",      width=80,  anchor="center", stretch=False)

        self.tree.tag_configure("ok",           foreground="#2f9e44")
        self.tree.tag_configure("error",        foreground="#e03131")
        self.tree.tag_configure("running",      foreground="#1971c2")
        self.tree.tag_configure("intervention", foreground="#f08c00")
        self.tree.tag_configure("none",         foreground="#868e96")

        vsb = ttk.Scrollbar(table_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")
        self.tree.bind("<<TreeviewSelect>>", self._on_row_select)

        actions = tk.Frame(self, pady=6, padx=12)
        actions.pack(fill="x")

        self.lbl_selected = tk.Label(actions, text="No agent selected", font=("Segoe UI", 10), fg="#666666")
        self.lbl_selected.pack(side="left")

        self.btn_retry = tk.Button(
            actions, text="↺  Retry selected", font=("Segoe UI", 10),
            bg="#1971c2", fg="white", disabledforeground="#aaaaaa",
            relief="flat", cursor="hand2", padx=12, pady=4,
            state="disabled", command=self._retry,
        )
        self.btn_retry.pack(side="right", padx=4)

        log_frame = tk.LabelFrame(self, text="Recent events", font=("Segoe UI", 10, "bold"), padx=4, pady=4)
        log_frame.pack(fill="x", padx=12, pady=(0, 12))

        self.log_text = tk.Text(
            log_frame, height=6, font=("Consolas", 9), state="disabled",
            bg="#1e1e2e", fg="#cdd6f4", relief="flat", wrap="word",
        )
        log_vsb = ttk.Scrollbar(log_frame, orient="vertical", command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=log_vsb.set)

        self.log_text.tag_configure("info",    foreground="#89dceb")
        self.log_text.tag_configure("warning", foreground="#f9e2af")
        self.log_text.tag_configure("error",   foreground="#f38ba8")
        self.log_text.tag_configure("success", foreground="#a6e3a1")
        self.log_text.tag_configure("dim",     foreground="#6c7086")

        self.log_text.pack(side="left", fill="x", expand=True)
        log_vsb.pack(side="right", fill="y")

        self._log(None, "info", "Panel iniciado")

    # ── Data refresh ───────────────────────────────────────────────────────

    def start_auto_refresh(self):
        self._refresh()

    def _refresh(self):
        """
        Fetches /status + /config/agents on a background thread — these used
        to run synchronously here, which meant the whole panel window
        couldn't appear until both round-trips (and the SQL Server hits
        behind them) finished. Retries a few times before giving up — right
        after the panel opens, the dispatcher's local API may not have bound
        its port yet (window shows up first), which used to surface as a
        one-off "<urlopen error ...>" that then worked fine a second later
        anyway. Always ends up rescheduling itself for the next cycle,
        success or not.
        """
        run_async_retrying(
            self,
            work=lambda: (self.api.get("/status"), self.api.get("/config/agents")),
            on_done=self._apply_refresh,
            on_final_error=self._on_refresh_error,
        )

    def _on_refresh_error(self, e: Exception):
        self._log(None, "error", f"No se pudo conectar con el dispatcher: {e}")
        self.after(30_000, self._refresh)

    def _apply_refresh(self, resp: tuple):
        status_resp, agents_resp = resp
        try:
            agent_cfgs = {a["provider"]: a for a in agents_resp.get("agents", []) if a.get("enabled")}
            enabled    = set(agent_cfgs)

            statuses          = [s for s in status_resp.get("agents", []) if s.get("provider") in enabled]
            intervention_jobs = [j for j in status_resp.get("intervention", []) if j.get("provider") in enabled]
            int_providers     = {j["provider"] for j in intervention_jobs}
            statuses_by_prov  = {s["provider"]: s for s in statuses}

            now = datetime.now()
            for s in statuses:
                cfg    = agent_cfgs.get(s["provider"], {})
                hour   = cfg.get("schedule_hour")
                minute = cfg.get("schedule_minute", 0)
                if hour is not None:
                    next_dt = now.replace(hour=int(hour), minute=int(minute), second=0, microsecond=0)
                    if next_dt <= now:
                        next_dt = next_dt.replace(day=next_dt.day + 1)
                    s["next_run"] = next_dt.isoformat()

            self._update_summary(statuses, intervention_jobs)
            self._update_intervention_rows(intervention_jobs, statuses_by_prov)
            self._update_table(statuses, int_providers)

            self.lbl_last.config(text=f"Updated: {datetime.now().strftime('%H:%M:%S')}")
        except Exception as e:
            self._log(None, "error", f"Refresh error: {e}")

        self.after(30_000, self._refresh)

    def _update_summary(self, statuses: list, intervention_jobs: list):
        ok  = sum(1 for s in statuses if s.get("last_result") == "ok")
        err = sum(1 for s in statuses if s.get("last_result") == "error")
        run = sum(1 for s in statuses if s.get("last_result") == "running")
        inv = len(intervention_jobs)

        self.lbl_ok.config(text=f"✔  OK: {ok}")
        self.lbl_interv.config(text=f"⚠  Intervention: {inv}")
        self.lbl_err.config(text=f"✖  Error: {err}")
        self.lbl_run.config(text=f"◉  Running: {run}" if run else "")

    def _update_intervention_rows(self, jobs: list, statuses_by_provider: dict):
        for w in self.interv_rows.winfo_children():
            w.destroy()

        if not jobs:
            self.interv_outer.pack_forget()
            return

        self.interv_outer.pack(fill="x", padx=12, pady=(8, 0))

        for job in jobs:
            provider = job["provider"]
            reason   = job.get("intervention_reason", "Requires intervention")
            status   = statuses_by_provider.get(provider, {})
            last_run = self._fmt_date(status.get("last_run"))
            files    = status.get("files_today", 0)
            last_err = status.get("last_error")

            border = tk.Frame(self.interv_rows, bg="#f08c00")
            border.pack(fill="x", pady=5)
            card = tk.Frame(border, bg="white")
            card.pack(fill="x", padx=2, pady=2)
            tk.Frame(card, bg="#f08c00", width=6).pack(side="left", fill="y")

            tk.Label(card, text="⚠", font=("Segoe UI", 16), fg="#f08c00", bg="white", padx=8, pady=10).pack(side="left")

            info = tk.Frame(card, bg="white")
            info.pack(side="left", fill="x", expand=True, pady=8)

            top_row = tk.Frame(info, bg="white")
            top_row.pack(fill="x")
            tk.Label(top_row, text=provider.upper(), font=("Segoe UI", 12, "bold"), fg="#1a1a2e", bg="white").pack(side="left")
            tk.Label(
                top_row, text=f"   Última ejecución: {last_run}   ·   Archivos hoy: {files}",
                font=("Segoe UI", 9), fg="#999999", bg="white",
            ).pack(side="left")

            tk.Label(info, text=reason, font=("Segoe UI", 10), fg="#555555", bg="white", anchor="w").pack(fill="x")

            if last_err:
                tk.Label(
                    info, text=f"Último error: {last_err[:90]}",
                    font=("Segoe UI", 9), fg="#e03131", bg="white", anchor="w",
                ).pack(fill="x")

            btn_col = tk.Frame(card, bg="white")
            btn_col.pack(side="right", padx=14, pady=8)

            play_btn = tk.Button(
                btn_col, text="▶  Play", font=("Segoe UI", 11, "bold"),
                bg="#f08c00", fg="white", relief="flat", cursor="hand2",
                padx=20, pady=7, command=lambda p=provider: self._play(p),
            )
            play_btn.pack()

            tk.Frame(btn_col, height=4, bg="white").pack()

            ign_btn = tk.Button(
                btn_col, text="✕  Ignore", font=("Segoe UI", 9),
                bg="#eeeeee", fg="#555555", relief="flat", cursor="hand2",
                padx=10, pady=4, command=lambda p=provider: self._ignore(p),
            )
            ign_btn.pack(fill="x")

    def _update_table(self, statuses: list, int_providers: set):
        selected = self._selected_provider
        for item in self.tree.get_children():
            self.tree.delete(item)

        ICONS = {"ok": "●", "error": "●", "running": "◉", "requires_intervention": "●", None: "○"}
        TAGS  = {"ok": "ok", "error": "error", "running": "running", "requires_intervention": "intervention", None: "none"}

        for s in statuses:
            provider = s.get("provider", "")
            result   = s.get("last_result")
            if provider in int_providers:
                result = "requires_intervention"

            self.tree.insert(
                "", "end", iid=provider,
                values=(
                    ICONS.get(result, "○"),
                    provider.upper(),
                    self._fmt_result(result, s.get("last_error"), s.get("files_today", 0)),
                    s.get("files_today", 0),
                    self._fmt_date(s.get("last_run")),
                    self._fmt_date(s.get("next_run")),
                    s.get("current_version", "—"),
                ),
                tags=(TAGS.get(result, "none"),),
            )

        if selected and self.tree.exists(selected):
            self.tree.selection_set(selected)

    # ── Actions ────────────────────────────────────────────────────────────

    def _on_row_select(self, _event):
        sel = self.tree.selection()
        if sel:
            self._selected_provider = sel[0]
            self.lbl_selected.config(text=f"Selected: {self._selected_provider.upper()}")
            self.btn_retry.config(state="normal")

    def _play(self, provider: str):
        try:
            if provider == "fiserv":
                self._launch_capture(provider)
            else:
                self.api.post(f"/jobs/{provider}/play")
                cfg = self.api.get(f"/config/agents/{provider}")
                portal_url = cfg.get("portal_url")
                if portal_url:
                    import webbrowser
                    webbrowser.open(portal_url)
            self._log(provider, "info", "Play authorized")
        except ApiError as e:
            self._log(provider, "error", f"No se pudo autorizar: {e}")
        self._refresh()

    def _launch_capture(self, provider: str):
        """
        Session capture needs a visible browser in THIS interactive session —
        same mechanism the tray uses (see ui/tray.py's _launch_capture):
        spawn the dispatcher exe with --capture-session, which saves the
        session and authorizes the job directly once the user logs in.
        """
        import subprocess, sys as _sys
        flags = {}
        if _sys.platform == "win32":
            flags["creationflags"] = subprocess.CREATE_NO_WINDOW

        dispatcher_exe = _BASE_DIR / "atana_dispatcher.exe"
        if dispatcher_exe.exists():
            cmd = [str(dispatcher_exe), "--capture-session", provider]
        else:
            cmd = [_sys.executable, "-m", "dispatcher.main", "--capture-session", provider]

        try:
            subprocess.Popen(cmd, cwd=_BASE_DIR, **flags)
            self._log(provider, "info", "Abriendo navegador para login manual...")
        except Exception as e:
            self._log(provider, "error", f"No se pudo lanzar la captura de sesión: {e}")

    def _ignore(self, provider: str):
        try:
            self.api.post(f"/jobs/{provider}/ignore")
            self._log(provider, "warning", "Job ignored")
        except ApiError as e:
            self._log(provider, "error", f"No se pudo ignorar: {e}")
        self._refresh()

    def _retry(self):
        if not self._selected_provider:
            return
        provider = self._selected_provider
        try:
            self.api.post(f"/jobs/{provider}", {"started_by": "manual"})
            self._log(provider, "info", "Job queued for retry")
        except ApiError as e:
            self._log(provider, "error", f"No se pudo reintentar: {e}")
        self._refresh()

    # ── Log ────────────────────────────────────────────────────────────────

    def _log(self, provider: str | None, level: str, message: str):
        time_str = datetime.now().strftime("%H:%M:%S")
        tag_str  = f"[{provider.upper()}]" if provider else "[SYSTEM]"

        self.log_text.config(state="normal")
        self.log_text.insert("end", f"{time_str}  ", "dim")
        self.log_text.insert("end", f"{tag_str:<14}", level)
        self.log_text.insert("end", f"  {message}\n")
        self.log_text.see("end")
        self.log_text.config(state="disabled")
        logger.log(level.upper() if level in ("info", "warning", "error") else "DEBUG", f"{tag_str} {message}")

    # ── Helpers ────────────────────────────────────────────────────────────

    def _fmt_result(self, result: str, error: str = None, files: int = 0) -> str:
        if result == "ok":
            suffix = f"  ({files} files)" if files else ""
            return f"OK{suffix}"
        if result == "error":                 return f"Error: {(error or '')[:40]}"
        if result == "running":               return "Running..."
        if result == "requires_intervention": return "⚠ Intervention required"
        if result == "ignored":               return "Ignored"
        return "Not run"

    def _fmt_date(self, dt) -> str:
        if not dt:
            return "—"
        if isinstance(dt, str):
            try:
                dt = datetime.fromisoformat(dt)
            except Exception:
                return str(dt)[:16]
        return dt.strftime("%d/%m %H:%M")
