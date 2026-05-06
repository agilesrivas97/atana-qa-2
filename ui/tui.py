"""
ui/tui.py
=========
Native tkinter dashboard window.
Opens from the tray icon ("Open dashboard") or via --tui flag.
"""

import json
import tkinter as tk
from tkinter import ttk
from datetime import datetime

from dispatcher import db
from shared.paths import BASE_DIR as _BASE_DIR, CONFIG_FILE as _CONFIG_FILE


class DashboardWindow:

    def __init__(self, config: dict):
        self.config = config
        self._selected_provider: str = None

        self.root = tk.Tk()
        self.root.title("ATANA Agents")
        self.root.minsize(900, 700)
        self.root.geometry("1060x760")

        self._setup_style()
        self._build_ui()

    # ── Style ──────────────────────────────────────────────────────────────────

    def _setup_style(self):
        style = ttk.Style()
        available = style.theme_names()
        if "aqua" in available:
            style.theme_use("aqua")      # macOS
        elif "vista" in available:
            style.theme_use("vista")     # Windows
        elif "clam" in available:
            style.theme_use("clam")

        style.configure("Treeview",         rowheight=30, font=("Segoe UI", 10))
        style.configure("Treeview.Heading", font=("Segoe UI", 10, "bold"))
        style.configure("Treeview",         relief="flat")
        style.layout("Treeview", [("Treeview.treearea", {"sticky": "nswe"})])

    # ── Layout ─────────────────────────────────────────────────────────────────

    def _build_ui(self):
        root = self.root

        # ── Header ────────────────────────────────────────────────────────────
        header = tk.Frame(root, bg="#1a1a2e", pady=8)
        header.pack(fill="x")

        tk.Label(
            header, text="ATANA Agents",
            font=("Segoe UI", 14, "bold"),
            fg="white", bg="#1a1a2e",
        ).pack(side="left", padx=16)

        version = self.config.get("app", {}).get("version", "?")
        tk.Label(
            header, text=f"v{version}",
            font=("Segoe UI", 10),
            fg="#8888aa", bg="#1a1a2e",
        ).pack(side="left")

        tk.Button(
            header, text="⟳  Refresh",
            font=("Segoe UI", 10),
            bg="#2d2d5e", fg="white",
            relief="flat", cursor="hand2",
            padx=12, pady=4,
            command=self._refresh,
        ).pack(side="right", padx=12)

        self.lbl_last = tk.Label(
            header, text="",
            font=("Segoe UI", 9),
            fg="#8888aa", bg="#1a1a2e",
        )
        self.lbl_last.pack(side="right", padx=4)

        # ── Summary bar ───────────────────────────────────────────────────────
        summary = tk.Frame(root, bg="#f0f0f0", pady=7, padx=16)
        summary.pack(fill="x")

        self.lbl_ok    = tk.Label(summary, text="✔  OK: 0",           fg="#2f9e44", bg="#f0f0f0", font=("Segoe UI", 10, "bold"))
        self.lbl_interv= tk.Label(summary, text="⚠  Intervention: 0", fg="#f08c00", bg="#f0f0f0", font=("Segoe UI", 10, "bold"))
        self.lbl_err   = tk.Label(summary, text="✖  Error: 0",        fg="#e03131", bg="#f0f0f0", font=("Segoe UI", 10, "bold"))
        self.lbl_run   = tk.Label(summary, text="",                    fg="#1971c2", bg="#f0f0f0", font=("Segoe UI", 10, "bold"))

        for lbl in (self.lbl_ok, self.lbl_interv, self.lbl_err, self.lbl_run):
            lbl.pack(side="left", padx=14)

        # ── Intervention section ──────────────────────────────────────────────
        self.interv_outer = tk.Frame(root, bg="#fff3cd", bd=1, relief="solid")
        # Packed/unpacked dynamically

        tk.Label(
            self.interv_outer,
            text="⚠  Intervention required — click Play to authorize",
            font=("Segoe UI", 11, "bold"),
            fg="#856404", bg="#fff3cd",
            pady=6, padx=12,
        ).pack(fill="x", anchor="w")

        # Canvas + scrollbar para que con muchos agentes no rompa el layout
        interv_canvas_frame = tk.Frame(self.interv_outer, bg="#fff3cd")
        interv_canvas_frame.pack(fill="x", padx=8, pady=(0, 8))

        self._interv_canvas = tk.Canvas(
            interv_canvas_frame, bg="#fff3cd",
            highlightthickness=0, bd=0,
        )
        interv_vsb = ttk.Scrollbar(
            interv_canvas_frame, orient="vertical",
            command=self._interv_canvas.yview,
        )
        self._interv_canvas.configure(yscrollcommand=interv_vsb.set)
        self._interv_canvas.pack(side="left", fill="x", expand=True)
        interv_vsb.pack(side="right", fill="y")

        self.interv_rows = tk.Frame(self._interv_canvas, bg="#fff3cd")
        self._interv_canvas_window = self._interv_canvas.create_window(
            (0, 0), window=self.interv_rows, anchor="nw",
        )

        def _on_interv_resize(e):
            self._interv_canvas.itemconfig(self._interv_canvas_window, width=e.width)  # noqa: used via bind
        self._interv_canvas.bind("<Configure>", _on_interv_resize)

        def _on_interv_frame_resize(e):
            h = min(self.interv_rows.winfo_reqheight(), 220)
            self._interv_canvas.configure(
                scrollregion=self._interv_canvas.bbox("all"),
                height=h,
            )
        self.interv_rows.bind("<Configure>", _on_interv_frame_resize)

        # ── Agent table ───────────────────────────────────────────────────────
        table_frame = tk.LabelFrame(
            root, text="Agent status",
            font=("Segoe UI", 10, "bold"),
            padx=4, pady=4,
        )
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

        vsb = ttk.Scrollbar(table_frame, orient="vertical",   command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)

        self.tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")

        self.tree.bind("<<TreeviewSelect>>", self._on_row_select)

        # ── Actions ───────────────────────────────────────────────────────────
        actions = tk.Frame(root, pady=6, padx=12)
        actions.pack(fill="x")

        self.lbl_selected = tk.Label(
            actions, text="No agent selected",
            font=("Segoe UI", 10), fg="#666666",
        )
        self.lbl_selected.pack(side="left")

        self.btn_retry = tk.Button(
            actions, text="↺  Retry selected",
            font=("Segoe UI", 10),
            bg="#1971c2", fg="white",
            disabledforeground="#aaaaaa",
            relief="flat", cursor="hand2",
            padx=12, pady=4,
            state="disabled",
            command=self._retry,
        )
        self.btn_retry.pack(side="right", padx=4)

        # ── Log ───────────────────────────────────────────────────────────────
        log_frame = tk.LabelFrame(
            root, text="Recent events",
            font=("Segoe UI", 10, "bold"),
            padx=4, pady=4,
        )
        log_frame.pack(fill="x", padx=12, pady=(0, 12))

        self.log_text = tk.Text(
            log_frame, height=6,
            font=("Consolas", 9),
            state="disabled",
            bg="#1e1e2e", fg="#cdd6f4",
            relief="flat", wrap="word",
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

    # ── Data refresh ───────────────────────────────────────────────────────────

    def _refresh(self):
        try:
            agent_cfgs        = {r["provider"]: r for r in db.get_all_agent_configs() if r.get("enabled")}
            enabled           = set(agent_cfgs)
            statuses          = [s for s in db.get_all_status() if s.get("provider") in enabled]
            intervention_jobs = [j for j in db.get_intervention_jobs() if j.get("provider") in enabled]
            int_providers     = {j["provider"] for j in intervention_jobs}
            statuses_by_prov  = {s["provider"]: s for s in statuses}

            # Inject next_run computed from schedule config (agent_status.next_run is not populated)
            now = datetime.now()
            for s in statuses:
                cfg = agent_cfgs.get(s["provider"], {})
                hour = cfg.get("schedule_hour")
                minute = cfg.get("schedule_minute", 0)
                if hour is not None:
                    next_dt = now.replace(hour=int(hour), minute=int(minute), second=0, microsecond=0)
                    if next_dt <= now:
                        next_dt = next_dt.replace(day=next_dt.day + 1)
                    s["next_run"] = next_dt

            self._update_summary(statuses, intervention_jobs)
            self._update_intervention_rows(intervention_jobs, statuses_by_prov)
            self._update_table(statuses, int_providers)

            self.lbl_last.config(text=f"Updated: {datetime.now().strftime('%H:%M:%S')}")
        except Exception as e:
            self._log(None, "error", f"Refresh error: {e}")

        # Auto-refresh every 30 s
        self.root.after(30_000, self._refresh)

    def _update_summary(self, statuses: list, intervention_jobs: list):
        ok  = sum(1 for s in statuses if s.get("last_result") == "ok")
        err = sum(1 for s in statuses if s.get("last_result") == "error")
        run = sum(1 for s in statuses if s.get("last_result") == "running")
        inv = len(intervention_jobs)

        self.lbl_ok.config(text=f"✔  OK: {ok}")
        self.lbl_interv.config(text=f"⚠  Intervention: {inv}")
        self.lbl_err.config(text=f"✖  Error: {err}")
        self.lbl_run.config(text=f"◉  Running: {run}" if run else "")

    def _update_intervention_rows(self, jobs: list, statuses_by_provider: dict = None):
        for w in self.interv_rows.winfo_children():
            w.destroy()

        if not jobs:
            self.interv_outer.pack_forget()
            return

        self.interv_outer.pack(fill="x", padx=12, pady=(8, 0))
        statuses_by_provider = statuses_by_provider or {}

        for job in jobs:
            provider = job["provider"]
            reason   = job.get("intervention_reason", "Requires intervention")
            status   = statuses_by_provider.get(provider, {})
            last_run = self._fmt_date(status.get("last_run"))
            files    = status.get("files_today", 0)
            last_err = status.get("last_error")

            # Outer border frame (simulates a colored border cross-platform)
            border = tk.Frame(self.interv_rows, bg="#f08c00")
            border.pack(fill="x", pady=5)

            # Inner white card
            card = tk.Frame(border, bg="white")
            card.pack(fill="x", padx=2, pady=2)

            # Left orange accent strip
            tk.Frame(card, bg="#f08c00", width=6).pack(side="left", fill="y")

            # Icon
            tk.Label(
                card, text="⚠",
                font=("Segoe UI", 16), fg="#f08c00", bg="white",
                padx=8, pady=10,
            ).pack(side="left")

            # Info block
            info = tk.Frame(card, bg="white")
            info.pack(side="left", fill="x", expand=True, pady=8)

            # Provider + last run line
            top_row = tk.Frame(info, bg="white")
            top_row.pack(fill="x")
            tk.Label(
                top_row, text=provider.upper(),
                font=("Segoe UI", 12, "bold"), fg="#1a1a2e", bg="white",
            ).pack(side="left")
            tk.Label(
                top_row,
                text=f"   Última ejecución: {last_run}   ·   Archivos hoy: {files}",
                font=("Segoe UI", 9), fg="#999999", bg="white",
            ).pack(side="left")

            # Reason
            tk.Label(
                info, text=reason,
                font=("Segoe UI", 10), fg="#555555", bg="white", anchor="w",
            ).pack(fill="x")

            # Last error
            if last_err:
                tk.Label(
                    info, text=f"Último error: {last_err[:90]}",
                    font=("Segoe UI", 9), fg="#e03131", bg="white", anchor="w",
                ).pack(fill="x")

            # Buttons column
            btn_col = tk.Frame(card, bg="white")
            btn_col.pack(side="right", padx=14, pady=8)

            play_btn = tk.Button(
                btn_col, text="▶  Play",
                font=("Segoe UI", 11, "bold"),
                bg="#f08c00", fg="white",
                relief="flat", cursor="hand2",
                padx=20, pady=7,
                command=lambda p=provider: self._play(p),
            )
            play_btn.pack()
            play_btn.bind("<Enter>", lambda e, b=play_btn: b.config(bg="#d97706"))
            play_btn.bind("<Leave>", lambda e, b=play_btn: b.config(bg="#f08c00"))

            tk.Frame(btn_col, height=4, bg="white").pack()

            ign_btn = tk.Button(
                btn_col, text="✕  Ignore",
                font=("Segoe UI", 9),
                bg="#eeeeee", fg="#555555",
                relief="flat", cursor="hand2",
                padx=10, pady=4,
                command=lambda p=provider: self._ignore(p),
            )
            ign_btn.pack(fill="x")
            ign_btn.bind("<Enter>", lambda e, b=ign_btn: b.config(bg="#dddddd"))
            ign_btn.bind("<Leave>", lambda e, b=ign_btn: b.config(bg="#eeeeee"))

    def _update_table(self, statuses: list, int_providers: set):
        selected = self._selected_provider

        for item in self.tree.get_children():
            self.tree.delete(item)

        ICONS = {
            "ok":                    "●",
            "error":                 "●",
            "running":               "◉",
            "requires_intervention": "●",
            None:                    "○",
        }
        TAGS = {
            "ok":                    "ok",
            "error":                 "error",
            "running":               "running",
            "requires_intervention": "intervention",
            None:                    "none",
        }

        for s in statuses:
            provider = s.get("provider", "")
            result   = s.get("last_result")

            if provider in int_providers:
                result = "requires_intervention"

            self.tree.insert(
                "", "end",
                iid=provider,
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

    # ── Actions ────────────────────────────────────────────────────────────────

    def _on_row_select(self, _event):
        sel = self.tree.selection()
        if sel:
            self._selected_provider = sel[0]
            self.lbl_selected.config(text=f"Selected: {self._selected_provider.upper()}")
            self.btn_retry.config(state="normal")

    def _play(self, provider: str):
        db.authorize_job(provider)
        self._log(provider, "info", "Play authorized")

        if provider == "fiserv":
            import subprocess, sys
            script = _BASE_DIR / "launcher" / "capturador_fiserv.py"
            subprocess.Popen([sys.executable, str(script)])
        else:
            portal_url = self.config.get("agents", {}).get(provider, {}).get("portal_url")
            if portal_url:
                import webbrowser
                webbrowser.open(portal_url)

        self._refresh()

    def _ignore(self, provider: str):
        db.ignore_job(provider)
        self._log(provider, "warning", "Job ignored")
        self._refresh()

    def _retry(self):
        if self._selected_provider:
            db.create_job(self._selected_provider, started_by="manual")
            self._log(self._selected_provider, "info", "Job queued for retry")
            self._refresh()

    # ── Log ────────────────────────────────────────────────────────────────────

    def _log(self, provider: str | None, level: str, message: str):
        time_str = datetime.now().strftime("%H:%M:%S")
        tag_str  = f"[{provider.upper()}]" if provider else "[SYSTEM]"

        self.log_text.config(state="normal")
        self.log_text.insert("end", f"{time_str}  ", "dim")
        self.log_text.insert("end", f"{tag_str:<14}", level)
        self.log_text.insert("end", f"  {message}\n")
        self.log_text.see("end")
        self.log_text.config(state="disabled")

    # ── Helpers ────────────────────────────────────────────────────────────────

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

    def run(self):
        self._log(None, "info", "Dashboard started")
        self._refresh()
        self.root.mainloop()


# ── Entry point ────────────────────────────────────────────────────────────────

def main():
    config = json.loads(_CONFIG_FILE.read_text(encoding="utf-8"))
    db.init(config)

    DashboardWindow(config).run()


if __name__ == "__main__":
    main()
