"""
agents/fiserv.py
================
Download agent for Fiserv — TOTP/API mode only.

Authentication flow (confirmed from network capture 2026-04-16):
  1. POST /api/Users/requestOtp   → returns data.totpToken
  2. Generate OTP via pyotp.TOTP(shared_secret).now()
  3. POST /api/Users/authenticate → returns data.token (JWT)
  4. POST /settlement/Settlement/SettlementFileList  → file list
  5. POST /settlement/Settlement/downloadUploadedFile → file content

The JWT is cached in session_store for 8 hours to avoid re-authenticating
on every run. TOTP mode is fully automatic — no human intervention needed.

Config keys (extra_config JSON):
  auth_mode          "totp"  (only supported mode)
  totp_secret_enc    Fernet-encrypted TOTP shared secret
  capture_timeout_sec  unused (kept for schema compatibility)
"""
__version__ = "3.0.0"

from pathlib import Path
from typing import Optional

import pyotp
from curl_cffi import requests as cffi_requests
from loguru import logger

from agents.base import AgentBase, SessionExpired
from shared.session_store import SessionStore


API_BASE_URL = "https://merchantcenter.fiservapp.com"


class FiservAgent(AgentBase):

    name = "fiserv"

    def __init__(self, config: dict):
        super().__init__(config)
        self.session_store = SessionStore(self.name)
        self.jwt: Optional[str] = None

    # ── Intervention — never needed in TOTP mode ──────────────────────────────

    def requires_intervention(self) -> bool:
        return False

    def run(self, job_id: int, **kwargs) -> dict:
        try:
            return super().run(job_id, **kwargs)
        except SessionExpired:
            logger.info(f"[{self.name}] Session expired — re-logging automatically...")
            if not self._login_totp():
                raise
            logger.info(f"[{self.name}] Re-login successful — retrying job...")
            return super().run(job_id, **kwargs)

    # ── Login / logout ────────────────────────────────────────────────────────

    def login(self) -> bool:
        """Re-uses cached JWT when still valid; otherwise does full TOTP login."""
        cached = self.session_store.get()
        if cached and cached.get("jwt"):
            self.jwt = cached["jwt"]
            logger.info(f"[{self.name}] Reusing cached JWT")
            return True
        return self._login_totp()

    def logout(self):
        self.jwt = None

    # ── TOTP authentication ───────────────────────────────────────────────────

    def _login_totp(self) -> bool:
        """
        Authenticates via Fiserv REST API using a TOTP code.

        Step 1 — POST /api/Users/requestOtp
          Body: {username, password, deviceName:"", phoneNumber:"", typeAuthentication:"TOTP"}
          Response: data.totpToken

        Step 2 — generate OTP: pyotp.TOTP(secret).now()

        Step 3 — POST /api/Users/authenticate
          Body: {username, password, otp, deviceName:username, totpToken}
          Response: data.token  (JWT, valid ~8h)
        """
        try:
            import pyotp
        except ImportError:
            raise RuntimeError("pyotp is required — run: pip install pyotp")

        secret   = self.config.get("totp_secret", "")
        username = self.config.get("username", "")
        password = self.config.get("password", "")
        base     = self.config.get("api_base_url", API_BASE_URL).rstrip("/")

        # Diagnostic: log which keys arrived from extra_config decryption
        extra_keys = [k for k in self.config if k not in ("username", "password", "destination_folder", "rename_pattern", "provider", "enabled", "portal_url", "schedule_hour", "schedule_minute", "max_retries", "retry_interval_min")]
        logger.debug(f"[{self.name}] Config keys from extra_config: {extra_keys}")
        logger.debug(f"[{self.name}] totp_secret present: {bool(secret)}, length: {len(secret)}, starts_with: {secret[:6]!r}")

        if not secret:
            raise RuntimeError(
                "[fiserv] TOTP secret not configured — "
                "set totp_secret_enc in extra_config (DB)"
            )

        logger.debug(f"[{self.name}] username present: {bool(username)}, len={len(username)}")
        logger.debug(f"[{self.name}] password present: {bool(password)}, len={len(password)}")
        logger.info(f"[{self.name}] TOTP login — requesting OTP challenge...")

        with cffi_requests.Session(impersonate="chrome120") as client:
            # Step 1 — request OTP challenge
            r1 = client.post(
                f"{base}/api/Users/requestOtp",
                json={
                    "username":           username,
                    "password":           password,
                    "deviceName":         "",
                    "phoneNumber":        "",
                    "typeAuthentication": "TOTP",
                },
                headers={"Content-Type": "application/json"},
                timeout=30,
            )
            logger.debug(f"[{self.name}] requestOtp status={r1.status_code} body={r1.text[:300]}")
            if r1.status_code not in (200, 201):
                raise RuntimeError(
                    f"[fiserv] requestOtp failed: {r1.status_code} {r1.text[:200]}"
                )

            body1      = r1.json() if r1.content else {}
            data1      = body1.get("data", body1)
            totp_token = data1.get("totpToken") or data1.get("token") or ""
            logger.debug(f"[{self.name}] OTP challenge received — totpToken present: {bool(totp_token)}, len={len(totp_token)}")

            # Step 2 — generate OTP
            # Strip spaces/dashes/padding that some portals add for readability, then uppercase
            import re as _re
            secret_b32 = _re.sub(r'[\s\-=]', '', secret).upper()
            otp_code = pyotp.TOTP(secret_b32).now()
            logger.debug(f"[{self.name}] OTP generated: {otp_code}")

            # Step 3 — authenticate
            r2 = client.post(
                f"{base}/api/Users/authenticate",
                json={
                    "username":   username,
                    "password":   password,
                    "otp":        otp_code,
                    "deviceName": username,
                    "totpToken":  totp_token,
                },
                headers={"Content-Type": "application/json"},
                timeout=30,
            )
            logger.debug(f"[{self.name}] authenticate status={r2.status_code} body={r2.text[:500]}")
            if r2.status_code not in (200, 201):
                raise RuntimeError(
                    f"[fiserv] authenticate failed: {r2.status_code} {r2.text[:200]}"
                )

            body2 = r2.json() if r2.content else {}
            data2 = body2.get("data", body2)
            jwt   = (
                data2.get("token")
                or data2.get("jwt")
                or data2.get("access_token")
                or data2.get("accessToken")
            )
            if not jwt:
                raise RuntimeError(
                    f"[fiserv] No JWT in authenticate response: {body2}"
                )

        existing = self.session_store.get() or {}
        existing["jwt"] = jwt
        self.session_store.save(existing, expires_in_hours=8)
        self.jwt = jwt
        logger.info(f"[{self.name}] TOTP login successful — JWT cached (8h)")
        return True

    # ── List files ────────────────────────────────────────────────────────────

    def list_files(self) -> list[dict]:
        """
        POST /settlement/Settlement/SettlementFileList

        Uses period_from to period_to (set by AgentBase.run) as the anchor.

        Paginates with Skip/Take until a page returns fewer than PAGE_SIZE items.
        Each item returned: {"name": aggregatorHeaderId}
        """
        if not self.jwt:
            raise RuntimeError("[fiserv] JWT not set — call login() first")

        base     = self.config.get("api_base_url", API_BASE_URL).rstrip("/")
        url      = f"{base}/settlement/Settlement/SettlementFileList"
        from datetime import timedelta as _td
        end_date = self.period_to.date() + _td(days=1)
        from_str = f"{self.period_from.date().isoformat()}T03:00:00.000Z"
        to_str   = f"{end_date.isoformat()}T03:00:00.000Z"
        headers   = {
            "Authorization": f"Bearer {self.jwt}",
            "Content-Type":  "application/json",
        }

        logger.info(
            f"[{self.name}] Fetching file list {self.period_from.date()} → {self.period_to.date()}..."
        )

        PAGE_SIZE = 40
        items     = []
        skip      = 0

        with cffi_requests.Session(impersonate="chrome120") as client:
            while True:
                r = client.post(
                    url,
                    json={
                        "From":              from_str,
                        "To":                to_str,
                        "MerchantDocuments": [""],
                        "MerchantNumbers":   [],
                        "DateRangeType":     "SETT_DATE",
                        "Currency":          "",
                        "Skip":              skip,
                        "Take":              PAGE_SIZE,
                    },
                    headers=headers,
                    timeout=30,
                )

                if r.status_code == 401:
                    self.session_store.invalidate()
                    raise SessionExpired("[fiserv] JWT expired — re-login needed")

                if r.status_code != 200:
                    logger.error(
                        f"[{self.name}] list_files error — status={r.status_code} "
                        f"url={url} skip={skip}\n"
                        f"response: {r.text[:500]}"
                    )
                    raise RuntimeError(
                        f"[fiserv] list_files API error: {r.status_code} {r.text[:300]}"
                    )

                body = r.json() if r.content else {}
                page = (
                    body if isinstance(body, list)
                    else body.get("data", body.get("files", []))
                )

                for entry in page:
                    logger.debug(f"[{self.name}] entry fields: {list(entry.keys()) if isinstance(entry, dict) else entry}")
                    name = (
                        entry.get("aggregatorHeaderId")
                        or entry.get("fileName")
                        or entry.get("name")
                        or ""
                    )
                    if name:
                        items.append({"name": name})

                logger.debug(f"[{self.name}] Page skip={skip}: {len(page)} record(s)")

                if len(page) < PAGE_SIZE:
                    break
                skip += PAGE_SIZE

        logger.info(f"[{self.name}] {len(items)} file(s) found")
        return items

    # ── Download ──────────────────────────────────────────────────────────────

    def download(self, item: dict) -> Optional[Path]:
        """
        POST /settlement/Settlement/downloadUploadedFile
        Body: {"fileName": aggregatorHeaderId}
        """
        if not self.jwt:
            raise RuntimeError("[fiserv] JWT not set — call login() first")

        name = item.get("name", "")
        if not name:
            logger.error(f"[{self.name}] download() called with empty name")
            return None

        base    = self.config.get("api_base_url", API_BASE_URL).rstrip("/")
        url     = f"{base}/settlement/Settlement/downloadUploadedFile"
        dest    = self.destination / self.rename(name)
        headers = {
            "Authorization": f"Bearer {self.jwt}",
            "Content-Type":  "application/json",
        }

        logger.info(f"[{self.name}] Downloading: {name} → {dest.name}")
        logger.debug(f"[{self.name}] POST {url} body={{'fileName': '{name}'}}")
        try:
            with cffi_requests.Session(impersonate="chrome120") as client:
                r = client.post(url, json={"fileName": name}, headers=headers, timeout=120)

            logger.debug(
                f"[{self.name}] Response: status={r.status_code} "
                f"content-type={r.headers.get('content-type', '?')} "
                f"size={len(r.content):,} bytes"
            )

            if r.status_code == 401:
                self.session_store.invalidate()
                raise SessionExpired("[fiserv] JWT expired during download")
            if r.status_code != 200:
                logger.error(
                    f"[{self.name}] Download failed — status={r.status_code} file={name}\n"
                    f"  URL: {url}\n"
                    f"  Body: {r.text[:500] or '(empty)'}"
                )
                return None

            content = r.content
            if not content:
                logger.error(f"[{self.name}] Empty response body for {name}")
                return None

            if not self._verify_content(content, name):
                return None

            dest.write_bytes(content)
            logger.info(f"[{self.name}] Saved: {dest} ({len(content):,} bytes)")
            return dest

        except SessionExpired:
            raise
        except Exception as e:
            logger.error(f"[{self.name}] Download exception for {name}: {type(e).__name__}: {e}")
            return None
