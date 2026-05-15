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

A real Chromium browser (Playwright) is used to bypass Radware Bot Manager.
A single browser context is shared across login, list_files and download for
the duration of each job, so Radware cookies set on the warm-up GET persist
through all subsequent API calls.

A fresh JWT is obtained on every job run — no caching.

Config keys (extra_config JSON):
  auth_mode          "totp"  (only supported mode)
  totp_secret_enc    Fernet-encrypted TOTP shared secret
  api_base_url       optional override (default: API_BASE_URL)
"""
__version__ = "4.0.0"

import json as _json
import re as _re
from pathlib import Path
from typing import Optional

import pyotp
from loguru import logger

from agents.base import AgentBase


API_BASE_URL = "https://merchantcenter.fiservapp.com"


class FiservAgent(AgentBase):

    name = "fiserv"

    def __init__(self, config: dict):
        super().__init__(config)
        self._base    = self.config.get("api_base_url", API_BASE_URL).rstrip("/")
        self._pw      = None
        self._browser = None
        self._pw_ctx  = None
        self.jwt: Optional[str] = None

    # ── Intervention — never needed in TOTP mode ──────────────────────────────

    def requires_intervention(self) -> bool:
        return False

    # ── Login / logout ────────────────────────────────────────────────────────

    def login(self) -> bool:
        """Always performs a full TOTP login — no JWT caching."""
        return self._login_totp()

    def logout(self):
        self.jwt = None
        if self._pw_ctx:
            try:
                self._pw_ctx.close()
            except Exception:
                pass
        if self._browser:
            try:
                self._browser.close()
            except Exception:
                pass
        if self._pw:
            try:
                self._pw.stop()
            except Exception:
                pass
        self._pw_ctx = self._browser = self._pw = None

    def _browser_headers(self, extra: dict = None) -> dict:
        headers = {
            "Accept":             "application/json, text/plain, */*",
            "Accept-Language":    "es-AR,es;q=0.9,en-US;q=0.8,en;q=0.7",
            "Accept-Encoding":    "gzip, deflate, br",
            "Origin":             self._base,
            "Referer":            self._base + "/",
            "sec-ch-ua":          '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
            "sec-ch-ua-mobile":   "?0",
            "sec-ch-ua-platform": '"Windows"',
            "Sec-Fetch-Dest":     "empty",
            "Sec-Fetch-Mode":     "cors",
            "Sec-Fetch-Site":     "same-origin",
            "Content-Type":       "application/json",
        }
        if extra:
            headers.update(extra)
        return headers

    # ── Playwright context ────────────────────────────────────────────────────

    def _ensure_pw_ctx(self):
        """Creates the shared Playwright browser context once per job.
        The warm-up GET lets Radware run its JS challenge and set cookies
        that are reused by all subsequent API calls in the same context."""
        if self._pw_ctx is not None:
            return

        from playwright.sync_api import sync_playwright
        try:
            from playwright_stealth import stealth_sync
        except ImportError:
            stealth_sync = None

        self._pw      = sync_playwright().start()
        self._browser = self._pw.chromium.launch(headless=True)
        self._pw_ctx  = self._browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            )
        )
        page = self._pw_ctx.new_page()
        if stealth_sync:
            stealth_sync(page)
        try:
            page.goto(self._base, wait_until="networkidle", timeout=30_000)
        except Exception:
            page.goto(self._base, wait_until="domcontentloaded", timeout=30_000)
        page.close()
        logger.info(f"[{self.name}] Playwright context ready (Radware warm-up done)")

    def _pw_post(self, url: str, payload: dict, extra_headers: dict = None) -> dict:
        """POST via shared Playwright context — returns parsed JSON."""
        self._ensure_pw_ctx()
        resp = self._pw_ctx.request.post(
            url,
            data=_json.dumps(payload),
            headers=self._browser_headers(extra_headers),
        )
        text = resp.text()
        logger.debug(f"[{self.name}] POST {url} → status={resp.status} body={text[:300]}")
        if resp.status not in (200, 201):
            raise RuntimeError(f"[fiserv][pw] HTTP {resp.status}: {text[:200]}")
        if "Radware Bot Manager" in text or ("captcha" in text.lower() and "<html" in text.lower()):
            raise RuntimeError("[fiserv][pw] Radware still blocking — check stealth setup")
        try:
            return _json.loads(text) if text.strip() else {}
        except ValueError:
            raise RuntimeError(f"[fiserv][pw] Non-JSON response: {text[:300]}")

    def _pw_bytes(self, url: str, payload: dict, extra_headers: dict = None) -> bytes:
        """POST via shared Playwright context — returns raw bytes (for file downloads)."""
        self._ensure_pw_ctx()
        resp = self._pw_ctx.request.post(
            url,
            data=_json.dumps(payload),
            headers=self._browser_headers(extra_headers),
        )
        if resp.status == 401:
            raise RuntimeError("[fiserv][pw] JWT expired (401)")
        if resp.status != 200:
            raise RuntimeError(f"[fiserv][pw] Download HTTP {resp.status}: {resp.text()[:200]}")
        content = resp.body()
        logger.debug(
            f"[{self.name}] POST {url} → status={resp.status} "
            f"content-type={resp.headers.get('content-type', '?')} "
            f"size={len(content):,} bytes"
        )
        return content

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
          Response: data.token  (JWT)
        """
        secret   = self.config.get("totp_secret", "")
        username = self.config.get("username", "")
        password = self.config.get("password", "")

        extra_keys = [k for k in self.config if k not in (
            "username", "password", "destination_folder", "rename_pattern",
            "provider", "enabled", "portal_url", "schedule_hour",
            "schedule_minute", "max_retries", "retry_interval_min",
        )]
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

        # Step 1 — request OTP challenge
        body1 = self._pw_post(
            f"{self._base}/api/Users/requestOtp",
            {
                "username":           username,
                "password":           password,
                "deviceName":         "",
                "phoneNumber":        "",
                "typeAuthentication": "TOTP",
            },
        )
        data1      = body1.get("data", body1)
        totp_token = data1.get("totpToken") or data1.get("token") or ""
        logger.debug(f"[{self.name}] OTP challenge received — totpToken present: {bool(totp_token)}, len={len(totp_token)}")

        # Step 2 — generate OTP
        secret_b32 = _re.sub(r'[\s\-=]', '', secret).upper()
        otp_code   = pyotp.TOTP(secret_b32).now()
        logger.debug(f"[{self.name}] OTP generated: {otp_code}")

        # Step 3 — authenticate
        body2 = self._pw_post(
            f"{self._base}/api/Users/authenticate",
            {
                "username":   username,
                "password":   password,
                "otp":        otp_code,
                "deviceName": username,
                "totpToken":  totp_token,
            },
        )
        data2 = body2.get("data", body2)
        jwt   = (
            data2.get("token")
            or data2.get("jwt")
            or data2.get("access_token")
            or data2.get("accessToken")
        )
        if not jwt:
            raise RuntimeError(f"[fiserv][pw] No JWT in authenticate response: {body2}")

        self.jwt = jwt
        logger.info(f"[{self.name}] TOTP login successful")
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

        url      = f"{self._base}/settlement/Settlement/SettlementFileList"
        from datetime import timedelta as _td
        end_date = self.period_to.date() + _td(days=1)
        from_str = f"{self.period_from.date().isoformat()}T03:00:00.000Z"
        to_str   = f"{end_date.isoformat()}T03:00:00.000Z"
        auth     = {"Authorization": f"Bearer {self.jwt}"}

        logger.info(
            f"[{self.name}] Fetching file list {self.period_from.date()} → {self.period_to.date()}..."
        )

        PAGE_SIZE = 40
        items     = []
        skip      = 0

        while True:
            body = self._pw_post(
                url,
                {
                    "From":              from_str,
                    "To":                to_str,
                    "MerchantDocuments": [""],
                    "MerchantNumbers":   [],
                    "DateRangeType":     "SETT_DATE",
                    "Currency":          "",
                    "Skip":              skip,
                    "Take":              PAGE_SIZE,
                },
                extra_headers=auth,
            )
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

        url  = f"{self._base}/settlement/Settlement/downloadUploadedFile"
        dest = self.destination / self.rename(name)
        auth = {"Authorization": f"Bearer {self.jwt}"}

        logger.info(f"[{self.name}] Downloading: {name} → {dest.name}")
        try:
            content = self._pw_bytes(url, {"fileName": name}, extra_headers=auth)

            if not content:
                logger.error(f"[{self.name}] Empty response body for {name}")
                return None

            if not self._verify_content(content, name):
                return None

            dest.write_bytes(content)
            logger.info(f"[{self.name}] Saved: {dest} ({len(content):,} bytes)")
            return dest

        except Exception as e:
            logger.error(f"[{self.name}] Download exception for {name}: {type(e).__name__}: {e}")
            return None
