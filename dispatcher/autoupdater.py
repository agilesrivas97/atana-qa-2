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
import time

import httpx
from loguru import logger

from dispatcher import db
from dispatcher.version import APP_VERSION

SERVICE_NAME = "AtanaDispatcher"


# ── Startup cleanup ───────────────────────────────────────────────────────────

def _kill_other_instances():
    """
    Kills all atana_dispatcher.exe processes except the current one.
    This releases any file handles held by the tray or other instances on the
    running exe, allowing the .old backup to be deleted and the swap to proceed.
    Only runs on Windows frozen builds.
    """
    if os.name != "nt" or not getattr(sys, "frozen", False):
        return
    try:
        current_pid = os.getpid()
        exe_name    = Path(sys.executable).name
        ps = (
            f"Get-WmiObject Win32_Process "
            f"| Where-Object {{ $_.Name -eq '{exe_name}' -and $_.ProcessId -ne {current_pid} }} "
            f"| ForEach-Object {{ Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }}"
        )
        subprocess.run(
            ["powershell", "-NonInteractive", "-WindowStyle", "Hidden", "-Command", ps],
            capture_output=True, timeout=15,
        )
        logger.debug("[autoupdater] Other instances terminated")
    except Exception as e:
        logger.debug(f"[autoupdater] Could not kill other instances: {e}")



def _cleanup_old_exe():
    """
    Deletes the .old backup left by the previous update.
    Must run unconditionally at startup — not only when a new version is found,
    because on the first check after an update the version comparison returns
    'up to date' and would skip this otherwise.
    Retries up to 5 times with 2-second delays to handle Windows file locks
    (the OS keeps image files mapped briefly after the old process exits).
    """
    if not getattr(sys, "frozen", False):
        return
    exe_path = Path(sys.executable)
    old_exe = exe_path.with_name(exe_path.name + ".old")
    if not old_exe.exists():
        return

    _kill_other_instances()

    import time as _time
    for attempt in range(1, 6):
        try:
            old_exe.unlink()
            logger.info(f"[autoupdater] Eliminado backup anterior: {old_exe.name}")
            return
        except Exception as e:
            if attempt < 5:
                logger.debug(
                    f"[autoupdater] No se pudo eliminar {old_exe.name} "
                    f"(intento {attempt}/5): {e} — reintentando en 2s"
                )
                _time.sleep(2)
            else:
                logger.warning(
                    f"[autoupdater] No se pudo eliminar {old_exe.name} "
                    f"luego de 5 intentos: {e}"
                )


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
    token = cfg.get("github_token", "").strip()
    owner = cfg.get("github_owner", "").strip()
    repo  = cfg.get("github_repo",  "").strip()
    if not (token and owner and repo):
        return None
    logger.debug(f"[autoupdater] Token loaded — length={len(token)}, prefix={token[:8]}...")
    return {"token": token, "owner": owner, "repo": repo}


def _headers(token: str) -> dict:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }








# ── Public API ─────────────────────────────────────────────────────────────────

def check_for_update():
    """
    Checks GitHub for a newer exe release.
    If found: downloads it, spawns the update script and exits the process.
    Safe to call in a background thread.
    """
    # Always clean up .old from the previous update, regardless of version check outcome.
    # This must run before any early return so that a just-updated process (which is
    # now 'up to date') still removes the leftover backup on its first call.
    _cleanup_old_exe()

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

        old_exe = exe_path.with_name(exe_path.name + ".old")

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

        logger.info("[autoupdater] Applying update via Windows rename pattern...")
        _kill_other_instances()
        try:
            # Remove leftover .old before swap — startup cleanup may have failed if the
            # file was still locked then; by now (post-download) it should be free.
            if old_exe.exists():
                import time as _t
                for _attempt in range(1, 4):
                    try:
                        old_exe.unlink()
                        logger.debug(f"[autoupdater] Removed {old_exe.name} before swap")
                        break
                    except Exception as _e:
                        if _attempt < 3:
                            logger.debug(f"[autoupdater] {old_exe.name} still locked (attempt {_attempt}/3) — retrying in 3s")
                            _t.sleep(3)
                        else:
                            logger.error(f"[autoupdater] {old_exe.name} still locked after 3 attempts — aborting update: {_e}")
                            tmp_exe.unlink(missing_ok=True)
                            return

            # En Windows se puede renombrar un ejecutable aunque esté en memoria
            logger.debug(f"[autoupdater] Renaming {exe_path.name} -> {old_exe.name}")
            os.rename(exe_path, old_exe)

            logger.debug(f"[autoupdater] Swapping {tmp_exe.name} -> {exe_path.name}")
            os.rename(tmp_exe, exe_path)

            logger.success("[autoupdater] Swap successful. Terminating process to trigger NSSM restart...")
            time.sleep(1)
            os._exit(0)
            
        except Exception as e:
            logger.error(f"[autoupdater] Update swap failed: {e}")
            # Intentar revertir si falló
            if old_exe.exists() and not exe_path.exists():
                try:
                    os.rename(old_exe, exe_path)
                except Exception:
                    pass

    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            logger.debug("[autoupdater] No releases published yet — skipping update check")
        elif e.response.status_code == 401:
            logger.error(
                "[autoupdater] 401 Unauthorized — posibles causas:\n"
                "  1. El PAT fue revocado (GitHub lo detecta automáticamente si se expuso)\n"
                "  2. El PAT fue cifrado con una fernet_key diferente a la que está en la DB\n"
                "  3. El PAT expiró\n"
                "  → Generá un PAT nuevo en github.com/settings/tokens y actualizalo con setup_db_cli"
            )
        else:
            logger.warning(f"[autoupdater] GitHub API error: {e.response.status_code} — {e.response.text[:200]}")
    except Exception as e:
        logger.error(f"[autoupdater] Update check failed: {e}")
