
from __future__ import annotations
import json
import secrets
import time
from contextlib import contextmanager
from datetime import datetime
from typing import Optional

import pyodbc
from cryptography.fernet import Fernet, InvalidToken
from loguru import logger

from dispatcher.database_factory import build_connection_args


# ── Connection ────────────────────────────────────────────────────────────────

_connection_args: dict = {}

# ── Fernet encryption helpers ─────────────────────────────────────────────────

_fernet: Optional[Fernet] = None


def _get_fernet() -> Optional[Fernet]:
    """Returns a Fernet instance using the key stored in system_config."""
    global _fernet
    if _fernet is None:
        try:
            with connect() as conn:
                row = conn.execute(
                    "SELECT value_config FROM system_config WHERE key_config = 'fernet_key'"
                ).fetchone()
            if row and row[0]:
                _fernet = Fernet(row[0].encode())
                logger.debug("[db] Fernet key loaded from system_config")
            else:
                logger.warning("[db] fernet_key not found in system_config — encrypted fields will return empty")
        except Exception as e:
            logger.warning(f"Could not load Fernet key from system_config: {e}")
    return _fernet


def _decrypt(data: bytes) -> str:
    """Decrypts Fernet-encrypted bytes. Returns empty string if key missing or invalid."""
    if not data:
        return ""
    f = _get_fernet()
    if not f:
        logger.debug("[db] _decrypt: no Fernet key available — returning empty")
        return ""
    try:
        return f.decrypt(data).decode()
    except InvalidToken:
        logger.warning("[db] _decrypt: InvalidToken — wrong Fernet key or corrupted value")
        return ""
    except Exception as e:
        logger.warning(f"[db] _decrypt: unexpected error — {e}")
        return ""


def encrypt_value(value: str) -> bytes:
    """Encrypts a string with Fernet. Use this when saving credentials to the DB."""
    if not value:
        return b""
    f = _get_fernet()
    if not f:
        raise RuntimeError("Fernet key not available — cannot encrypt")
    return f.encrypt(value.encode())


def _resolve_driver(preferred: str) -> str:
    available = pyodbc.drivers()
    if preferred in available:
        return preferred
    for candidate in [
        "ODBC Driver 18 for SQL Server",
        "ODBC Driver 17 for SQL Server",
        "ODBC Driver 13 for SQL Server",
        "SQL Server Native Client 11.0",
        "SQL Server",
    ]:
        if candidate in available and candidate != preferred:
            logger.warning(f"[db] Driver '{preferred}' not found — using '{candidate}'")
            return candidate
    return preferred


def _wait_for_sql(retries: int = 10, delay: int = 10):
    """Retries the SQL Server connection until it succeeds or retries are exhausted.

    Handles the case where Windows starts ATANA before SQL Server is ready.
    With retries=10 and delay=10s the service waits up to ~100s before giving up.
    """
    for attempt in range(1, retries + 1):
        try:
            with connect() as conn:
                conn.execute("SELECT 1")
            logger.info(f"SQL Server connection OK (attempt {attempt})")
            return
        except Exception as e:
            if attempt < retries:
                logger.warning(
                    f"SQL Server not ready (attempt {attempt}/{retries}) — "
                    f"retrying in {delay}s... ({e})"
                )
                time.sleep(delay)
            else:
                logger.error(f"SQL Server unreachable after {retries} attempts: {e}")
                raise


def init(config: dict):
    """Initialize the SQL Server connection."""
    global _connection_args
    db = config["database"]

    driver = _resolve_driver(db.get("driver", "ODBC Driver 18 for SQL Server"))

    _connection_args = build_connection_args(db, driver)

    _wait_for_sql()


@contextmanager
def connect():
    """Context manager for connections. Always closes the connection."""
    conn_str = _connection_args.get("conn_str", "")
    uid = _connection_args.get("uid")
    pwd = _connection_args.get("pwd")
    
    if uid and pwd:
        conn = pyodbc.connect(conn_str, uid=uid, pwd=pwd, autocommit=False)
    else:
        conn = pyodbc.connect(conn_str, autocommit=False)

    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ── Jobs ──────────────────────────────────────────────────────────────────────

def create_batch(agents: list[str], started_by: str = "api") -> int:
    """Inserta un job orquestador. El dispatcher lo procesa via process_batches()."""
    agents_csv = ",".join(agents)
    with connect() as conn:
        row = conn.execute("""
            INSERT INTO orchestrator_agent_jobs (agents, started_by)
            OUTPUT INSERTED.id
            VALUES (?, ?)
        """, agents_csv, started_by).fetchone()
    return row[0]


def get_pending_jobs() -> list[dict]:
    """Returns all jobs in pending or authorized status."""
    with connect() as conn:
        rows = conn.execute("""
            SELECT id, provider, status, attempts, max_retries,
                   requested_at, started_by, intervention_reason
            FROM agent_jobs
            WHERE status IN ('pending', 'authorized')
            ORDER BY requested_at ASC
        """).fetchall()

    return [_row_to_dict(r) for r in rows]


def get_intervention_jobs() -> list[dict]:
    """Returns jobs waiting for human intervention."""
    with connect() as conn:
        rows = conn.execute("""
            SELECT id, provider, intervention_reason, requested_at
            FROM agent_jobs
            WHERE status = 'requires_intervention'
            ORDER BY requested_at ASC
        """).fetchall()

    return [_row_to_dict(r) for r in rows]


def get_intervention_job(provider: str) -> dict | None:
    """Returns the active requires_intervention job for a provider, or None."""
    with connect() as conn:
        row = conn.execute("""
            SELECT id, provider, intervention_reason, requested_at
            FROM agent_jobs
            WHERE provider = ? AND status = 'requires_intervention'
        """, provider).fetchone()
    return _row_to_dict(row) if row else None


def create_job(provider: str, started_by: str = "scheduler", max_retries: int = 3) -> int:
    """Creates a new job only if there is no active job for that provider."""
    with connect() as conn:
        existing = conn.execute("""
            SELECT COUNT(1) FROM agent_jobs
            WHERE provider = ?
              AND status IN ('pending', 'running', 'authorized', 'requires_intervention')
        """, provider).fetchone()[0]

        if existing:
            logger.info(f"[{provider}] Already has an active job — skipping creation")
            return -1

        row = conn.execute("""
            INSERT INTO agent_jobs (provider, status, requested_at, started_by, max_retries)
            OUTPUT INSERTED.id
            VALUES (?, 'pending', GETDATE(), ?, ?)
        """, provider, started_by, max_retries).fetchone()
    return row[0]


def update_job_started(job_id: int, period_from: datetime = None, period_to: datetime = None):
    with connect() as conn:
        conn.execute("""
            UPDATE agent_jobs
            SET status = 'running', started_at = GETDATE(),
                period_from = ?,
                period_to = ?
            WHERE id = ?
        """, period_from, period_to, job_id)


def update_job_success(job_id: int, files_downloaded: int = 0):
    with connect() as conn:
        conn.execute("""
            UPDATE agent_jobs
            SET status = 'ok', finished_at = GETDATE(),
                files_downloaded = ?
            WHERE id = ?
        """, files_downloaded, job_id)
        
def get_provider_last_period_end(provider: str) -> Optional[datetime]:
    with connect() as conn:
        row = conn.execute("""
            SELECT TOP 1 period_to FROM agent_jobs
            WHERE provider = ? AND status = 'ok' AND period_to IS NOT NULL
            ORDER BY period_to DESC
        """, provider).fetchone()
    return row[0] if row else None


def update_job_error(job_id: int, error_msg: str):
    with connect() as conn:
        conn.execute("""
            UPDATE agent_jobs
            SET status = 'error',
                error_msg = ?,
                finished_at = GETDATE(),
                attempts = attempts + 1
            WHERE id = ?
        """, error_msg[:500], job_id)


def update_job_intervention(job_id: int, reason: str):
    with connect() as conn:
        conn.execute("""
            UPDATE agent_jobs
            SET status = 'requires_intervention',
                intervention_reason = ?,
                finished_at = GETDATE()
            WHERE id = ?
        """, reason[:200], job_id)


def authorize_job(provider: str):
    """User clicks Play — authorizes the intervention-pending job."""
    with connect() as conn:
        conn.execute("""
            UPDATE agent_jobs
            SET status = 'authorized', authorized_at = GETDATE()
            WHERE provider = ?
              AND status = 'requires_intervention'
        """, provider)


def ignore_job(provider: str):
    """Ignore the intervention job for today."""
    with connect() as conn:
        conn.execute("""
            UPDATE agent_jobs
            SET status = 'ignored'
            WHERE provider = ?
              AND status = 'requires_intervention'
        """, provider)


def increment_attempts(job_id: int):
    with connect() as conn:
        conn.execute("""
            UPDATE agent_jobs
            SET attempts = attempts + 1
            WHERE id = ?
        """, job_id)


# ── Downloaded files ──────────────────────────────────────────────────────────

def insert_file(job_id: int, provider: str, data: dict) -> int:
    """Records a downloaded file."""
    with connect() as conn:
        row = conn.execute("""
            INSERT INTO downloaded_files
                (job_id, provider, original_name, final_name,
                 local_path, file_date, bytes, sha256)
            OUTPUT INSERTED.id
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
            job_id,
            provider,
            data.get("original_name", ""),
            data.get("final_name", ""),
            data.get("local_path", ""),
            data.get("file_date"),
            data.get("bytes", 0),
            data.get("sha256"),
        ).fetchone()
    return row[0]


def already_downloaded(provider: str, original_name: str) -> bool:
    """Checks if a file was already downloaded previously."""
    with connect() as conn:
        row = conn.execute("""
            SELECT COUNT(1) FROM downloaded_files
            WHERE provider = ? AND original_name = ?
        """, provider, original_name).fetchone()
    return row[0] > 0


# ── Agent status ──────────────────────────────────────────────────────────────

def update_status(provider: str, data: dict):
    """Inserts or updates the status of an agent (UPSERT)."""
    with connect() as conn:
        conn.execute("""
            MERGE agent_status AS target
            USING (SELECT ? AS provider) AS source ON target.provider = source.provider
            WHEN MATCHED THEN
                UPDATE SET
                    last_run       = GETDATE(),
                    last_result    = ?,
                    last_error     = ?,
                    files_today    = ?,
                    session_active = ?,
                    next_run       = ?,
                    updated_at     = GETDATE()
            WHEN NOT MATCHED THEN
                INSERT (provider, last_run, last_result, last_error, files_today, session_active, next_run, active, updated_at)
                VALUES (?, GETDATE(), ?, ?, ?, ?, ?, 1, GETDATE());
        """,
            provider,
            data.get("result", "ok"),
            data.get("error"),
            data.get("files_today", 0),
            1 if data.get("session_active") else 0,
            data.get("next_run"),
            # WHEN NOT MATCHED values
            provider,
            data.get("result", "ok"),
            data.get("error"),
            data.get("files_today", 0),
            1 if data.get("session_active") else 0,
            data.get("next_run"),
        )


def get_all_status() -> list[dict]:
    """
    Status of all agents for the UI.
    Falls back to agent_jobs if agent_status has no run data yet.
    files_today is counted directly from downloaded_files for accuracy.
    """
    with connect() as conn:
        rows = conn.execute("""
            SELECT
                s.provider,
                s.current_version,
                COALESCE(s.last_run, j.finished_at)   AS last_run,
                COALESCE(s.last_result, j.status)     AS last_result,
                COALESCE(s.last_error, j.error_msg)   AS last_error,
                (
                    SELECT COUNT(*) FROM downloaded_files df
                    WHERE df.provider = s.provider
                      AND CAST(df.downloaded_at AS DATE) = CAST(GETDATE() AS DATE)
                )                                     AS files_today,
                s.session_active,
                s.next_run,
                s.active,
                j.finished_at                         AS last_job_at,
                j.status                              AS last_job_status
            FROM agent_status s
            LEFT JOIN (
                SELECT provider, status, finished_at, error_msg,
                       ROW_NUMBER() OVER (
                           PARTITION BY provider
                           ORDER BY finished_at DESC
                       ) AS rn
                FROM agent_jobs
                WHERE status IN ('ok', 'error', 'requires_intervention', 'ignored')
                  AND finished_at IS NOT NULL
            ) j ON j.provider = s.provider AND j.rn = 1
            ORDER BY s.provider
        """).fetchall()
    return [_row_to_dict(r) for r in rows]


def update_version(provider: str, version: str):
    with connect() as conn:
        conn.execute("""
            UPDATE agent_status SET current_version = ?
            WHERE provider = ?
        """, version, provider)


# ── System config ─────────────────────────────────────────────────────────────

def get_system_config() -> dict:
    """
    Returns all rows from system_config as a flat {key: value} dict.
    Values with an '_enc' suffix are automatically decrypted.
    """
    with connect() as conn:
        rows = conn.execute("SELECT key_config, value_config FROM system_config").fetchall()

    result = {}
    for key, value in rows:
        if key.endswith("_enc"):
            plain_key = key[:-4]          # strip '_enc'
            try:
                result[plain_key] = _decrypt(value.encode() if isinstance(value, str) else value)
            except Exception:
                result[plain_key] = ""
        else:
            result[key] = value
    return result


def set_system_config(key: str, value: str):
    """Inserts or updates a key in system_config."""
    with connect() as conn:
        conn.execute("""
            MERGE system_config AS target
            USING (SELECT ? AS key_config) AS source ON target.key_config = source.key_config
            WHEN MATCHED THEN
                UPDATE SET value_config = ?, updated_at = GETDATE()
            WHEN NOT MATCHED THEN
                INSERT (key_config, value_config) VALUES (?, ?);
        """, key, value, key, value)


# Logical (plaintext) keys that must be encrypted before being written to
# system_config, and are stored under '<key>_enc'. Keep in sync with the
# '_enc' columns documented in README.md's system_config table.
_SYSTEM_SECRET_KEYS = {"smtp_password", "github_token"}

# Master keys are never written through update_system_config — they have
# their own rotate_* functions because changing them requires re-encrypting
# (or invalidating) everything protected by the old value.
_SYSTEM_PROTECTED_KEYS = {"fernet_key", "session_key", "api_key"}


def get_system_config_masked() -> dict:
    """
    Same shape as get_system_config(), but NEVER decrypts. Secret keys
    (suffix '_enc') come back as booleans '<key>_set' instead of the value.
    Safe to expose over the API.
    """
    with connect() as conn:
        rows = conn.execute("SELECT key_config, value_config FROM system_config").fetchall()

    result = {}
    for key, value in rows:
        if key.endswith("_enc"):
            result[f"{key[:-4]}_set"] = bool(value)
        else:
            result[key] = value
    return result


def update_system_config(fields: dict) -> None:
    """
    Bulk-updates system_config from plaintext logical keys (e.g. 'smtp_password',
    'check_jobs_interval_min'). Keys in _SYSTEM_SECRET_KEYS are encrypted here
    and stored as '<key>_enc'; everything else is written as-is.
    Master keys (fernet_key/session_key/api_key) are rejected — use
    rotate_fernet_key() / rotate_session_key() / rotate_api_key() instead.
    """
    for key in fields:
        if key in _SYSTEM_PROTECTED_KEYS:
            raise ValueError(f"'{key}' cannot be set directly — use the rotate_* functions")

    for key, value in fields.items():
        if key in _SYSTEM_SECRET_KEYS:
            set_system_config(f"{key}_enc", encrypt_value(value).decode() if value else "")
        else:
            set_system_config(key, value)


def set_tray_heartbeat(pid: int) -> None:
    """
    Records the tray's last-alive signal. Read back by dispatcher/main.py's
    _tray_watchdog_check() to detect a tray that's still running but hung.
    """
    set_system_config("tray_heartbeat", datetime.now().isoformat())
    set_system_config("tray_pid", str(pid))


def rotate_api_key() -> str:
    """Generates and persists a new local API key (dispatcher <-> tray/panel). Returns it."""
    new_key = secrets.token_urlsafe(30)
    set_system_config("api_key", new_key)
    return new_key


def rotate_fernet_key() -> dict:
    """
    Generates a new fernet_key and re-encrypts everything protected by the
    current one: agent_config.password_enc, the '_enc' sub-fields (and
    mercadopago's accounts[].access_token_enc) inside agent_config.extra_config,
    and every '_enc' key in system_config.

    Runs as a single DB transaction: every row is decrypted with the OLD key
    and re-encrypted with the NEW key in memory first, and the new fernet_key
    is only written to system_config as the very last statement. If any row
    fails to decrypt (wrong/corrupted old key), the exception propagates and
    connect() rolls back the whole transaction — the old key stays in effect
    and nothing is left half-migrated.
    """
    global _fernet

    with connect() as conn:
        row = conn.execute(
            "SELECT value_config FROM system_config WHERE key_config = 'fernet_key'"
        ).fetchone()
        if not row or not row[0]:
            raise RuntimeError("No fernet_key configured — nothing to rotate")

        old_f   = Fernet(row[0].encode())
        new_key = Fernet.generate_key()
        new_f   = Fernet(new_key)

        agents_updated = 0
        agent_rows = conn.execute(
            "SELECT provider, password_enc, extra_config FROM agent_config"
        ).fetchall()

        for provider, password_enc, extra_config in agent_rows:
            new_password_enc = None
            if password_enc:
                raw = password_enc if isinstance(password_enc, bytes) else bytes(password_enc)
                new_password_enc = new_f.encrypt(old_f.decrypt(raw))

            new_extra = None
            if extra_config:
                try:
                    extra = json.loads(extra_config)
                except (json.JSONDecodeError, TypeError):
                    extra = {}

                for k, v in list(extra.items()):
                    if k.endswith("_enc") and v:
                        raw = v.encode() if isinstance(v, str) else v
                        extra[k] = new_f.encrypt(old_f.decrypt(raw)).decode()
                    elif k == "accounts" and isinstance(v, list):
                        for acc in v:
                            token_enc = acc.get("access_token_enc")
                            if token_enc:
                                raw = token_enc.encode() if isinstance(token_enc, str) else token_enc
                                acc["access_token_enc"] = new_f.encrypt(old_f.decrypt(raw)).decode()
                new_extra = json.dumps(extra)

            conn.execute(
                "UPDATE agent_config SET password_enc = ?, extra_config = ? WHERE provider = ?",
                new_password_enc, new_extra, provider,
            )
            agents_updated += 1

        sys_updated = 0
        sys_rows = conn.execute("SELECT key_config, value_config FROM system_config").fetchall()
        for key_config, value_config in sys_rows:
            if not key_config.endswith("_enc") or not value_config:
                continue
            raw = value_config.encode() if isinstance(value_config, str) else value_config
            new_value = new_f.encrypt(old_f.decrypt(raw)).decode()
            conn.execute(
                "UPDATE system_config SET value_config = ?, updated_at = GETDATE() WHERE key_config = ?",
                new_value, key_config,
            )
            sys_updated += 1

        # Written LAST — if anything above raised, this never runs and the
        # whole transaction (including the UPDATEs above) rolls back.
        conn.execute(
            "UPDATE system_config SET value_config = ?, updated_at = GETDATE() WHERE key_config = 'fernet_key'",
            new_key.decode(),
        )

    # Committed successfully — drop the cached Fernet instance so the next
    # _get_fernet() call reloads and picks up the new key.
    _fernet = None

    return {"agents_updated": agents_updated, "system_keys_updated": sys_updated}


def rotate_session_key() -> dict:
    """
    Generates a new session_key. Browser sessions in session_store are
    ephemeral and already re-creatable by design (agents re-login whenever a
    session is missing/invalid — see session_store.py), so instead of
    re-encrypting them in place we simply invalidate every existing session
    and let agents recapture them on their next run under the new key.

    Returns the new key under 'new_key' so the caller (dispatcher/api.py) can
    immediately refresh shared.session_store's in-memory Fernet instance —
    NEVER expose 'new_key' in an HTTP response.
    """
    new_key = Fernet.generate_key().decode()
    with connect() as conn:
        result = conn.execute("UPDATE session_store SET valid = 0")
        sessions_invalidated = result.rowcount if result.rowcount is not None else 0
        conn.execute(
            "UPDATE system_config SET value_config = ?, updated_at = GETDATE() WHERE key_config = 'session_key'",
            new_key,
        )
    return {"sessions_invalidated": sessions_invalidated, "new_key": new_key}


# ── Agent config ──────────────────────────────────────────────────────────────

def _decode_agent_row(row: dict) -> dict:
    """
    Decrypts password_enc → password and merges extra_config JSON into the row.
    Returns the enriched dict that agents receive as their config.
    """
    cfg = dict(row)

    # Decrypt top-level password
    if cfg.get("password_enc"):
        raw = cfg["password_enc"] if isinstance(cfg["password_enc"], bytes) else bytes(cfg["password_enc"])
        decrypted_pwd = _decrypt(raw)
        cfg["password"] = decrypted_pwd
    cfg.pop("password_enc", None)

    # Parse and merge extra_config JSON
    extra_raw = cfg.pop("extra_config", None)
    if extra_raw:
        try:
            extra = json.loads(extra_raw)
        except (json.JSONDecodeError, TypeError):
            extra = {}

        # Decrypt any _enc fields inside extra_config
        decrypted_extra = {}
        for k, v in extra.items():
            if k.endswith("_enc") and isinstance(v, (str, bytes)):
                plain_key = k[:-4]
                raw = v.encode() if isinstance(v, str) else v
                decrypted = _decrypt(raw)
                decrypted_extra[plain_key] = decrypted
            elif k == "accounts" and isinstance(v, list):
                # mercadopago accounts — decrypt each access_token_enc
                accounts = []
                for acc in v:
                    acc = dict(acc)
                    if "access_token_enc" in acc:
                        raw = acc.pop("access_token_enc")
                        raw = raw.encode() if isinstance(raw, str) else raw
                        acc["access_token"] = _decrypt(raw)
                    accounts.append(acc)
                decrypted_extra["accounts"] = accounts
            else:
                decrypted_extra[k] = v

        cfg.update(decrypted_extra)

    return cfg


def get_agent_config(provider: str) -> Optional[dict]:
    """Returns the decrypted config dict for a single provider, or None if not found."""
    with connect() as conn:
        row = conn.execute("""
            SELECT provider, enabled, username, password_enc, destination_folder,
                   rename_pattern, max_retries, retry_interval_min, portal_url,
                   schedule_hour, schedule_minute, extra_config
            FROM agent_config
            WHERE provider = ?
        """, provider).fetchone()
    if not row:
        return None
    return _decode_agent_row(_row_to_dict(row))


def get_all_agent_configs() -> list[dict]:
    """Returns decrypted config dicts for all agents."""
    with connect() as conn:
        rows = conn.execute("""
            SELECT provider, enabled, username, password_enc, destination_folder,
                   rename_pattern, max_retries, retry_interval_min, portal_url,
                   schedule_hour, schedule_minute, extra_config
            FROM agent_config
            ORDER BY provider
        """).fetchall()
    return [_decode_agent_row(_row_to_dict(r)) for r in rows]


_AGENT_CONFIG_COLUMNS = """
    provider, enabled, username, password_enc, destination_folder,
    rename_pattern, max_retries, retry_interval_min, portal_url,
    schedule_hour, schedule_minute, extra_config
"""

# Columns updatable as-is (no encryption, no extra_config merge) via update_agent_config.
_AGENT_TOP_LEVEL_COLUMNS = {
    "enabled", "username", "destination_folder", "rename_pattern",
    "max_retries", "retry_interval_min", "portal_url",
    "schedule_hour", "schedule_minute",
}

# Logical extra_config field names that must be encrypted (stored as '<field>_enc').
# Extend this when a new agent introduces a new secret field.
_AGENT_SECRET_EXTRA_FIELDS = {
    "fiserv":      {"totp_secret"},
    "naranjax":    {"imap_password"},
    "mercadopago": set(),  # handled via the 'accounts' special-case below
}


def _mask_agent_row(row: dict) -> dict:
    """
    Like _decode_agent_row, but NEVER decrypts — secret fields become
    booleans ('<field>_set') instead of the value. Safe to expose over the API.
    """
    cfg = dict(row)

    cfg["has_password"] = bool(cfg.get("password_enc"))
    cfg.pop("password_enc", None)

    extra_raw = cfg.pop("extra_config", None)
    if extra_raw:
        try:
            extra = json.loads(extra_raw)
        except (json.JSONDecodeError, TypeError):
            extra = {}

        masked_extra = {}
        for k, v in extra.items():
            if k.endswith("_enc"):
                masked_extra[f"{k[:-4]}_set"] = bool(v)
            elif k == "accounts" and isinstance(v, list):
                masked_extra["accounts"] = [
                    {
                        **{kk: vv for kk, vv in acc.items() if kk != "access_token_enc"},
                        "access_token_set": bool(acc.get("access_token_enc")),
                    }
                    for acc in v
                ]
            else:
                masked_extra[k] = v

        cfg.update(masked_extra)

    return cfg


def get_agent_config_masked(provider: str) -> Optional[dict]:
    """Returns the config for a single provider with all secrets masked (never decrypted)."""
    with connect() as conn:
        row = conn.execute(f"""
            SELECT {_AGENT_CONFIG_COLUMNS}
            FROM agent_config
            WHERE provider = ?
        """, provider).fetchone()
    if not row:
        return None
    return _mask_agent_row(_row_to_dict(row))


def get_all_agent_configs_masked() -> list[dict]:
    """Returns all agent configs with all secrets masked (never decrypted). Safe for the API."""
    with connect() as conn:
        rows = conn.execute(f"""
            SELECT {_AGENT_CONFIG_COLUMNS}
            FROM agent_config
            ORDER BY provider
        """).fetchall()
    return [_mask_agent_row(_row_to_dict(r)) for r in rows]


def update_agent_config(provider: str, fields: dict) -> bool:
    """
    Updates agent_config for `provider` from plaintext logical keys — the same
    shape returned by get_agent_config()/_decode_agent_row (e.g. 'password',
    'totp_secret', 'accounts': [{'alias':.., 'access_token':..}], 'enabled', ...).
    Secret fields are encrypted with the current fernet_key before writing.
    Only keys present in `fields` are touched; everything else is left as-is.
    Returns False if `fields` was empty (nothing to do) or the provider doesn't exist.
    """
    if not fields:
        return False

    set_clauses: list[str] = []
    params: list = []

    for col in _AGENT_TOP_LEVEL_COLUMNS:
        if col in fields:
            value = fields[col]
            if col == "enabled":
                value = 1 if value else 0
            set_clauses.append(f"{col} = ?")
            params.append(value)

    if "password" in fields:
        set_clauses.append("password_enc = ?")
        pwd = fields["password"]
        params.append(encrypt_value(pwd) if pwd else None)

    extra_keys = set(fields) - _AGENT_TOP_LEVEL_COLUMNS - {"password", "provider"}
    if extra_keys:
        with connect() as conn:
            row = conn.execute(
                "SELECT extra_config FROM agent_config WHERE provider = ?", provider
            ).fetchone()
        existing_raw = row[0] if row else None
        try:
            existing = json.loads(existing_raw) if existing_raw else {}
        except (json.JSONDecodeError, TypeError):
            existing = {}

        secret_fields = _AGENT_SECRET_EXTRA_FIELDS.get(provider, set())

        for key in extra_keys:
            value = fields[key]
            if key == "accounts" and isinstance(value, list):
                # The caller (panel) only ever sees the MASKED accounts list —
                # it never has the encrypted token for an account it didn't
                # just edit. Merge by alias instead of a blind overwrite, so
                # sending the list back after e.g. renaming an alias doesn't
                # silently wipe every other account's token.
                existing_by_alias = {
                    a.get("alias"): a for a in existing.get("accounts", []) if isinstance(a, dict)
                }
                accounts = []
                for acc in value:
                    acc   = dict(acc)
                    alias = acc.get("alias")
                    if "access_token" in acc:
                        token = acc.pop("access_token")
                        acc["access_token_enc"] = encrypt_value(token).decode() if token else ""
                    elif "access_token_enc" not in acc and alias in existing_by_alias:
                        prev = existing_by_alias[alias].get("access_token_enc")
                        if prev:
                            acc["access_token_enc"] = prev
                    accounts.append(acc)
                existing["accounts"] = accounts
            elif key in secret_fields or f"{key}_enc" in existing:
                # In the registry (new field for this provider) OR already
                # stored encrypted under this name (mirrors _mask_agent_row's
                # read-side detection) — either way, keep it encrypted rather
                # than silently downgrading it to plaintext because someone
                # forgot to add it to _AGENT_SECRET_EXTRA_FIELDS.
                existing[f"{key}_enc"] = encrypt_value(value).decode() if value else ""
                existing.pop(key, None)
            else:
                existing[key] = value

        set_clauses.append("extra_config = ?")
        params.append(json.dumps(existing))

    if not set_clauses:
        return False

    set_clauses.append("updated_at = GETDATE()")
    params.append(provider)

    with connect() as conn:
        cursor = conn.execute(
            f"UPDATE agent_config SET {', '.join(set_clauses)} WHERE provider = ?",
            *params,
        )
        updated = cursor.rowcount if cursor.rowcount is not None else 0
    return updated > 0


# ── Session store ─────────────────────────────────────────────────────────────

def save_session(provider: str, encrypted_data: bytes, expires_at: datetime):
    with connect() as conn:
        conn.execute("""
            MERGE session_store AS target
            USING (SELECT ? AS provider) AS source ON target.provider = source.provider
            WHEN MATCHED THEN
                UPDATE SET encrypted_data = ?, created_at = GETDATE(), expires_at = ?, valid = 1
            WHEN NOT MATCHED THEN
                INSERT (provider, encrypted_data, expires_at)
                VALUES (?, ?, ?);
        """, provider, encrypted_data, expires_at, provider, encrypted_data, expires_at)


def get_session(provider: str) -> Optional[bytes]:
    with connect() as conn:
        row = conn.execute("""
            SELECT encrypted_data FROM session_store
            WHERE provider = ?
              AND valid = 1
              AND expires_at > GETDATE()
        """, provider).fetchone()
    return row[0] if row else None


def invalidate_session(provider: str):
    with connect() as conn:
        conn.execute("""
            UPDATE session_store SET valid = 0
            WHERE provider = ?
        """, provider)


# ── Orchestrator (job batches) ────────────────────────────────────────────────

def get_pending_batches() -> list[dict]:
    """Returns orchestrator_agent_jobs rows in 'pending' status."""
    with connect() as conn:
        rows = conn.execute("""
            SELECT id, agents, started_by, requested_at
            FROM orchestrator_agent_jobs
            WHERE status = 'pending'
            ORDER BY requested_at ASC
        """).fetchall()
    return [_row_to_dict(r) for r in rows]


def mark_batch_processed(batch_id: int):
    with connect() as conn:
        conn.execute("""
            UPDATE orchestrator_agent_jobs
            SET status = 'processed', started_at = GETDATE()
            WHERE id = ?
        """, batch_id)


def mark_batch_error(batch_id: int, error: str):
    with connect() as conn:
        conn.execute("""
            UPDATE orchestrator_agent_jobs
            SET status = 'error', error_msg = ?
            WHERE id = ?
        """, error[:500], batch_id)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _row_to_dict(row) -> dict:
    if row is None:
        return {}
    if hasattr(row, "cursor_description"):
        columns = [col[0] for col in row.cursor_description]
        return dict(zip(columns, row))
    columns = [col[0] for col in row.__class__.__description__]
    return dict(zip(columns, row))
