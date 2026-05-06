# dispatcher/api.py
"""
Internal HTTP server for the dispatcher.
Runs in a separate thread, does not block the scheduler.
Port configurable in config.json → app.api_port (default 8765).
Protected with optional API key.
"""

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse

from loguru import logger

from dispatcher import db
from dispatcher.agent_loader import known_providers as _known_providers

_api_key: str  = ""
_port:    int  = 8765
_config:  dict = {}

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

    def do_GET(self):
        path = urlparse(self.path).path

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
                length = int(self.headers.get("Content-Length", 0))
                body   = {}
                if length:
                    body = json.loads(self.rfile.read(length))

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

        self._json(404, {"error": "not found"})
