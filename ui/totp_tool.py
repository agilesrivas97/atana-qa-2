"""
ui/totp_tool.py
================
Standalone "🔑 TOTP" tab in the panel's top-level CTkTabview (see
ui/panel_app.py) — independent of any single agent. Upload a QR photo (a
Google Authenticator "export accounts" QR, or any single otpauth:// QR), see
the secret and live 6-digit codes right here to confirm you scanned the
right thing, then optionally push it into an agent's CREDENCIALES as a
convenience. Same decoder as tools/extractor.py (atana_otp.exe) and the QR
button already living inside each agent's TOTP row in ui/config_panel.py.

The QR image is decoded locally, in this process — it never leaves the
machine. Only the extracted secret (if you choose to save it to an agent)
goes out, over the same authenticated local API everything else uses —
never SQL Server directly.

UI toolkit: CustomTkinter — see ui/theme.py for the shared palette and the
TitledFrame helper (stands in for tk.LabelFrame, which CTk has no
equivalent of). The provider/account selectors use ctk.CTkOptionMenu
instead of the old ttk.Combobox(state="readonly").
"""

import time
import tkinter as tk
from tkinter import filedialog, messagebox

import customtkinter as ctk
import pyotp

from shared.api_client import ApiClient, ApiError
from ui import theme
from ui.async_utils import run_async_retrying

_FIELD_FONT = theme.FONT_BODY


class TotpToolTab(ctk.CTkFrame):

    def __init__(self, parent, api: ApiClient):
        super().__init__(parent, fg_color="transparent")
        self.api = api
        self._accounts: list[dict] = []
        self._selected: dict | None = None
        self._agents: list[dict] = []

        self._build_ui()
        self._load_agents()
        self._tick()

    # ── UI ─────────────────────────────────────────────────────────────

    def _build_ui(self):
        header = ctk.CTkFrame(self, fg_color="transparent")
        header.pack(fill="x", padx=16, pady=16)
        ctk.CTkLabel(header, text="Generador TOTP desde foto de QR", font=theme.FONT_TITLE).pack(anchor="w")
        ctk.CTkLabel(
            header,
            text="Subí la foto del QR de Google Authenticator (o cualquier QR otpauth://) para\n"
                 "ver el secreto y los códigos en vivo antes de guardarlo en un agente. La imagen\n"
                 "se procesa acá mismo — nunca sale de esta máquina ni pasa por la API.",
            font=theme.FONT_SMALL, text_color=theme.TEXT_DIM, justify="left",
        ).pack(anchor="w", pady=(6, 0))

        ctk.CTkButton(
            header, text="📷 Subir foto del QR", width=220, font=_FIELD_FONT,
            fg_color=theme.PRIMARY, hover_color=theme.PRIMARY_HOVER,
            command=self._upload_qr,
        ).pack(anchor="w", pady=(12, 0))

        # Selector — solo aparece si el QR trae varias cuentas (export de Google Authenticator)
        self._picker_frame = ctk.CTkFrame(self, fg_color="transparent")

        # Card de detalle
        self.card = theme.TitledFrame(self, "Cuenta")
        self.card.pack(fill="x", padx=16, pady=16)

        self._name_lbl = ctk.CTkLabel(self.card.body, text="—", font=(theme.FONT_FAMILY, 12, "bold"))
        self._name_lbl.grid(row=0, column=0, columnspan=3, sticky="w", padx=10, pady=(0, 12))

        ctk.CTkLabel(self.card.body, text="Secreto:", font=_FIELD_FONT).grid(row=1, column=0, sticky="w", padx=10)
        self._secret_var = tk.StringVar(value="")
        self._secret_entry = ctk.CTkEntry(
            self.card.body, textvariable=self._secret_var, show="•", width=300, font=theme.FONT_MONO_BODY,
        )
        self._secret_entry.grid(row=1, column=1, sticky="w", padx=8)

        self._show_var = tk.BooleanVar(value=False)
        ctk.CTkCheckBox(
            self.card.body, text="Mostrar", variable=self._show_var, font=theme.FONT_SMALL,
            command=lambda: self._secret_entry.configure(show="" if self._show_var.get() else "•"),
        ).grid(row=1, column=2, sticky="w")

        ctk.CTkButton(
            self.card.body, text="📋 Copiar secreto", width=180, font=_FIELD_FONT,
            fg_color=theme.NEUTRAL, hover_color=theme.NEUTRAL_HOVER, text_color=theme.TEXT,
            command=self._copy_secret,
        ).grid(row=2, column=1, sticky="w", padx=8, pady=(4, 0))

        # Códigos en vivo
        codes = ctk.CTkFrame(self.card.body, fg_color="transparent")
        codes.grid(row=3, column=0, columnspan=3, sticky="w", padx=10, pady=(16, 0))

        self._code_prev = self._code_box(codes, "Anterior", 0)
        self._code_now  = self._code_box(codes, "Actual", 1, big=True)
        self._code_next = self._code_box(codes, "Siguiente", 2)

        self._countdown_lbl = ctk.CTkLabel(self.card.body, text="", font=theme.FONT_SMALL, text_color=theme.TEXT_DIM)
        self._countdown_lbl.grid(row=4, column=0, columnspan=3, sticky="w", padx=10, pady=(8, 0))

        # Guardar en un agente (conveniencia)
        save_box = theme.TitledFrame(self.card.body, "Guardar en un agente (opcional)")
        save_box.grid(row=5, column=0, columnspan=3, sticky="we", padx=10, pady=(16, 0))

        ctk.CTkLabel(save_box.body, text="Agente:", font=_FIELD_FONT).grid(row=0, column=0, sticky="w", padx=10)
        self._provider_var = tk.StringVar()
        self._provider_menu = ctk.CTkOptionMenu(save_box.body, variable=self._provider_var, values=[""], width=160)
        self._provider_menu.grid(row=0, column=1, sticky="w", padx=8)

        ctk.CTkLabel(save_box.body, text="Campo:", font=_FIELD_FONT).grid(row=0, column=2, sticky="w", padx=(12, 0))
        self._field_var = tk.StringVar(value="totp_secret")
        ctk.CTkEntry(save_box.body, textvariable=self._field_var, width=130, font=_FIELD_FONT).grid(row=0, column=3, sticky="w", padx=8)

        ctk.CTkButton(
            save_box.body, text="💾 Guardar", width=110, font=_FIELD_FONT,
            fg_color=theme.PRIMARY, hover_color=theme.PRIMARY_HOVER,
            command=self._save_to_agent,
        ).grid(row=0, column=4, sticky="w", padx=(8, 0))

        self._save_status = ctk.CTkLabel(save_box.body, text="", font=theme.FONT_SMALL)
        self._save_status.grid(row=1, column=0, columnspan=5, sticky="w", padx=10, pady=(6, 0))

        self._set_empty_state()

    def _code_box(self, parent, label, col, big=False):
        box = ctk.CTkFrame(parent, fg_color="transparent")
        box.grid(row=0, column=col, padx=14)
        ctk.CTkLabel(box, text=label, font=(theme.FONT_FAMILY, 8), text_color=theme.TEXT_DIM).pack()
        lbl = ctk.CTkLabel(
            box, text="------", font=(theme.FONT_MONO, 20 if big else 14, "bold"),
            text_color=theme.TEXT if big else theme.TEXT_DIM,
        )
        lbl.pack()
        return lbl

    def _set_empty_state(self):
        self._name_lbl.configure(text="Subí una foto de QR para empezar")
        self._secret_var.set("")
        for lbl in (self._code_prev, self._code_now, self._code_next):
            lbl.configure(text="------")
        self._countdown_lbl.configure(text="")

    # ── QR upload ──────────────────────────────────────────────────────

    def _upload_qr(self):
        path = filedialog.askopenfilename(
            title="Seleccioná la foto del QR",
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

        self._accounts = accounts
        self._render_picker()
        self._select_account(0)

    def _render_picker(self):
        for w in self._picker_frame.winfo_children():
            w.destroy()

        if len(self._accounts) <= 1:
            self._picker_frame.pack_forget()
            return

        self._picker_frame.pack(fill="x", padx=16, pady=(8, 0))
        ctk.CTkLabel(
            self._picker_frame, text=f"El QR trae {len(self._accounts)} cuentas — elegí una:",
            font=_FIELD_FONT,
        ).pack(anchor="w")

        names = [f"{i + 1}. {a.get('name') or a.get('issuer') or '(sin nombre)'}" for i, a in enumerate(self._accounts)]
        menu = ctk.CTkOptionMenu(
            self._picker_frame, values=names, width=420,
            command=lambda choice: self._select_account(names.index(choice)),
        )
        menu.set(names[0])
        menu.pack(anchor="w", pady=(4, 0))

    def _select_account(self, idx: int):
        if idx < 0 or idx >= len(self._accounts):
            return
        self._selected = self._accounts[idx]
        name   = self._selected.get("name") or self._selected.get("issuer") or "(sin nombre)"
        issuer = self._selected.get("issuer") or ""
        title  = name + (f"  ·  {issuer}" if issuer and issuer != name else "")
        self._name_lbl.configure(text=title)
        self._secret_var.set(self._selected.get("secret", ""))

    # ── Live codes — se actualiza cada 1s mientras el tab exista ────────

    def _tick(self):
        if self._selected and self._selected.get("secret"):
            try:
                secret  = self._selected["secret"]
                padding = (8 - len(secret) % 8) % 8
                totp    = pyotp.TOTP(secret + "=" * padding)
                now     = time.time()
                self._code_prev.configure(text=totp.at(now - 30))
                self._code_now.configure(text=totp.now())
                self._code_next.configure(text=totp.at(now + 30))
                remaining = 30 - int(now) % 30
                self._countdown_lbl.configure(text=f"El código actual se renueva en {remaining}s")
            except Exception:
                pass  # secreto todavía inválido/incompleto — no romper el tick
        self.after(1000, self._tick)

    def _copy_secret(self):
        if not self._selected:
            return
        self.clipboard_clear()
        self.clipboard_append(self._selected.get("secret", ""))

    # ── Guardar en un agente ─────────────────────────────────────────────

    def _load_agents(self):
        run_async_retrying(
            self, work=lambda: self.api.get("/config/agents").get("agents", []),
            on_done=self._on_agents_loaded,
            on_final_error=lambda e: None,  # el combo queda vacío — no es crítico para este tab
        )

    def _on_agents_loaded(self, agents: list):
        self._agents = [a for a in agents if a.get("available", True)]
        names = [a["provider"] for a in self._agents]
        self._provider_menu.configure(values=names or [""])
        if names:
            self._provider_var.set(names[0])

    def _save_to_agent(self):
        if not self._selected or not self._selected.get("secret"):
            messagebox.showwarning("ATANA", "Subí primero una foto de QR.", parent=self)
            return
        provider = self._provider_var.get().strip()
        field    = self._field_var.get().strip()
        if not provider:
            messagebox.showwarning("ATANA", "Elegí un agente.", parent=self)
            return
        if not field:
            messagebox.showwarning("ATANA", "Indicá el nombre del campo (ej: totp_secret).", parent=self)
            return

        if not messagebox.askyesno(
            "Confirmar", f"¿Guardar este secreto TOTP como '{field}' en {provider.upper()}?", parent=self,
        ):
            return

        try:
            self.api.put(f"/config/agents/{provider}", {field: self._selected["secret"]})
            self._save_status.configure(text=f"Guardado en {provider.upper()} ✔", text_color=theme.SUCCESS)
        except ApiError as e:
            self._save_status.configure(text=f"Error: {e}", text_color=theme.DANGER)
        self.after(5000, lambda: self._save_status.configure(text=""))
