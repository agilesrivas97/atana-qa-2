#!/usr/bin/env python3
"""
setup_db_cli.py
===============
Asistente interactivo de consola para configurar la base de datos ATANA.

Uso:
    python tools/setup_db_cli.py

Requiere:
    pip install pyodbc cryptography
"""

import json
import os
import re
import secrets
import string
import subprocess
import sys
from typing import Optional

# ── Verificar dependencias ────────────────────────────────────────────────────

def _check_deps():
    missing = []
    try:
        import pyodbc  # noqa: F401
    except ImportError:
        missing.append("pyodbc")
    try:
        from cryptography.fernet import Fernet  # noqa: F401
    except ImportError:
        missing.append("cryptography")
    if missing:
        print(f"\n[ERROR] Dependencias faltantes: {', '.join(missing)}")
        print(f"  Instalar con:  pip install {' '.join(missing)}")
        sys.exit(1)

_check_deps()

import pyodbc
from cryptography.fernet import Fernet

# ── Auto-detección ────────────────────────────────────────────────────────────

def _git_remote_owner_repo() -> tuple[str, str]:
    """Reads owner and repo from 'git remote get-url origin'. Returns ('', '') on failure."""
    try:
        url = subprocess.check_output(
            ["git", "remote", "get-url", "origin"],
            stderr=subprocess.DEVNULL, text=True,
        ).strip()
        # HTTPS: https://github.com/owner/repo.git
        # SSH:   git@github.com:owner/repo.git
        m = re.search(r"github\.com[:/]([^/]+)/([^/]+?)(?:\.git)?$", url)
        if m:
            return m.group(1), m.group(2)
    except Exception:
        pass
    return "", ""


def _detect_sql_instances() -> list[str]:
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
    try:
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


def _probe_trusted(server: str, driver: str) -> bool:
    try:
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


# ── Consola ───────────────────────────────────────────────────────────────────

W = 65

def banner(title: str):
    print("\n" + "=" * W)
    print(f"  {title}")
    print("=" * W)

def section(title: str):
    pad = W - len(title) - 5
    print(f"\n── {title} {'─' * max(pad, 2)}")

def ok(msg: str):   print(f"  [OK]  {msg}")
def warn(msg: str): print(f"  [!!]  {msg}")
def err(msg: str):  print(f"  [ERR] {msg}")


def ask(prompt: str, default: str = "") -> str:
    """Pide un valor; si hay default, Enter lo acepta."""
    label = f"  {prompt} [{default}]: " if default else f"  {prompt}: "
    while True:
        val = input(label).strip()
        if val:
            return val
        if default:
            return default
        print("    (campo requerido)")


def ask_optional(prompt: str, default: str = "") -> str:
    """Como ask() pero acepta vacío."""
    label = f"  {prompt} [{default}] (Enter=omitir): " if default else f"  {prompt} (Enter=omitir): "
    val = input(label).strip()
    return val if val else default


def ask_secret(prompt: str) -> str:
    while True:
        val = input(f"  {prompt}: ").strip()
        if val:
            return val
        print("    (campo requerido)")


def ask_secret_optional(prompt: str) -> str:
    return input(f"  {prompt} (Enter=omitir): ").strip()


def ask_int(prompt: str, default: int, min_v: int = None, max_v: int = None) -> int:
    while True:
        val = input(f"  {prompt} [{default}]: ").strip()
        if not val:
            return default
        try:
            n = int(val)
            if min_v is not None and n < min_v:
                print(f"    (mínimo: {min_v})")
                continue
            if max_v is not None and n > max_v:
                print(f"    (máximo: {max_v})")
                continue
            return n
        except ValueError:
            print("    (debe ser un número entero)")


def ask_yn(prompt: str, default: bool = True) -> bool:
    hint = "S/n" if default else "s/N"
    while True:
        val = input(f"  {prompt} [{hint}]: ").strip().lower()
        if not val:
            return default
        if val in ("s", "si", "sí", "y", "yes"):
            return True
        if val in ("n", "no"):
            return False
        print("    (responder S o N)")


def _skip() -> bool:
    return not ask_yn("¿Configurar este agente ahora?", default=False)

# ── Cifrado ───────────────────────────────────────────────────────────────────

_fernet: Optional[Fernet] = None


def _set_fernet(key: str):
    global _fernet
    _fernet = Fernet(key.encode() if isinstance(key, str) else key)


def enc_str(value: str) -> str:
    """Token Fernet como string (para system_config y campos JSON extra_config)."""
    if not value:
        return ""
    return _fernet.encrypt(value.encode()).decode()


def enc_bytes(value: str) -> bytes:
    """Token Fernet como bytes (para agent_config.password_enc VARBINARY)."""
    if not value:
        return b""
    return _fernet.encrypt(value.encode())

# ── Base de datos ─────────────────────────────────────────────────────────────

_conn_str = ""


def _connect() -> pyodbc.Connection:
    return pyodbc.connect(_conn_str, autocommit=False)


def _test_conn() -> bool:
    try:
        c = pyodbc.connect(_conn_str, autocommit=True)
        c.execute("SELECT 1")
        c.close()
        return True
    except Exception as e:
        err(str(e))
        return False


def _table_exists(conn, table: str) -> bool:
    row = conn.execute(
        "SELECT COUNT(1) FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_NAME = ?", table
    ).fetchone()
    return row[0] > 0


def _upsert_sys(conn, key: str, value: str):
    conn.execute("""
        MERGE system_config AS t
        USING (SELECT ? AS k) AS s ON t.key_config = s.k
        WHEN MATCHED THEN
            UPDATE SET value_config = ?, updated_at = GETDATE()
        WHEN NOT MATCHED THEN
            INSERT (key_config, value_config) VALUES (?, ?);
    """, key, value, key, value)


def _update_agent(conn, provider: str, d: dict):
    # rename_pattern no se toca desde este script: queda vacío por defecto
    # y solo se configura a mano directamente en la base de datos.
    conn.execute("""
        UPDATE agent_config SET
            enabled            = ?,
            username           = ?,
            password_enc       = ?,
            destination_folder = ?,
            max_retries        = ?,
            retry_interval_min = ?,
            portal_url         = ?,
            schedule_hour      = ?,
            schedule_minute    = ?,
            extra_config       = ?,
            updated_at         = GETDATE()
        WHERE provider = ?
    """,
        d.get("enabled", 0),
        d.get("username"),
        d.get("password_enc"),
        d.get("destination_folder"),
        d.get("max_retries", 3),
        d.get("retry_interval_min", 15),
        d.get("portal_url"),
        d.get("schedule_hour"),
        d.get("schedule_minute", 0),
        d.get("extra_config"),
        provider,
    )

# ── Configuradores de agentes ─────────────────────────────────────────────────

def _cfg_prisma(conn):
    section("Agente: PRISMA")
    print("  Auth: usuario + contraseña (portal web via Playwright)")
    if _skip():
        return

    username = ask("Usuario Prisma").replace(" ", "")
    password = ask_secret("Contraseña Prisma")
    dest     = ask("Carpeta de destino", r"C:\Program Files\ATANA\settlements\prisma")
    hour     = ask_int("Hora de ejecución (0-23)", 8, 0, 23)
    minute   = ask_int("Minuto (0-59)", 0, 0, 59)
    enable   = ask_yn("¿Habilitar agente?", True)

    _update_agent(conn, "prisma", {
        "enabled": 1 if enable else 0, "username": username,
        "password_enc": enc_bytes(password), "destination_folder": dest,
        "max_retries": 3, "retry_interval_min": 15,
        "schedule_hour": hour, "schedule_minute": minute,
    })
    conn.commit()
    ok("Prisma guardado")


def _cfg_cabal(conn):
    section("Agente: CABAL")
    print("  Auth: usuario + contraseña (portal web via Playwright)")
    if _skip():
        return

    username = ask("Usuario Cabal").replace(" ", "")
    password = ask_secret("Contraseña Cabal")
    dest     = ask("Carpeta de destino", r"C:\Program Files\ATANA\settlements\cabal")
    hour     = ask_int("Hora de ejecución (0-23)", 8, 0, 23)
    minute   = ask_int("Minuto (0-59)", 5, 0, 59)
    enable   = ask_yn("¿Habilitar agente?", True)

    _update_agent(conn, "cabal", {
        "enabled": 1 if enable else 0, "username": username,
        "password_enc": enc_bytes(password), "destination_folder": dest,
        "max_retries": 3, "retry_interval_min": 15,
        "schedule_hour": hour, "schedule_minute": minute,
    })
    conn.commit()
    ok("Cabal guardado")


def _cfg_naranjax(conn):
    section("Agente: NARANJAX")
    print("  Auth: usuario + contraseña + OTP por email (IMAP)")
    if _skip():
        return

    username  = ask("Usuario NaranjaX").replace(" ", "")
    password  = ask_secret("Contraseña NaranjaX")
    dest      = ask("Carpeta de destino", r"C:\Program Files\ATANA\settlements\naranjax")
    hour      = ask_int("Hora de ejecución (0-23)", 8, 0, 23)
    minute    = ask_int("Minuto (0-59)", 10, 0, 59)

    print("\n  Configuración IMAP para recibir el OTP:")
    imap_host = ask("Host IMAP", "imap.gmail.com")
    imap_user = ask("Email IMAP (el que recibe el OTP)").replace(" ", "")
    imap_pass = ask_secret("Contraseña IMAP / App password de Gmail")
    otp_from  = ask("Email del remitente del OTP", "noreply@naranjax.com").replace(" ", "")

    enable = ask_yn("¿Habilitar agente?", True)

    extra = {
        "imap_host":         imap_host,
        "imap_username":     imap_user,
        "imap_password_enc": enc_str(imap_pass),
        "otp_sender":        otp_from,
    }
    _update_agent(conn, "naranjax", {
        "enabled": 1 if enable else 0, "username": username,
        "password_enc": enc_bytes(password), "destination_folder": dest,
        "max_retries": 2, "retry_interval_min": 30,
        "schedule_hour": hour, "schedule_minute": minute,
        "extra_config": json.dumps(extra, ensure_ascii=False),
    })
    conn.commit()
    ok("NaranjaX guardado")


def _cfg_fiserv(conn):
    section("Agente: FISERV")
    print("  Auth: TOTP automático — sin intervención humana")
    if _skip():
        return

    username    = ask("Usuario (merchantcenter.fiservapp.com)").replace(" ", "")
    password    = ask_secret("Contraseña Fiserv")

    print()
    print("  TOTP Secret: es el shared secret que se escanea con Google Authenticator.")
    print("  Formato base32 — ejemplo: JBSWY3DPEHPK3PXP")
    totp_secret = ask_secret("TOTP shared secret").replace(" ", "")

    dest      = ask("Carpeta de destino", r"C:\Program Files\ATANA\settlements\fiserv")
    hour      = ask_int("Hora de ejecución (0-23)", 8, 0, 23)
    minute    = ask_int("Minuto (0-59)", 15, 0, 59)
    enable    = ask_yn("¿Habilitar agente?", True)

    extra = {
        "auth_mode":       "totp",
        "totp_secret_enc": enc_str(totp_secret),
    }
    _update_agent(conn, "fiserv", {
        "enabled": 1 if enable else 0, "username": username,
        "password_enc": enc_bytes(password), "destination_folder": dest,
        "max_retries": 2, "retry_interval_min": 30,
        "portal_url": "https://merchantcenter.fiservapp.com",
        "schedule_hour": hour, "schedule_minute": minute,
        "extra_config": json.dumps(extra, ensure_ascii=False),
    })
    conn.commit()
    ok("Fiserv guardado")


def _cfg_amex(conn):
    section("Agente: AMEX")
    print("  Auth: usuario + contraseña (portal web via Playwright)")
    if _skip():
        return

    username = ask("Usuario Amex").replace(" ", "")
    password = ask_secret("Contraseña Amex")
    dest     = ask("Carpeta de destino", r"C:\Program Files\ATANA\settlements\amex")
    hour     = ask_int("Hora de ejecución (0-23)", 8, 0, 23)
    minute   = ask_int("Minuto (0-59)", 20, 0, 59)
    enable   = ask_yn("¿Habilitar agente?", True)

    _update_agent(conn, "amex", {
        "enabled": 1 if enable else 0, "username": username,
        "password_enc": enc_bytes(password), "destination_folder": dest,
        "max_retries": 3, "retry_interval_min": 10,
        "schedule_hour": hour, "schedule_minute": minute,
    })
    conn.commit()
    ok("Amex guardado")


def _cfg_getnet(conn):
    section("Agente: GETNET")
    print("  Auth: sesión Playwright + resolución CAPTCHA (2captcha.com)")
    if _skip():
        return

    username    = ask("Usuario Getnet").replace(" ", "")
    password    = ask_secret("Contraseña Getnet")
    captcha_key = ask("API key de 2captcha.com").replace(" ", "")
    dest        = ask("Carpeta de destino", r"C:\Program Files\ATANA\settlements\getnet")
    hour        = ask_int("Hora de ejecución (0-23)", 8, 0, 23)
    minute      = ask_int("Minuto (0-59)", 25, 0, 59)
    enable      = ask_yn("¿Habilitar agente?", True)

    extra = {"captcha_api_key": captcha_key, "mode": "persisted_session"}
    _update_agent(conn, "getnet", {
        "enabled": 1 if enable else 0, "username": username,
        "password_enc": enc_bytes(password), "destination_folder": dest,
        "max_retries": 1, "retry_interval_min": 0,
        "schedule_hour": hour, "schedule_minute": minute,
        "extra_config": json.dumps(extra, ensure_ascii=False),
    })
    conn.commit()
    ok("Getnet guardado")


def _cfg_mercadopago(conn):
    section("Agente: MERCADO PAGO")
    print("  Auth: access token por cuenta (API REST — sin browser)")
    if _skip():
        return

    dest      = ask("Carpeta de destino", r"C:\Program Files\ATANA\settlements\mercadopago")
    hour      = ask_int("Hora de ejecución (0-23)", 8, 0, 23)
    minute    = ask_int("Minuto (0-59)", 30, 0, 59)

    accounts = []
    print("\n  Cuentas de Mercado Pago (podés agregar varias):")
    while True:
        n = len(accounts) + 1
        print(f"\n  Cuenta #{n}:")
        alias = ask("Alias (nombre descriptivo)")
        token = ask_secret("Access token").replace(" ", "")
        accounts.append({"alias": alias, "access_token_enc": enc_str(token)})
        if not ask_yn("  ¿Agregar otra cuenta?", False):
            break

    enable = ask_yn("¿Habilitar agente?", True)

    extra = {
        "accounts":         accounts,
        "timezone":         "GMT-03",
        "separator":        ";",
        "poll_interval_seg": 2,
        "poll_timeout_seg": 300,
    }
    _update_agent(conn, "mercadopago", {
        "enabled": 1 if enable else 0,
        "username": None, "password_enc": None,
        "destination_folder": dest,
        "max_retries": 5, "retry_interval_min": 2,
        "schedule_hour": hour, "schedule_minute": minute,
        "extra_config": json.dumps(extra, ensure_ascii=False),
    })
    conn.commit()
    ok("Mercado Pago guardado")

# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    if sys.platform == "win32":
        os.system("")  # habilitar ANSI en Windows 10+

    banner("ATANA — Asistente de configuración de base de datos")
    print("\n  Este script carga la configuración en una instalación nueva.")
    print("  Requisito: schema.sql ya aplicado sobre la base de datos.")
    print("  Podés ejecutarlo de nuevo en cualquier momento para actualizar.")
    print("\n  Ctrl+C cancela sin guardar el paso actual.\n")

    global _conn_str

    # ── 1. Conexión ───────────────────────────────────────────────────────────
    section("Conexión a SQL Server")

    # Cadena SSMS opcional — pre-rellena servidor, database y modo de auth
    ssms_raw = ask_optional("Cadena de conexión SSMS (Enter para saltar)")
    ssms = _parse_ssms_connstr(ssms_raw) if ssms_raw.strip() else {}

    # Servidor — mostrar instancias detectadas
    instances = _detect_sql_instances()
    ssms_server = ssms.get("server", "")
    if instances:
        print(f"\n  Instancias SQL Server detectadas:")
        for idx, inst in enumerate(instances, 1):
            tag = "  <- recomendado" if idx == 1 else ""
            print(f"    [{idx}] {inst}{tag}")
        print(f"    [0] Ingresar manualmente")
        raw_choice = input(
            f"  Elegí una opción [1]: "
        ).strip()
        if raw_choice == "0":
            server = ask("Servidor", ssms_server or instances[0])
        elif raw_choice.isdigit() and 1 <= int(raw_choice) <= len(instances):
            server = instances[int(raw_choice) - 1]
        else:
            server = ssms_server or instances[0]
    else:
        server = ask("Servidor", ssms_server or r"localhost\SQLEXPRESS")

    database = ask("Base de datos", ssms.get("database", "atana"))

    # Driver ODBC — mostrar solo los disponibles
    drivers = _detect_odbc_drivers()
    if len(drivers) > 1:
        print(f"\n  Drivers ODBC disponibles:")
        for idx, drv in enumerate(drivers, 1):
            tag = "  <- recomendado" if idx == 1 else ""
            print(f"    [{idx}] {drv}{tag}")
        raw_drv = input(f"  Elegí un driver [1]: ").strip()
        if raw_drv.isdigit() and 1 <= int(raw_drv) <= len(drivers):
            selected_driver = drivers[int(raw_drv) - 1]
        else:
            selected_driver = drivers[0]
    else:
        selected_driver = drivers[0]

    # Windows Auth — intentar auto-detectar
    ssms_trusted = ssms.get("trusted", None)
    if ssms_trusted is None:
        print(f"\n  Probando Windows Auth con {server}...", end="", flush=True)
        win_auth_ok = _probe_trusted(server, selected_driver)
        print(" OK" if win_auth_ok else " no disponible")
        trusted = ask_yn("¿Usar Windows Authentication?", win_auth_ok)
    else:
        trusted = ask_yn("¿Usar Windows Authentication?", ssms_trusted)

    if trusted:
        _conn_str = (
            f"DRIVER={{{selected_driver}}};"
            f"SERVER={server};DATABASE={database};"
            f"Trusted_Connection=yes;TrustServerCertificate=yes;"
        )
    else:
        sql_user = ask("Usuario SQL Server")
        sql_pass = ask_secret("Contraseña SQL Server")
        _conn_str = (
            f"DRIVER={{{selected_driver}}};"
            f"SERVER={server};DATABASE={database};"
            f"UID={sql_user};PWD={sql_pass};TrustServerCertificate=yes;"
        )

    print("  Conectando...", end="", flush=True)
    if not _test_conn():
        # Fallback al driver genérico si el seleccionado falla
        fallback = "SQL Server"
        if selected_driver != fallback and fallback in set(pyodbc.drivers()):
            _conn_str = _conn_str.replace(selected_driver, fallback)
            print(f"\n  Reintentando con driver '{fallback}'...", end="", flush=True)
            if not _test_conn():
                print()
                err("No se pudo conectar. Verifica servidor, base de datos y credenciales.")
                sys.exit(1)
        else:
            print()
            err("No se pudo conectar. Verifica servidor, base de datos y credenciales.")
            sys.exit(1)
    print(" OK")

    conn = _connect()

    if not _table_exists(conn, "system_config"):
        err("Tabla system_config no encontrada.")
        err("Ejecuta schema.sql primero y vuelve a correr este script.")
        conn.close()
        sys.exit(1)
    ok("Tablas encontradas")

    # ── 2. Fernet keys ────────────────────────────────────────────────────────
    section("Claves de cifrado Fernet")

    existing_fk = None
    row = conn.execute(
        "SELECT value_config FROM system_config WHERE key_config='fernet_key'"
    ).fetchone()
    if row and row[0] and not row[0].startswith("REPLACE_"):
        existing_fk = row[0]

    if existing_fk:
        print("  Ya existe una fernet_key en la base de datos.")
        regen = ask_yn(
            "  ¿Generar claves nuevas? (ATENCIÓN: invalidará las credenciales guardadas)",
            False,
        )
        if regen:
            fernet_key  = Fernet.generate_key().decode()
            session_key = Fernet.generate_key().decode()
            print(f"\n  fernet_key  = {fernet_key}")
            print(f"  session_key = {session_key}")
            warn("Guardá estas claves — sin ellas no se descifran las credenciales.")
        else:
            fernet_key  = existing_fk
            row2 = conn.execute(
                "SELECT value_config FROM system_config WHERE key_config='session_key'"
            ).fetchone()
            session_key = (
                row2[0]
                if row2 and row2[0] and not row2[0].startswith("REPLACE_")
                else Fernet.generate_key().decode()
            )
            ok("Usando claves existentes")
    else:
        fernet_key  = Fernet.generate_key().decode()
        session_key = Fernet.generate_key().decode()
        print(f"\n  fernet_key  = {fernet_key}")
        print(f"  session_key = {session_key}")
        warn("Guardá estas claves — sin ellas no se descifran las credenciales.")

    _set_fernet(fernet_key)
    _upsert_sys(conn, "fernet_key",  fernet_key)
    _upsert_sys(conn, "session_key", session_key)
    conn.commit()
    ok("Claves guardadas")

    # ── 3. Sistema ────────────────────────────────────────────────────────────
    section("Configuración del sistema")

    log_dir  = ask("Directorio de logs", r"C:\Program Files\ATANA\logs").strip("'\"")
    api_port = ask_int("Puerto API REST", 8765, 1024, 65535)
    interval = ask_int("Intervalo de chequeo de trabajos (minutos)", 5, 1, 60)
    debug    = ask_yn("¿Modo debug?", False)

    # api_key — generar si no existe
    row = conn.execute(
        "SELECT value_config FROM system_config WHERE key_config='api_key'"
    ).fetchone()
    existing_ak = row[0] if row and row[0] and not row[0].startswith("REPLACE_") else None

    if existing_ak:
        if ask_yn("  Ya existe una api_key. ¿Generar nueva?", False):
            api_key = secrets.token_urlsafe(30)
            print(f"  api_key = {api_key}")
        else:
            api_key = existing_ak
            ok("Usando api_key existente")
    else:
        api_key = secrets.token_urlsafe(30)
        print(f"\n  api_key generada: {api_key}")
        warn("Esta clave la usa la API REST de ATANA. Guardala.")

    _upsert_sys(conn, "log_dir",  log_dir)
    _upsert_sys(conn, "api_port", str(api_port))
    _upsert_sys(conn, "check_jobs_interval_min", str(interval))
    _upsert_sys(conn, "debug",    "true" if debug else "false")
    _upsert_sys(conn, "api_key",  api_key)
    conn.commit()
    ok("Sistema guardado")

    # ── 4. GitHub auto-update ─────────────────────────────────────────────────
    section("GitHub Auto-Update (opcional)")
    print("  Permite que ATANA se actualice automáticamente desde GitHub Releases.")

    if ask_yn("¿Configurar?", False):
        _git_owner, _git_repo = _git_remote_owner_repo()
        owner        = ask("GitHub owner / organización", _git_owner)
        repo         = ask("Repositorio", _git_repo)
        token        = ask_secret_optional("GitHub PAT (Personal Access Token)")
        update_hours = ask_int("Verificar updates cada N horas", 6, 1, 24)

        _upsert_sys(conn, "github_owner", owner)
        _upsert_sys(conn, "github_repo",  repo)
        _upsert_sys(conn, "check_update_interval_hours", str(update_hours))
        if token:
            _upsert_sys(conn, "github_token_enc", enc_str(token))
        conn.commit()
        ok("Auto-update guardado")

    # ── 5. SMTP ───────────────────────────────────────────────────────────────
    section("Notificaciones SMTP (opcional)")

    if ask_yn("¿Configurar notificaciones por email?", False):
        smtp_host = ask("Host SMTP", "smtp.gmail.com")
        smtp_port = ask_int("Puerto SMTP", 587, 1, 65535)
        smtp_user = ask("Email remitente")
        smtp_pass = ask_secret("Contraseña / App password")
        smtp_to   = ask("Email destinatario")

        _upsert_sys(conn, "smtp_host",         smtp_host)
        _upsert_sys(conn, "smtp_port",         str(smtp_port))
        _upsert_sys(conn, "smtp_username",     smtp_user)
        _upsert_sys(conn, "smtp_password_enc", enc_str(smtp_pass))
        _upsert_sys(conn, "smtp_recipient",    smtp_to)
        _upsert_sys(conn, "smtp_enabled",      "true")
        conn.commit()
        ok("SMTP guardado")

    # ── 6. Agentes ────────────────────────────────────────────────────────────
    banner("Configuración de agentes")
    print("  Configurá solo los agentes que necesitás. Podés volver")
    print("  a ejecutar el script en cualquier momento para completar el resto.\n")

    # Solo mostramos los agentes habilitados para configuración manual en este asistente.
    # Los demás agentes siguen existiendo en el sistema pero están "ocultos" aquí.
    for fn in (_cfg_fiserv, _cfg_mercadopago):
        fn(conn)

    # ── 7. Resumen ────────────────────────────────────────────────────────────
    banner("Resumen final")

    rows = conn.execute("""
        SELECT provider,
               enabled,
               ISNULL(username, 'N/A')                                        AS username,
               CASE WHEN password_enc IS NOT NULL THEN 'cifrada' ELSE '---' END AS pwd,
               ISNULL(destination_folder, '---')                              AS dest,
               schedule_hour, schedule_minute
        FROM   agent_config
        ORDER  BY schedule_hour, schedule_minute
    """).fetchall()

    print(f"\n  {'Agente':<14} {'Estado':<13} {'Usuario':<24} {'Pass':<8} Horario")
    print("  " + "-" * 68)
    for r in rows:
        prov, enabled, user, pwd, dest, hour, minute = r
        estado  = "HABILITADO  " if enabled else "deshabilitado"
        horario = f"{hour:02d}:{minute:02d}" if hour is not None else "manual"
        print(f"  {prov:<14} {estado:<13} {user[:23]:<24} {pwd:<8} {horario}")

    conn.close()

    banner("¡Listo!")
    print(f"\n  Logs          → {log_dir}")
    print(f"  API REST      → http://localhost:{api_port}")
    print("\n  Próximos pasos:")
    print("  1. Reiniciá el servicio ATANA (services.msc)")
    print("  2. Verificá que los agentes habilitados aparezcan en el tray")
    print("  3. Para modificar la config, volvé a ejecutar este script\n")
    input("\n  Presiona Enter para salir...")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n\n  Cancelado por el usuario.\n")
        sys.exit(0)
    except Exception as e:
        import traceback
        print(f"\n\n  [ERROR INESPERADO] {e}\n")
        traceback.print_exc()
        input("\n  Presiona Enter para salir...")
        sys.exit(1)
