"""
ui/panel_main.py
=================
Entry point for atana_panel.exe — the Overview + Configuración window.

Ships as its OWN executable (see tools/build_panel.py), separate from
atana_dispatcher.exe, so it can be opened from the install folder (a Start
Menu/Desktop shortcut, or "📊 Abrir panel" / "⚙ Configuración" in the tray
menu) whether or not the tray or the dispatcher service happen to be running.

Talks ONLY to the dispatcher's local API (http://localhost:{api_port}) via
shared/api_client.py — it never touches SQL Server or the Fernet keys
directly, so this process only ever needs app.api_key/app.api_port from
config.json, not database credentials.
"""

import argparse
import json
import sys

from loguru import logger

from shared.paths import BASE_DIR, CONFIG_FILE


def _setup_logging():
    log_dir = BASE_DIR / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)

    logger.remove()
    if sys.stderr is not None:
        logger.add(
            sys.stderr,
            format="<green>{time:HH:mm:ss}</green> | <level>{level: <8}</level> | {message}",
            level="INFO",
            colorize=False,
        )
    logger.add(
        log_dir / "panel_{time:YYYY-MM-DD}.log",
        rotation="00:00", retention="30 days", compression="zip",
        format="{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {name}:{line} | {message}",
        level="DEBUG", encoding="utf-8",
    )


def _load_config() -> dict:
    """
    Only reads app.api_key/app.api_port (and 'agents' for cosmetics) — never
    the 'database' section. If config.json is missing/unreadable we still
    open the window; every API call will just fail with a clear message
    instead of the panel refusing to start.
    """
    if not CONFIG_FILE.exists():
        logger.warning(f"config.json not found at {CONFIG_FILE}")
        return {}
    try:
        return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning(f"Could not read config.json: {e}")
        return {}


def main():
    parser = argparse.ArgumentParser(description="ATANA Panel")
    parser.add_argument(
        "--config", action="store_true",
        help="Open directly on the Configuración tab",
    )
    args = parser.parse_args()

    _setup_logging()
    logger.info("ATANA Panel starting...")

    config = _load_config()

    # Imported after logging is configured, and lazily so `--help`/argparse
    # errors don't pay for importing tkinter.
    from ui.panel_app import PanelApp
    PanelApp(config, open_config=args.config).run()


if __name__ == "__main__":
    main()
