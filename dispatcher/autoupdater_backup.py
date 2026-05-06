"""
dispatcher/autoupdater.py
=========================
Auto-update del exe principal desde GitHub Releases.

Flujo:
  1. check_for_update() se llama al inicio y antes de cada ciclo de jobs
  2. Consulta la API de GitHub para la ultima release
  3. Compara tag_name con APP_VERSION embebida en el exe
  4. Si hay version nueva:
     a. Descarga atana_dispatcher.exe a la carpeta de instalacion (.new)
     b. Verifica SHA256
     c. Escribe un script PowerShell que:
        - Mata todos los procesos atana_dispatcher
        - Para el servicio NSSM (cancela el restart timer)
        - Espera que el exe quede libre
        - Verifica que el source existe (detecta quarantine de AV)
        - Swapea el exe (guarda backup)
        - Restaura AppExit Default = restart en el registro
        - Inicia el servicio con el nuevo exe
     d. Deshabilita el restart automatico de NSSM via registro (AppExit = ignore)
        para que NSSM no reinicie con el exe viejo antes de que el swap ocurra
     e. Lanza el script PS en background y hace os._exit(0)
     f. El script PS restaura AppExit = restart y arranca el servicio nuevo

Configuracion en system_config (BD):
  github_token               -> Personal Access Token (repo:read)
  github_owner               -> Usuario u organizacion del repo
  github_repo                -> Nombre del repositorio
  check_update_interval_hours -> Cada cuantas horas chequear (default 6)
"""

import hashlib
import os
import sys
import tempfile
import subprocess
from pathlib import Path

import httpx
from loguru import logger

from dispatcher import db, notifier
from dispatcher.version import APP_VERSION

SERVICE_NAME = "AtanaDispatcher"


# ── Version comparison ────────────────────────────────────────────────────────

def _parse_version(v: str) -> tuple[int, ...]:
    """Converts 'v2.1.0' or '2.1.0' to (2, 1, 0)."""
    v = v.lstrip("v").strip()
    try:
        return tuple(int(x) for x in v.split("."))
    except ValueError:
        return (0,)


def _is_newer(remote: str, current: str) -> bool:
    return _parse_version(remote) > _parse_version(current)


# ── GitHub helpers ────────────────────────────────────────────────────────────

def _github_config() -> dict | None:
    cfg = db.get_system_config()
    # get_system_config() strips '_enc' and decrypts automatically, so
    # 'github_token_enc' in the DB is returned here as 'github_token'.
    token = cfg.get("github_token", "")
    owner = cfg.get("github_owner", "")
    repo  = cfg.get("github_repo", "")
    if not (token and owner and repo):
        return None
    return {"token": token, "owner": owner, "repo": repo}


def _headers(token: str) -> dict:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


# ── NSSM restart control ──────────────────────────────────────────────────────

def _set_nssm_appexit(value: str) -> bool:
    """
    Sets NSSM's AppExit Default registry value for AtanaDispatcher.

    NSSM stores AppExit as a SUBKEY under Parameters, not a value in Parameters:
      HKLM\...\Parameters\AppExit\  value "Default" = "Ignore" | "Restart"

    value="Ignore"  -> NSSM will NOT restart the service when the process exits.
    value="Restart" -> NSSM will restart normally (default behavior).

    Called before os._exit() to prevent NSSM from restarting with the old exe
    while the PS swap script is still running. The PS script restores it to
    "Restart" after the swap completes (Step 4b).
    """
    if os.name != "nt":
        return False
    try:
        import winreg
        # AppExit is a SUBKEY under Parameters — create it if missing
        key_path = rf"SYSTEM\CurrentControlSet\Services\{SERVICE_NAME}\Parameters\AppExit"
        key = winreg.CreateKeyEx(
            winreg.HKEY_LOCAL_MACHINE, key_path,
            access=winreg.KEY_SET_VALUE,
        )
        winreg.SetValueEx(key, "Default", 0, winreg.REG_SZ, value)
        winreg.CloseKey(key)
        logger.info(f"[autoupdater] NSSM AppExit\\Default set to '{value}'")
        return True
    except Exception as e:
        logger.warning(f"[autoupdater] Could not set NSSM AppExit\\Default='{value}': {e}")
        return False


# ── Update script ─────────────────────────────────────────────────────────────

def _write_update_script(exe_path: Path, new_exe: Path, version: str) -> Path:
    """Writes a self-deleting PowerShell script that swaps the exe."""
    target   = str(exe_path)
    source   = str(new_exe)
    backup   = str(exe_path.with_suffix(".exe.bak"))
    log_dir  = str(exe_path.parent / "logs")
    nssm_exe = str(exe_path.parent / "nssm.exe")
    reg_path = rf"HKLM:\SYSTEM\CurrentControlSet\Services\{SERVICE_NAME}\Parameters"

    script = f"""\
# Log to %TEMP% first (always writable), then try the install dir
$logTemp = "$env:TEMP\\atana_update.log"
"[$(Get-Date)] ===== ATANA AutoUpdate to {version} =====" | Out-File $logTemp -Append -Encoding utf8

$logDir = "{log_dir}"
try {{
    if (-not (Test-Path $logDir)) {{ New-Item -ItemType Directory -Path $logDir -Force | Out-Null }}
    $log = "$logDir\\autoupdate.log"
    "[$(Get-Date)] ===== ATANA AutoUpdate to {version} =====" | Out-File $log -Append -Encoding utf8
}} catch {{
    $log = $logTemp
    "[$(Get-Date)] WARNING: could not create log in $logDir, using TEMP" | Out-File $logTemp -Append -Encoding utf8
}}

try {{
    # Step 1 - Kill ALL atana_dispatcher processes (service + tray)
    "[$(Get-Date)] Step 1 - killing all atana_dispatcher processes..." | Out-File $log -Append -Encoding utf8
    $before = Get-Process -Name atana_dispatcher -ErrorAction SilentlyContinue
    "[$(Get-Date)] Processes found: $(($before | Measure-Object).Count)" | Out-File $log -Append -Encoding utf8
    & taskkill /F /IM atana_dispatcher.exe /T 2>&1 | Out-File $log -Append -Encoding utf8
    Start-Sleep -Seconds 1
    $after = Get-Process -Name atana_dispatcher -ErrorAction SilentlyContinue
    "[$(Get-Date)] Processes after taskkill: $(($after | Measure-Object).Count)" | Out-File $log -Append -Encoding utf8

    # Step 2 - Stop NSSM service (belt-and-suspenders alongside the registry AppExit=ignore)
    "[$(Get-Date)] Step 2 - nssm stop..." | Out-File $log -Append -Encoding utf8
    & "{nssm_exe}" stop {SERVICE_NAME} 2>&1 | Out-File $log -Append -Encoding utf8
    "[$(Get-Date)] nssm stop returned" | Out-File $log -Append -Encoding utf8

    # Step 3 - Wait until exe is unlocked (process already dead from os._exit)
    "[$(Get-Date)] Step 3 - checking file lock on {target}..." | Out-File $log -Append -Encoding utf8
    $locked = $true
    $retries = 0
    while ($locked -and $retries -lt 15) {{
        try {{
            $fs = [System.IO.File]::Open("{target}", [System.IO.FileMode]::Open, [System.IO.FileAccess]::ReadWrite, [System.IO.FileShare]::None)
            $fs.Close()
            $locked = $false
            "[$(Get-Date)] File is free (attempt $retries)" | Out-File $log -Append -Encoding utf8
        }} catch {{
            $retries++
            "[$(Get-Date)] File still locked (attempt $retries/15) - retaskkill..." | Out-File $log -Append -Encoding utf8
            & taskkill /F /IM atana_dispatcher.exe /T 2>&1 | Out-File $log -Append -Encoding utf8
            Start-Sleep -Seconds 2
        }}
    }}
    if ($locked) {{
        throw "Exe still locked after 30s - aborting swap"
    }}

    # Step 4 - Verify source exists (AV quarantine check) then swap: old -> backup, new -> target
    "[$(Get-Date)] Step 4 - verifying source and swapping exe..." | Out-File $log -Append -Encoding utf8
    if (-not (Test-Path "{source}")) {{
        throw "Source file missing before swap (AV quarantine?): {source}"
    }}
    "[$(Get-Date)] Source size: $((Get-Item '{source}').Length) bytes" | Out-File $log -Append -Encoding utf8
    if (Test-Path "{backup}") {{ Remove-Item "{backup}" -Force }}
    Move-Item "{target}" "{backup}" -Force
    Move-Item "{source}" "{target}" -Force
    "[$(Get-Date)] Swap OK - new size: $((Get-Item '{target}').Length) bytes" | Out-File $log -Append -Encoding utf8

    # Step 4b - Restore NSSM AppExit Default = restart (was set to ignore before os._exit)
    "[$(Get-Date)] Step 4b - restoring NSSM AppExit Default to restart..." | Out-File $log -Append -Encoding utf8
    try {{
        Set-ItemProperty -Path "{reg_path}" -Name "AppExit Default" -Value "restart"
        "[$(Get-Date)] NSSM AppExit restored to restart" | Out-File $log -Append -Encoding utf8
    }} catch {{
        "[$(Get-Date)] WARNING: could not restore AppExit Default: $_" | Out-File $log -Append -Encoding utf8
    }}

    # Step 5 - Start service with new exe
    "[$(Get-Date)] Step 5 - starting service..." | Out-File $log -Append -Encoding utf8
    & "{nssm_exe}" start {SERVICE_NAME} 2>&1 | Out-File $log -Append -Encoding utf8
    "[$(Get-Date)] nssm start returned" | Out-File $log -Append -Encoding utf8

    "[$(Get-Date)] ===== Update complete =====" | Out-File $log -Append -Encoding utf8

}} catch {{
    "[$(Get-Date)] ERROR: $_" | Out-File $log -Append -Encoding utf8
    if ((Test-Path "{backup}") -and (-not (Test-Path "{target}"))) {{
        "[$(Get-Date)] Restoring backup..." | Out-File $log -Append -Encoding utf8
        Move-Item "{backup}" "{target}" -Force
    }}
    # Always restore AppExit = restart so NSSM can recover, even on error
    try {{
        Set-ItemProperty -Path "{reg_path}" -Name "AppExit Default" -Value "restart"
        "[$(Get-Date)] NSSM AppExit restored to restart (error path)" | Out-File $log -Append -Encoding utf8
    }} catch {{
        "[$(Get-Date)] WARNING: could not restore AppExit Default on error path: $_" | Out-File $log -Append -Encoding utf8
    }}
    & "{nssm_exe}" start {SERVICE_NAME} 2>&1 | Out-File $log -Append -Encoding utf8
}}

# Self-delete
Remove-Item $MyInvocation.MyCommand.Path -Force -ErrorAction SilentlyContinue
"""
    fd, ps_path_str = tempfile.mkstemp(suffix=".ps1", prefix="atana_upd_")
    os.close(fd)
    ps_path = Path(ps_path_str)
    # utf-8-sig adds BOM — required for PowerShell 5.1 to read UTF-8 correctly.
    # Without BOM, PS5.1 treats the file as the system codepage (Windows-1252)
    # which can corrupt non-ASCII content and cause parse failures.
    ps_path.write_text(script, encoding="utf-8-sig")
    return ps_path


def _launch_update_script(ps_path: Path):
    """
    Launches the PS script using WMI to completely escape NSSM's Job Object.
    If we use standard subprocess.Popen, the PS script inherits the Job Object
    and NSSM will kill it as soon as the service main process dies.
    """
    flags = 0
    if os.name == "nt":
        flags = subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS
        
        # Invoke-WmiMethod spawns the process via the WMI service (wmiprvse.exe),
        # which breaks it entirely out of the current process group/job object.
        cmd = (
            "Invoke-WmiMethod -Class Win32_Process -Name Create -ArgumentList "
            f"'powershell.exe -WindowStyle Hidden -ExecutionPolicy Bypass -NonInteractive -File \"{ps_path}\"'"
        )
        
        subprocess.Popen(
            ["powershell", "-WindowStyle", "Hidden", "-NonInteractive", "-Command", cmd],
            creationflags=flags,
            close_fds=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    else:
        subprocess.Popen(
            ["bash", str(ps_path)],
            close_fds=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )


# ── Public API ─────────────────────────────────────────────────────────────────

def check_for_update():
    """
    Checks GitHub for a newer exe release.
    If found: downloads it, spawns the update script and exits the process.
    Safe to call in a background thread.
    """
    cfg = _github_config()
    if not cfg:
        logger.warning("[autoupdater] GitHub config not set (github_token/owner/repo) — skipping update check")
        return

    try:
        logger.info(f"[autoupdater] Checking for updates (current: {APP_VERSION})...")
        logger.debug(f"[autoupdater] GitHub repo: {cfg['owner']}/{cfg['repo']}")
        url = f"https://api.github.com/repos/{cfg['owner']}/{cfg['repo']}/releases/latest"
        logger.debug(f"[autoupdater] GET {url}")
        with httpx.Client(timeout=15, follow_redirects=True) as client:
            resp = client.get(url, headers=_headers(cfg["token"]))
            logger.debug(f"[autoupdater] GitHub response: {resp.status_code}")
            resp.raise_for_status()
            release = resp.json()

        remote_version = release.get("tag_name", "")
        logger.debug(f"[autoupdater] Remote tag: {remote_version!r}")
        if not remote_version:
            logger.warning("[autoupdater] Could not read tag_name from release")
            return

        if not _is_newer(remote_version, APP_VERSION):
            logger.info(f"[autoupdater] Up to date — current={APP_VERSION}, latest={remote_version}")
            return

        logger.info(f"[autoupdater] New version available: {remote_version} (current: {APP_VERSION})")

        assets = release.get("assets", [])
        logger.debug(f"[autoupdater] Release assets: {[a['name'] for a in assets]}")

        asset = next(
            (a for a in assets if a["name"] == "atana_dispatcher.exe"),
            None,
        )
        build_info_asset = next(
            (a for a in assets if a["name"] == "build_info.json"),
            None,
        )
        if not asset:
            logger.warning("[autoupdater] Release has no atana_dispatcher.exe asset — skipping")
            return
        if not build_info_asset:
            logger.warning("[autoupdater] Release has no build_info.json asset — skipping (cannot verify integrity)")
            return

        logger.debug(f"[autoupdater] exe asset id={asset['id']} size={asset['size']:,}")
        logger.debug(f"[autoupdater] build_info asset id={build_info_asset['id']}")

        # Only apply the update when running as a frozen exe (not in dev mode)
        if not getattr(sys, "frozen", False):
            logger.info("[autoupdater] Dev mode — update skipped")
            return

        exe_path = Path(sys.executable)
        logger.debug(f"[autoupdater] Current exe: {exe_path}")

        dl_headers = dict(_headers(cfg["token"]))
        dl_headers["Accept"] = "application/octet-stream"

        with httpx.Client(timeout=60, follow_redirects=True) as client:
            bi_url = (
                f"https://api.github.com/repos/{cfg['owner']}/{cfg['repo']}"
                f"/releases/assets/{build_info_asset['id']}"
            )
            logger.debug(f"[autoupdater] Downloading build_info.json from {bi_url}")
            bi_resp = client.get(bi_url, headers=dl_headers)
            logger.debug(f"[autoupdater] build_info response: {bi_resp.status_code}")
            bi_resp.raise_for_status()
            try:
                build_info = bi_resp.json()
                logger.debug(f"[autoupdater] build_info: {build_info}")
            except Exception:
                logger.warning("[autoupdater] Could not parse build_info.json — skipping")
                return

        expected_sha256 = build_info.get("sha256", "")
        if not expected_sha256:
            logger.warning("[autoupdater] build_info.json has no sha256 field — skipping")
            return
        logger.debug(f"[autoupdater] Expected SHA256: {expected_sha256}")

        # Download to the install directory (same folder as the running exe).
        # Downloading to %TEMP% causes Windows Defender to scan/quarantine the file
        # before the PS swap can move it, silently breaking the update.
        tmp_exe = exe_path.parent / "atana_dispatcher.new"
        tmp_exe.unlink(missing_ok=True)
        logger.debug(f"[autoupdater] Download path: {tmp_exe}")

        asset_url = (
            f"https://api.github.com/repos/{cfg['owner']}/{cfg['repo']}"
            f"/releases/assets/{asset['id']}"
        )
        logger.debug(f"[autoupdater] Downloading exe from {asset_url}")
        logger.info(f"[autoupdater] Downloading {asset['size']:,} bytes...")

        with httpx.Client(timeout=300, follow_redirects=True) as client:
            with client.stream("GET", asset_url, headers=dl_headers) as r:
                logger.debug(f"[autoupdater] Download stream response: {r.status_code}")
                r.raise_for_status()
                tmp_exe.write_bytes(r.read())

        logger.debug(f"[autoupdater] Download complete — {tmp_exe.stat().st_size:,} bytes on disk")

        # Verify SHA256 before executing anything
        sha = hashlib.sha256()
        with open(tmp_exe, "rb") as f:
            for chunk in iter(lambda: f.read(8192), b""):
                sha.update(chunk)
        actual_sha256 = sha.hexdigest()
        logger.debug(f"[autoupdater] Actual SHA256:   {actual_sha256}")

        if actual_sha256 != expected_sha256:
            tmp_exe.unlink(missing_ok=True)
            logger.error(
                f"[autoupdater] SHA256 mismatch — aborting update\n"
                f"  expected: {expected_sha256}\n"
                f"  actual:   {actual_sha256}"
            )
            return

        logger.success(
            f"[autoupdater] Download verified — {tmp_exe.stat().st_size:,} bytes, SHA256 OK"
        )

        ps_path = _write_update_script(exe_path, tmp_exe, remote_version)
        logger.debug(f"[autoupdater] Update script written: {ps_path}")

        notifier.notify(
            "dispatcher", "agent_updated",
            f"Nueva version {remote_version} descargada — reiniciando servicio..."
        )
        notifier.flush()

        logger.info(f"[autoupdater] Launching update script ({ps_path}) and exiting process...")

        # Disable NSSM auto-restart BEFORE launching the script and exiting.
        # This prevents NSSM from restarting with the OLD exe while the PS swap
        # script is still running. The PS script restores AppExit=restart after
        # the swap completes (Step 4b), so normal restarts resume with the new exe.
        _set_nssm_appexit("ignore")

        _launch_update_script(ps_path)

        import time
        # Darle tiempo suficiente a PowerShell para que arranque su entorno .NET 
        # y envíe la instrucción a WMI antes de suicidar el proceso padre de Python.
        time.sleep(4)
        logger.info("[autoupdater] Terminating process now (os._exit)...")
        os._exit(0)

    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            logger.debug("[autoupdater] No releases published yet — skipping update check")
        else:
            logger.warning(f"[autoupdater] GitHub API error: {e.response.status_code} — {e.response.text[:200]}")
    except Exception as e:
        logger.error(f"[autoupdater] Update check failed: {e}")
