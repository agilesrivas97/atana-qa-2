"""
ATANA Agentes — Asistente de configuración inicial de base de datos.

Ejecutado como: atana_dispatcher.exe --setup-db

Pasos:
  1. Ingresar datos de conexion a SQL Server
  2. Probar conexion y guardar config.json
  3. Crear tablas (idempotente — seguro de re-ejecutar)
  4. Generar claves Fernet y guardarlas en system_config
"""

from __future__ import annotations

import json
import sys
import threading
import tkinter as tk
from tkinter import messagebox, ttk
from pathlib import Path

from cryptography.fernet import Fernet

# ── Path resolution ───────────────────────────────────────────────────────────

if getattr(sys, "frozen", False):
    ROOT = Path(sys.executable).parent
else:
    ROOT = Path(__file__).parent.parent

CONFIG_FILE = ROOT / "config.json"

# ── Schema SQL — idempotente (IF NOT EXISTS en cada objeto) ───────────────────

_SCHEMA_BATCHES = [
    # agent_jobs
    """
IF OBJECT_ID('agent_jobs', 'U') IS NULL
CREATE TABLE agent_jobs (
    id                  INT IDENTITY(1,1) PRIMARY KEY,
    provider            VARCHAR(50)  NOT NULL,
    status              VARCHAR(30)  NOT NULL DEFAULT 'pending',
    intervention_reason VARCHAR(200) NULL,
    error_msg           VARCHAR(500) NULL,
    attempts            INT          NOT NULL DEFAULT 0,
    max_retries         INT          NOT NULL DEFAULT 3,
    files_downloaded    INT          NOT NULL DEFAULT 0,
    period_from        DATETIME     NULL,
    period_to        DATETIME     NULL,
    requested_at        DATETIME     NOT NULL DEFAULT GETDATE(),
    started_at          DATETIME     NULL,
    finished_at         DATETIME     NULL,
    authorized_at       DATETIME     NULL,
    started_by          VARCHAR(50)  NOT NULL DEFAULT 'scheduler',
    CONSTRAINT chk_status CHECK (status IN (
        'pending','running','authorized','ok',
        'error','requires_intervention','ignored'
    ))
)
""",
    # downloaded_files
    """
IF OBJECT_ID('downloaded_files', 'U') IS NULL
CREATE TABLE downloaded_files (
    id                  INT IDENTITY(1,1) PRIMARY KEY,
    job_id              INT          NOT NULL REFERENCES agent_jobs(id),
    provider            VARCHAR(50)  NOT NULL,
    original_name       VARCHAR(300) NOT NULL,
    final_name          VARCHAR(300) NOT NULL,
    local_path          VARCHAR(500) NOT NULL,
    file_date           DATE         NULL,
    bytes               BIGINT       NOT NULL DEFAULT 0,
    sha256              VARCHAR(64)  NULL,
    downloaded_at       DATETIME     NOT NULL DEFAULT GETDATE(),
    processed_by_atana  BIT          NOT NULL DEFAULT 0
)
""",
    # agent_status
    """
IF OBJECT_ID('agent_status', 'U') IS NULL
CREATE TABLE agent_status (
    provider            VARCHAR(50)  NOT NULL PRIMARY KEY,
    current_version     VARCHAR(20)  NULL,
    last_run            DATETIME     NULL,
    last_result         VARCHAR(20)  NULL,
    last_error          VARCHAR(500) NULL,
    files_today         INT          NOT NULL DEFAULT 0,
    session_active      BIT          NOT NULL DEFAULT 0,
    next_run            DATETIME     NULL,
    active              BIT          NOT NULL DEFAULT 1,
    updated_at          DATETIME     NOT NULL DEFAULT GETDATE()
)
""",
    # agent_config
    """
IF OBJECT_ID('agent_config', 'U') IS NULL
CREATE TABLE agent_config (
    provider            VARCHAR(50)    NOT NULL PRIMARY KEY,
    enabled             BIT            NOT NULL DEFAULT 0,
    username            VARCHAR(200)   NULL,
    password_enc        VARBINARY(MAX) NULL,
    destination_folder  VARCHAR(500)   NULL,
    rename_pattern      VARCHAR(200)   NULL,
    max_retries         INT            NOT NULL DEFAULT 3,
    retry_interval_min  INT            NOT NULL DEFAULT 15,
    portal_url          VARCHAR(500)   NULL,
    schedule_hour       INT            NULL,
    schedule_minute     INT            NOT NULL DEFAULT 0,
    extra_config        NVARCHAR(MAX)  NULL,
    updated_at          DATETIME       NOT NULL DEFAULT GETDATE()
)
""",
    # system_config
    """
IF OBJECT_ID('system_config', 'U') IS NULL
CREATE TABLE system_config (
    key_config   VARCHAR(100)  NOT NULL PRIMARY KEY,
    value_config NVARCHAR(MAX) NOT NULL,
    updated_at   DATETIME      NOT NULL DEFAULT GETDATE()
)
""",
    # session_store
    """
IF OBJECT_ID('session_store', 'U') IS NULL
CREATE TABLE session_store (
    provider            VARCHAR(50)    NOT NULL PRIMARY KEY,
    encrypted_data      VARBINARY(MAX) NOT NULL,
    created_at          DATETIME       NOT NULL DEFAULT GETDATE(),
    expires_at          DATETIME       NOT NULL,
    valid               BIT            NOT NULL DEFAULT 1
)
""",
    # orchestrator_agent_jobs
    """
IF OBJECT_ID('orchestrator_agent_jobs', 'U') IS NULL
CREATE TABLE orchestrator_agent_jobs (
    id           INT IDENTITY(1,1) PRIMARY KEY,
    agents       VARCHAR(500) NOT NULL,
    status       VARCHAR(20)  NOT NULL DEFAULT 'pending',
    requested_at DATETIME     NOT NULL DEFAULT GETDATE(),
    started_at   DATETIME     NULL,
    started_by   VARCHAR(50)  NOT NULL DEFAULT 'manual',
    error_msg    VARCHAR(500) NULL,
    CONSTRAINT chk_orch_status CHECK (status IN ('pending','processed','error'))
)
""",
    # agent_status initial rows
    """
MERGE agent_status AS t
USING (VALUES
    ('prisma',      1),
    ('cabal',       1),
    ('naranjax',    1),
    ('fiserv',      1),
    ('amex',        1),
    ('getnet',      0),
    ('mercadopago', 1)
) AS s(provider, active) ON t.provider = s.provider
WHEN NOT MATCHED THEN
    INSERT (provider, active) VALUES (s.provider, s.active);
""",
    # agent_config initial rows
    """
MERGE agent_config AS t
USING (VALUES
    ('prisma',      0, './settlements/prisma',      '{merchant}_{date}.{ext}', 8,  0),
    ('cabal',       0, './settlements/cabal',       'CABAL_{date}.{ext}',      8,  5),
    ('naranjax',    0, './settlements/naranjax',    'NX_{date}.{ext}',         8, 10),
    ('fiserv',      0, './settlements/fiserv',      'FISERV_{date}.{ext}',     8, 15),
    ('amex',        0, './settlements/amex',        'AMEX_{date}.{ext}',       8, 20),
    ('getnet',      0, './settlements/getnet',      'GETNET_{date}.{ext}',     8, 25),
    ('mercadopago', 0, './settlements/mercadopago', 'MP_{alias}_{date}.{ext}', 8, 30)
) AS s(provider, enabled, destination_folder, rename_pattern, schedule_hour, schedule_minute)
ON t.provider = s.provider
WHEN NOT MATCHED THEN
    INSERT (provider, enabled, destination_folder, rename_pattern, schedule_hour, schedule_minute)
    VALUES (s.provider, s.enabled, s.destination_folder, s.rename_pattern, s.schedule_hour, s.schedule_minute);
""",
    # system_config initial rows (keys only — fernet keys se generan despues)
    """
MERGE system_config AS t
USING (VALUES
    ('log_dir',                   './logs'),
    ('debug',                     'false'),
    ('api_port',                  '8765'),
    ('check_jobs_interval_min',   '5'),
    ('check_update_interval_hours','6'),
    ('github_token_enc',              ''),
    ('github_owner',              ''),
    ('github_repo',               ''),
    ('smtp_host',                 'smtp.gmail.com'),
    ('smtp_port',                 '587'),
    ('smtp_username',             ''),
    ('smtp_password_enc',         ''),
    ('smtp_recipient',            ''),
    ('smtp_enabled',              'false')
) AS s(key_config, value_config) ON t.key_config = s.key_config
WHEN NOT MATCHED THEN
    INSERT (key_config, value_config) VALUES (s.key_config, s.value_config);
""",
    # views
    """
IF OBJECT_ID('vw_files_to_import', 'V') IS NULL
EXEC('
CREATE VIEW vw_files_to_import AS
SELECT f.id, f.provider, f.final_name, f.local_path, f.file_date, f.bytes, f.downloaded_at
FROM downloaded_files f
WHERE f.processed_by_atana = 0
')
""",
    """
IF OBJECT_ID('vw_agent_status', 'V') IS NULL
EXEC('
CREATE VIEW vw_agent_status AS
SELECT s.provider, s.current_version, s.last_run, s.last_result, s.last_error,
       s.files_today, s.session_active, s.next_run,
       j.status AS active_job, j.intervention_reason
FROM agent_status s
LEFT JOIN agent_jobs j ON j.provider = s.provider
    AND j.id = (SELECT TOP 1 id FROM agent_jobs WHERE provider = s.provider ORDER BY requested_at DESC)
')
""",
    """
IF NOT EXISTS (
    SELECT 1 FROM sys.columns 
    WHERE Name = N'period_from' AND Object_ID = Object_ID(N'agent_jobs')
)
BEGIN
    ALTER TABLE agent_jobs ADD 
        period_from        DATETIME     NULL,
        period_to        DATETIME     NULL,
        files_downloaded    INT          NOT NULL DEFAULT 0;
END
""",
]


# ── Auto-detection helpers ────────────────────────────────────────────────────

def _detect_sql_instances() -> list[str]:
    """Detecta instancias SQL Server locales vía registro de Windows."""
    instances: list[str] = []
    try:
        import winreg
        try:
            winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                           r'SYSTEM\CurrentControlSet\Services\MSSQLSERVER')
            instances.append('localhost')
        except FileNotFoundError:
            pass
        try:
            key = winreg.OpenKey(
                winreg.HKEY_LOCAL_MACHINE,
                r'SOFTWARE\Microsoft\Microsoft SQL Server\Instance Names\SQL',
            )
            i = 0
            while True:
                try:
                    name, _, _ = winreg.EnumValue(key, i)
                    instances.append(rf'localhost\{name}')
                    i += 1
                except OSError:
                    break
        except FileNotFoundError:
            pass
    except Exception:
        pass
    return instances or [r'localhost\SQLEXPRESS']


def _detect_odbc_drivers() -> list[str]:
    """Retorna drivers ODBC de SQL Server disponibles, mejor primero."""
    try:
        import pyodbc
        available = set(pyodbc.drivers())
    except Exception:
        available = set()
    preferred = [
        "ODBC Driver 18 for SQL Server",
        "ODBC Driver 17 for SQL Server",
        "ODBC Driver 13 for SQL Server",
        "SQL Server Native Client 11.0",
        "SQL Server",
    ]
    result = [d for d in preferred if d in available]
    result += [d for d in available if 'SQL Server' in d and d not in result]
    return result or ["ODBC Driver 18 for SQL Server"]


def _probe_trusted_connection(server: str, driver: str) -> bool:
    """Intenta conectar con Windows Auth. Retorna True si tiene acceso."""
    try:
        import pyodbc
        cn = pyodbc.connect(
            f"DRIVER={{{driver}}};SERVER={server};DATABASE=master;"
            "Trusted_Connection=yes;TrustServerCertificate=yes;",
            timeout=3,
        )
        cn.close()
        return True
    except Exception:
        return False


def _parse_ssms_connstr(raw: str) -> dict:
    """Parsea una cadena de conexion de SSMS y retorna campos del wizard."""
    parts: dict[str, str] = {}
    for seg in raw.split(';'):
        if '=' in seg:
            k, _, v = seg.partition('=')
            parts[k.strip().lower()] = v.strip()
    return {
        "server":   parts.get("data source", ""),
        "database": parts.get("initial catalog", "atana"),
        "trusted":  parts.get("integrated security", "").lower() in ("true", "sspi"),
    }


# ── Connection string ─────────────────────────────────────────────────────────

def _build_conn_str(db: dict) -> str:
    if db.get("trusted_connection"):
        return (
            f"DRIVER={{{db['driver']}}};"
            f"SERVER={db['server']};"
            f"DATABASE={db['database']};"
            f"Trusted_Connection=yes;"
            f"TrustServerCertificate=yes;"
        )
    return (
        f"DRIVER={{{db['driver']}}};"
        f"SERVER={db['server']};"
        f"DATABASE={db['database']};"
        f"UID={db['username']};"
        f"PWD={db['password']};"
        f"TrustServerCertificate=yes;"
    )


# ── Main application ──────────────────────────────────────────────────────────

class SetupApp:

    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("ATANA Agentes - Configuracion inicial")
        self.root.geometry("520x530")
        self.root.resizable(False, False)

        self._conn_vars: dict[str, tk.StringVar] = {}
        self._conn_entries: dict[str, ttk.Widget] = {}
        self._trusted_var = tk.BooleanVar(value=False)
        self._ssms_var    = tk.StringVar()

        # Detect available instances and drivers before building the UI
        self._instances = _detect_sql_instances()
        self._drivers   = _detect_odbc_drivers()

        self._build_ui()

        # Probe Windows Auth in background and pre-select if available
        threading.Thread(target=self._probe_trusted_bg, daemon=True).start()

    def _probe_trusted_bg(self):
        server = self._conn_vars.get("server", tk.StringVar()).get()
        driver = self._drivers[0] if self._drivers else "SQL Server"
        if _probe_trusted_connection(server, driver):
            self.root.after(0, lambda: self._trusted_var.set(True))
            self.root.after(0, self._toggle_trusted)
            self.root.after(0, lambda: self._probe_lbl.config(
                text="Autenticacion Windows detectada automaticamente",
                foreground="#2f9e44",
            ))

    def _build_ui(self):
        self._main = ttk.Frame(self.root, padding=24)
        self._main.pack(fill="both", expand=True)

        ttk.Label(self._main, text="Configuracion inicial de base de datos",
                  font=("Segoe UI", 11, "bold")).grid(
            row=0, column=0, columnspan=3, sticky="w", pady=(0, 12)
        )

        # ── Cadena SSMS (opcional) ────────────────────────────────────────────
        ttk.Label(self._main, text="Cadena SSMS:").grid(
            row=1, column=0, sticky="e", padx=(0, 10), pady=(0, 2)
        )
        ttk.Entry(self._main, textvariable=self._ssms_var, width=28).grid(
            row=1, column=1, sticky="ew", pady=(0, 2)
        )
        ttk.Button(self._main, text="Aplicar", width=7,
                   command=self._apply_ssms).grid(
            row=1, column=2, sticky="w", padx=(4, 0), pady=(0, 2)
        )

        ttk.Separator(self._main, orient="horizontal").grid(
            row=2, column=0, columnspan=3, sticky="ew", pady=(4, 8)
        )

        # ── Campos de conexion ────────────────────────────────────────────────
        db_defaults: dict = {}
        if CONFIG_FILE.exists():
            try:
                db_defaults = json.loads(
                    CONFIG_FILE.read_text(encoding="utf-8")
                ).get("database", {})
            except Exception:
                pass

        server_default = db_defaults.get("server") or (
            self._instances[0] if self._instances else r"localhost\SQLEXPRESS"
        )
        driver_default = db_defaults.get("driver") or (
            self._drivers[0] if self._drivers else "ODBC Driver 18 for SQL Server"
        )

        fields = [
            ("Servidor SQL:",   "server",   server_default),
            ("Base de datos:",  "database", db_defaults.get("database", "atana")),
            ("Driver ODBC:",    "driver",   driver_default),
            ("Usuario SQL:",    "username", db_defaults.get("username", "")),
            ("Contrasena SQL:", "password", ""),
        ]

        for i, (label, key, default) in enumerate(fields, start=3):
            ttk.Label(self._main, text=label).grid(
                row=i, column=0, sticky="e", padx=(0, 10), pady=4
            )
            var = tk.StringVar(value=default)
            self._conn_vars[key] = var

            if key == "server":
                widget: ttk.Widget = ttk.Combobox(
                    self._main, textvariable=var,
                    values=self._instances, width=32,
                )
            elif key == "driver":
                widget = ttk.Combobox(
                    self._main, textvariable=var,
                    values=self._drivers, width=32,
                )
            else:
                widget = ttk.Entry(
                    self._main, textvariable=var, width=34,
                    show="*" if key == "password" else "",
                )

            widget.grid(row=i, column=1, columnspan=2, sticky="ew", pady=4)
            self._conn_entries[key] = widget

        # ── Trusted Connection ────────────────────────────────────────────────
        ttk.Checkbutton(
            self._main,
            text="Autenticacion Windows (Trusted Connection)",
            variable=self._trusted_var,
            command=self._toggle_trusted,
        ).grid(row=8, column=1, columnspan=2, sticky="w", pady=(4, 0))

        self._probe_lbl = ttk.Label(self._main, text="", font=("Segoe UI", 8))
        self._probe_lbl.grid(row=9, column=1, columnspan=2, sticky="w")

        ttk.Separator(self._main, orient="horizontal").grid(
            row=10, column=0, columnspan=3, sticky="ew", pady=(8, 4)
        )

        # ── Botones ───────────────────────────────────────────────────────────
        btn_frame = ttk.Frame(self._main)
        btn_frame.grid(row=11, column=0, columnspan=3, pady=(6, 6))

        ttk.Button(btn_frame, text="Probar conexion",
                   command=self._test_conn).pack(side="left", padx=(0, 8))
        self._btn = ttk.Button(btn_frame, text="Configurar base de datos",
                               command=self._run_setup)
        self._btn.pack(side="left")

        # ── Log ───────────────────────────────────────────────────────────────
        self._status = tk.Text(
            self._main, height=5, width=56,
            font=("Consolas", 8), state="disabled",
            relief="flat", background="#f5f5f5",
        )
        self._status.grid(row=12, column=0, columnspan=3, sticky="ew", pady=(4, 0))

        self._main.columnconfigure(1, weight=1)
        self._toggle_trusted()

    def _apply_ssms(self):
        raw = self._ssms_var.get().strip()
        if not raw:
            return
        parsed = _parse_ssms_connstr(raw)
        if parsed["server"]:
            self._conn_vars["server"].set(parsed["server"])
        if parsed["database"]:
            self._conn_vars["database"].set(parsed["database"])
        self._trusted_var.set(parsed["trusted"])
        self._toggle_trusted()

    def _toggle_trusted(self):
        trusted = self._trusted_var.get()
        for key in ("username", "password"):
            e = self._conn_entries.get(key)
            if e:
                e.configure(state="disabled" if trusted else "normal")

    def _log(self, msg: str):
        self._status.configure(state="normal")
        self._status.insert("end", msg + "\n")
        self._status.see("end")
        self._status.configure(state="disabled")
        self.root.update_idletasks()
        from loguru import logger as _logger
        if "ERROR" in msg:
            _logger.error(msg)
        elif msg.strip():
            _logger.info(msg)

    def _current_db_cfg(self) -> dict:
        db_cfg = {k: v.get() for k, v in self._conn_vars.items()}
        db_cfg["trusted_connection"] = self._trusted_var.get()
        return db_cfg

    def _test_conn(self):
        import pyodbc
        db_cfg = self._current_db_cfg()
        self._log("Probando conexion...")
        try:
            conn_str = _build_conn_str(db_cfg)
            pyodbc.connect(conn_str, timeout=8).close()
            self._log("  Conexion OK")
        except Exception as e:
            self._log(f"  ERROR: {e}")

    def _run_setup(self):
        import pyodbc

        self._btn.configure(state="disabled")
        self._status.configure(state="normal")
        self._status.delete("1.0", "end")
        self._status.configure(state="disabled")

        db_cfg = self._current_db_cfg()

        # 1. Probar conexion
        self._log("Probando conexion a SQL Server...")
        try:
            conn_str = _build_conn_str(db_cfg)
            pyodbc.connect(conn_str, timeout=8).close()
        except Exception as e:
            self._log(f"ERROR: {e}")
            self._btn.configure(state="normal")
            return
        self._log("  Conexion OK")

        # 2. Guardar config.json
        self._log("Guardando config.json...")
        try:
            CONFIG_FILE.write_text(
                json.dumps({"database": db_cfg}, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
        except PermissionError:
            self._log(
                "ERROR: Sin permisos para escribir config.json.\n"
                "Ejecutar como Administrador."
            )
            self._btn.configure(state="normal")
            return
        self._log("  config.json guardado")

        # 3. Crear tablas
        self._log("Creando tablas en la base de datos...")
        try:
            conn = pyodbc.connect(conn_str)
            conn.autocommit = True
            errors = []
            for batch in _SCHEMA_BATCHES:
                batch = batch.strip()
                if not batch:
                    continue
                try:
                    conn.execute(batch)
                except Exception as e:
                    errors.append(str(e)[:120])
            conn.close()
        except Exception as e:
            self._log(f"ERROR al conectar: {e}")
            self._btn.configure(state="normal")
            return

        if errors:
            for err in errors:
                self._log(f"  Aviso: {err}")
        else:
            self._log("  Tablas creadas correctamente")

        # 4. Generar y guardar claves Fernet
        self._log("Generando claves Fernet...")
        try:
            fernet_key  = Fernet.generate_key().decode()
            session_key = Fernet.generate_key().decode()
            conn = pyodbc.connect(conn_str)
            conn.autocommit = True
            for key, value in [("fernet_key", fernet_key), ("session_key", session_key)]:
                conn.execute("""
                    MERGE system_config AS t
                    USING (SELECT ? AS kc) AS s ON t.key_config = s.kc
                    WHEN NOT MATCHED THEN INSERT (key_config, value_config) VALUES (?, ?)
                    WHEN MATCHED AND t.value_config IN (
                        'REPLACE_WITH_GENERATED_FERNET_KEY', ''
                    ) THEN UPDATE SET value_config = ?, updated_at = GETDATE();
                """, key, key, value, value)
            conn.close()
            self._log("  Claves Fernet generadas y guardadas")
        except Exception as e:
            self._log(f"  Aviso claves Fernet: {e}")

        self._log("")
        self._log("Configuracion completada.")
        self._log("El servicio AtanaDispatcher esta listo para iniciar.")
        messagebox.showinfo(
            "Configuracion completada",
            "La base de datos fue configurada correctamente.\n\n"
            "El servicio AtanaDispatcher esta listo para iniciar.",
        )
        self.root.destroy()


# ── Entry point ───────────────────────────────────────────────────────────────

def run():
    from loguru import logger
    log_dir = ROOT / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    logger.add(
        log_dir / "setup_db.log",
        format="{time:YYYY-MM-DD HH:mm:ss} | {level:<8} | {message}",
        level="DEBUG",
        encoding="utf-8",
    )
    logger.info("=== ATANA Setup DB wizard started ===")
    root = tk.Tk()
    SetupApp(root)
    root.mainloop()
    logger.info("=== ATANA Setup DB wizard closed ===")


if __name__ == "__main__":
    run()
