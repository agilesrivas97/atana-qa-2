# dispatcher/api.py
"""
Internal HTTP server for the dispatcher.
Runs in a separate thread, does not block the scheduler.
Port configurable in config.json → app.api_port (default 8765).
Protected with optional API key.
"""

import json
import os
import re
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse

from loguru import logger

from dispatcher import db
from dispatcher.agent_loader import known_providers as _known_providers
from shared import session_store
from shared.paths import CONFIG_FILE

_api_key: str  = ""
_port:    int  = 8765
_config:  dict = {}

# ── Key rotation background state ──────────────────────────────────────────────
# rotate-master runs in a background thread (it can take a moment to
# re-encrypt every row) so it never blocks the request-handling loop; progress
# is polled via GET /config/keys/rotate-status.
_rotation_lock   = threading.Lock()
_rotation_status = {"state": "idle", "detail": None, "started_at": None, "finished_at": None}

def init(config: dict):
    global _api_key, _port, _config
    _api_key = config.get("app", {}).get("api_key", "")
    _port    = int(config.get("app", {}).get("api_port", 8765))
    _config  = config


def start():
    """Starts the HTTP server in a daemon thread."""
    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    logger.info(f"Internal API listening on http://localhost:{_port}")


def _run():
    try:
        server = HTTPServer(("localhost", _port), _Handler)
        server.serve_forever()
    except OSError as e:
        if e.errno == 48:
            logger.debug(f"Internal API ignored: port {_port} already in use by main Dispatcher.")
        else:
            logger.error(f"Error starting internal API: {e}")


def _json_serial(obj):
    """Serializer for non-JSON-serializable objects (datetime, date)."""
    from datetime import datetime, date
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    raise TypeError(f"Object of type {type(obj)} is not JSON serializable")



class _Handler(BaseHTTPRequestHandler):

    def log_message(self, format, *args):
        pass  # Silence HTTP access logs

    def _auth(self) -> bool:
        if not _api_key:
            return True  # No API key configured = open (localhost only)
        return self.headers.get("X-API-Key") == _api_key

    def _json(self, code: int, data: dict):
        body = json.dumps(data, default=_json_serial).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", len(body))
        self.end_headers()
        self.wfile.write(body)

    def _read_json_body(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length", 0))
        except (TypeError, ValueError):
            length = 0
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length))
        except Exception:
            return {}

    def do_GET(self):
        path  = urlparse(self.path).path
        parts = [p for p in path.split("/") if p]

        if path == "/health":
            self._json(200, {"status": "ok"})
            return

        if path == "/status":
            if not self._auth():
                self._json(401, {"error": "unauthorized"})
                return
            try:
                statuses = db.get_all_status()
                pending  = db.get_intervention_jobs()
                self._json(200, {
                    "agents":       statuses,
                    "intervention": pending,
                })
            except Exception as e:
                self._json(500, {"error": str(e)})
            return

        if path == "/agents":
            if not self._auth():
                self._json(401, {"error": "unauthorized"})
                return
            try:
                configs = db.get_all_agent_configs()
                result = [
                    {
                        "provider":     c["provider"],
                        "enabled":      c.get("enabled", False),
                        "portal_url":   c.get("portal_url", ""),
                        "schedule_hour": c.get("schedule_hour"),
                    }
                    for c in configs
                ]
                self._json(200, {"agents": result})
            except Exception as e:
                self._json(500, {"error": str(e)})
            return

        # GET /config/system — masked system_config (secrets come back as '<key>_set' booleans)
        if len(parts) == 2 and parts[0] == "config" and parts[1] == "system":
            if not self._auth():
                self._json(401, {"error": "unauthorized"})
                return
            try:
                self._json(200, {"system": db.get_system_config_masked()})
            except Exception as e:
                self._json(500, {"error": str(e)})
            return

        # GET /config/system/secret/{field} — decrypted value of a secret
        # system_config key (smtp_password, github_token). Same tradeoff as the
        # per-agent secret endpoint below: only exists to pre-fill "Reemplazar".
        if len(parts) == 4 and parts[0] == "config" and parts[1] == "system" and parts[2] == "secret":
            if not self._auth():
                self._json(401, {"error": "unauthorized"})
                return
            field = parts[3]
            try:
                value = db.get_system_secret(field)
                logger.warning(f"[system] Plaintext read of secret field '{field}' via API")
                self._json(200, {"field": field, "value": value})
            except ValueError as e:
                self._json(400, {"error": str(e)})
            except Exception as e:
                self._json(500, {"error": str(e)})
            return

        # GET /config/agents — masked config for every agent
        if len(parts) == 2 and parts[0] == "config" and parts[1] == "agents":
            if not self._auth():
                self._json(401, {"error": "unauthorized"})
                return
            try:
                known  = set(_known_providers())
                agents = db.get_all_agent_configs_masked()
                for a in agents:
                    a["available"] = a["provider"] in known
                self._json(200, {"agents": agents})
            except Exception as e:
                self._json(500, {"error": str(e)})
            return

        # GET /config/agents/{provider} — masked config for one agent
        if len(parts) == 3 and parts[0] == "config" and parts[1] == "agents":
            if not self._auth():
                self._json(401, {"error": "unauthorized"})
                return
            provider = parts[2]
            try:
                cfg = db.get_agent_config_masked(provider)
                if cfg is None:
                    self._json(404, {"error": f"Unknown provider: {provider}"})
                else:
                    cfg["available"] = provider in _known_providers()
                    self._json(200, cfg)
            except Exception as e:
                self._json(500, {"error": str(e)})
            return

        # GET /config/agents/{provider}/secret/{field} — the DECRYPTED value of a
        # single top-level or extra_config secret field (e.g. 'password',
        # 'totp_secret'). Used only to pre-fill the "Reemplazar" dialog in the
        # panel so the user can see/edit the current value instead of typing
        # blind. Logged — this is the one path that returns plaintext secrets
        # over the API, by explicit design request.
        if len(parts) == 5 and parts[0] == "config" and parts[1] == "agents" and parts[3] == "secret":
            if not self._auth():
                self._json(401, {"error": "unauthorized"})
                return
            provider, field = parts[2], parts[4]
            try:
                cfg = db.get_agent_config(provider)
                if cfg is None:
                    self._json(404, {"error": f"Unknown provider: {provider}"})
                    return
                logger.warning(f"[{provider}] Plaintext read of secret field '{field}' via API")
                self._json(200, {"field": field, "value": cfg.get(field, "") or ""})
            except Exception as e:
                self._json(500, {"error": str(e)})
            return

        # GET /config/agents/{provider}/secret/accounts/{alias} — same, for one
        # MercadoPago-style account's access_token.
        if (len(parts) == 6 and parts[0] == "config" and parts[1] == "agents"
                and parts[3] == "secret" and parts[4] == "accounts"):
            if not self._auth():
                self._json(401, {"error": "unauthorized"})
                return
            provider, alias = parts[2], parts[5]
            try:
                cfg = db.get_agent_config(provider)
                if cfg is None:
                    self._json(404, {"error": f"Unknown provider: {provider}"})
                    return
                acc = next((a for a in cfg.get("accounts", []) if a.get("alias") == alias), None)
                if acc is None:
                    self._json(404, {"error": f"Unknown alias: {alias}"})
                    return
                logger.warning(f"[{provider}] Plaintext read of account '{alias}' token via API")
                self._json(200, {"alias": alias, "value": acc.get("access_token", "") or ""})
            except Exception as e:
                self._json(500, {"error": str(e)})
            return

        # GET /config/keys/rotate-status — progress of the last/current rotate-master run
        if len(parts) == 3 and parts[0] == "config" and parts[1] == "keys" and parts[2] == "rotate-status":
            if not self._auth():
                self._json(401, {"error": "unauthorized"})
                return
            with _rotation_lock:
                self._json(200, dict(_rotation_status))
            return

        self._json(404, {"error": "not found"})

    def do_PUT(self):
        if not self._auth():
            self._json(401, {"error": "unauthorized"})
            return

        path  = urlparse(self.path).path
        parts = [p for p in path.split("/") if p]
        body  = self._read_json_body()

        # PUT /config/system — body: {key: plaintext_value, ...}
        if len(parts) == 2 and parts[0] == "config" and parts[1] == "system":
            try:
                db.update_system_config(body)
                logger.info(f"[config] system_config updated via API — keys: {sorted(body)}")
                self._json(200, {"ok": True})
            except ValueError as e:
                self._json(400, {"error": str(e)})
            except Exception as e:
                logger.error(f"Error updating system_config via API: {e}")
                self._json(500, {"error": str(e)})
            return

        # PUT /config/agents/{provider} — body: {field: plaintext_value, ...}
        if len(parts) == 3 and parts[0] == "config" and parts[1] == "agents":
            provider = parts[2]
            if provider not in _known_providers():
                self._json(400, {"error": f"Unknown provider: {provider}"})
                return
            try:
                updated = db.update_agent_config(provider, body)
                logger.info(f"[{provider}] agent_config updated via API — fields: {sorted(body)}")
                self._json(200, {"ok": updated, "provider": provider})
            except Exception as e:
                logger.error(f"[{provider}] Error updating agent_config via API: {e}")
                self._json(500, {"error": str(e)})
            return

        self._json(404, {"error": "not found"})

    def do_POST(self):
        if not self._auth():
            self._json(401, {"error": "unauthorized"})
            return

        path  = urlparse(self.path).path
        parts = [p for p in path.split("/") if p]

        # POST /jobs/all  → queue all active agents via batch
        if len(parts) == 2 and parts[0] == "jobs" and parts[1] == "all":
            try:
                providers = [
                    r["provider"] for r in db.get_all_agent_configs()
                    if r.get("enabled")
                ]
                if not providers:
                    self._json(400, {"error": "no enabled agents"})
                    return
                batch_id = db.create_batch(providers, started_by="api")
                logger.info(f"[orchestrator] Batch {batch_id} created via API /all — {providers}")
                self._json(201, {"batch_id": batch_id, "providers": providers})
            except Exception as e:
                self._json(500, {"error": str(e)})
            return

        # POST /jobs/{provider}/ignore
        if len(parts) == 3 and parts[0] == "jobs" and parts[2] == "ignore":
            provider = parts[1]
            try:
                db.ignore_job(provider)
                logger.info(f"[{provider}] Job ignored via API")
                self._json(200, {"ok": True, "provider": provider})
            except Exception as e:
                self._json(500, {"error": str(e)})
            return

        # POST /jobs/{provider}/play
        if len(parts) == 3 and parts[0] == "jobs" and parts[2] == "play":
            provider = parts[1]
            try:
                db.authorize_job(provider)
                logger.info(f"[{provider}] Play authorized via API")
                self._json(200, {"ok": True, "provider": provider})
            except Exception as e:
                self._json(500, {"error": str(e)})
            return

        # POST /jobs/{provider}
        if len(parts) == 2 and parts[0] == "jobs":
            provider = parts[1]
            if provider not in _known_providers():
                self._json(400, {"error": f"Unknown provider: {provider}"})
                return
            try:
                body = self._read_json_body()

                job_id = db.create_job(
                    provider,
                    started_by  = body.get("started_by", "api"),
                    max_retries = body.get("max_retries", 3),
                )
                if job_id == -1:
                    self._json(409, {"error": "job already active", "provider": provider})
                else:
                    logger.info(f"[{provider}] Job {job_id} created via API")
                    self._json(201, {"job_id": job_id, "provider": provider})
            except Exception as e:
                logger.error(f"Error creating job via API: {e}")
                self._json(500, {"error": str(e)})
            return

        # POST /tray/heartbeat — body: {"pid": <int>}. Written every 30s by the tray;
        # polled by dispatcher/main.py's _tray_watchdog_check to detect a hung tray.
        if len(parts) == 2 and parts[0] == "tray" and parts[1] == "heartbeat":
            try:
                body = self._read_json_body()
                pid  = int(body.get("pid", 0))
                db.set_tray_heartbeat(pid)
                self._json(200, {"ok": True})
            except Exception as e:
                self._json(500, {"error": str(e)})
            return

        # POST /config/keys/rotate-api-key — rotates the local dispatcher<->tray/panel auth token
        if len(parts) == 3 and parts[0] == "config" and parts[1] == "keys" and parts[2] == "rotate-api-key":
            try:
                new_key = db.rotate_api_key()

                global _api_key
                _api_key = new_key  # this running server accepts the new key immediately

                _sync_api_key_to_config_file(new_key)
                logger.info("[rotate] API key rotated")
                self._json(200, {"api_key": new_key})
            except Exception as e:
                logger.error(f"[rotate] Error rotating API key: {e}")
                self._json(500, {"error": str(e)})
            return

        # POST /config/keys/rotate-master — body: {"targets": ["fernet_key", "session_key"]}
        # Re-encrypts (fernet_key) or invalidates (session_key) everything protected by the
        # old value. Runs in a background thread — poll GET /config/keys/rotate-status.
        if len(parts) == 3 and parts[0] == "config" and parts[1] == "keys" and parts[2] == "rotate-master":
            body    = self._read_json_body()
            targets = set(body.get("targets", [])) & {"fernet_key", "session_key"}
            if not targets:
                self._json(400, {"error": "targets must include 'fernet_key' and/or 'session_key'"})
                return

            with _rotation_lock:
                if _rotation_status["state"] == "running":
                    self._json(409, {"error": "a rotation is already in progress"})
                    return
                _rotation_status.update(
                    state="running", detail=None,
                    started_at=datetime.now(timezone.utc).isoformat(), finished_at=None,
                )

            threading.Thread(
                target=_run_rotation, args=(targets,), daemon=True, name="key-rotation",
            ).start()
            logger.info(f"[rotate] Master key rotation started — targets: {sorted(targets)}")
            self._json(202, {"status": "started", "targets": sorted(targets)})
            return

        # POST /service/restart — exits the process; NSSM's AppRestartDelay (15s, set by the
        # installer) brings it back up automatically. Stuck jobs left 'running' are reset to
        # 'pending' at the next startup (see main.py), so this is safe mid-job too.
        if len(parts) == 2 and parts[0] == "service" and parts[1] == "restart":
            logger.warning("[service] Restart requested via API — exiting so NSSM relaunches it")
            self._json(202, {"status": "restarting"})

            def _delayed_exit():
                import time
                time.sleep(1)  # let the HTTP response above actually flush to the client
                os._exit(0)

            threading.Thread(target=_delayed_exit, daemon=True, name="service-restart").start()
            return

        self._json(404, {"error": "not found"})


# ── Key rotation helpers (module-level — used by the handler above) ────────────

def _sync_api_key_to_config_file(new_key: str):
    """
    Immediately mirrors the new api_key into config.json so the tray and panel
    pick it up on their next reload without waiting for a full dispatcher
    restart (dispatcher.main.load_config() normally only writes this at boot).
    Mirrors the same read/fix/write pattern used there.
    """
    if not CONFIG_FILE.exists():
        return
    try:
        text = CONFIG_FILE.read_text(encoding="utf-8")
        try:
            raw = json.loads(text)
        except json.JSONDecodeError:
            text = re.sub(
                r'\\([^"\\/bfnrtu]|u(?![0-9a-fA-F]{4}))',
                lambda m: '\\\\' + m.group(1),
                text,
            )
            raw = json.loads(text)

        raw.setdefault("app", {})
        raw["app"]["api_key"] = new_key
        CONFIG_FILE.write_text(json.dumps(raw, indent=2, ensure_ascii=False), encoding="utf-8")
        logger.debug("[rotate] config.json api_key synced")
    except Exception as e:
        logger.warning(f"[rotate] Could not sync api_key to config.json: {e}")


def _run_rotation(targets: set):
    """
    Background worker for POST /config/keys/rotate-master. Never raises —
    failures are recorded in _rotation_status so the old key(s) stay in
    effect and the caller finds out via GET /config/keys/rotate-status.
    """
    summary = {}
    try:
        if "fernet_key" in targets:
            summary["fernet_key"] = db.rotate_fernet_key()

        if "session_key" in targets:
            session_result  = db.rotate_session_key()
            new_session_key = session_result.pop("new_key", None)
            if new_session_key:
                try:
                    # Refresh shared.session_store's cached Fernet instance so
                    # this running service uses the new key immediately —
                    # otherwise it would keep encrypting new sessions with the
                    # old one until the next full restart.
                    session_store.init(new_session_key)
                except Exception as e:
                    logger.warning(f"[rotate] session_store cache refresh failed: {e}")
            summary["session_key"] = session_result

        with _rotation_lock:
            _rotation_status.update(
                state="done", detail=summary,
                finished_at=datetime.now(timezone.utc).isoformat(),
            )
        logger.success(f"[rotate] Master key rotation completed: {summary}")

    except Exception as e:
        logger.error(f"[rotate] Master key rotation FAILED — old key(s) still in effect: {e}")
        with _rotation_lock:
            _rotation_status.update(
                state="error", detail=str(e),
                finished_at=datetime.now(timezone.utc).isoformat(),
            )
