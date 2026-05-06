import argparse
import json
import os
import subprocess
import sys
import threading
import time
import traceback
from datetime import datetime
from pathlib import Path

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from loguru import logger

from dispatcher import db, notifier, api, autoupdater
from dispatcher.agent_loader import load_agent, known_providers
from dispatcher.version import APP_VERSION
from shared import session_store

from shared.paths import CONFIG_FILE

_config: dict = {}

# Timestamp of the last successful update check — used to throttle GitHub calls.
_last_update_check: datetime | None = None

# Signals that Playwright Chromium is installed and ready.
# process_jobs() waits on this before executing any agent.
_chromium_ready = threading.Event()

# APScheduler instance — module-level so _sync_scheduler_jobs() can access it.
scheduler: "BackgroundScheduler | None" = None


def load_config() -> dict:
    """
    Loads the full application config.
    DB credentials come from config.json; everything else is read from SQL Server.
    """
    if not CONFIG_FILE.exists():
        logger.error(f"config.json not found at {CONFIG_FILE}")
        sys.exit(1)

    import re as _re
    text = CONFIG_FILE.read_text(encoding="utf-8")
    try:
        raw = json.loads(text)
    except json.JSONDecodeError:
        # Safety net: fix unescaped backslashes (e.g. server\SQLEXPRESS typed manually)
        # Handles \X and \u not followed by 4 hex digits without touching valid escapes
        text = _re.sub(
            r'\\([^"\\/bfnrtu]|u(?![0-9a-fA-F]{4}))',
            lambda m: '\\\\' + m.group(1),
            text,
        )
        raw = json.loads(text)
    db.init(raw)

    sys_cfg    = db.get_system_config()
    agent_cfgs = {r["provider"]: r for r in db.get_all_agent_configs()}

    # Escribir api_key y api_port a config.json para que el proceso del tray los lea.
    # El tray no tiene acceso a la DB — solo lee config.json.
    try:
        raw.setdefault("app", {})
        raw["app"]["api_port"] = int(sys_cfg.get("api_port", 8765))
        raw["app"]["api_key"]  = sys_cfg.get("api_key", "")
        CONFIG_FILE.write_text(
            json.dumps(raw, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        logger.debug("[config] api_key/api_port actualizados en config.json")
    except Exception as e:
        logger.warning(f"[config] No se pudo actualizar config.json: {e}")

    return {
        "database": raw["database"],
        "app":      sys_cfg,
        "agents":   agent_cfgs,
    }


def get_agent(name: str, config: dict):
    """Instantiates an agent from the bundled registry."""
    cls = load_agent(name)
    return cls(config)


# ── Job processing ────────────────────────────────────────────────────────────

def run_provider(provider: str, job_id: int, authorized: bool = False, requested_at=None):
    """Runs a specific agent for a given job.

    authorized=True means the user clicked Play — skip requires_intervention()
    and call run() directly so the agent can open the browser / capture session.
    """
    ag_config = _config.get("agents", {}).get(provider, {})
    if not ag_config.get("enabled", False):
        logger.debug(f"[{provider}] Disabled — skipping")
        db.update_job_error(job_id, "Agent disabled")
        return

    try:
        agent = get_agent(provider, _config)

        # Skip intervention check for authorized jobs — human already clicked Play
        if not authorized and agent.requires_intervention():
            db.update_job_intervention(job_id, agent.intervention_reason())
            notifier.notify(provider, "requires_intervention", agent.intervention_reason())
            logger.warning(f"[{provider}] Requires intervention")
            return

        if authorized:
            logger.info(f"[{provider}] Authorized by user — running directly")

        result = agent.run(job_id, requested_at=requested_at)

        if result["ok"]:
            logger.success(f"[{provider}] OK — {len(result['downloaded'])} downloaded")
        else:
            logger.error(f"[{provider}] Failed — {result['errors']}")

    except NotImplementedError:
        logger.warning(f"[{provider}] Not yet implemented")
        db.update_job_error(job_id, "Agent not implemented")
    except Exception as e:
        logger.exception(f"[{provider}] Unexpected error: {e}")
        db.update_job_error(job_id, str(e))


def process_batches():
    """
    Reads pending rows from orchestrator_agent_jobs and creates
    an individual agent_job for each provider listed in the 'agents' field (CSV).
    """
    batches = db.get_pending_batches()
    if not batches:
        return

    _known = set(known_providers())

    for batch in batches:
        batch_id  = batch["id"]
        providers = [p.strip() for p in batch["agents"].split(",") if p.strip()]
        origin    = batch.get("started_by", "batch")

        logger.info(f"[orchestrator] Batch {batch_id} ({origin}): {providers}")
        errors = []
        for provider in providers:
            if provider not in _known:
                logger.warning(f"[orchestrator] Unknown agent '{provider}' — skipping")
                errors.append(f"Unknown agent: {provider}")
                continue
            job_id = db.create_job(provider, started_by=f"batch:{batch_id}")
            if job_id == -1:
                logger.debug(f"[orchestrator] [{provider}] Already has active job — skipping")
            else:
                logger.info(f"[orchestrator] [{provider}] Job {job_id} created from batch {batch_id}")

        if errors:
            db.mark_batch_error(batch_id, "; ".join(errors))
        else:
            db.mark_batch_processed(batch_id)


def _check_update_if_due(force: bool = False):
    """
    Runs a synchronous update check if enough time has passed since the last one,
    OR if forced (e.g. by a manually triggered job).
    Blocks until the check completes — if a new version is found, autoupdater
    downloads it and calls sys.exit(0), so agents never run on an outdated exe.
    """
    global _last_update_check
    interval_hours = int(_config.get("app", {}).get("check_update_interval_hours", "6"))
    if interval_hours == 0 and not force:
        logger.debug("[updater] Update check disabled (check_update_interval_hours=0)")
        return
        
    now = datetime.now()
    is_due = _last_update_check is None or (now - _last_update_check).total_seconds() >= interval_hours * 3600
    
    if force or is_due:
        reason = "forced before running jobs" if force else f"interval={interval_hours}h due"
        logger.debug(f"[updater] Running update check ({reason})")
        _last_update_check = now
        autoupdater.check_for_update()
    else:
        remaining = interval_hours * 3600 - (now - _last_update_check).total_seconds()
        logger.debug(f"[updater] Next update check in {remaining/3600:.1f}h")

# ── Scheduler helpers ─────────────────────────────────────────────────────────

def _already_ran_today(provider: str) -> bool:
    """
    Returns True if the provider already had a job created today.
    Avoids duplicate executions when forcing immediate run.
    """
    try:
        with db.connect() as conn:
            row = conn.execute("""
                SELECT TOP 1 1
                FROM agent_jobs
                WHERE provider = ?
                  AND finished_at is not null
                  AND CAST(finished_at AS DATE) = CAST(GETDATE() AS DATE)
                  AND status IN ('pending', 'running', 'ok', 'processed')
            """, (provider,)).fetchone()

            return row is not None
    except Exception as e:
        logger.warning(f"[{provider}] Could not check today's execution: {e}")
        return False


def _debug_scheduler_jobs():
    """Logs current scheduler jobs and next execution time (safe)."""
    try:
        if scheduler:
            from datetime import datetime

            for job in scheduler.get_jobs():
                try:
                    next_run = None

                    # Intentar obtener atributo directo (si existe)
                    if hasattr(job, "next_run_time"):
                        next_run = job.next_run_time

                    # Fallback: calcular manualmente (SIEMPRE funciona)
                    if not next_run and hasattr(job, "trigger"):
                        next_run = job.trigger.get_next_fire_time(None, datetime.now())

                    logger.warning(
                        f"[SCHEDULER] id={job.id} "
                        f"next_run={next_run} "
                        f"trigger={job.trigger}"
                    )

                except Exception as inner:
                    logger.warning(f"[SCHEDULER] error leyendo job {job}: {inner}")

    except Exception as e:
        logger.debug(f"[scheduler] debug failed: {e}")

def _sync_scheduler_jobs():
    """Adds / removes per-provider CronTrigger jobs to match current DB config.
    Also forces immediate execution if today's schedule already passed and
    the job was not executed yet.
    """
    if scheduler is None:
        return

    from datetime import datetime

    now = datetime.now()

    for provider, cfg in _config.get("agents", {}).items():
        job_id  = f"schedule_{provider}"
        hour    = cfg.get("schedule_hour")
        minute  = cfg.get("schedule_minute", 0)
        enabled = cfg.get("enabled", False)

        if not enabled or hour is None:
            if scheduler.get_job(job_id):
                scheduler.remove_job(job_id)
                logger.info(f"[{provider}] Cron job removido (deshabilitado o sin hora)")
            continue

        try:
            trigger = CronTrigger(
                hour=int(hour),
                minute=int(minute),
                timezone="America/Argentina/Buenos_Aires",
            )

            next_run = trigger.get_next_fire_time(None, now)

            logger.warning(f"[{provider}] Proxima ejecucion programada: {next_run}")

            run_now = False

            # Si ya pasó hoy → decidir ejecución inmediata
            if next_run and next_run.date() > now.date():
                if not _already_ran_today(provider):
                    logger.warning(f"[{provider}] Hora ya pasó hoy — ejecución inmediata")
                    run_now = True
                else:
                    logger.info(f"[{provider}] Ya ejecutado hoy — skip ejecución inmediata")

            if scheduler.get_job(job_id) is None:
                scheduler.add_job(
                    queue_scheduler_job,
                    trigger,
                    args=[provider],
                    id=job_id,           
                    misfire_grace_time=None,  # siempre ejecutar
                    coalesce=True,  
                )
                logger.info(f"[{provider}] Cron job agregado: {int(hour):02d}:{int(minute):02d}")

            if run_now:
                try:
                    queue_scheduler_job(provider)
                except Exception as e:
                    logger.error(f"[{provider}] Error en ejecución inmediata: {e}")

        except Exception as e:
            logger.error(f"[{provider}] Error configurando cron: {e}")

    _debug_scheduler_jobs()

def _ensure_tray_registry():
    """Keeps HKCU\\...\\Run entry pointing to the current exe path."""
    if os.name != "nt":
        return
    try:
        import winreg
        exe = str(Path(sys.executable).resolve())
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Run",
            0, winreg.KEY_SET_VALUE,
        )
        winreg.SetValueEx(key, "AtanaTray", 0, winreg.REG_SZ, f'"{exe}" --tray')
        winreg.CloseKey(key)
        logger.debug("[tray] Registro HKCU\\Run actualizado")
    except Exception as e:
        logger.debug(f"[tray] No se pudo actualizar registro: {e}")


def _ensure_tray_running():
    """
    Checks if the tray process is already running; if not, launches it.
    In service mode (Session 0) uses Task Scheduler to cross the session boundary.
    In interactive mode uses a direct subprocess call.
    Called at every dispatcher startup — covers first boot, post-update and tray crash.
    """
    if os.name != "nt" or not getattr(sys, "frozen", False):
        return

    exe_stem = Path(sys.executable).stem
    ps_check = (
        f"$r = Get-WmiObject Win32_Process "
        f"| Where-Object {{ $_.Name -like '{exe_stem}*' -and $_.CommandLine -like '*--tray*' }}; "
        f"if ($r) {{ exit 0 }} else {{ exit 1 }}"
    )
    try:
        res = subprocess.run(
            ["powershell", "-NonInteractive", "-WindowStyle", "Hidden", "-Command", ps_check],
            capture_output=True, timeout=10,
        )
        if res.returncode == 0:
            logger.debug("[tray] Tray already running — skip")
            return
    except Exception as e:
        logger.debug(f"[tray] Could not check tray process: {e}")

    logger.info("[tray] Tray not running — launching...")

    if _running_as_service():
        exe_path = Path(sys.executable)
        ps = f"""
$exe = '{exe_path}'
$explorer = Get-WmiObject Win32_Process -Filter "Name='explorer.exe'" | Select-Object -First 1
if (-not $explorer) {{ Write-Host 'No interactive user logged in'; exit 0 }}
$owner = $explorer.GetOwner()
$user  = "$($owner.Domain)\$($owner.User)"
$action    = New-ScheduledTaskAction -Execute $exe -Argument '--tray'
$trigger   = New-ScheduledTaskTrigger -Once -At (Get-Date).AddSeconds(5)
$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
$settings  = New-ScheduledTaskSettingsSet -DeleteExpiredTaskAfter '00:01:00' -ExecutionTimeLimit '00:05:00'
Unregister-ScheduledTask -TaskName 'AtanaTrayStart' -Confirm:$false -ErrorAction SilentlyContinue
Register-ScheduledTask -TaskName 'AtanaTrayStart' -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Force | Out-Null
Start-ScheduledTask -TaskName 'AtanaTrayStart'
Write-Host "Tray launched for: $user"
"""
        result = subprocess.run(
            ["powershell", "-NonInteractive", "-WindowStyle", "Hidden", "-Command", ps],
            capture_output=True, text=True, timeout=20,
        )
        out = result.stdout.strip()
        if result.returncode == 0:
            logger.info(f"[tray] {out}")
        else:
            logger.warning(f"[tray] Task Scheduler failed (rc={result.returncode}): {result.stderr.strip()}")
    else:
        _start_tray()


def process_jobs():
    """Reads pending/authorized jobs from SQL Server and executes them."""
    if not _chromium_ready.is_set():
        logger.debug("Chromium not ready yet — skipping job cycle")
        return

    # Refresh agent configs so that changes in DB (enabled, schedule_hour) take effect
    # without restarting the dispatcher.
    global _config
    try:
        _config["agents"] = {r["provider"]: r for r in db.get_all_agent_configs()}
        _sync_scheduler_jobs()
    except Exception as e:
        logger.warning(f"[config] No se pudo refrescar agentes: {e}")

    logger.debug("Job cycle started")
    process_batches()
    jobs = db.get_pending_jobs()
    if not jobs:
        logger.debug("No pending jobs")
        # Si NO hay trabajos que ejecutar, evaluamos si le toca el checkeo por rutina del cron (ocio)
        _check_update_if_due(force=False)
        return

    # Si SÍ hay trabajos (de cron o manuales), se fuerza SIEMPRE a validar la versión antes de arrancar.
    _check_update_if_due(force=True)

    logger.info(f"Pending jobs: {len(jobs)} [v{APP_VERSION}]")

    for job in jobs:
        provider = job["provider"]
        job_id   = job["id"]

        if job["attempts"] >= job["max_retries"]:
            logger.warning(f"[{provider}] Job {job_id} exhausted retries")
            db.update_job_error(job_id, "Maximum retries reached")
            continue

        authorized   = job.get("status") == "authorized"
        requested_at = job.get("requested_at")
        logger.info(f"[{provider}] Processing job {job_id} (origin: {job.get('started_by')}){' [AUTHORIZED]' if authorized else ''}")
        run_provider(provider, job_id, authorized=authorized, requested_at=requested_at)

    notifier.flush()


def queue_all_enabled(started_by: str = "scheduler"):
    """Queues all enabled agents. Used by scheduler and --run-all."""
    providers = [
        name for name, cfg in _config["agents"].items()
        if cfg.get("enabled", False)
    ]
    for provider in providers:
        job_id = db.create_job(provider, started_by=started_by)
        if job_id == -1:
            logger.debug(f"[{provider}] Already has an active job — skipping")
        else:
            logger.info(f"[{provider}] Job {job_id} queued by {started_by}")


def queue_scheduler_job(provider: str):
    logger.info(f"[{provider}] Scheduler triggered — creating job")
    try:
        job_id = db.create_job(provider, started_by="scheduler")
        if job_id == -1:
            logger.info(f"[{provider}] Skipped — already has an active job")
        else:
            logger.info(f"[{provider}] Job {job_id} queued by scheduler")
    except Exception as e:
        logger.error(f"[{provider}] Failed to create scheduled job: {e}")


# ── Logging ───────────────────────────────────────────────────────────────────

def _install_crash_hooks():
    """Captura excepciones no manejadas en el hilo principal y en hilos de background."""

    def _on_exception(exc_type, exc_value, exc_tb):
        if issubclass(exc_type, (KeyboardInterrupt, SystemExit)):
            sys.__excepthook__(exc_type, exc_value, exc_tb)
            return
        logger.critical(
            "UNHANDLED EXCEPTION — proceso terminando\n" +
            "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
        )

    def _on_thread_exception(args):
        if args.exc_type in (KeyboardInterrupt, SystemExit):
            return
        logger.critical(
            f"UNHANDLED EXCEPTION en hilo '{args.thread.name}'\n" +
            "".join(traceback.format_exception(args.exc_type, args.exc_value, args.exc_traceback))
        )

    sys.excepthook                = _on_exception
    threading.excepthook          = _on_thread_exception


def setup_logging(config: dict):
    from shared.paths import BASE_DIR
    app          = config.get("app", {})
    raw_log_dir  = app.get("log_dir", "./logs").strip("'\"")
    log_dir      = Path(raw_log_dir) if Path(raw_log_dir).is_absolute() else BASE_DIR / raw_log_dir
    log_dir.mkdir(parents=True, exist_ok=True)
    debug   = str(app.get("debug", "false")).lower() == "true"

    logger.remove()
    if sys.stdout is not None:
        if hasattr(sys.stdout, 'reconfigure'):
            try:
                sys.stdout.reconfigure(encoding='utf-8', errors='replace')
            except Exception:
                pass
        logger.add(
            sys.stdout,
            format="<green>{time:HH:mm:ss}</green> | <level>{level: <8}</level> | {message}",
            level="DEBUG" if debug else "INFO",
            colorize=True,
        )
    logger.add(
        log_dir / "dispatcher_{time:YYYY-MM-DD}.log",
        rotation="00:00", retention="30 days", compression="zip",
        format="{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {name}:{line} | {message}",
        level="DEBUG", encoding="utf-8",
    )


def _browsers_dir() -> Path:
    """
    Returns a persistent directory for Playwright browser binaries.
    Critical when running as a PyInstaller exe: the default Playwright path
    would be inside the ephemeral _MEI temp dir and disappears on exit.
    """
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path.home() / ".local" / "share"
    return base / "atana" / "browsers"


def _chromium_installed() -> bool:
    """Returns True if Chromium is already present in the persistent browsers dir."""
    browsers = _browsers_dir()
    exes = list(browsers.glob("**/chrome.exe")) + list(browsers.glob("**/chrome"))
    return len(exes) > 0


def _install_chromium(silent: bool = False):
    """
    Installs Playwright Chromium to a PERSISTENT directory.
    Sets PLAYWRIGHT_BROWSERS_PATH so all subsequent Playwright calls
    in this process find the browser correctly.

    silent=True: background update check — logs at DEBUG level, no "is ready" banner.
    """
    browsers = _browsers_dir()
    browsers.mkdir(parents=True, exist_ok=True)

    os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(browsers)

    if not silent:
        logger.info(f"Installing Playwright Chromium at {browsers}...")
    else:
        logger.debug(f"Background Chromium update check at {browsers}")
    try:
        from playwright._impl._driver import compute_driver_executable, get_driver_env
        driver_executable, driver_cli = compute_driver_executable()

        env = get_driver_env()
        env["PLAYWRIGHT_BROWSERS_PATH"] = str(browsers)

        subprocess.run(
            [driver_executable, driver_cli, "install", "chromium"],
            env=env,
            check=True,
            text=True,
        )
        if not silent:
            logger.success("Playwright Chromium is ready")
        else:
            logger.debug("Background Chromium update check complete")
    except Exception as e:
        logger.error(f"Failed to install/update Chromium: {e}")
    finally:
        _chromium_ready.set()


# ── Startup modes ─────────────────────────────────────────────────────────────

def _running_as_service() -> bool:
    """True when launched as a Windows service.

    Primary signal: NSSM sets ATANA_SERVICE=1 in the service env.
    Fallback: services run in Session 0 which has no SESSIONNAME env var.
    """
    if os.environ.get("ATANA_SERVICE") == "1":
        return True
    # On Windows, interactive sessions always have SESSIONNAME ("Console" or RDP name).
    # Session 0 (services) does not — use this as a reliable fallback.
    if os.name == "nt" and not os.environ.get("SESSIONNAME"):
        return True
    return False


def _start_tray():
    """Launches the system tray icon as a separate background process."""
    exe = Path(sys.executable)
    try:
        subprocess.Popen(
            [str(exe), "--tray"],
            cwd=exe.parent,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        logger.debug("Tray icon launched")
    except Exception as e:
        logger.warning(f"Could not start tray icon: {e}")


def _parse_args():
    parser = argparse.ArgumentParser(description="ATANA Agents Dispatcher")
    parser.add_argument(
        "--run",
        action="store_true",
        help="Queue all enabled agents immediately and process"
    )
    parser.add_argument(
        "--provider",
        type=str,
        default=None,
        help="Specific provider(s) separated by comma: prisma,cabal"
    )
    parser.add_argument(
        "--tui",
        action="store_true",
        help="Open the tkinter dashboard window"
    )
    parser.add_argument(
        "--tray",
        action="store_true",
        help="Run as system tray icon (UI process)"
    )
    parser.add_argument(
        "--capture-session",
        type=str,
        default=None,
        metavar="PROVIDER",
        help="Open a visible browser to capture a manual session (e.g. fiserv)",
    )
    parser.add_argument(
        "--setup-db",
        action="store_true",
        help="Run the initial database setup wizard",
    )
    return parser.parse_args()


def main():
    global _config

    args = _parse_args()

    # --setup-db: run initial database setup wizard and exit
    if args.setup_db:
        from ui.setup_db import run as run_setup
        run_setup()
        return

    # --tui: open the tkinter dashboard and exit (no dispatcher)
    if args.tui:
        from ui.tui import main as tui_main
        tui_main()
        return

    # --tray: run as system tray icon and exit (no dispatcher)
    if args.tray:
        from ui.tray import start as tray_start
        tray_start()
        return

    # --capture-session PROVIDER: open visible browser, save session, exit
    if args.capture_session:
        _config = load_config()
        setup_logging(_config)
        os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(_browsers_dir()))
        session_store.init(_config.get("app", {}).get("session_key"))
        from dispatcher.capture import capture_session
        capture_session(args.capture_session.lower(), _config)
        return

    _config = load_config()
    setup_logging(_config)
    _install_crash_hooks()

    logger.info("=" * 50)
    logger.info(f"ATANA Agents v{APP_VERSION} starting...")
    if args.run:
        providers = args.provider.split(",") if args.provider else "all"
        logger.info(f"Mode: --run ({providers})")
    elif _running_as_service():
        logger.info("Mode: Windows service (headless)")
    else:
        logger.info("Mode: permanent service")
    logger.info("=" * 50)

    # Startup diagnostics — helps identify service detection and path issues
    from shared.paths import BASE_DIR
    logger.debug(f"ATANA_SERVICE  = {os.environ.get('ATANA_SERVICE', '(not set)')}")
    logger.debug(f"SESSIONNAME    = {os.environ.get('SESSIONNAME', '(not set)')}")
    logger.debug(f"Frozen exe     = {getattr(sys, 'frozen', False)}")
    logger.debug(f"Executable     = {sys.executable}")
    logger.debug(f"BASE_DIR       = {BASE_DIR}")

    notifier.init(_config)
    try:
        session_store.init(_config.get("app", {}).get("session_key"))
    except RuntimeError as e:
        logger.error(f"Session store init failed: {e}")
        sys.exit(1)

    # Always set PLAYWRIGHT_BROWSERS_PATH pointing to the persistent dir BEFORE
    # any agent or Playwright call — even when Chromium is already installed.
    # Without this, Playwright looks inside the ephemeral _MEIPASS temp dir and fails.
    os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(_browsers_dir())
    logger.debug(f"PLAYWRIGHT_BROWSERS_PATH = {os.environ['PLAYWRIGHT_BROWSERS_PATH']}")

    # Chromium: install synchronously on first run, background check on subsequent runs
    if not _chromium_installed():
        logger.info("Playwright Chromium not found — installing before starting (this may take a moment)...")
        _install_chromium()
    else:
        _chromium_ready.set()
        logger.info("Playwright Chromium already installed")
        threading.Thread(target=lambda: _install_chromium(silent=True), daemon=True, name="chromium-update").start()

    # Reset jobs that got stuck in 'running' from a previous crash
    with db.connect() as conn:
        conn.execute("UPDATE agent_jobs SET status = 'pending' WHERE status = 'running'")

    # ── --run mode ────────────────────────────────────────────────────────────
    if args.run:
        if args.provider:
            for p in [x.strip() for x in args.provider.split(",")]:
                job_id = db.create_job(p, started_by="run")
                if job_id == -1:
                    logger.warning(f"[{p}] Already has an active, paused, or stuck job. Skipped to avoid overlap.")
        else:
            queue_all_enabled(started_by="run")

        if not db.get_pending_jobs():
            logger.info("No new pending jobs to process.")
        else:
            process_jobs()

    # ── Scheduler (always active) ─────────────────────────────────────────────
    global scheduler
    scheduler = BackgroundScheduler(timezone="America/Argentina/Buenos_Aires")

    check_interval = int(_config.get("app", {}).get("check_jobs_interval_min", "5"))

    # Run the startup update check synchronously before the scheduler starts.
    # _last_update_check is NOT stamped here — _check_update_if_due() will stamp it
    # on the first job cycle (15s later), ensuring the check also runs before the
    # first actual agent job.
    autoupdater.check_for_update()

    from datetime import timedelta
    scheduler.add_job(
        process_jobs, "interval",
        minutes=check_interval,
        id="process_jobs",
        next_run_time=datetime.now() + timedelta(seconds=15) if not args.run else None,
    )

    # Initial sync of per-provider cron jobs — dynamic refresh happens in process_jobs().
    if not args.run:
        _sync_scheduler_jobs()

    api.init(_config)
    api.start()

    # Keep HKCU Run entry pointing to current exe (survives updates / path changes).
    if not args.run:
        _ensure_tray_registry()
        _ensure_tray_running()

    scheduler.start()
    logger.info("Dispatcher active — waiting for jobs or human intervention...")

    try:
        while True:
            time.sleep(30)
    except (KeyboardInterrupt, SystemExit):
        logger.info("Dispatcher stopped")
        scheduler.shutdown()


if __name__ == "__main__":
    main()
