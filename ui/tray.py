"""
ui/tray.py
==========
System tray icon.
● Green  = all agents OK
● Yellow = agents require intervention
● Red    = unresolved errors
● Blue   = an agent is running
● Gray   = initializing / unknown
"""

import json
import os
import sys
import threading
from shared.paths import BASE_DIR as _BASE_DIR, CONFIG_FILE as _CONFIG_FILE
from shared.api_client import ApiClient, ApiError
from PIL import Image, ImageDraw
import pystray
from loguru import logger



# ── Icons (generated with Pillow) ─────────────────────────────────────────────

def _make_icon(color: str) -> Image.Image:
    img  = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    colors = {
        "green":  "#2f9e44",
        "yellow": "#f08c00",
        "red":    "#e03131",
        "blue":   "#1971c2",
        "gray":   "#868e96",
    }

    draw.ellipse([4, 4, 60, 60], fill=colors.get(color, "#868e96"))
    draw.text((22, 18), "A", fill="white")
    return img


# ── System Tray ────────────────────────────────────────────────────────────────

class SystemTray:

    def __init__(self):
        self._current_color = "gray"
        self._icon: pystray.Icon = None
        self._config: dict = {}
        self._api_client = ApiClient()
        self._stop_requested = False
        # Track providers already alerted — only alert once per new intervention
        self._alerted: set[str] = set()
        # Set to provider name while a session capture is in progress
        self._capturing: str | None = None

    def start(self):
        import time as _time
        logger.info("Starting system tray...")
        self._stop_requested = False

        if not any(t.name == "TrayUpdateLoop" for t in threading.enumerate()):
            threading.Thread(target=self._update_loop, daemon=True, name="TrayUpdateLoop").start()

        while not self._stop_requested:
            try:
                self._icon = pystray.Icon(
                    "atana",
                    _make_icon("gray"),
                    "ATANA Agents",
                    menu=self._build_menu(),
                )
                self._icon.run()
            except Exception as e:
                if not self._stop_requested:
                    logger.error(f"[tray] pystray error: {e} — reiniciando en 5s")
                    _time.sleep(5)

    # ── Background update loop ─────────────────────────────────────────────────

    def _update_loop(self):
        import time
        while True:
            self._try_reload_config()
            self._update_icon()
            self._send_heartbeat()
            time.sleep(30)

    def _send_heartbeat(self):
        """
        Tells the dispatcher this tray is alive (system_config: tray_heartbeat/
        tray_pid). If this goes stale, _tray_watchdog_check() in main.py kills
        this PID so the Scheduled Task watchdog can bring up a fresh instance.
        """
        try:
            self._api("POST", "/tray/heartbeat", {"pid": os.getpid()})
        except Exception as e:
            logger.debug(f"Tray heartbeat failed: {e}")

    def _api(self, method: str, path: str, body: dict = None) -> dict:
        # Kept as a thin wrapper (same call signature everywhere in this file)
        # around the client shared with ui/panel_main.py — see shared/api_client.py.
        self._api_client.set_config(self._config)
        try:
            return self._api_client.call(method, path, body, timeout=3)
        except ApiError as e:
            raise RuntimeError(f"API {method} {path} error: {e}") from e

    def _load_agents_from_api(self):
        """Loads enabled agents from API into self._config['agents']."""
        try:
            resp = self._api("GET", "/agents")
            self._config["agents"] = {r["provider"]: r for r in resp.get("agents", [])}
            logger.debug(f"Tray: {len(self._config['agents'])} agents loaded from API")
        except Exception as e:
            logger.debug(f"Tray: could not load agents from API: {e}")

    def _try_reload_config(self):
        """Re-reads config.json to update API port and key."""
        if not _CONFIG_FILE.exists():
            return
        try:
            import re as _re
            raw_text = _CONFIG_FILE.read_text(encoding="utf-8")
            try:
                config = json.loads(raw_text)
            except json.JSONDecodeError:
                raw_text = _re.sub(
                    r'\\([^"\\/bfnrtu]|u(?![0-9a-fA-F]{4}))',
                    lambda m: '\\\\' + m.group(1),
                    raw_text,
                )
                config = json.loads(raw_text)
            self._config = config
            self._load_agents_from_api()
        except Exception as e:
            logger.debug(f"Tray: config reload attempt failed: {e}")

    def _enabled_providers(self) -> set[str]:
        return {
            p for p, cfg in self._config.get("agents", {}).items()
            if cfg.get("enabled", False)
        }

    def _update_icon(self):
        try:
            status_resp = self._api("GET", "/status")
            enabled      = self._enabled_providers()
            statuses     = [s for s in status_resp.get("agents", []) if s.get("provider") in enabled]
            intervention_jobs = [j for j in status_resp.get("intervention", []) if j.get("provider") in enabled]

            has_error        = any(s.get("last_result") == "error" for s in statuses)
            has_intervention = len(intervention_jobs) > 0
            has_running      = any(s.get("last_result") == "running" for s in statuses)

            if has_running:
                new_color = "blue"
            elif has_intervention:
                new_color = "yellow"
            elif has_error:
                new_color = "red"
            else:
                new_color = "green"

            if new_color != self._current_color:
                logger.debug(f"Tray icon: {self._current_color} -> {new_color}")

            if new_color != self._current_color or has_intervention:
                self._current_color = new_color
                if self._icon:
                    self._icon.icon  = _make_icon(new_color)
                    self._icon.title = self._tooltip(intervention_jobs, statuses)
                    self._icon.menu  = self._build_menu(intervention_jobs)

            # Show alert only for providers not yet alerted in this cycle
            if has_intervention:
                new_providers = {
                    j["provider"] for j in intervention_jobs
                    if j["provider"] not in self._alerted
                }
                if new_providers:
                    self._alerted |= new_providers
                    self._show_intervention_alert(
                        [j for j in intervention_jobs if j["provider"] in new_providers]
                    )
            else:
                # No interventions — reset so they alert again next time
                self._alerted.clear()

        except Exception as e:
            logger.debug(f"Tray update API error: {e}")
            if self._current_color != "gray":
                self._current_color = "gray"
                if self._icon:
                    self._icon.icon = _make_icon("gray")
                    self._icon.title = "ATANA Agents — Offline (Dispatcher not running?)"

    def _tooltip(self, pending: list, statuses: list) -> str:
        if pending:
            return f"ATANA — {len(pending)} agent(s) waiting for intervention"
        errors = sum(1 for s in statuses if s.get("last_result") == "error")
        if errors:
            return f"ATANA — {errors} error(s)"
        return "ATANA Agents — all OK"

    # ── Menu ──────────────────────────────────────────────────────────────────

    def _build_menu(self, pending: list = None) -> pystray.Menu:
        items = []

        if self._capturing:
            # Show a disabled item while capture is running
            items.append(pystray.MenuItem(
                f"⏳ Capturing {self._capturing.upper()} session...",
                lambda *_: None,
                enabled=False,
            ))
            items.append(pystray.Menu.SEPARATOR)
        elif pending:
            for job in pending:
                name = job["provider"].upper()
                def _make_play_cb(p):
                    return lambda icon, item: self._play(p)
                items.append(pystray.MenuItem(f"▶ Play {name}", _make_play_cb(job["provider"])))
            items.append(pystray.Menu.SEPARATOR)

        enabled_providers = [
            p for p, cfg in self._config.get("agents", {}).items()
            if cfg.get("enabled", False)
        ]

        def _make_retry_cb(p):
            return lambda icon, item: self._retry(p)

        if enabled_providers:
            items.append(
                pystray.MenuItem(
                    "Retry agent",
                    pystray.Menu(
                        pystray.MenuItem("All", lambda *_: self._retry_all()),
                        pystray.Menu.SEPARATOR,
                        *[
                            pystray.MenuItem(p.capitalize(), _make_retry_cb(p))
                            for p in enabled_providers
                        ],
                    ),
                )
            )

        items.append(pystray.Menu.SEPARATOR)
        items.append(pystray.MenuItem("📊 Abrir panel",    lambda *_: self._open_panel()))
        items.append(pystray.MenuItem("⚙ Configuración",  lambda *_: self._open_panel(config=True)))
        items.append(pystray.Menu.SEPARATOR)
        items.append(pystray.MenuItem("✕ Salir", self._exit))

        return pystray.Menu(*items)

    # ── Actions ───────────────────────────────────────────────────────────────

    def _play(self, provider: str):
        """User clicked Play — launch session capture (if supported) then authorize."""
        logger.info(f"Manual play: {provider}")
        self._alerted.discard(provider)

        # Providers that need Playwright session capture before running
        _capture_providers = {"fiserv"}

        if provider in _capture_providers:
            # capture.py will authorize the job after saving the session
            self._launch_capture(provider)
            return
        else:
            # For other providers with a portal_url, just open the browser
            portal_url = (
                self._config.get("agents", {})
                .get(provider, {})
                .get("portal_url")
            )
            if portal_url:
                import webbrowser
                try:
                    webbrowser.open(portal_url)
                except Exception as e:
                    logger.warning(f"[{provider}] Could not open browser: {e}")

        try:
            self._api("POST", f"/jobs/{provider}/play")
        except Exception as e:
            logger.warning(f"[{provider}] Could not authorize via API: {e}")

    def _launch_capture(self, provider: str):
        """
        Launches session capture as a subprocess in the user's interactive session.

        The dispatcher service runs in Session 0 (no display), so it cannot open
        a visible browser. Capture must run here, in the tray process (user session),
        using the --capture-session flag. The agent saves the session and authorizes
        the job directly via DB when done.

        Blocks re-entry: Play is disabled while a capture is already running.
        """
        if self._capturing:
            logger.warning(f"[{provider}] Capture already in progress for {self._capturing} — ignoring")
            if self._icon:
                try:
                    self._icon.notify(
                        f"Session capture for {self._capturing.upper()} is still in progress.",
                        "ATANA",
                    )
                except Exception:
                    pass
            return

        self._capturing = provider
        self._update_icon()  # redraw menu immediately (Play disabled)

        if self._icon:
            try:
                self._icon.notify(
                    f"Log in to {provider.upper()} in the browser that just opened.\n"
                    "The session will be saved automatically once you complete the login.",
                    "ATANA — Login required",
                )
            except Exception:
                pass

        def _run():
            import subprocess
            exe = sys.executable
            # Frozen exe (Windows production): the exe handles --capture-session directly.
            # Dev / plain Python interpreter: must go through -m dispatcher.main.
            if getattr(sys, "frozen", False):
                cmd = [exe, "--capture-session", provider]
            else:
                cmd = [exe, "-m", "dispatcher.main", "--capture-session", provider]
            logger.info(f"[{provider}] Launching capture subprocess: {' '.join(cmd)}")
            try:
                flags = {}
                if sys.platform == "win32":
                    flags["creationflags"] = subprocess.CREATE_NO_WINDOW
                result = subprocess.run(
                    cmd,
                    cwd=_BASE_DIR,
                    **flags,
                )
                if result.returncode == 0:
                    logger.success(f"[{provider}] Capture subprocess completed OK")
                else:
                    logger.error(f"[{provider}] Capture subprocess exited with code {result.returncode}")
                    if self._icon:
                        try:
                            self._icon.notify(
                                f"Session capture failed (exit {result.returncode}). Check logs.",
                                f"ATANA — {provider.upper()} error",
                            )
                        except Exception:
                            pass
            except Exception as e:
                logger.error(f"[{provider}] Could not launch capture subprocess: {e}")
            finally:
                self._capturing = None
                self._alerted.discard(provider)
                self._update_icon()

        threading.Thread(target=_run, daemon=True, name=f"capture-{provider}").start()

    def _retry_all(self):
        """Queue all enabled agents for manual execution."""
        enabled = self._enabled_providers()
        logger.info(f"Manual retry all: {', '.join(sorted(enabled))}")
        queued = []
        for provider in sorted(enabled):
            try:
                self._api("POST", f"/jobs/{provider}/ignore")
            except Exception:
                pass
                
        try:
            self._api("POST", "/jobs/all", {"providers": sorted(enabled)})
            queued = sorted(enabled)
        except Exception as e:
            logger.warning(f"Could not queue batch retry: {e}")
            
        if queued and self._icon:
            try:
                self._icon.notify(
                    f"Queued: {', '.join(queued)}",
                    "ATANA — Retry all",
                )
            except Exception:
                pass

    def _retry(self, provider: str):
        """Manually queue an agent, cancelling any stuck intervention job first."""
        logger.info(f"Manual retry: {provider}")
        try:
            self._api("POST", f"/jobs/{provider}/ignore")
            resp = self._api("POST", f"/jobs/{provider}", {"started_by": "manual"})
            logger.info(f"[{provider}] Retry job created via API")
        except Exception as e:
            logger.warning(f"[{provider}] Could not create retry job: {e}")

    def _open_panel(self, config: bool = False):
        """
        Opens the panel (Overview + Configuración) as its own process, so it
        works independently of the tray — it's a separate exe in the install
        folder (atana_panel.exe), not a mode of the tray/dispatcher exe.
        """
        import subprocess, sys
        flags = {}
        if sys.platform == "win32":
            flags["creationflags"] = subprocess.CREATE_NO_WINDOW

        args = ["--config"] if config else []

        if getattr(sys, "frozen", False):
            panel_exe = _BASE_DIR / "atana_panel.exe"
            if not panel_exe.exists():
                logger.warning(f"Panel not found at {panel_exe}")
                if self._icon:
                    try:
                        self._icon.notify("atana_panel.exe no encontrado en la carpeta de instalación.", "ATANA")
                    except Exception:
                        pass
                return
            subprocess.Popen([str(panel_exe), *args], **flags)
        else:
            # Dev fallback until ui/panel_main.py ships (see Fase 3 del plan).
            subprocess.Popen(
                [sys.executable, "-m", "ui.tui"],
                cwd=_BASE_DIR,
                **flags,
            )

    def _exit(self, icon=None, item=None):
        self._stop_requested = True
        if self._icon:
            self._icon.stop()

    # ── Intervention alert ────────────────────────────────────────────────────

    def _show_intervention_alert(self, jobs: list):
        """Show a native OS notification. Play buttons are in the tray menu."""
        names = ", ".join(j["provider"].upper() for j in jobs)
        msg   = f"{names} — click the yellow icon and press Play"
        try:
            if self._icon:
                self._icon.notify(msg, "ATANA — Action required")
        except Exception as e:
            logger.debug(f"Could not show notification: {e}")


def _setup_logging():
    """Configure file logging for the tray process."""
    import sys as _sys
    log_dir = _BASE_DIR / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)

    logger.remove()
    if _sys.stderr is not None:
        logger.add(
            _sys.stderr,
            format="<green>{time:HH:mm:ss}</green> | <level>{level: <8}</level> | {message}",
            level="INFO",
            colorize=False,
        )
    logger.add(
        log_dir / "tray_{time:YYYY-MM-DD}.log",
        rotation="00:00", retention="30 days", compression="zip",
        format="{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {name}:{line} | {message}",
        level="DEBUG", encoding="utf-8",
    )


def start():
    """System tray entry point."""
    _setup_logging()
    logger.info("ATANA Tray starting...")

    config = {}
    if _CONFIG_FILE.exists():
        try:
            config = json.loads(_CONFIG_FILE.read_text(encoding="utf-8"))
        except Exception as e:
            logger.warning(f"Could not load config.json: {e}")
    else:
        logger.warning(f"config.json not found at {_CONFIG_FILE}")

    tray = SystemTray()
    tray._config = config
    tray._load_agents_from_api()
    tray.start()


if __name__ == "__main__":
    start()
