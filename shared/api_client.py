"""
shared/api_client.py
=====================
Small HTTP client for the dispatcher's internal API (http://localhost:{api_port}).

Used by every process that runs in the user's interactive session — the tray
(ui/tray.py) and the panel (ui/panel_main.py). Neither one talks to SQL Server
or the Fernet keys directly: they only ever see `app.api_key`/`app.api_port`
from config.json, and the dispatcher (the service, Session 0) decides what's
safe to hand back over the API.
"""

import json
import urllib.error
import urllib.request


class ApiError(RuntimeError):
    """Raised for any failed call — network error, non-2xx status, bad JSON, etc."""


class ApiClient:

    def __init__(self, config: dict = None):
        self.port    = 8765
        self.api_key = ""
        if config:
            self.set_config(config)

    def set_config(self, config: dict):
        """Re-reads app.api_port/app.api_key — call after config.json changes."""
        app = (config or {}).get("app", {})
        self.port    = app.get("api_port", 8765)
        self.api_key = app.get("api_key", "")

    def call(self, method: str, path: str, body: dict = None, timeout: float = 5) -> dict:
        req = urllib.request.Request(f"http://localhost:{self.port}{path}", method=method)
        if self.api_key:
            req.add_header("X-API-Key", self.api_key)

        if body is not None:
            data = json.dumps(body).encode("utf-8")
            req.add_header("Content-Type", "application/json")
            req.add_header("Content-Length", str(len(data)))
            req.data = data

        try:
            with urllib.request.urlopen(req, timeout=timeout) as response:
                raw = response.read()
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as e:
            try:
                payload = json.loads(e.read())
            except Exception:
                payload = {}
            raise ApiError(payload.get("error", f"HTTP {e.code}")) from e
        except Exception as e:
            raise ApiError(str(e)) from e

    def get(self, path: str, **kw) -> dict:
        return self.call("GET", path, **kw)

    def post(self, path: str, body: dict = None, **kw) -> dict:
        return self.call("POST", path, body, **kw)

    def put(self, path: str, body: dict = None, **kw) -> dict:
        return self.call("PUT", path, body, **kw)
