"""
agents/base.py
==============
Base class that all agents implement.
Defines the interface and provides shared logic:
retry, state, logging, file renaming.
"""

from __future__ import annotations
import hashlib
import re
import time
from abc import ABC, abstractmethod
from datetime import datetime
from pathlib import Path
from typing import Optional

from loguru import logger

from dispatcher import db, notifier


class AgentBase(ABC):

    name: str = "base"

    def __init__(self, config: dict):
        self.config        = config["agents"][self.name]
        self.destination   = self._validated_destination(self.config["destination_folder"])
        self.pattern       = self.config.get("rename_pattern") or ""
        self.max_retries   = self.config.get("max_retries", 3)
        self.retry_interval = self.config.get("retry_interval_min", 15)
        self.destination.mkdir(parents=True, exist_ok=True)
        logger.debug(f"[{self.name}] Initialized — destination={self.destination} max_retries={self.max_retries}")

    @staticmethod
    def _validated_destination(raw: str) -> Path:
        from shared.paths import BASE_DIR
        if any(part == ".." for part in Path(raw).parts):
            raise RuntimeError(f"destination_folder contains path traversal: {raw}")
        dest = Path(raw)
        if not dest.is_absolute():
            dest = (BASE_DIR / dest).resolve()
        return dest

    # ── Interface each agent implements ──────────────────────────────────────

    @abstractmethod
    def login(self) -> bool:
        """Login to the portal. Returns True on success."""
        ...

    @abstractmethod
    def list_files(self) -> list[dict]:
        """
        Lists available files to download.
        Each item: {"name": str, "path": str, ...}
        """
        ...

    @abstractmethod
    def download(self, item: dict) -> Optional[Path]:
        """
        Downloads a file. Returns the Path on success, None on failure.
        """
        ...

    def logout(self):
        """Closes the session. Optional — implement if the portal requires it."""
        pass

    def requires_intervention(self) -> bool:
        """
        Returns True if this agent needs human intervention before running
        (invalid session, OTP, etc.).
        Most agents return False — only Getnet and special cases override.
        """
        return False

    def intervention_reason(self) -> str:
        """Description of why intervention is required."""
        return "Intervention required"

    def capture_session(self) -> None:
        """
        Opens a visible browser so the user can log in and capture a session.
        Agents that need Playwright-based session capture must override this.
        Called by --capture-session PROVIDER via capture.py.
        """
        raise NotImplementedError(
            f"[{self.name}] does not support session capture — "
            "this agent uses API tokens or email, not a browser session"
        )

    # ── Main flow — the Dispatcher calls this ─────────────────────────────────

    def run(self, job_id: int, requested_at: datetime = None) -> dict:
        """
        Orchestrates the complete flow with retries.
        Do not override in agents — implement the abstract methods instead.

        requested_at: when the job was originally scheduled. Agents should use
        self.reference_date as the anchor for date-based report queries so that
        a job that runs late (e.g. PC was off) still fetches the correct date's data.
        """
        self.reference_date = (requested_at or datetime.now()).date()

        from datetime import datetime as dt, time as dtime, timedelta
        yesterday = self.reference_date - timedelta(days=1)
        self.period_to = dt.combine(yesterday, dtime(23, 59, 59))

        last_end = db.get_provider_last_period_end(self.name)
        if last_end:
            # Start from the beginning of the day after the last covered period
            self.period_from = dt.combine(last_end.date() + timedelta(days=1), dtime(0, 0, 0))
        else:
            self.period_from = dt.combine(yesterday, dtime(0, 0, 0))

        result = {
            "ok":         False,
            "downloaded": [],
            "skipped":    [],
            "errors":     [],
            "start":      datetime.now().isoformat(),
        }

        db.update_job_started(job_id, self.period_from, self.period_to)
        logger.info(f"[{self.name}] Starting execution (job {job_id}) from {self.period_from} to {self.period_to}")

        attempt = 0
        while attempt < self.max_retries:
            attempt += 1
            logger.info(f"[{self.name}] Attempt {attempt}/{self.max_retries}")

            try:
                if not self.login():
                    raise RuntimeError("Login failed")

                items = self.list_files()
                if not items:
                    logger.info(f"[{self.name}] No files available")
                    result["ok"] = True
                    break

                for item in items:
                    name = item.get("name", "")

                    if db.already_downloaded(self.name, name):
                        logger.debug(f"[{self.name}] Already downloaded: {name}")
                        result["skipped"].append(name)
                        continue

                    path = self.download(item)
                    if path:
                        sha256 = self._sha256(path)
                        db.insert_file(job_id, self.name, {
                            "original_name": name,
                            "final_name":    path.name,
                            "local_path":    str(path),
                            "file_date":     self._extract_date(name),
                            "bytes":         path.stat().st_size,
                            "sha256":        sha256,
                        })
                        result["downloaded"].append(str(path))
                        logger.info(f"[{self.name}] Downloaded: {path.name}")
                    else:
                        result["errors"].append(f"Could not download: {name}")

                result["ok"] = True
                break

            except InterventionRequired as e:
                logger.warning(f"[{self.name}] Intervention required: {e}")
                db.update_job_intervention(job_id, str(e))
                notifier.notify(self.name, "requires_intervention", str(e))
                result["errors"].append(str(e))
                return result

            except Exception as e:
                logger.warning(f"[{self.name}] Error on attempt {attempt}: {e}")
                result["errors"].append(str(e))
                db.increment_attempts(job_id)

                if attempt < self.max_retries:
                    logger.info(f"[{self.name}] Waiting {self.retry_interval} min before retrying...")
                    time.sleep(self.retry_interval * 60)

            finally:
                try:
                    self.logout()
                except Exception:
                    pass

        result["end"] = datetime.now().isoformat()

        if result["ok"]:
            db.update_job_success(job_id, files_downloaded=len(result["downloaded"]))
            db.update_status(self.name, {
                "result":        "ok",
                "files_today":   len(result["downloaded"]),
                "session_active": True,
            })
            logger.info(
                f"[{self.name}] Completed: "
                f"{len(result['downloaded'])} downloaded, "
                f"{len(result['skipped'])} skipped"
            )
        else:
            db.update_job_error(job_id, result["errors"][-1] if result["errors"] else "Unknown error")
            db.update_status(self.name, {
                "result":      "error",
                "error":       result["errors"][-1] if result["errors"] else "",
                "files_today": len(result["downloaded"]),
            })
            notifier.notify(
                self.name, "error",
                result["errors"][-1] if result["errors"] else "Unknown error"
            )

        return result

    # ── Shared helpers ────────────────────────────────────────────────────────

    def rename(self, original_name: str, extra_data: dict = None) -> str:
        """
        Renames a file according to the configured pattern.
        Agents can override to extract provider-specific data from the filename.
        """
        if not self.pattern:
            return original_name

        data = {
            "original":       original_name,
            "originfilename": Path(original_name).stem,
            "ext":            Path(original_name).suffix.lstrip("."),
            "date":           self.reference_date.strftime("%Y-%m-%d") if hasattr(self, "reference_date") else "",
            "merchant":       self.config.get("username") or "",
        }
        if extra_data:
            data.update(extra_data)

        try:
            return self.pattern.format(**data)
        except KeyError as e:
            logger.warning(f"[{self.name}] Renaming error: missing key {e} in pattern '{self.pattern}'")
            return original_name

    def _sha256(self, path: Path) -> str:
        sha = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(8192), b""):
                sha.update(chunk)
        return sha.hexdigest()

    def _extract_date(self, name: str) -> Optional["datetime"]:
        """Attempts to extract a date from the filename."""
        from datetime import date
        m = re.search(r'(\d{4})(\d{2})(\d{2})', name)
        if m:
            try:
                return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            except Exception:
                pass
        return None

    def _verify_content(self, content: bytes, name: str) -> bool:
        """Verifies that downloaded content is not an HTML error page."""
        if len(content) < 100:
            logger.warning(f"[{self.name}] File too small: {name}")
            return False
        if content[:15].lower().startswith(b"<!doctype") or content[:6].lower() == b"<html>":
            logger.warning(f"[{self.name}] Unexpected HTML response: {name}")
            return False
        return True


# ── Custom exceptions ─────────────────────────────────────────────────────────

class InterventionRequired(Exception):
    """
    Raised when the agent needs human action.
    The Dispatcher marks the job as requires_intervention
    and notifies the user — does not retry automatically.
    """
    pass


class SessionExpired(InterventionRequired):
    """The session expired and needs manual re-login."""
    pass


class OTPTimeout(InterventionRequired):
    """The OTP did not arrive within the expected time."""
    pass


class CaptchaUnresolved(InterventionRequired):
    """The captcha could not be resolved automatically."""
    pass
