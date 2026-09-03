"""
ui/config_panel.py
===================
"Configuración" tab: system-wide settings (General / SMTP / Auto-update) plus
one tab per agent, generated dynamically from GET /config/agents.

Everything here goes through shared/api_client.py — NEVER SQL Server
directly. Every secret field (password, token, TOTP secret, account token)
is a plain masked Entry pre-filled with the CURRENT decrypted value (fetched
once, async, right when the tab populates — see
GET /config/agents/{provider}/secret/{field} in dispatcher/api.py) with a
"Mostrar" checkbox to reveal it — no separate "Reemplazar" dialog. You see
it, edit it in place, and it's sent back and encrypted server-side
(dispatcher/db.py) on save, same as every other field.

Loading: every tab that needs data shows a "Cargando..." placeholder first —
window and tabview appear instantly — and gets populated once its GET
finishes, via ui/async_utils.run_async_retrying (background thread + Tk-safe
handoff, retries the odd transient failure right after the panel opens). Two
shared fetches feed the whole ConfigTab: one GET /config/system for
General+SMTP+Auto-update (used to be 3 separate round-trips — one per tab —
which is why this used to feel slow to open), one GET /config/agents for
every agent tab.

Saving: a single "💾 Guardar todo" button lives bottom-right of ConfigTab and
saves every tab's fields — plain and secret alike — in one shot. MercadoPago
-style accounts still save immediately per-row (add/remove/token), since
each is a one-off list operation, not form state.

'Guardar todo' itself is two-phase to keep the window responsive: each tab's
_build_save_job() reads its Tk vars and returns a plain {endpoint, body} dict
— fast, main-thread only, since Tk variables aren't safe to touch off-thread.
ConfigTab then fires all the PUTs in a single background thread (via
ui.async_utils.run_async) and only touches widgets again once every request
is back. Doing the PUTs inline on the main thread (the original approach)
blocked the Tk event loop for the whole batch — several agents' worth of
sequential HTTP round-trips — which is what caused the window to stop
repainting and show stale/garbled text while "Guardando..." was up.

UI toolkit: CustomTkinter (see ui/theme.py for the shared palette and the
TitledFrame helper standing in for tk.LabelFrame, which CTk has no
equivalent of). The per-agent/per-section tabs used to be pages added
directly to a ttk.Notebook; now they're plain frames packed INSIDE the
frame a ctk.CTkTabview hands back from .add(name) — see ConfigTab. That's
the only structural change from the ttk.Notebook days; every tab class
below (_SettingsTabBase and its subclasses) is otherwise unchanged, since
it's still just "a widget with a parent" either way.
"""

import tkinter as tk
from tkinter import filedialog, messagebox

import customtkinter as ctk

from shared.api_client import ApiClient, ApiError
from ui import theme
from ui.async_utils import run_async, run_async_retrying

_FIELD_FONT = theme.FONT_BODY
_LABEL_FONT = theme.FONT_SUBTITLE

_AGENT_TOP_LEVEL_FIELDS = [
    # (key, label, kind)  kind in {str, int, bool, folder} — 'username' excluded,
    # it's rendered inside the CREDENCIALES group instead (see AgentConfigTab).
    ("enabled",            "Habilitado",                 "bool"),
    ("destination_folder", "Carpeta de destino",         "folder"),
    ("rename_pattern",     "Patrón de renombrado",       "str"),
    ("portal_url",         "URL del portal",              "str"),
    ("schedule_hour",      "Hora de ejecución (0-23)",   "int"),
    ("schedule_minute",    "Minuto de ejecución (0-59)", "int"),
    ("max_retries",        "Reintentos máximos",         "int"),
    ("retry_interval_min", "Minutos entre reintentos",   "int"),
]
_AGENT_TOP_LEVEL_KEYS = (
    {k for k, _, _ in _AGENT_TOP_LEVEL_FIELDS}
    | {"provider", "has_password", "available", "username"}
)

# Agents that authenticate purely via per-account access tokens (MercadoPago's
# REST API, one token per alias/cuenta) instead of a single username/password —
# CREDENCIALES shows only the Cuentas editor for these, no Usuario/Contraseña.
_PROVIDERS_WITHOUT_LOGIN = {"mercadopago"}

# Per-provider extra secret fields that must always be offered in CREDENCIALES,
# even before they've ever been set. The generic '<field>_set' loop in
# AgentConfigTab._build() only picks up a field once its '<field>_enc' key
# already exists somewhere in extra_config — a brand-new/never-configured row
# has nothing to key off of, so without this registry the field simply never
# appears and there'd be no way to set it the first time (mirrors
# dispatcher/db.py's _AGENT_SECRET_EXTRA_FIELDS, plus the display label).
_AGENT_EXTRA_SECRET_FIELDS = {
    "fiserv":   [("totp_secret", "TOTP (Google Authenticator)")],
    "naranjax": [("imap_password", "Contraseña IMAP")],
}

# Same problem, for the non-secret "otras cosas" extra fields (timezone, poll
# settings, etc.): the loop over agent.items() in AgentConfigTab._build() only
# shows a key that's already present in extra_config — a row with
# extra_config = NULL/{} (never seeded, or reset) would render with none of
# these, and there's no "+ agregar campo" to add one back. Registered here so
# they always show, values blank until set.
_AGENT_EXTRA_PLAIN_FIELDS = {
    "mercadopago": [
        ("timezone", "Zona horaria"),
        ("separator", "Separador CSV"),
        ("poll_interval_seg", "Intervalo de polling (seg)"),
        ("poll_timeout_seg", "Timeout de polling (seg)"),
    ],
    "naranjax": [
        ("imap_host", "Servidor IMAP"),
        ("imap_username", "Usuario IMAP (email OTP)"),
        ("otp_sender", "Remitente del OTP"),
    ],
}

# Extra_config keys that exist in some rows (old seed data) but are dead —
# no longer read by anything — so they're hidden from "otras cosas" even
# though the generic agent.items() loop would otherwise pick them up. Left
# untouched in the DB (update_agent_config only touches keys it's sent), just
# not shown/editable here.
_AGENT_HIDDEN_EXTRA_FIELDS = {"auth_mode"}

# Agent tabs in the order they should appear — the two live/implemented
# agents first (in this order), then anything else (future or not-yet-
# available providers) alphabetically after them.
_AGENT_TAB_PRIORITY = ["fiserv", "mercadopago"]

# Small icon per agent tab, for quick scanning — falls back to a generic
# robot/construction icon for a provider with no entry here (any future
# agent, or one of the not-yet-implemented ones).
_AGENT_TAB_ICONS = {"fiserv": "🏦", "mercadopago": "💳"}


def _agent_tab_sort_key(agent: dict):
    provider = agent.get("provider", "")
    try:
        priority = _AGENT_TAB_PRIORITY.index(provider)
    except ValueError:
        priority = len(_AGENT_TAB_PRIORITY)
    return (priority, provider)


def _scrollable(tab_frame) -> ctk.CTkScrollableFrame:
    """
    Wraps a CTkTabview page in a scrollable container — a tab's form
    (CREDENCIALES + otras cosas, or the Operación/Seguridad boxes in
    General) can end up taller than the window, especially if the panel
    gets resized down; without this, whatever doesn't fit is just gone,
    with no way to reach it. The tab's own content class (GeneralSettingsTab,
    AgentConfigTab, etc.) doesn't need to know about this — it's built
    exactly as before, just with this as its parent instead of the raw
    tabview page.
    """
    container = ctk.CTkScrollableFrame(tab_frame, fg_color="transparent")
    container.pack(fill="both", expand=True)
    return container


# ── Small reusable dialogs ──────────────────────────────────────────────────

def _confirm(parent, title: str, message: str) -> bool:
    return messagebox.askyesno(title, message, parent=parent)


class _LoadingPlaceholder(ctk.CTkFrame):
    """Centered 'Cargando...' shown the instant a tab is created, before its
    data has come back from the API."""

    def __init__(self, parent, text: str = "Cargando..."):
        super().__init__(parent, fg_color="transparent")
        wrap = ctk.CTkFrame(self, fg_color="transparent")
        wrap.place(relx=0.5, rely=0.4, anchor="center")
        ctk.CTkLabel(wrap, text=text, font=(theme.FONT_FAMILY, 11), text_color=theme.TEXT_DIM).pack()

    def save(self) -> bool:
        return True


class _SettingsTabBase(ctk.CTkFrame):
    """
    Common scaffolding for a settings form: shows a 'Cargando...' placeholder
    on construction (no network call happens in __init__ — see ConfigTab,
    which fetches once and calls populate() on every tab when the data is
    in), then a grid of labeled fields plus a small status label (no
    button — saving is global, see ConfigTab) once populate() runs.

    Supports one nested "grouped" section (used for the CREDENCIALES box —
    usuario/password/tokens visually separated from the rest) via
    _start_group()/_end_group(): fields added in between go inside a
    theme.TitledFrame instead of directly on the tab.
    """

    def __init__(self, parent, api: ApiClient, title: str):
        super().__init__(parent, fg_color="transparent")
        self.api = api
        self._title = title
        self._vars: dict[str, tk.Variable] = {}
        self._row = 0
        self._save_status: ctk.CTkLabel | None = None
        self.form = self

        self._loading = ctk.CTkLabel(
            self, text=f"{title}\n\nCargando...", font=(theme.FONT_FAMILY, 11), text_color=theme.TEXT_DIM,
        )
        self._loading.place(relx=0.5, rely=0.4, anchor="center")

    def _start_form(self):
        """Call once, at the top of populate() — clears the loading
        placeholder and starts the real grid layout."""
        self._loading.destroy()
        ctk.CTkLabel(self, text=self._title, font=theme.FONT_TITLE).grid(
            row=0, column=0, columnspan=3, sticky="w", padx=16, pady=(16, 8),
        )
        self._row = 1

    def _start_group(self, title: str):
        """Opens a bordered card ('CREDENCIALES', etc) — subsequent
        _add_field/_add_secret_row calls land inside its .body until
        _end_group()."""
        row = self._row
        box = theme.TitledFrame(self, title)
        box.grid(row=row, column=0, columnspan=3, sticky="we", padx=16, pady=(4, 10))
        box.body.grid_columnconfigure(1, weight=1)
        self._row += 1

        self._outer_form = self.form
        self._outer_row  = self._row
        self.form = box.body
        self._row = 0

    def _end_group(self):
        self.form = self._outer_form
        self._row = self._outer_row

    def _add_field(self, key: str, label: str, kind: str, value):
        row = self._row
        ctk.CTkLabel(self.form, text=label, font=_FIELD_FONT).grid(row=row, column=0, sticky="w", padx=16, pady=4)

        if kind == "bool":
            var = tk.BooleanVar(value=bool(value))
            ctk.CTkCheckBox(self.form, text="", variable=var, width=24).grid(row=row, column=1, sticky="w", padx=4)
        elif kind == "folder":
            var = tk.StringVar(value=str(value) if value is not None else "")
            entry = ctk.CTkEntry(self.form, textvariable=var, width=300, font=_FIELD_FONT)
            entry.grid(row=row, column=1, sticky="w", padx=4)
            ctk.CTkButton(
                self.form, text="📁", width=48, height=32, font=theme.FONT_ICON,
                fg_color=theme.NEUTRAL, hover_color=theme.NEUTRAL_HOVER, text_color=theme.TEXT,
                command=lambda v=var: v.set(filedialog.askdirectory(initialdir=v.get() or None) or v.get()),
            ).grid(row=row, column=2, sticky="w")
        else:
            var = tk.StringVar(value="" if value is None else str(value))
            ctk.CTkEntry(self.form, textvariable=var, width=320, font=_FIELD_FONT).grid(row=row, column=1, sticky="w", padx=4)

        self._vars[key] = var
        self._row += 1
        return var

    def _add_secret_row(self, key: str, label: str, fetch_value, extra=None):
        """Inline masked Entry for a password/token/secret — no more 'Reemplazar'
        dialog. Starts empty and gets pre-filled with the current decrypted
        value once `fetch_value()` (a GET .../secret/{field} call) resolves,
        via run_async_retrying so a slow/transient fetch doesn't block the
        rest of the form from showing. The var lands in self._vars[key], same
        as any plain field, so _collect_plain(...) picks it up for free —
        whatever's in the box when 'Guardar todo' runs gets sent back and
        re-encrypted server-side."""
        row = self._row
        ctk.CTkLabel(self.form, text=label, font=_FIELD_FONT).grid(row=row, column=0, sticky="w", padx=16, pady=4)

        var = tk.StringVar(value="")
        entry = ctk.CTkEntry(self.form, textvariable=var, show="•", width=260, font=theme.FONT_MONO_BODY)
        entry.grid(row=row, column=1, sticky="w", padx=4)
        self._vars[key] = var

        show_var = tk.BooleanVar(value=False)
        ctk.CTkCheckBox(
            self.form, text="Mostrar", variable=show_var, font=theme.FONT_SMALL,
            command=lambda: entry.configure(show="" if show_var.get() else "•"),
        ).grid(row=row, column=2, sticky="w")

        if extra:
            extra_frame = ctk.CTkFrame(self.form, fg_color="transparent")
            extra_frame.grid(row=row, column=3, sticky="w", padx=(6, 0))
            extra(extra_frame)

        run_async_retrying(
            self, work=fetch_value, on_done=lambda v: var.set(v or ""),
            on_final_error=lambda e: None,  # queda vacío — no bloquea el resto del form
        )

        self._row += 1

    def _collect_plain(self, keys: list[str]) -> dict:
        """Reads current values back out of self._vars for the given keys,
        coercing ints and skipping blanks so we don't clobber unrelated fields."""
        body = {}
        for key in keys:
            var = self._vars.get(key)
            if var is None:
                continue
            if isinstance(var, tk.BooleanVar):
                body[key] = var.get()
                continue
            raw = var.get().strip()
            body[key] = raw if raw != "" else None
        return body

    def _status_row(self):
        """Small inline feedback label — no button. Saving itself happens via
        ConfigTab's single global 'Guardar todo'."""
        row = self._row + 1
        self._save_status = ctk.CTkLabel(self, text="", font=theme.FONT_SMALL)
        self._save_status.grid(row=row, column=0, columnspan=3, sticky="w", padx=16, pady=(4, 12))

    def _flash_saved(self, ok: bool, detail: str = ""):
        if self._save_status is None:
            return
        if ok:
            self._save_status.configure(text="Guardado ✔", text_color=theme.SUCCESS)
        else:
            self._save_status.configure(text=f"Error: {detail}", text_color=theme.DANGER)
        self.after(4000, lambda: self._save_status.configure(text=""))

    def _build_save_job(self) -> dict | None:
        """
        Overridden by subclasses. Reads this tab's Tk vars (main-thread only —
        Tk variables aren't safe to touch off-thread) and returns
        {'tab': self, 'endpoint': ..., 'body': ...} for ConfigTab to PUT from
        a background thread — no network I/O happens here. Returns None if
        there's nothing to save (tab hasn't finished loading yet) or if
        client-side validation failed (already reported via messagebox).
        """
        return None


# ── General ──────────────────────────────────────────────────────────────

class GeneralSettingsTab(_SettingsTabBase):

    _INT_FIELDS = {"check_jobs_interval_min", "check_update_interval_hours", "api_port"}

    def __init__(self, parent, api: ApiClient):
        super().__init__(parent, api, "Configuración general")
        self._loaded = False

    def populate(self, sys_cfg: dict):
        self._start_form()
        self._add_field("check_jobs_interval_min",     "Minutos entre chequeos de jobs",      "str", sys_cfg.get("check_jobs_interval_min", 5))
        self._add_field("check_update_interval_hours", "Horas entre chequeos de actualización", "str", sys_cfg.get("check_update_interval_hours", 6))
        self._add_field("api_port",                    "Puerto de la API interna",             "str", sys_cfg.get("api_port", 8765))
        self._add_field("debug",                        "Logs en modo debug",                  "bool", str(sys_cfg.get("debug", "")).lower() == "true")

        self._status_row()
        self._build_operation_section()
        self._build_security_section()
        self._loaded = True

    def _build_save_job(self) -> dict | None:
        if not self._loaded:
            return None
        body = self._collect_plain(list(self._INT_FIELDS))
        body["debug"] = "true" if self._vars["debug"].get() else "false"
        body = {k: v for k, v in body.items() if v is not None}
        return {"tab": self, "endpoint": "/config/system", "body": body}

    def _build_operation_section(self):
        row = self._row + 2
        op = theme.TitledFrame(self, "Operación")
        op.grid(row=row, column=0, columnspan=3, sticky="w", padx=16, pady=(16, 0))
        self._row = row + 1

        ctk.CTkLabel(
            op.body, text="Reinicia el proceso del dispatcher (servicio Windows). Se recupera solo\n"
                          "en ~15s — cualquier job a mitad de camino se retoma en el próximo ciclo.",
            font=theme.FONT_SMALL, text_color=theme.TEXT_DIM, justify="left",
        ).grid(row=0, column=0, sticky="w", padx=10, pady=(0, 8))

        ctk.CTkButton(
            op.body, text="🔄 Reiniciar servicio", width=200, font=_FIELD_FONT,
            fg_color=theme.NEUTRAL, hover_color=theme.NEUTRAL_HOVER, text_color=theme.TEXT,
            command=self._restart_service,
        ).grid(row=1, column=0, sticky="w", padx=10)

        self._op_spinner = theme.Spinner(op.body)
        self._op_spinner.grid(row=1, column=1, sticky="w", padx=(6, 0))

        self._op_status = ctk.CTkLabel(op.body, text="", font=theme.FONT_SMALL)
        self._op_status.grid(row=2, column=0, columnspan=2, sticky="w", padx=10, pady=(8, 0))

    def _restart_service(self):
        if not _confirm(
            self, "Reiniciar servicio",
            "Esto reinicia el proceso del dispatcher (AtanaDispatcher). Los agentes en curso se "
            "interrumpen y se retoman solos en el próximo ciclo — no se pierde nada, pero puede "
            "tardar unos segundos en volver a responder.\n\n¿Continuar?",
        ):
            return

        # Async, no llamada directa: un POST hecho en el hilo principal
        # congela toda la ventana mientras espera respuesta — el mismo
        # problema que ya se arregló para "Guardar todo" (ver _save_all).
        self._op_spinner.start()
        self._op_status.configure(text="Reiniciando...", text_color=theme.PRIMARY)

        def _on_done(_):
            self._op_spinner.stop()
            self._op_status.configure(text="Reiniciando... puede tardar ~15-20s en volver.", text_color=theme.PRIMARY)

        def _on_error(e):
            self._op_spinner.stop()
            self._op_status.configure(text=f"Error: {e}", text_color=theme.DANGER)

        run_async(self, work=lambda: self.api.post("/service/restart"), on_done=_on_done, on_error=_on_error)

    def _build_security_section(self):
        row = self._row + 1
        sec = theme.TitledFrame(self, "Seguridad")
        sec.grid(row=row, column=0, columnspan=3, sticky="w", padx=16, pady=16)

        ctk.CTkLabel(
            sec.body, text="Rotar la API key es instantáneo. Rotar las claves maestras\n"
                       "re-encripta (o invalida) todas las credenciales guardadas — puede tardar un momento.",
            font=theme.FONT_SMALL, text_color=theme.TEXT_DIM, justify="left",
        ).grid(row=0, column=0, columnspan=2, sticky="w", padx=10, pady=(0, 8))

        ctk.CTkButton(
            sec.body, text="🔄 Regenerar API key", width=190, font=_FIELD_FONT,
            fg_color=theme.NEUTRAL, hover_color=theme.NEUTRAL_HOVER, text_color=theme.TEXT,
            command=self._rotate_api_key,
        ).grid(row=1, column=0, sticky="w", padx=(10, 8))

        ctk.CTkButton(
            sec.body, text="🔄 Rotar claves maestras (fernet + session)", width=320, font=_FIELD_FONT,
            fg_color=theme.DANGER, hover_color=theme.DANGER_HOVER,
            command=self._rotate_master,
        ).grid(row=1, column=1, sticky="w")

        self._sec_spinner = theme.Spinner(sec.body)
        self._sec_spinner.grid(row=1, column=2, sticky="w", padx=(6, 0))

        self._sec_status = ctk.CTkLabel(sec.body, text="", font=theme.FONT_SMALL)
        self._sec_status.grid(row=2, column=0, columnspan=3, sticky="w", padx=10, pady=(8, 0))

    def _rotate_api_key(self):
        if not _confirm(self, "Regenerar API key",
                         "Esto invalida la API key actual. El tray y el panel se actualizan solos "
                         "en su próxima recarga (≤30s). ¿Continuar?"):
            return

        self._sec_spinner.start()
        self._sec_status.configure(text="Regenerando...", text_color=theme.PRIMARY)

        def _on_done(resp):
            self._sec_spinner.stop()
            self.api.api_key = resp.get("api_key", self.api.api_key)
            self._sec_status.configure(text="API key regenerada ✔", text_color=theme.SUCCESS)

        def _on_error(e):
            self._sec_spinner.stop()
            self._sec_status.configure(text=f"Error: {e}", text_color=theme.DANGER)

        run_async(self, work=lambda: self.api.post("/config/keys/rotate-api-key"), on_done=_on_done, on_error=_on_error)

    def _rotate_master(self):
        if not _confirm(
            self, "Rotar claves maestras",
            "Esto va a RE-ENCRIPTAR todas las credenciales guardadas (passwords, TOTP, tokens) "
            "y va a invalidar las sesiones de browser activas (se recrean solas en el próximo login).\n\n"
            "No cierres el panel hasta que termine. ¿Continuar?",
        ):
            return

        self._sec_spinner.start()
        self._sec_status.configure(text="Iniciando rotación...", text_color=theme.PRIMARY)

        def _on_done(_):
            self._sec_status.configure(text="Rotando...", text_color=theme.PRIMARY)
            self.after(1500, self._poll_rotation)

        def _on_error(e):
            self._sec_spinner.stop()
            messagebox.showerror("ATANA", f"No se pudo iniciar la rotación: {e}", parent=self)

        run_async(
            self, work=lambda: self.api.post("/config/keys/rotate-master", {"targets": ["fernet_key", "session_key"]}),
            on_done=_on_done, on_error=_on_error,
        )

    def _poll_rotation(self):
        # Se queda en el hilo principal a propósito: es solo una lectura de
        # un dict en memoria del lado del servidor (sin ida y vuelta a SQL
        # Server), responde casi al instante — no vale la pena el async acá.
        try:
            status = self.api.get("/config/keys/rotate-status")
        except ApiError as e:
            self._sec_spinner.stop()
            self._sec_status.configure(text=f"Error consultando estado: {e}", text_color=theme.DANGER)
            return

        state = status.get("state")
        if state == "running":
            self.after(1500, self._poll_rotation)
        elif state == "done":
            self._sec_spinner.stop()
            self._sec_status.configure(text="Rotación completada ✔", text_color=theme.SUCCESS)
        elif state == "error":
            self._sec_spinner.stop()
            self._sec_status.configure(text=f"Falló — claves anteriores siguen vigentes: {status.get('detail')}", text_color=theme.DANGER)
        else:
            self._sec_spinner.stop()
            self._sec_status.configure(text="")


# ── SMTP ─────────────────────────────────────────────────────────────────

class SmtpSettingsTab(_SettingsTabBase):

    def __init__(self, parent, api: ApiClient):
        super().__init__(parent, api, "Notificaciones por SMTP")
        self._loaded = False

    def populate(self, sys_cfg: dict):
        self._start_form()

        self._start_group("CREDENCIALES")
        self._add_field("smtp_username", "Usuario / remitente", "str", sys_cfg.get("smtp_username", ""))
        self._add_secret_row(
            "smtp_password", "Contraseña",
            fetch_value=lambda: self.api.get("/config/system/secret/smtp_password").get("value", ""),
        )
        self._end_group()

        self._add_field("smtp_enabled",   "Notificaciones activas",  "bool", str(sys_cfg.get("smtp_enabled", "")).lower() == "true")
        self._add_field("smtp_host",      "Servidor SMTP",           "str",  sys_cfg.get("smtp_host", ""))
        self._add_field("smtp_port",      "Puerto",                  "str",  sys_cfg.get("smtp_port", 587))
        self._add_field("smtp_recipient", "Destinatario de alertas", "str",  sys_cfg.get("smtp_recipient", ""))

        self._status_row()
        self._loaded = True

    def _build_save_job(self) -> dict | None:
        if not self._loaded:
            return None
        body = self._collect_plain(["smtp_host", "smtp_port", "smtp_username", "smtp_recipient", "smtp_password"])
        body["smtp_enabled"] = "true" if self._vars["smtp_enabled"].get() else "false"
        body = {k: v for k, v in body.items() if v is not None}
        return {"tab": self, "endpoint": "/config/system", "body": body}


# ── Auto-update ──────────────────────────────────────────────────────────

class AutoUpdateSettingsTab(_SettingsTabBase):

    def __init__(self, parent, api: ApiClient):
        super().__init__(parent, api, "Auto-update (GitHub Releases)")
        self._loaded = False

    def populate(self, sys_cfg: dict):
        self._start_form()

        self._start_group("CREDENCIALES")
        self._add_secret_row(
            "github_token", "Personal Access Token",
            fetch_value=lambda: self.api.get("/config/system/secret/github_token").get("value", ""),
        )
        self._end_group()

        self._add_field("github_owner", "Usuario / organización", "str", sys_cfg.get("github_owner", ""))
        self._add_field("github_repo",  "Repositorio",            "str", sys_cfg.get("github_repo", ""))

        ctk.CTkLabel(
            self, text="Dejar el token en blanco desactiva el auto-update.",
            font=theme.FONT_SMALL, text_color=theme.TEXT_DIM,
        ).grid(row=self._row, column=0, columnspan=3, sticky="w", padx=16, pady=(0, 8))
        self._row += 1

        self._status_row()
        self._loaded = True

    def _build_save_job(self) -> dict | None:
        if not self._loaded:
            return None
        body = self._collect_plain(["github_owner", "github_repo", "github_token"])
        body = {k: v for k, v in body.items() if v is not None}
        return {"tab": self, "endpoint": "/config/system", "body": body}


# ── Per-agent ────────────────────────────────────────────────────────────

class AgentConfigTab(_SettingsTabBase):
    """
    Renders the common agent_config columns plus whatever provider-specific
    extra_config fields the API returned — generically: any '<x>_set' key
    becomes a secret-replace row inside CREDENCIALES, 'accounts' gets its own
    mini list editor (also inside CREDENCIALES), everything else is a plain
    field below. This means a brand-new agent with a new extra_config schema
    gets a reasonable form for free, no code changes here.

    Built with `agent` already in hand (ConfigTab fetches every agent's
    config in one GET /config/agents) — no loading placeholder needed, no
    per-tab network call.
    """

    def __init__(self, parent, api: ApiClient, provider: str, agent: dict):
        super().__init__(parent, api, provider.upper())
        self.provider = provider
        self._extra_plain_keys: list[str] = []
        self._extra_secret_keys: list[str] = []
        self._accounts: list[dict] = []
        self._start_form()
        self._build(agent)

    def _build(self, agent: dict):
        has_login = self.provider not in _PROVIDERS_WITHOUT_LOGIN

        # ── CREDENCIALES ──────────────────────────────────────────────────
        self._start_group("CREDENCIALES")

        if has_login:
            self._add_field("username", "Usuario", "str", agent.get("username"))
            self._add_secret_row(
                "password", "Contraseña / Token",
                fetch_value=lambda: self.api.get(f"/config/agents/{self.provider}/secret/password").get("value", ""),
            )

        accounts_built = False
        for key, value in agent.items():
            if key in _AGENT_TOP_LEVEL_KEYS:
                continue
            if key == "accounts" and isinstance(value, list):
                self._accounts = value
                self._build_accounts_section()
                accounts_built = True
            elif key.endswith("_set"):
                logical = key[:-4]
                is_totp = logical == "totp_secret"
                self._extra_secret_keys.append(logical)
                self._add_secret_row(
                    logical,
                    "TOTP (Google Authenticator)" if is_totp else logical.replace("_", " ").capitalize(),
                    fetch_value=lambda k=logical: self.api.get(f"/config/agents/{self.provider}/secret/{k}").get("value", ""),
                    extra=self._qr_button(logical) if is_totp else None,
                )

        if not has_login and not accounts_built:
            # Un agente sin login único (MercadoPago) siempre necesita poder
            # agregar cuentas — incluso antes de tener la primera, cuando
            # extra_config todavía no trae la clave 'accounts' (por ejemplo
            # si el seed nunca se completó o quedó vacío).
            self._build_accounts_section()

        # Campos extra que este provider siempre debe poder configurar (ver
        # _AGENT_EXTRA_SECRET_FIELDS) — el loop de arriba solo agrega la fila
        # si '<field>_enc' ya existe en extra_config; esto cubre el caso de
        # un agente recién dado de alta que todavía no tiene nada guardado.
        already_rendered = set(self._extra_secret_keys)
        for logical, label in _AGENT_EXTRA_SECRET_FIELDS.get(self.provider, []):
            if logical in already_rendered:
                continue
            is_totp = logical == "totp_secret"
            self._extra_secret_keys.append(logical)
            self._add_secret_row(
                logical, label,
                fetch_value=lambda k=logical: self.api.get(f"/config/agents/{self.provider}/secret/{k}").get("value", ""),
                extra=self._qr_button(logical) if is_totp else None,
            )

        self._end_group()

        # ── otras cosas ───────────────────────────────────────────────────
        for key, label, kind in _AGENT_TOP_LEVEL_FIELDS:
            self._add_field(key, label, kind, agent.get(key))

        for key, value in agent.items():
            if key in _AGENT_TOP_LEVEL_KEYS or key == "accounts" or key.endswith("_set"):
                continue
            if key in _AGENT_HIDDEN_EXTRA_FIELDS:
                continue
            self._extra_plain_keys.append(key)
            self._add_field(key, key.replace("_", " ").capitalize(), "str", value)

        # Igual que con los secretos extra arriba: agrega los campos que este
        # provider siempre debería tener aunque extra_config no los traiga
        # todavía (fila nunca configurada / extra_config vacío o NULL).
        already_plain = set(self._extra_plain_keys)
        for key, label in _AGENT_EXTRA_PLAIN_FIELDS.get(self.provider, []):
            if key in already_plain:
                continue
            self._extra_plain_keys.append(key)
            self._add_field(key, label, "str", agent.get(key))

        self._status_row()

    def _build_save_job(self) -> dict | None:
        body = self._collect_plain(
            ["username", "password"]
            + [k for k, _, _ in _AGENT_TOP_LEVEL_FIELDS if k != "enabled"]
            + self._extra_plain_keys
            + self._extra_secret_keys
        )
        body["enabled"] = self._vars["enabled"].get()
        for k in ("schedule_hour", "schedule_minute", "max_retries", "retry_interval_min"):
            if body.get(k) not in (None, ""):
                try:
                    body[k] = int(body[k])
                except ValueError:
                    messagebox.showerror("ATANA", f"[{self.provider.upper()}] '{k}' debe ser un número.", parent=self)
                    return None
        body = {k: v for k, v in body.items() if v is not None}
        return {"tab": self, "endpoint": f"/config/agents/{self.provider}", "body": body}

    def _put_secret(self, body: dict):
        try:
            self.api.put(f"/config/agents/{self.provider}", body)
            self._flash_saved(True)
        except ApiError as e:
            messagebox.showerror("ATANA", f"No se pudo guardar: {e}", parent=self)

    # ── TOTP desde foto del QR ────────────────────────────────────────────
    # El único paso "raro" del alta de un agente con 2FA: en vez de pedirle al
    # cliente el secreto en texto, le sacamos una foto al QR de Google
    # Authenticator y lo leemos acá mismo. La imagen nunca sale de la máquina
    # ni pasa por la API — se decodifica en el proceso del panel
    # (shared/totp_extractor.py) y solo el secreto ya extraído se manda a
    # guardar (cifrado server-side, como cualquier otro secreto).

    def _qr_button(self, logical_key: str):
        def _factory(parent_frame):
            ctk.CTkButton(
                parent_frame, text="📷 Generar desde foto del QR", width=230, font=_FIELD_FONT,
                fg_color=theme.NEUTRAL, hover_color=theme.NEUTRAL_HOVER, text_color=theme.TEXT,
                command=lambda: self._upload_qr(logical_key),
            ).pack(side="left", padx=(6, 0))
        return _factory

    def _upload_qr(self, logical_key: str):
        path = filedialog.askopenfilename(
            title="Seleccioná la foto del QR (Google Authenticator)",
            filetypes=[("Imágenes", "*.png *.jpg *.jpeg *.bmp"), ("Todos los archivos", "*.*")],
        )
        if not path:
            return
        try:
            from shared.totp_extractor import extract_secrets_from_image
            accounts = extract_secrets_from_image(path)
        except Exception as e:
            messagebox.showerror("ATANA", f"No se pudo leer el QR: {e}", parent=self)
            return

        secret = accounts[0]["secret"]
        if len(accounts) > 1:
            names = "\n".join(f"- {a.get('name') or a.get('issuer') or '(sin nombre)'}" for a in accounts)
            if not _confirm(
                self, "Varias cuentas encontradas",
                f"El QR trae {len(accounts)} cuentas:\n{names}\n\nSe va a usar la primera. ¿Continuar?",
            ):
                return

        if not _confirm(self, "Confirmar TOTP", "Se detectó un secreto TOTP en el QR. ¿Guardarlo?"):
            return
        self._put_secret({logical_key: secret})
        if logical_key in self._vars:
            self._vars[logical_key].set(secret)  # refleja el nuevo valor en el campo inline

    # ── accounts (MercadoPago y cualquier agente con múltiples cuentas) ──
    # Sin límite de cantidad — "+ Agregar cuenta" siempre agrega una más.
    # Vive dentro de CREDENCIALES (se llama desde _build, entre _start_group
    # y _end_group).

    def _build_accounts_section(self):
        row = self._row
        box = theme.TitledFrame(self.form, "Cuentas")
        box.grid(row=row, column=0, columnspan=3, sticky="we", padx=4, pady=8)
        self._row += 1

        self._accounts_list = ctk.CTkFrame(box.body, fg_color="transparent")
        self._accounts_list.pack(fill="x")

        ctk.CTkButton(
            box.body, text="+ Agregar cuenta", width=170, font=_FIELD_FONT,
            fg_color=theme.NEUTRAL, hover_color=theme.NEUTRAL_HOVER, text_color=theme.TEXT,
            command=self._add_account,
        ).pack(anchor="w", pady=(8, 0))

        self._render_accounts()

    def _render_accounts(self):
        for w in self._accounts_list.winfo_children():
            w.destroy()
        self._account_vars: dict[str, tk.StringVar] = {}

        for acc in self._accounts:
            alias = acc.get("alias", "(sin alias)")
            row_f = ctk.CTkFrame(self._accounts_list, fg_color="transparent")
            row_f.pack(fill="x", pady=2)

            ctk.CTkLabel(row_f, text=alias, font=_FIELD_FONT, width=140, anchor="w").pack(side="left")

            var = tk.StringVar(value="")
            self._account_vars[alias] = var
            entry = ctk.CTkEntry(row_f, textvariable=var, show="•", width=220, font=theme.FONT_MONO_BODY)
            entry.pack(side="left", padx=4)

            show_var = tk.BooleanVar(value=False)
            ctk.CTkCheckBox(
                row_f, text="Mostrar", variable=show_var, font=theme.FONT_SMALL,
                command=lambda e=entry, s=show_var: e.configure(show="" if s.get() else "•"),
            ).pack(side="left")

            ctk.CTkButton(
                row_f, text="💾", width=48, height=32, font=theme.FONT_ICON,
                fg_color=theme.NEUTRAL, hover_color=theme.NEUTRAL_HOVER, text_color=theme.TEXT,
                command=lambda a=alias: self._save_account_token(a),
            ).pack(side="left", padx=4)
            ctk.CTkButton(
                row_f, text="✕", width=48, height=32, font=theme.FONT_ICON,
                fg_color=theme.DANGER, hover_color=theme.DANGER_HOVER,
                command=lambda a=alias: self._remove_account(a),
            ).pack(side="left")

            run_async_retrying(
                self, work=lambda a=alias: self.api.get(f"/config/agents/{self.provider}/secret/accounts/{a}").get("value", ""),
                on_done=lambda v, var=var: var.set(v or ""),
                on_final_error=lambda e: None,
            )

    def _add_account(self):
        win = ctk.CTkToplevel(self)
        win.title("Agregar cuenta")
        win.transient(self.winfo_toplevel())
        # Pequeña demora antes de grab_set(): CTkToplevel puede no estar
        # completamente mapeada todavía en el instante en que se crea, y
        # pedir el grab antes de eso puede fallar en algunas plataformas.
        win.after(100, win.grab_set)

        ctk.CTkLabel(win, text="Alias:", font=_FIELD_FONT).grid(row=0, column=0, sticky="w", padx=12, pady=(12, 4))
        alias_var = tk.StringVar()
        ctk.CTkEntry(win, textvariable=alias_var, width=220).grid(row=0, column=1, padx=12, pady=(12, 4))

        ctk.CTkLabel(win, text="Access token:", font=_FIELD_FONT).grid(row=1, column=0, sticky="w", padx=12, pady=4)
        token_var = tk.StringVar()
        ctk.CTkEntry(win, textvariable=token_var, width=220, show="•").grid(row=1, column=1, padx=12, pady=4)

        def _confirm_add():
            alias = alias_var.get().strip()
            token = token_var.get().strip()
            if not alias:
                messagebox.showerror("ATANA", "El alias no puede estar vacío.", parent=win)
                return
            if any(a.get("alias") == alias for a in self._accounts):
                messagebox.showerror("ATANA", "Ya existe una cuenta con ese alias.", parent=win)
                return
            next_accounts = self._accounts + [{"alias": alias, "access_token_set": bool(token)}]
            self._save_accounts(next_accounts, extra_token={alias: token} if token else {})
            win.destroy()

        btns = ctk.CTkFrame(win, fg_color="transparent")
        btns.grid(row=2, column=0, columnspan=2, pady=12, padx=12)
        ctk.CTkButton(
            btns, text="Agregar", command=_confirm_add, width=100,
            fg_color=theme.PRIMARY, hover_color=theme.PRIMARY_HOVER,
        ).pack(side="left", padx=4)
        ctk.CTkButton(
            btns, text="Cancelar", command=win.destroy, width=100,
            fg_color=theme.NEUTRAL, hover_color=theme.NEUTRAL_HOVER, text_color=theme.TEXT,
        ).pack(side="left")

        win.bind("<Return>", lambda e: _confirm_add())
        win.bind("<Escape>", lambda e: win.destroy())

    def _save_account_token(self, alias: str):
        var = self._account_vars.get(alias)
        if var is None:
            return
        self._save_accounts(self._accounts, extra_token={alias: var.get()})

    def _remove_account(self, alias: str):
        if not _confirm(self, "Quitar cuenta", f"¿Quitar la cuenta '{alias}'?"):
            return
        next_accounts = [a for a in self._accounts if a.get("alias") != alias]
        self._save_accounts(next_accounts)

    def _save_accounts(self, accounts: list[dict], extra_token: dict | None = None):
        """Sends `accounts` back. Only aliases present in `extra_token` carry
        a plaintext access_token — everything else is merged server-side
        against the previously stored token (see dispatcher/db.py
        update_agent_config).

        Takes the WOULD-BE list as a parameter instead of reading/mutating
        self._accounts directly, and only commits it to self._accounts once
        the PUT actually succeeds — add/remove used to mutate self._accounts
        up front, before the request, so a failed save (network hiccup, API
        down) left local state silently out of sync with the server: a
        'quitar cuenta' that failed would still make the account disappear
        from the DB on the NEXT unrelated successful save, well after the
        error dialog told you nothing had happened."""
        extra_token = extra_token or {}
        payload = []
        for acc in accounts:
            item = {k: v for k, v in acc.items() if k != "access_token_set"}
            if acc.get("alias") in extra_token:
                item["access_token"] = extra_token[acc["alias"]]
            payload.append(item)

        try:
            self.api.put(f"/config/agents/{self.provider}", {"accounts": payload})
            self._accounts = accounts
            self._render_accounts()
            self._flash_saved(True)
        except ApiError as e:
            messagebox.showerror("ATANA", f"No se pudo guardar la cuenta: {e}", parent=self)


# ── Agentes aún no disponibles ───────────────────────────────────────────

class _UnavailableAgentTab(ctk.CTkFrame):
    """
    Placeholder para providers que ya tienen fila en agent_config (seed_config.sql)
    pero todavía no tienen bot implementado (agent_loader.known_providers() no
    los incluye) — naranjax, getnet, cabal, amex, prisma al momento de escribir esto.
    """

    def __init__(self, parent, provider: str):
        super().__init__(parent, fg_color="transparent")
        wrap = ctk.CTkFrame(self, fg_color="transparent")
        wrap.place(relx=0.5, rely=0.4, anchor="center")

        ctk.CTkLabel(wrap, text=provider.upper(), font=(theme.FONT_FAMILY, 14, "bold"), text_color=theme.TEXT_DIM).pack()
        ctk.CTkLabel(
            wrap, text="Este agente todavía no está disponible.",
            font=_FIELD_FONT, text_color=theme.TEXT_DIM,
        ).pack(pady=(6, 0))

    def save(self) -> bool:
        return True  # nada que guardar


# ── Orchestrator tab ─────────────────────────────────────────────────────

class ConfigTab(ctk.CTkFrame):
    """
    Builds its whole shell (footer + CTkTabview + one placeholder tab each)
    synchronously — this is all just widget construction, no network calls,
    so the window appears immediately. The two GETs that feed every tab
    (/config/system, /config/agents) run in the background via
    run_async_retrying and populate tabs in place once they land.
    """

    def __init__(self, parent, api: ApiClient):
        super().__init__(parent, fg_color="transparent")
        self.api = api
        self._savable_tabs: list = []

        # footer packed BEFORE the tabview so it claims its strip at the
        # bottom first — otherwise the tabview's fill="both" expand=True
        # would grab the whole frame and leave no room for it.
        footer = ctk.CTkFrame(self, fg_color="transparent")
        footer.pack(fill="x", side="bottom", padx=16, pady=8)

        self._global_status = ctk.CTkLabel(footer, text="", font=theme.FONT_SMALL)
        self._global_status.pack(side="right", padx=12)

        self._save_spinner = theme.Spinner(footer)
        self._save_spinner.pack(side="right")

        ctk.CTkButton(
            footer, text="💾 Guardar todo", width=170, font=_FIELD_FONT,
            fg_color=theme.PRIMARY, hover_color=theme.PRIMARY_HOVER,
            command=self._save_all,
        ).pack(side="right")

        self.tabview = ctk.CTkTabview(self, fg_color="transparent")
        self.tabview.pack(fill="both", expand=True)

        general_frame = self.tabview.add("🔧  General")
        smtp_frame    = self.tabview.add("✉️  SMTP")
        update_frame  = self.tabview.add("🔄  Auto-update")
        # Placeholder tab — deleted and replaced by real per-agent tabs once
        # GET /config/agents lands (see _on_agents_loaded).
        self._agents_tab_key = "🤖  Agentes"
        agents_frame = self.tabview.add(self._agents_tab_key)

        self.general_tab = GeneralSettingsTab(_scrollable(general_frame), api)
        self.general_tab.pack(fill="both", expand=True)
        self.smtp_tab = SmtpSettingsTab(_scrollable(smtp_frame), api)
        self.smtp_tab.pack(fill="both", expand=True)
        self.update_tab = AutoUpdateSettingsTab(_scrollable(update_frame), api)
        self.update_tab.pack(fill="both", expand=True)
        self._savable_tabs += [self.general_tab, self.smtp_tab, self.update_tab]

        self._agents_placeholder = _LoadingPlaceholder(agents_frame, "Cargando agentes...")
        self._agents_placeholder.pack(fill="both", expand=True)

        # retrying, not one-shot: right after the panel opens, the dispatcher's
        # local API may not have bound its port yet (window shows up first) —
        # a few quick retries absorb that instead of surfacing a one-off
        # "<urlopen error ...>" that then works fine a second later anyway.
        run_async_retrying(
            self, work=lambda: self.api.get("/config/system").get("system", {}),
            on_done=self._on_system_loaded,
            on_final_error=lambda e: messagebox.showwarning(
                "ATANA", f"No se pudo cargar la configuración: {e}", parent=self,
            ),
        )
        run_async_retrying(
            self, work=lambda: self.api.get("/config/agents").get("agents", []),
            on_done=self._on_agents_loaded,
            on_final_error=lambda e: messagebox.showwarning(
                "ATANA", f"No se pudo cargar la configuración de agentes: {e}", parent=self,
            ),
        )

    def _on_system_loaded(self, sys_cfg: dict):
        self.general_tab.populate(sys_cfg)
        self.smtp_tab.populate(sys_cfg)
        self.update_tab.populate(sys_cfg)

    def _on_agents_loaded(self, agents: list):
        self.tabview.delete(self._agents_tab_key)

        for agent in sorted(agents, key=_agent_tab_sort_key):
            provider  = agent["provider"]
            available = agent.get("available", True)
            icon = _AGENT_TAB_ICONS.get(provider, "🤖" if available else "🚧")
            tab_frame = self.tabview.add(f"{icon}  {provider.capitalize()}")
            if available:
                tab = AgentConfigTab(_scrollable(tab_frame), self.api, provider, agent)
                self._savable_tabs.append(tab)
            else:
                # Sin scroll acá — es solo un mensaje centrado con .place(),
                # nunca necesita más espacio del que ya tiene.
                tab = _UnavailableAgentTab(tab_frame, provider)
            tab.pack(fill="both", expand=True)

    def _save_all(self):
        """
        Two phases, deliberately split so the window keeps repainting while
        this runs:
          1. HERE, on the main thread: each tab's _build_save_job() reads its
             Tk vars and does client-side validation — fast, no network, and
             the only place Tk vars are touched (they aren't thread-safe).
          2. In a background thread: every resulting PUT is sent. Doing this
             inline on the main thread (the original approach) blocked the Tk
             event loop for the whole batch and was the actual cause of the
             window freezing/garbling mid-save.
        """
        jobs = []
        for tab in self._savable_tabs:
            job = tab._build_save_job()
            if job is not None:
                jobs.append(job)

        if not jobs:
            self._global_status.configure(text="Nada para guardar", text_color=theme.TEXT_DIM)
            self.after(3000, lambda: self._global_status.configure(text=""))
            return

        self._global_status.configure(text="Guardando...", text_color=theme.PRIMARY)
        self._save_spinner.start()

        def _send_all():
            results = []
            for job in jobs:
                try:
                    self.api.put(job["endpoint"], job["body"])
                    results.append((job["tab"], True, ""))
                except Exception as e:
                    results.append((job["tab"], False, str(e)))
            return results

        def _on_error(e):
            self._save_spinner.stop()
            self._global_status.configure(text=f"Error: {e}", text_color=theme.DANGER)

        run_async(
            self, work=_send_all,
            on_done=self._on_save_all_done,
            on_error=_on_error,
        )

    def _on_save_all_done(self, results: list):
        self._save_spinner.stop()
        ok_count = 0
        failed: list[str] = []
        for tab, ok, detail in results:
            tab._flash_saved(ok, detail)
            title = getattr(tab, "provider", type(tab).__name__.replace("SettingsTab", ""))
            if ok:
                ok_count += 1
            else:
                failed.append(f"{title} ({detail})" if detail else str(title))

        if failed:
            self._global_status.configure(
                text=f"{ok_count} guardado(s), falló: {', '.join(failed)}", text_color=theme.DANGER,
            )
        else:
            self._global_status.configure(text=f"Todo guardado ✔ ({ok_count})", text_color=theme.SUCCESS)
        self.after(6000, lambda: self._global_status.configure(text=""))
