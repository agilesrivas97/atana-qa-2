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
window and Notebook appear instantly — and gets populated once its
GET finishes, via ui/async_utils.run_async_retrying (background thread +
Tk-safe handoff, retries the odd transient failure right after the panel
opens). Two shared fetches feed the whole ConfigTab: one GET /config/system
for General+SMTP+Auto-update (used to be 3 separate round-trips — one per
tab — which is why this used to feel slow to open), one GET /config/agents
for every agent tab.

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
"""

import tkinter as tk
from tkinter import ttk, filedialog, messagebox

from shared.api_client import ApiClient, ApiError
from ui.async_utils import run_async, run_async_retrying

_FIELD_FONT = ("Segoe UI", 10)
_LABEL_FONT = ("Segoe UI", 10, "bold")

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


# ── Small reusable dialogs ──────────────────────────────────────────────────

def _confirm(parent, title: str, message: str) -> bool:
    return messagebox.askyesno(title, message, parent=parent)


class _LoadingPlaceholder(ttk.Frame):
    """Centered 'Cargando...' shown the instant a tab is created, before its
    data has come back from the API."""

    def __init__(self, parent, text: str = "Cargando..."):
        super().__init__(parent)
        wrap = tk.Frame(self)
        wrap.place(relx=0.5, rely=0.4, anchor="center")
        tk.Label(wrap, text=text, font=("Segoe UI", 11), fg="#999999").pack()

    def save(self) -> bool:
        return True


class _SettingsTabBase(ttk.Frame):
    """
    Common scaffolding for a settings form: shows a 'Cargando...' placeholder
    on construction (no network call happens in __init__ — see ConfigTab,
    which fetches once and calls populate() on every tab when the data is
    in), then a grid of labeled fields plus a small status label (no
    button — saving is global, see ConfigTab) once populate() runs.

    Supports one nested "grouped" section (used for the CREDENCIALES box —
    usuario/password/tokens visually separated from the rest) via
    _start_group()/_end_group(): fields added in between go inside a
    LabelFrame instead of directly on the tab.
    """

    def __init__(self, parent, api: ApiClient, title: str):
        super().__init__(parent)
        self.api = api
        self._title = title
        self._vars: dict[str, tk.Variable] = {}
        self._row = 0
        self._save_status: tk.Label | None = None
        self.form = self

        self._loading = tk.Label(self, text=f"{title}\n\nCargando...", font=("Segoe UI", 11), fg="#999999")
        self._loading.place(relx=0.5, rely=0.4, anchor="center")

    def _start_form(self):
        """Call once, at the top of populate() — clears the loading
        placeholder and starts the real grid layout."""
        self._loading.destroy()
        tk.Label(self, text=self._title, font=("Segoe UI", 13, "bold")).grid(
            row=0, column=0, columnspan=3, sticky="w", padx=16, pady=(16, 8),
        )
        self._row = 1

    def _start_group(self, title: str):
        """Opens a bordered LabelFrame ('CREDENCIALES', etc) — subsequent
        _add_field/_add_secret_row calls land inside it until _end_group()."""
        row = self._row
        box = tk.LabelFrame(self, text=title, font=_LABEL_FONT, padx=12, pady=8)
        box.grid(row=row, column=0, columnspan=3, sticky="we", padx=16, pady=(4, 10))
        box.grid_columnconfigure(1, weight=1)
        self._row += 1

        self._outer_form = self.form
        self._outer_row  = self._row
        self.form = box
        self._row = 0

    def _end_group(self):
        self.form = self._outer_form
        self._row = self._outer_row

    def _add_field(self, key: str, label: str, kind: str, value):
        row = self._row
        tk.Label(self.form, text=label, font=_FIELD_FONT).grid(row=row, column=0, sticky="w", padx=16, pady=4)

        if kind == "bool":
            var = tk.BooleanVar(value=bool(value))
            tk.Checkbutton(self.form, variable=var).grid(row=row, column=1, sticky="w", padx=4)
        elif kind == "folder":
            var = tk.StringVar(value=str(value) if value is not None else "")
            entry = tk.Entry(self.form, textvariable=var, width=44, font=_FIELD_FONT)
            entry.grid(row=row, column=1, sticky="w", padx=4)
            tk.Button(
                self.form, text="📁", relief="flat", cursor="hand2",
                command=lambda v=var: v.set(filedialog.askdirectory(initialdir=v.get() or None) or v.get()),
            ).grid(row=row, column=2, sticky="w")
        else:
            var = tk.StringVar(value="" if value is None else str(value))
            tk.Entry(self.form, textvariable=var, width=46, font=_FIELD_FONT).grid(row=row, column=1, sticky="w", padx=4)

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
        tk.Label(self.form, text=label, font=_FIELD_FONT).grid(row=row, column=0, sticky="w", padx=16, pady=4)

        var = tk.StringVar(value="")
        entry = tk.Entry(self.form, textvariable=var, show="•", width=38, font=("Consolas", 10))
        entry.grid(row=row, column=1, sticky="w", padx=4)
        self._vars[key] = var

        show_var = tk.BooleanVar(value=False)
        tk.Checkbutton(
            self.form, text="Mostrar", variable=show_var,
            command=lambda: entry.config(show="" if show_var.get() else "•"),
        ).grid(row=row, column=2, sticky="w")

        if extra:
            extra_frame = tk.Frame(self.form)
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
        self._save_status = tk.Label(self, text="", font=("Segoe UI", 9))
        self._save_status.grid(row=row, column=0, columnspan=3, sticky="w", padx=16, pady=(4, 12))

    def _flash_saved(self, ok: bool, detail: str = ""):
        if self._save_status is None:
            return
        if ok:
            self._save_status.config(text="Guardado ✔", fg="#2f9e44")
        else:
            self._save_status.config(text=f"Error: {detail}", fg="#e03131")
        self.after(4000, lambda: self._save_status.config(text=""))

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
        op = tk.LabelFrame(self, text="Operación", font=_LABEL_FONT, padx=12, pady=12)
        op.grid(row=row, column=0, columnspan=3, sticky="w", padx=16, pady=(16, 0))
        self._row = row + 1

        tk.Label(
            op, text="Reinicia el proceso del dispatcher (servicio Windows). Se recupera solo\n"
                      "en ~15s — cualquier job a mitad de camino se retoma en el próximo ciclo.",
            font=("Segoe UI", 9), fg="#666666", justify="left",
        ).grid(row=0, column=0, sticky="w", pady=(0, 8))

        tk.Button(
            op, text="🔄 Reiniciar servicio", relief="flat", cursor="hand2",
            padx=10, pady=4, command=self._restart_service,
        ).grid(row=1, column=0, sticky="w")

        self._op_status = tk.Label(op, text="", font=("Segoe UI", 9))
        self._op_status.grid(row=2, column=0, sticky="w", pady=(8, 0))

    def _restart_service(self):
        if not _confirm(
            self, "Reiniciar servicio",
            "Esto reinicia el proceso del dispatcher (AtanaDispatcher). Los agentes en curso se "
            "interrumpen y se retoman solos en el próximo ciclo — no se pierde nada, pero puede "
            "tardar unos segundos en volver a responder.\n\n¿Continuar?",
        ):
            return
        try:
            self.api.post("/service/restart")
            self._op_status.config(text="Reiniciando... puede tardar ~15-20s en volver.", fg="#1971c2")
        except ApiError as e:
            self._op_status.config(text=f"Error: {e}", fg="#e03131")

    def _build_security_section(self):
        row = self._row + 1
        sec = tk.LabelFrame(self, text="Seguridad", font=_LABEL_FONT, padx=12, pady=12)
        sec.grid(row=row, column=0, columnspan=3, sticky="w", padx=16, pady=16)

        tk.Label(
            sec, text="Rotar la API key es instantáneo. Rotar las claves maestras\n"
                       "re-encripta (o invalida) todas las credenciales guardadas — puede tardar un momento.",
            font=("Segoe UI", 9), fg="#666666", justify="left",
        ).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 8))

        tk.Button(
            sec, text="🔄 Regenerar API key", relief="flat", cursor="hand2",
            padx=10, pady=4, command=self._rotate_api_key,
        ).grid(row=1, column=0, sticky="w", padx=(0, 8))

        tk.Button(
            sec, text="🔄 Rotar claves maestras (fernet + session)", relief="flat", cursor="hand2",
            padx=10, pady=4, command=self._rotate_master,
        ).grid(row=1, column=1, sticky="w")

        self._sec_status = tk.Label(sec, text="", font=("Segoe UI", 9))
        self._sec_status.grid(row=2, column=0, columnspan=2, sticky="w", pady=(8, 0))

    def _rotate_api_key(self):
        if not _confirm(self, "Regenerar API key",
                         "Esto invalida la API key actual. El tray y el panel se actualizan solos "
                         "en su próxima recarga (≤30s). ¿Continuar?"):
            return
        try:
            resp = self.api.post("/config/keys/rotate-api-key")
            self.api.api_key = resp.get("api_key", self.api.api_key)
            self._sec_status.config(text="API key regenerada ✔", fg="#2f9e44")
        except ApiError as e:
            self._sec_status.config(text=f"Error: {e}", fg="#e03131")

    def _rotate_master(self):
        if not _confirm(
            self, "Rotar claves maestras",
            "Esto va a RE-ENCRIPTAR todas las credenciales guardadas (passwords, TOTP, tokens) "
            "y va a invalidar las sesiones de browser activas (se recrean solas en el próximo login).\n\n"
            "No cierres el panel hasta que termine. ¿Continuar?",
        ):
            return
        try:
            self.api.post("/config/keys/rotate-master", {"targets": ["fernet_key", "session_key"]})
        except ApiError as e:
            messagebox.showerror("ATANA", f"No se pudo iniciar la rotación: {e}", parent=self)
            return
        self._sec_status.config(text="Rotando...", fg="#1971c2")
        self.after(1500, self._poll_rotation)

    def _poll_rotation(self):
        try:
            status = self.api.get("/config/keys/rotate-status")
        except ApiError as e:
            self._sec_status.config(text=f"Error consultando estado: {e}", fg="#e03131")
            return

        state = status.get("state")
        if state == "running":
            self.after(1500, self._poll_rotation)
        elif state == "done":
            self._sec_status.config(text="Rotación completada ✔", fg="#2f9e44")
        elif state == "error":
            self._sec_status.config(text=f"Falló — claves anteriores siguen vigentes: {status.get('detail')}", fg="#e03131")
        else:
            self._sec_status.config(text="")


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

        tk.Label(
            self, text="Dejar el token en blanco desactiva el auto-update.",
            font=("Segoe UI", 9), fg="#666666",
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
            tk.Button(
                parent_frame, text="📷 Generar desde foto del QR", relief="flat", cursor="hand2",
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
        box = tk.LabelFrame(self.form, text="Cuentas", font=_LABEL_FONT, padx=10, pady=10)
        box.grid(row=row, column=0, columnspan=3, sticky="we", padx=4, pady=8)
        self._row += 1

        self._accounts_list = tk.Frame(box)
        self._accounts_list.pack(fill="x")

        tk.Button(
            box, text="+ Agregar cuenta", relief="flat", cursor="hand2",
            command=self._add_account,
        ).pack(anchor="w", pady=(8, 0))

        self._render_accounts()

    def _render_accounts(self):
        for w in self._accounts_list.winfo_children():
            w.destroy()
        self._account_vars: dict[str, tk.StringVar] = {}

        for acc in self._accounts:
            alias = acc.get("alias", "(sin alias)")
            row_f = tk.Frame(self._accounts_list)
            row_f.pack(fill="x", pady=2)

            tk.Label(row_f, text=alias, font=_FIELD_FONT, width=18, anchor="w").pack(side="left")

            var = tk.StringVar(value="")
            self._account_vars[alias] = var
            entry = tk.Entry(row_f, textvariable=var, show="•", width=28, font=("Consolas", 10))
            entry.pack(side="left", padx=4)

            show_var = tk.BooleanVar(value=False)
            tk.Checkbutton(
                row_f, text="Mostrar", variable=show_var,
                command=lambda e=entry, s=show_var: e.config(show="" if s.get() else "•"),
            ).pack(side="left")

            tk.Button(
                row_f, text="💾", relief="flat", cursor="hand2",
                command=lambda a=alias: self._save_account_token(a),
            ).pack(side="left", padx=4)
            tk.Button(
                row_f, text="✕", relief="flat", cursor="hand2", fg="#e03131",
                command=lambda a=alias: self._remove_account(a),
            ).pack(side="left")

            run_async_retrying(
                self, work=lambda a=alias: self.api.get(f"/config/agents/{self.provider}/secret/accounts/{a}").get("value", ""),
                on_done=lambda v, var=var: var.set(v or ""),
                on_final_error=lambda e: None,
            )

    def _add_account(self):
        win = tk.Toplevel(self)
        win.title("Agregar cuenta")
        win.transient(self.winfo_toplevel())
        win.grab_set()

        tk.Label(win, text="Alias:", font=_FIELD_FONT).grid(row=0, column=0, sticky="w", padx=12, pady=(12, 4))
        alias_var = tk.StringVar()
        tk.Entry(win, textvariable=alias_var, width=30).grid(row=0, column=1, padx=12, pady=(12, 4))

        tk.Label(win, text="Access token:", font=_FIELD_FONT).grid(row=1, column=0, sticky="w", padx=12, pady=4)
        token_var = tk.StringVar()
        tk.Entry(win, textvariable=token_var, width=30, show="•").grid(row=1, column=1, padx=12, pady=4)

        def _confirm_add():
            alias = alias_var.get().strip()
            token = token_var.get().strip()
            if not alias:
                messagebox.showerror("ATANA", "El alias no puede estar vacío.", parent=win)
                return
            if any(a.get("alias") == alias for a in self._accounts):
                messagebox.showerror("ATANA", "Ya existe una cuenta con ese alias.", parent=win)
                return
            self._accounts.append({"alias": alias, "access_token_set": bool(token)})
            self._save_accounts(extra_token={alias: token} if token else {})
            win.destroy()

        btns = tk.Frame(win, pady=12, padx=12)
        btns.grid(row=2, column=0, columnspan=2)
        tk.Button(btns, text="Agregar", command=_confirm_add, bg="#1971c2", fg="white", relief="flat", padx=12).pack(side="left", padx=4)
        tk.Button(btns, text="Cancelar", command=win.destroy, relief="flat", padx=12).pack(side="left")

    def _save_account_token(self, alias: str):
        var = self._account_vars.get(alias)
        if var is None:
            return
        self._save_accounts(extra_token={alias: var.get()})

    def _remove_account(self, alias: str):
        if not _confirm(self, "Quitar cuenta", f"¿Quitar la cuenta '{alias}'?"):
            return
        self._accounts = [a for a in self._accounts if a.get("alias") != alias]
        self._save_accounts()

    def _save_accounts(self, extra_token: dict | None = None):
        """Sends the full accounts list back. Only aliases present in
        `extra_token` carry a plaintext access_token — everything else is
        merged server-side against the previously stored token (see
        dispatcher/db.py update_agent_config)."""
        extra_token = extra_token or {}
        payload = []
        for acc in self._accounts:
            item = {k: v for k, v in acc.items() if k != "access_token_set"}
            if acc.get("alias") in extra_token:
                item["access_token"] = extra_token[acc["alias"]]
            payload.append(item)

        try:
            self.api.put(f"/config/agents/{self.provider}", {"accounts": payload})
            self._render_accounts()
            self._flash_saved(True)
        except ApiError as e:
            messagebox.showerror("ATANA", f"No se pudo guardar la cuenta: {e}", parent=self)


# ── Agentes aún no disponibles ───────────────────────────────────────────

class _UnavailableAgentTab(ttk.Frame):
    """
    Placeholder para providers que ya tienen fila en agent_config (seed_config.sql)
    pero todavía no tienen bot implementado (agent_loader.known_providers() no
    los incluye) — naranjax, getnet, cabal, amex, prisma al momento de escribir esto.
    """

    def __init__(self, parent, provider: str):
        super().__init__(parent)
        wrap = tk.Frame(self)
        wrap.place(relx=0.5, rely=0.4, anchor="center")

        tk.Label(wrap, text=provider.upper(), font=("Segoe UI", 14, "bold"), fg="#999999").pack()
        tk.Label(
            wrap, text="Este agente todavía no está disponible.",
            font=_FIELD_FONT, fg="#666666",
        ).pack(pady=(6, 0))

    def save(self) -> bool:
        return True  # nada que guardar


# ── Orchestrator tab ─────────────────────────────────────────────────────

class ConfigTab(ttk.Frame):
    """
    Builds its whole shell (header + Notebook + one placeholder tab each)
    synchronously — this is all just widget construction, no network calls,
    so the window appears immediately. The two GETs that feed every tab
    (/config/system, /config/agents) run in the background via
    run_async_retrying and populate tabs in place once they land.
    """

    def __init__(self, parent, api: ApiClient):
        super().__init__(parent)
        self.api = api
        self._savable_tabs: list = []

        # footer packed BEFORE the notebook so it claims its strip at the
        # bottom first — otherwise the notebook's fill="both" expand=True
        # would grab the whole frame and leave no room for it.
        footer = tk.Frame(self, bg="#f0f0f0", pady=8, padx=16)
        footer.pack(fill="x", side="bottom")

        self._global_status = tk.Label(footer, text="", font=("Segoe UI", 9), bg="#f0f0f0")
        self._global_status.pack(side="right", padx=12)

        tk.Button(
            footer, text="💾 Guardar todo", bg="#1971c2", fg="white",
            relief="flat", cursor="hand2", padx=16, pady=6,
            command=self._save_all,
        ).pack(side="right")

        self.notebook = ttk.Notebook(self)
        self.notebook.pack(fill="both", expand=True)

        self.general_tab = GeneralSettingsTab(self.notebook, api)
        self.smtp_tab    = SmtpSettingsTab(self.notebook, api)
        self.update_tab  = AutoUpdateSettingsTab(self.notebook, api)
        for tab, label in ((self.general_tab, "General"), (self.smtp_tab, "SMTP"), (self.update_tab, "Auto-update")):
            self.notebook.add(tab, text=label)
            self._savable_tabs.append(tab)

        self._agents_placeholder = _LoadingPlaceholder(self.notebook, "Cargando agentes...")
        self.notebook.add(self._agents_placeholder, text="Agentes")

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
        self.notebook.forget(self._agents_placeholder)
        self._agents_placeholder.destroy()

        for agent in sorted(agents, key=lambda a: a.get("provider", "")):
            provider = agent["provider"]
            if agent.get("available", True):
                tab = AgentConfigTab(self.notebook, self.api, provider, agent)
                self._savable_tabs.append(tab)
            else:
                tab = _UnavailableAgentTab(self.notebook, provider)
            self.notebook.add(tab, text=provider.capitalize())

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
            self._global_status.config(text="Nada para guardar", fg="#999999")
            self.after(3000, lambda: self._global_status.config(text=""))
            return

        self._global_status.config(text="Guardando...", fg="#1971c2")

        def _send_all():
            results = []
            for job in jobs:
                try:
                    self.api.put(job["endpoint"], job["body"])
                    results.append((job["tab"], True, ""))
                except Exception as e:
                    results.append((job["tab"], False, str(e)))
            return results

        run_async(
            self, work=_send_all,
            on_done=self._on_save_all_done,
            on_error=lambda e: self._global_status.config(text=f"Error: {e}", fg="#e03131"),
        )

    def _on_save_all_done(self, results: list):
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
            self._global_status.config(
                text=f"{ok_count} guardado(s), falló: {', '.join(failed)}", fg="#e03131",
            )
        else:
            self._global_status.config(text=f"Todo guardado ✔ ({ok_count})", fg="#2f9e44")
        self.after(6000, lambda: self._global_status.config(text=""))
