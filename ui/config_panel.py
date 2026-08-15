"""
ui/config_panel.py
===================
"Configuración" tab: system-wide settings (General / SMTP / Auto-update) plus
one tab per agent, generated dynamically from GET /config/agents.

Everything here goes through shared/api_client.py — the panel never sees a
decrypted secret. Every secret field only ever shows "configurado / no
configurado" plus a "🔄 Reemplazar" button; the plaintext the user types
into that dialog is sent straight to the API, which encrypts it server-side
(dispatcher/db.py) before it ever touches the database.
"""

import tkinter as tk
from tkinter import ttk, filedialog, messagebox

from loguru import logger

from shared.api_client import ApiClient, ApiError

_FIELD_FONT = ("Segoe UI", 10)
_LABEL_FONT = ("Segoe UI", 10, "bold")

_AGENT_TOP_LEVEL_FIELDS = [
    # (key, label, kind)  kind in {str, int, bool, folder}
    ("enabled",            "Habilitado",                 "bool"),
    ("username",           "Usuario",                    "str"),
    ("destination_folder", "Carpeta de destino",         "folder"),
    ("rename_pattern",     "Patrón de renombrado",       "str"),
    ("portal_url",         "URL del portal",              "str"),
    ("schedule_hour",      "Hora de ejecución (0-23)",   "int"),
    ("schedule_minute",    "Minuto de ejecución (0-59)", "int"),
    ("max_retries",        "Reintentos máximos",         "int"),
    ("retry_interval_min", "Minutos entre reintentos",   "int"),
]
_AGENT_TOP_LEVEL_KEYS = {k for k, _, _ in _AGENT_TOP_LEVEL_FIELDS} | {"provider", "has_password"}


# ── Small reusable dialogs ──────────────────────────────────────────────────

def _ask_secret(parent, title: str, prompt: str, initial: str = "") -> str | None:
    """Modal dialog with a masked Entry. Returns the typed value, or None if cancelled."""
    result = {"value": None}

    win = tk.Toplevel(parent)
    win.title(title)
    win.resizable(False, False)
    win.transient(parent.winfo_toplevel())
    win.grab_set()

    tk.Label(win, text=prompt, font=_FIELD_FONT, padx=16, pady=(16, 4)).pack(anchor="w")

    var = tk.StringVar(value=initial)
    entry = tk.Entry(win, textvariable=var, show="•", width=44, font=_FIELD_FONT)
    entry.pack(padx=16, pady=4)
    entry.focus_set()

    show_var = tk.BooleanVar(value=False)
    tk.Checkbutton(
        win, text="Mostrar", variable=show_var,
        command=lambda: entry.config(show="" if show_var.get() else "•"),
    ).pack(anchor="w", padx=16)

    btns = tk.Frame(win, pady=12, padx=16)
    btns.pack(fill="x")

    def _ok():
        result["value"] = var.get()
        win.destroy()

    def _cancel():
        win.destroy()

    tk.Button(btns, text="Guardar", command=_ok, bg="#1971c2", fg="white", relief="flat", padx=14, pady=4).pack(side="right")
    tk.Button(btns, text="Cancelar", command=_cancel, relief="flat", padx=14, pady=4).pack(side="right", padx=6)

    win.bind("<Return>", lambda e: _ok())
    win.bind("<Escape>", lambda e: _cancel())
    win.wait_window()
    return result["value"]


def _confirm(parent, title: str, message: str) -> bool:
    return messagebox.askyesno(title, message, parent=parent)


class _SettingsTabBase(ttk.Frame):
    """Common scaffolding for a settings form: a grid of labeled fields plus
    a bottom bar with a single 'Guardar cambios' button."""

    def __init__(self, parent, api: ApiClient, title: str):
        super().__init__(parent)
        self.api = api
        self._vars: dict[str, tk.Variable] = {}
        self._row = 0

        tk.Label(self, text=title, font=("Segoe UI", 13, "bold")).grid(
            row=0, column=0, columnspan=3, sticky="w", padx=16, pady=(16, 8),
        )
        self._row = 1

        self.form = self  # subclasses may override to nest a sub-frame

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

    def _add_secret_row(self, key: str, label: str, is_set: bool, on_replace, extra=None):
        row = self._row
        tk.Label(self.form, text=label, font=_FIELD_FONT).grid(row=row, column=0, sticky="w", padx=16, pady=4)

        status = tk.Label(
            self.form,
            text="●  configurado" if is_set else "○  no configurado",
            fg="#2f9e44" if is_set else "#999999", font=_FIELD_FONT,
        )
        status.grid(row=row, column=1, sticky="w", padx=4)

        btns = tk.Frame(self.form)
        btns.grid(row=row, column=2, sticky="w")
        tk.Button(btns, text="🔄 Reemplazar", relief="flat", cursor="hand2", command=on_replace).pack(side="left")
        if extra:
            extra(btns)

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

    def _save_bar(self, on_save):
        row = self._row + 1
        bar = tk.Frame(self, pady=12)
        bar.grid(row=row, column=0, columnspan=3, sticky="w", padx=16)
        tk.Button(
            bar, text="💾 Guardar cambios", bg="#1971c2", fg="white",
            relief="flat", cursor="hand2", padx=14, pady=6, command=on_save,
        ).pack(side="left")
        self._save_status = tk.Label(bar, text="", font=("Segoe UI", 9), fg="#2f9e44")
        self._save_status.pack(side="left", padx=10)

    def _flash_saved(self, ok: bool, detail: str = ""):
        if ok:
            self._save_status.config(text="Guardado ✔", fg="#2f9e44")
        else:
            self._save_status.config(text=f"Error: {detail}", fg="#e03131")
        self.after(4000, lambda: self._save_status.config(text=""))


# ── General ──────────────────────────────────────────────────────────────

class GeneralSettingsTab(_SettingsTabBase):

    _INT_FIELDS = {"check_jobs_interval_min", "check_update_interval_hours", "api_port"}

    def __init__(self, parent, api: ApiClient):
        super().__init__(parent, api, "Configuración general")
        self._load()

    def _load(self):
        try:
            sys_cfg = self.api.get("/config/system").get("system", {})
        except ApiError as e:
            messagebox.showwarning("ATANA", f"No se pudo cargar la configuración: {e}", parent=self)
            sys_cfg = {}

        self._add_field("check_jobs_interval_min",     "Minutos entre chequeos de jobs",      "str", sys_cfg.get("check_jobs_interval_min", 5))
        self._add_field("check_update_interval_hours", "Horas entre chequeos de actualización", "str", sys_cfg.get("check_update_interval_hours", 6))
        self._add_field("api_port",                    "Puerto de la API interna",             "str", sys_cfg.get("api_port", 8765))
        self._add_field("debug",                        "Logs en modo debug",                  "bool", str(sys_cfg.get("debug", "")).lower() == "true")

        self._save_bar(self._save)
        self._build_security_section()

    def _save(self):
        body = self._collect_plain(list(self._INT_FIELDS))
        body["debug"] = "true" if self._vars["debug"].get() else "false"
        body = {k: v for k, v in body.items() if v is not None}
        try:
            self.api.put("/config/system", body)
            self._flash_saved(True)
        except ApiError as e:
            self._flash_saved(False, str(e))

    def _build_security_section(self):
        row = self._row + 2
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
        self._load()

    def _load(self):
        try:
            sys_cfg = self.api.get("/config/system").get("system", {})
        except ApiError as e:
            messagebox.showwarning("ATANA", f"No se pudo cargar SMTP: {e}", parent=self)
            sys_cfg = {}

        self._add_field("smtp_enabled",   "Notificaciones activas", "bool", str(sys_cfg.get("smtp_enabled", "")).lower() == "true")
        self._add_field("smtp_host",      "Servidor SMTP",          "str",  sys_cfg.get("smtp_host", ""))
        self._add_field("smtp_port",      "Puerto",                 "str",  sys_cfg.get("smtp_port", 587))
        self._add_field("smtp_username",  "Usuario / remitente",    "str",  sys_cfg.get("smtp_username", ""))
        self._add_field("smtp_recipient", "Destinatario de alertas", "str", sys_cfg.get("smtp_recipient", ""))

        self._add_secret_row(
            "smtp_password", "Contraseña", bool(sys_cfg.get("smtp_password_set")),
            on_replace=self._replace_password,
        )

        self._save_bar(self._save)

    def _save(self):
        body = self._collect_plain(["smtp_host", "smtp_port", "smtp_username", "smtp_recipient"])
        body["smtp_enabled"] = "true" if self._vars["smtp_enabled"].get() else "false"
        body = {k: v for k, v in body.items() if v is not None}
        try:
            self.api.put("/config/system", body)
            self._flash_saved(True)
        except ApiError as e:
            self._flash_saved(False, str(e))

    def _replace_password(self):
        value = _ask_secret(self, "Contraseña SMTP", "Nueva contraseña:")
        if value is None:
            return
        try:
            self.api.put("/config/system", {"smtp_password": value})
            self._flash_saved(True)
        except ApiError as e:
            messagebox.showerror("ATANA", f"No se pudo guardar: {e}", parent=self)


# ── Auto-update ──────────────────────────────────────────────────────────

class AutoUpdateSettingsTab(_SettingsTabBase):

    def __init__(self, parent, api: ApiClient):
        super().__init__(parent, api, "Auto-update (GitHub Releases)")
        self._load()

    def _load(self):
        try:
            sys_cfg = self.api.get("/config/system").get("system", {})
        except ApiError as e:
            messagebox.showwarning("ATANA", f"No se pudo cargar Auto-update: {e}", parent=self)
            sys_cfg = {}

        self._add_field("github_owner", "Usuario / organización", "str", sys_cfg.get("github_owner", ""))
        self._add_field("github_repo",  "Repositorio",            "str", sys_cfg.get("github_repo", ""))

        self._add_secret_row(
            "github_token", "Personal Access Token", bool(sys_cfg.get("github_token_set")),
            on_replace=self._replace_token,
        )

        tk.Label(
            self, text="Dejar el token en blanco desactiva el auto-update.",
            font=("Segoe UI", 9), fg="#666666",
        ).grid(row=self._row, column=0, columnspan=3, sticky="w", padx=16, pady=(0, 8))
        self._row += 1

        self._save_bar(self._save)

    def _save(self):
        body = self._collect_plain(["github_owner", "github_repo"])
        body = {k: v for k, v in body.items() if v is not None}
        try:
            self.api.put("/config/system", body)
            self._flash_saved(True)
        except ApiError as e:
            self._flash_saved(False, str(e))

    def _replace_token(self):
        value = _ask_secret(self, "GitHub Token", "Nuevo Personal Access Token:")
        if value is None:
            return
        try:
            self.api.put("/config/system", {"github_token": value})
            self._flash_saved(True)
        except ApiError as e:
            messagebox.showerror("ATANA", f"No se pudo guardar: {e}", parent=self)


# ── Per-agent ────────────────────────────────────────────────────────────

class AgentConfigTab(_SettingsTabBase):
    """
    Renders the common agent_config columns plus whatever provider-specific
    extra_config fields the API returned — generically: any '<x>_set' key
    becomes a secret-replace row, 'accounts' gets its own mini list editor,
    everything else is a plain field. This means a brand-new agent with a new
    extra_config schema gets a reasonable form for free, no code changes here.
    """

    def __init__(self, parent, api: ApiClient, provider: str, agent: dict):
        super().__init__(parent, api, provider.upper())
        self.provider = provider
        self._extra_plain_keys: list[str] = []
        self._accounts: list[dict] = []
        self._build(agent)

    def _build(self, agent: dict):
        for key, label, kind in _AGENT_TOP_LEVEL_FIELDS:
            self._add_field(key, label, kind, agent.get(key))

        self._add_secret_row(
            "password", "Contraseña del portal", bool(agent.get("has_password")),
            on_replace=self._replace_password,
        )

        for key, value in agent.items():
            if key in _AGENT_TOP_LEVEL_KEYS:
                continue
            if key == "accounts" and isinstance(value, list):
                self._accounts = value
                self._build_accounts_section()
            elif key.endswith("_set"):
                logical = key[:-4]
                extra_qr = self._qr_button(logical) if logical == "totp_secret" else None
                self._add_secret_row(
                    logical, logical.replace("_", " ").capitalize(), bool(value),
                    on_replace=lambda k=logical: self._replace_extra_secret(k),
                    extra=extra_qr,
                )
            else:
                self._extra_plain_keys.append(key)
                self._add_field(key, key.replace("_", " ").capitalize(), "str", value)

        self._save_bar(self._save)

    def _save(self):
        body = self._collect_plain(
            [k for k, _, _ in _AGENT_TOP_LEVEL_FIELDS if k != "enabled"] + self._extra_plain_keys
        )
        body["enabled"] = self._vars["enabled"].get()
        for k in ("schedule_hour", "schedule_minute", "max_retries", "retry_interval_min"):
            if body.get(k) not in (None, ""):
                try:
                    body[k] = int(body[k])
                except ValueError:
                    messagebox.showerror("ATANA", f"'{k}' debe ser un número.", parent=self)
                    return
        body = {k: v for k, v in body.items() if v is not None}
        try:
            self.api.put(f"/config/agents/{self.provider}", body)
            self._flash_saved(True)
        except ApiError as e:
            self._flash_saved(False, str(e))

    def _replace_password(self):
        value = _ask_secret(self, f"Contraseña — {self.provider.upper()}", "Nueva contraseña del portal:")
        if value is None:
            return
        self._put_secret({"password": value})

    def _replace_extra_secret(self, logical_key: str):
        value = _ask_secret(self, f"{logical_key} — {self.provider.upper()}", f"Nuevo valor para '{logical_key}':")
        if value is None:
            return
        self._put_secret({logical_key: value})

    def _put_secret(self, body: dict):
        try:
            self.api.put(f"/config/agents/{self.provider}", body)
            self._flash_saved(True)
        except ApiError as e:
            messagebox.showerror("ATANA", f"No se pudo guardar: {e}", parent=self)

    def _qr_button(self, logical_key: str):
        def _factory(parent_frame):
            tk.Button(
                parent_frame, text="📷 Subir QR", relief="flat", cursor="hand2",
                command=lambda: self._upload_qr(logical_key),
            ).pack(side="left", padx=(6, 0))
        return _factory

    def _upload_qr(self, logical_key: str):
        path = filedialog.askopenfilename(
            title="Seleccioná la imagen del QR",
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

    # ── accounts (e.g. mercadopago) ─────────────────────────────────────

    def _build_accounts_section(self):
        row = self._row
        box = tk.LabelFrame(self, text="Cuentas", font=_LABEL_FONT, padx=10, pady=10)
        box.grid(row=row, column=0, columnspan=3, sticky="we", padx=16, pady=8)
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

        for acc in self._accounts:
            alias    = acc.get("alias", "(sin alias)")
            has_tok  = bool(acc.get("access_token_set"))
            row_f = tk.Frame(self._accounts_list)
            row_f.pack(fill="x", pady=2)

            tk.Label(row_f, text=alias, font=_FIELD_FONT, width=24, anchor="w").pack(side="left")
            tk.Label(
                row_f, text="● token configurado" if has_tok else "○ sin token",
                fg="#2f9e44" if has_tok else "#999999", font=("Segoe UI", 9),
            ).pack(side="left", padx=8)
            tk.Button(
                row_f, text="🔄 Token", relief="flat", cursor="hand2",
                command=lambda a=alias: self._replace_account_token(a),
            ).pack(side="left", padx=4)
            tk.Button(
                row_f, text="✕", relief="flat", cursor="hand2", fg="#e03131",
                command=lambda a=alias: self._remove_account(a),
            ).pack(side="left")

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

    def _replace_account_token(self, alias: str):
        value = _ask_secret(self, f"Token — {alias}", f"Nuevo access token para '{alias}':")
        if value is None:
            return
        self._save_accounts(extra_token={alias: value})

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


# ── Orchestrator tab ─────────────────────────────────────────────────────

class ConfigTab(ttk.Frame):

    def __init__(self, parent, api: ApiClient):
        super().__init__(parent)
        self.api = api

        self.notebook = ttk.Notebook(self)
        self.notebook.pack(fill="both", expand=True)

        self.notebook.add(GeneralSettingsTab(self.notebook, api),    text="General")
        self.notebook.add(SmtpSettingsTab(self.notebook, api),       text="SMTP")
        self.notebook.add(AutoUpdateSettingsTab(self.notebook, api), text="Auto-update")

        self._load_agent_tabs()

    def _load_agent_tabs(self):
        try:
            agents = self.api.get("/config/agents").get("agents", [])
        except ApiError as e:
            logger.warning(f"Could not load agent configs: {e}")
            messagebox.showwarning("ATANA", f"No se pudo cargar la configuración de agentes: {e}", parent=self)
            return

        for agent in sorted(agents, key=lambda a: a.get("provider", "")):
            provider = agent["provider"]
            tab = AgentConfigTab(self.notebook, self.api, provider, agent)
            self.notebook.add(tab, text=provider.capitalize())
