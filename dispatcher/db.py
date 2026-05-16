
from __future__ import annotations
import json
from contextlib import contextmanager
from datetime import datetime
from typing import Optional

import pyodbc
from cryptography.fernet import Fernet, InvalidToken
from loguru import logger


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
    import time
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

    from dispatcher.database_factory import build_connection_args
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


# ── Agent config ──────────────────────────────────────────────────────────────

def _decode_agent_row(row: dict) -> dict:
    """
    Decrypts password_enc → password and merges extra_config JSON into the row.
    Returns the enriched dict that agents receive as their config.
    """
    cfg = dict(row)

    # Decrypt top-level password
    provider = cfg.get("provider", "?")
    if cfg.get("password_enc"):
        raw = cfg["password_enc"] if isinstance(cfg["password_enc"], bytes) else bytes(cfg["password_enc"])
        decrypted_pwd = _decrypt(raw)
        cfg["password"] = decrypted_pwd
        logger.debug(f"[db] [{provider}] password_enc: {len(raw)} bytes → decrypted ok={bool(decrypted_pwd)}")
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
                logger.debug(f"[db] Decrypted extra_config field: {k!r} → {plain_key!r} (ok={bool(decrypted)})")
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
