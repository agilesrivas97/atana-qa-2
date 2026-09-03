"""
ui/panel_app.py
================
Main window for the panel: a CTkTabview with "General" (live status overview,
same data the old ui/tui.py showed), "TOTP" (ui/totp_tool.py) and
"Configuración" (ui/config_panel.py).

Everything here goes through shared/api_client.py — no direct DB access.

UI toolkit: CustomTkinter (still Tkinter underneath — same .after()/threading
rules as before, see ui/async_utils.py) instead of plain tk/ttk, for a
modern look. Two things stay plain Tkinter deliberately: ttk.Treeview (no CTk
table/list widget exists) and tkinter.messagebox/filedialog (native OS
dialogs — correct to leave unskinned). See ui/theme.py for the palette and
the TitledFrame helper that stands in for the tk.LabelFrame this used to use.
"""

import tkinter as tk
from tkinter import messagebox, ttk
from datetime import datetime

import customtkinter as ctk
from loguru import logger

from shared.api_client import ApiClient
from shared.paths import BASE_DIR as _BASE_DIR
from ui import theme
from ui.async_utils import run_async, run_async_retrying
from ui.config_panel import ConfigTab
from ui.totp_tool import TotpToolTab


class PanelApp:

    def __init__(self, config: dict, open_config: bool = False):
        self.config = config
        self.api    = ApiClient(config)

        self.root = ctk.CTk()
        self.root.title("ATANA Agents — Panel")
        self.root.minsize(960, 720)
        self.root.geometry("1100x780")

        self._setup_style()

        self.tabview = ctk.CTkTabview(self.root)
        self.tabview.pack(fill="both", expand=True, padx=6, pady=6)

        general_frame = self.tabview.add("📊  General")
        totp_frame    = self.tabview.add("🔑  TOTP")
        config_frame  = self.tabview.add("⚙️  Configuración")

        self.overview_tab = OverviewTab(general_frame, self.api)
        self.overview_tab.pack(fill="both", expand=True)

        # Scrollable: la tarjeta de TOTP (secreto + códigos + "guardar en un
        # agente") puede terminar más alta que la ventana, sobre todo si se
        # achica — sin esto, lo que no entra simplemente desaparece.
        # El scrollbar se pinta del color real del tab (no hay forma limpia
        # de esconderlo del todo en CTkScrollableFrame — ver
        # ui/config_panel.py:_scrollable) para que no se note en reposo.
        totp_bg = theme.resolve_color(totp_frame, totp_frame.cget("fg_color"))
        try:
            totp_scroll = ctk.CTkScrollableFrame(
                totp_frame, fg_color="transparent",
                scrollbar_fg_color=totp_bg, scrollbar_button_color=totp_bg,
                scrollbar_button_hover_color=theme.BORDER,
            )
        except TypeError:
            totp_scroll = ctk.CTkScrollableFrame(totp_frame, fg_color="transparent")
        totp_scroll.pack(fill="both", expand=True)
        self.totp_tab = TotpToolTab(totp_scroll, self.api)
        self.totp_tab.pack(fill="both", expand=True)

        self.config_tab = ConfigTab(config_frame, self.api)
        self.config_tab.pack(fill="both", expand=True)

        if open_config:
            self.tabview.set("⚙️  Configuración")

        self._pending_update_shown: str | None = None

    def _check_pending_update(self):
        """
        Avisa "cerrame para actualizarme" solo mientras el panel está
        efectivamente abierto — no hay nada persistido del lado del panel,
        es simplemente este .after() corriendo mientras la ventana exista.

        El dato (system_config.pending_panel_update) lo escribe
        dispatcher/autoupdater.py cuando intenta reemplazar atana_panel.exe
        y lo encuentra en uso — se lee acá vía GET /config/system, que ya
        existía para la pestaña Configuración; no hay ningún endpoint nuevo
        del lado del dispatcher.
        """
        def _on_done(resp):
            version = (resp.get("system", {}) or {}).get("pending_panel_update", "")
            if version and version != self._pending_update_shown:
                self._pending_update_shown = version
                messagebox.showwarning(
                    "Actualización del panel",
                    f"Hay una actualización nueva del panel ({version}) esperando para instalarse.\n\n"
                    "Cerrá esta ventana para que se aplique sola en el próximo chequeo automático.",
                    parent=self.root,
                )
            self.root.after(120_000, self._check_pending_update)

        def _on_error(_e):
            self.root.after(120_000, self._check_pending_update)

        run_async(self.root, work=lambda: self.api.get("/config/system"), on_done=_on_done, on_error=_on_error)

    def _setup_style(self):
        # The one widget that stays plain ttk (Treeview, in OverviewTab) needs
        # its own restyle to match the dark CTk shell — "clam" is the only
        # built-in ttk theme that actually honors explicit color overrides;
        # native themes (vista/aqua) mostly ignore them and keep drawing a
        # white table, which would look broken next to everything else here.
        style = ttk.Style()
        style.theme_use("clam")

        style.configure(
            "Treeview", background=theme.TABLE_BG, fieldbackground=theme.TABLE_BG,
            foreground=theme.TEXT, rowheight=38, font=theme.FONT_TABLE, borderwidth=0,
        )
        style.map("Treeview", background=[("selected", theme.PRIMARY)], foreground=[("selected", "white")])
        style.configure(
            "Treeview.Heading", background=theme.TABLE_HEADER_BG, foreground=theme.TEXT,
            font=theme.FONT_TABLE_H, borderwidth=0, relief="flat",
        )
        style.map("Treeview.Heading", background=[("active", theme.TABLE_HEADER_BG)])

    def run(self):
        self.overview_tab.start_auto_refresh()
        self.root.after(5000, self._check_pending_update)
        self.root.mainloop()


class OverviewTab(ctk.CTkFrame):
    """Live agent status — port of the old ui/tui.py DashboardWindow, now
    reading everything from the local API instead of SQL Server directly."""

    def __init__(self, parent, api: ApiClient):
        super().__init__(parent, fg_color="transparent")
        self.api = api
        self._selected_provider: str | None = None
        self._refresh_after_id: str | None = None
        self._build_ui()

    # ── Layout ─────────────────────────────────────────────────────────────

    def _build_ui(self):
        header = ctk.CTkFrame(self, fg_color=theme.SURFACE, corner_radius=0)
        header.pack(fill="x")

        ctk.CTkLabel(
            header, text="ATANA Agents", font=theme.FONT_H1, text_color="white",
        ).pack(side="left", padx=16, pady=10)

        ctk.CTkButton(
            header, text="⟳  Actualizar", font=theme.FONT_BODY, width=130,
            fg_color=theme.SURFACE_ALT, hover_color=theme.NEUTRAL_HOVER, text_color="white",
            command=self._refresh,
        ).pack(side="right", padx=12, pady=8)

        self._refresh_spinner = theme.Spinner(header, text_color="white")
        self._refresh_spinner.pack(side="right", padx=(4, 0))

        # theme.TEXT_ON_DARK, no theme.TEXT_DIM — this label sits on the dark
        # header bar, the one deliberately-dark surface where the default
        # (dark-on-light) muted text color would be nearly unreadable.
        self.lbl_last = ctk.CTkLabel(header, text="", font=theme.FONT_SMALL, text_color=theme.TEXT_ON_DARK)
        self.lbl_last.pack(side="right", padx=4)

        summary = ctk.CTkFrame(self, fg_color="transparent")
        summary.pack(fill="x", padx=16, pady=8)

        self.lbl_ok     = ctk.CTkLabel(summary, text="✔  OK: 0",           text_color=theme.SUCCESS, font=theme.FONT_BODY_B)
        self.lbl_interv = ctk.CTkLabel(summary, text="⚠  Intervención: 0", text_color=theme.WARNING, font=theme.FONT_BODY_B)
        self.lbl_err    = ctk.CTkLabel(summary, text="✖  Error: 0",        text_color=theme.DANGER,  font=theme.FONT_BODY_B)
        self.lbl_run    = ctk.CTkLabel(summary, text="",                   text_color=theme.PRIMARY, font=theme.FONT_BODY_B)
        for lbl in (self.lbl_ok, self.lbl_interv, self.lbl_err, self.lbl_run):
            lbl.pack(side="left", padx=14)

        # El banner de intervención de más abajo NO se empaqueta acá — arranca
        # oculto y _apply_refresh() lo muestra (con .pack(before=self.table_frame))
        # solo cuando hay algo que mostrar. self.table_frame se crea y
        # empaqueta primero, más abajo, así siempre existe como referencia
        # estable para el 'before='.
        self.interv_outer = ctk.CTkFrame(
            self, fg_color=theme.CARD, corner_radius=8, border_width=1, border_color=theme.WARNING,
        )

        ctk.CTkLabel(
            self.interv_outer, text="⚠  Requiere intervención — presioná Autorizar para continuar",
            font=theme.FONT_BODY_B, text_color=theme.WARNING, anchor="w",
        ).pack(fill="x", padx=12, pady=(8, 4))

        interv_canvas_frame = ctk.CTkFrame(self.interv_outer, fg_color="transparent")
        interv_canvas_frame.pack(fill="x", padx=8, pady=(0, 8))

        self._interv_canvas = tk.Canvas(interv_canvas_frame, bg=theme.CARD, highlightthickness=0, bd=0)
        interv_vsb = ctk.CTkScrollbar(interv_canvas_frame, orientation="vertical", command=self._interv_canvas.yview)
        # Aparece solo si hay más intervenciones pendientes de las que entran
        # en los 220px de alto máxima (ver el bind de <Configure> más abajo);
        # con una o dos, que es lo normal, no hace falta scroll y no se ve.
        self._interv_canvas.configure(yscrollcommand=theme.autohide_scrollbar(interv_vsb))
        self._interv_canvas.pack(side="left", fill="x", expand=True)
        interv_vsb.pack(side="right", fill="y")

        # Plain tk.Frame (not CTkFrame) — this one lives embedded directly
        # inside a raw tk.Canvas via create_window, an untested combination
        # for a CTk widget; a plain Frame is the safe, known-good choice here.
        self.interv_rows = tk.Frame(self._interv_canvas, bg=theme.CARD)
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

        self.table_frame = theme.TitledFrame(self, "Estado de agentes")
        self.table_frame.pack(fill="both", expand=True, padx=12, pady=(8, 4))

        cols = ("st", "agent", "result", "files", "last_run", "next_run", "ver")
        self.tree = ttk.Treeview(self.table_frame.body, columns=cols, show="headings", height=9, selectmode="browse")

        self.tree.heading("st",       text="")
        self.tree.heading("agent",    text="Agente")
        self.tree.heading("result",   text="Último resultado")
        self.tree.heading("files",    text="Archivos hoy")
        self.tree.heading("last_run", text="Última corrida")
        self.tree.heading("next_run", text="Próxima corrida")
        self.tree.heading("ver",      text="Versión")

        self.tree.column("st",       width=36,  anchor="center", stretch=False)
        self.tree.column("agent",    width=150, anchor="w",      stretch=False)
        self.tree.column("result",   width=290, anchor="w")
        self.tree.column("files",    width=100, anchor="center", stretch=False)
        self.tree.column("last_run", width=135, anchor="center", stretch=False)
        self.tree.column("next_run", width=230, anchor="center", stretch=False)
        self.tree.column("ver",      width=90,  anchor="center", stretch=False)

        self.tree.tag_configure("ok",           foreground=theme.SUCCESS)
        self.tree.tag_configure("error",        foreground=theme.DANGER)
        self.tree.tag_configure("running",      foreground=theme.PRIMARY)
        self.tree.tag_configure("intervention", foreground=theme.WARNING)
        self.tree.tag_configure("none",         foreground=theme.TEXT_DIM)

        vsb = ctk.CTkScrollbar(self.table_frame.body, orientation="vertical", command=self.tree.yview)
        # Con 9 filas visibles (height=9 arriba) y normalmente pocos agentes
        # configurados, la mayoría de las veces entran todos y no hace falta
        # scroll — aparece solo cuando de verdad hay más agentes que filas.
        self.tree.configure(yscrollcommand=theme.autohide_scrollbar(vsb))
        self.tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")
        self.tree.bind("<<TreeviewSelect>>", self._on_row_select)

        actions = ctk.CTkFrame(self, fg_color="transparent")
        actions.pack(fill="x", padx=12, pady=6)

        self.lbl_selected = ctk.CTkLabel(actions, text="Ningún agente seleccionado", font=theme.FONT_BODY, text_color=theme.TEXT_DIM)
        self.lbl_selected.pack(side="left")

        self.btn_retry = ctk.CTkButton(
            actions, text="↺  Reintentar seleccionado", font=theme.FONT_BODY, width=220,
            fg_color=theme.PRIMARY, hover_color=theme.PRIMARY_HOVER, text_color_disabled=theme.TEXT_DIM_2,
            state="disabled", command=self._retry,
        )
        self.btn_retry.pack(side="right", padx=4)

        log_frame = theme.TitledFrame(self, "Eventos recientes")
        log_frame.pack(fill="x", padx=12, pady=(0, 12))

        self.log_text = ctk.CTkTextbox(log_frame.body, height=140, fg_color=theme.SURFACE, corner_radius=6)
        self.log_text.pack(fill="x", expand=True)

        # The colored per-line tags (info/warning/error/...) need the raw
        # tkinter.Text CTkTextbox wraps internally — CTkTextbox's own public
        # API doesn't expose tag_configure. `_textbox` is the (informally
        # documented, widely relied-on) attribute name for it.
        self._log_inner = self.log_text._textbox
        self._log_inner.configure(font=theme.FONT_MONO_BODY, fg=theme.TEXT_ON_DARK, bg=theme.SURFACE, state="disabled", wrap="word")
        self._log_inner.tag_configure("info",    foreground="#89dceb")
        self._log_inner.tag_configure("warning", foreground="#f9e2af")
        self._log_inner.tag_configure("error",   foreground="#f38ba8")
        self._log_inner.tag_configure("success", foreground="#a6e3a1")
        self._log_inner.tag_configure("dim",     foreground="#6c7086")

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

        Cancels any already-pending auto-refresh timer first: this is also
        wired to the "⟳ Actualizar" button, and without this a manual click
        while the 30s auto-loop already has one scheduled used to leave BOTH
        chains running — each one re-arming itself forever — so a few manual
        refreshes over a session could quietly multiply into several
        overlapping refresh cycles firing every 30s.
        """
        if self._refresh_after_id is not None:
            self.after_cancel(self._refresh_after_id)
            self._refresh_after_id = None

        self._refresh_spinner.start()
        run_async_retrying(
            self,
            work=lambda: (self.api.get("/status"), self.api.get("/config/agents")),
            on_done=self._apply_refresh,
            on_final_error=self._on_refresh_error,
        )

    def _schedule_next_refresh(self):
        if self._refresh_after_id is not None:
            self.after_cancel(self._refresh_after_id)
        self._refresh_after_id = self.after(30_000, self._refresh)

    def _on_refresh_error(self, e: Exception):
        self._refresh_spinner.stop()
        self._log(None, "error", f"No se pudo conectar con el dispatcher: {e}")
        self._schedule_next_refresh()

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

            self.lbl_last.configure(text=f"Actualizado: {datetime.now().strftime('%H:%M:%S')}")
        except Exception as e:
            self._log(None, "error", f"Refresh error: {e}")

        self._refresh_spinner.stop()
        self._schedule_next_refresh()

    def _update_summary(self, statuses: list, intervention_jobs: list):
        ok  = sum(1 for s in statuses if s.get("last_result") == "ok")
        err = sum(1 for s in statuses if s.get("last_result") == "error")
        run = sum(1 for s in statuses if s.get("last_result") == "running")
        inv = len(intervention_jobs)

        self.lbl_ok.configure(text=f"✔  OK: {ok}")
        self.lbl_interv.configure(text=f"⚠  Intervención: {inv}")
        self.lbl_err.configure(text=f"✖  Error: {err}")
        self.lbl_run.configure(text=f"◉  Corriendo: {run}" if run else "")

    def _fmt_countdown(self, next_dt: datetime) -> str:
        delta_min = int((next_dt - datetime.now()).total_seconds() // 60)
        if delta_min < 1:
            return "en instantes"
        hours, minutes = divmod(delta_min, 60)
        if hours > 0:
            return f"en {hours}h {minutes}min" if minutes else f"en {hours}h"
        return f"en {minutes} min"

    def _update_intervention_rows(self, jobs: list, statuses_by_provider: dict):
        for w in self.interv_rows.winfo_children():
            w.destroy()

        if not jobs:
            self.interv_outer.pack_forget()
            return

        self.interv_outer.pack(fill="x", padx=12, pady=(8, 0), before=self.table_frame)

        for job in jobs:
            provider = job["provider"]
            reason   = job.get("intervention_reason", "Requires intervention")
            status   = statuses_by_provider.get(provider, {})
            last_run = self._fmt_date(status.get("last_run"))
            files    = status.get("files_today", 0)
            last_err = status.get("last_error")

            card = ctk.CTkFrame(self.interv_rows, fg_color=theme.CARD_WHITE, corner_radius=8, border_width=1, border_color=theme.WARNING)
            card.pack(fill="x", pady=5)

            ctk.CTkLabel(card, text="⚠", font=(theme.FONT_FAMILY, 16), text_color=theme.WARNING).pack(side="left", padx=(12, 8), pady=10)

            info = ctk.CTkFrame(card, fg_color="transparent")
            info.pack(side="left", fill="x", expand=True, pady=8)

            top_row = ctk.CTkFrame(info, fg_color="transparent")
            top_row.pack(fill="x")
            ctk.CTkLabel(top_row, text=provider.upper(), font=(theme.FONT_FAMILY, 12, "bold"), text_color=theme.TEXT).pack(side="left")
            ctk.CTkLabel(
                top_row, text=f"   Última ejecución: {last_run}   ·   Archivos hoy: {files}",
                font=theme.FONT_SMALL, text_color=theme.TEXT_DIM,
            ).pack(side="left")

            ctk.CTkLabel(info, text=reason, font=theme.FONT_BODY, text_color=theme.TEXT, anchor="w").pack(fill="x")

            if last_err:
                ctk.CTkLabel(
                    info, text=f"Último error: {last_err[:90]}",
                    font=theme.FONT_SMALL, text_color=theme.DANGER, anchor="w",
                ).pack(fill="x")

            btn_col = ctk.CTkFrame(card, fg_color="transparent")
            btn_col.pack(side="right", padx=14, pady=8)

            ctk.CTkButton(
                btn_col, text="▶  Autorizar", font=(theme.FONT_FAMILY, 11, "bold"), width=140,
                fg_color=theme.WARNING, hover_color=theme.WARNING_HOVER, text_color="white",
                command=lambda p=provider: self._play(p),
            ).pack()

            ctk.CTkFrame(btn_col, height=4, fg_color="transparent").pack()

            ctk.CTkButton(
                btn_col, text="✕  Ignorar", font=theme.FONT_SMALL, width=140,
                fg_color=theme.NEUTRAL, hover_color=theme.NEUTRAL_HOVER, text_color=theme.TEXT,
                command=lambda p=provider: self._ignore(p),
            ).pack(fill="x")

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
                    self._fmt_next_run(s.get("next_run")),
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
            self.lbl_selected.configure(text=f"Seleccionado: {self._selected_provider.upper()}")
            self.btn_retry.configure(state="normal")

    def _play(self, provider: str):
        # Async, no llamada directa al POST+GET: hacerlo en el hilo principal
        # congelaba la ventana mientras esperaba respuesta — mismo problema
        # que ya se arregló en ui/config_panel.py para 'Guardar todo' y
        # 'Reiniciar servicio'.
        if provider == "fiserv":
            self._launch_capture(provider)
            self._log(provider, "info", "Autorizado")
            self._refresh()
            return

        def _do():
            self.api.post(f"/jobs/{provider}/play")
            return self.api.get(f"/config/agents/{provider}")

        def _on_done(cfg):
            portal_url = cfg.get("portal_url")
            if portal_url:
                import webbrowser
                webbrowser.open(portal_url)
            self._log(provider, "info", "Autorizado")
            self._refresh()

        def _on_error(e):
            self._log(provider, "error", f"No se pudo autorizar: {e}")
            self._refresh()

        run_async(self, work=_do, on_done=_on_done, on_error=_on_error)

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
        def _on_done(_):
            self._log(provider, "warning", "Job ignorado")
            self._refresh()

        def _on_error(e):
            self._log(provider, "error", f"No se pudo ignorar: {e}")
            self._refresh()

        run_async(self, work=lambda: self.api.post(f"/jobs/{provider}/ignore"), on_done=_on_done, on_error=_on_error)

    def _retry(self):
        if not self._selected_provider:
            return
        provider = self._selected_provider

        def _on_done(_):
            self._log(provider, "info", "Reintento encolado")
            self._refresh()

        def _on_error(e):
            self._log(provider, "error", f"No se pudo reintentar: {e}")
            self._refresh()

        run_async(
            self, work=lambda: self.api.post(f"/jobs/{provider}", {"started_by": "manual"}),
            on_done=_on_done, on_error=_on_error,
        )

    # ── Log ────────────────────────────────────────────────────────────────

    def _log(self, provider: str | None, level: str, message: str):
        time_str = datetime.now().strftime("%H:%M:%S")
        tag_str  = f"[{provider.upper()}]" if provider else "[SYSTEM]"

        self._log_inner.configure(state="normal")
        self._log_inner.insert("end", f"{time_str}  ", "dim")
        self._log_inner.insert("end", f"{tag_str:<14}", level)
        self._log_inner.insert("end", f"  {message}\n")
        self._log_inner.see("end")
        self._log_inner.configure(state="disabled")
        logger.log(level.upper() if level in ("info", "warning", "error") else "DEBUG", f"{tag_str} {message}")

    # ── Helpers ────────────────────────────────────────────────────────────

    def _fmt_result(self, result: str, error: str = None, files: int = 0) -> str:
        if result == "ok":
            suffix = f"  ({files} archivos)" if files else ""
            return f"OK{suffix}"
        if result == "error":                 return f"Error: {(error or '')[:40]}"
        if result == "running":               return "Corriendo..."
        if result == "requires_intervention": return "⚠ Requiere intervención"
        if result == "ignored":               return "Ignorado"
        return "Sin ejecutar"

    def _fmt_date(self, dt) -> str:
        if not dt:
            return "—"
        if isinstance(dt, str):
            try:
                dt = datetime.fromisoformat(dt)
            except Exception:
                return str(dt)[:16]
        return dt.strftime("%d/%m %H:%M")

    def _fmt_next_run(self, dt) -> str:
        """Igual que _fmt_date, pero con la cuenta regresiva al lado — antes
        vivía en una sección aparte ('Próximas corridas'); ahora es directo
        parte de esta misma columna, no hace falta un listado nuevo."""
        if not dt:
            return "—"
        if isinstance(dt, str):
            try:
                dt = datetime.fromisoformat(dt)
            except Exception:
                return str(dt)[:16]
        return f"{dt.strftime('%d/%m %H:%M')} · {self._fmt_countdown(dt)}"
