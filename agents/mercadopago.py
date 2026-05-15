"""
agents/mercadopago.py
=====================
Download agent for MercadoPago — Release Report (release_report).
Pure REST API with Bearer token. Supports multiple accounts.

Flow per account:
  1. login()      → GET /v1/account/release_report/list (validates each token)
  2. list_files() → configures columns, creates report, polls until ready
  3. download()   → GET /v1/account/release_report/{file_name} → CSV
"""
from __future__ import annotations

__version__ = "1.0.0"

import time
from pathlib import Path
from typing import Optional

import httpx
from loguru import logger

from agents.base import AgentBase, InterventionRequired
from dispatcher import db


BASE_URL = "https://api.mercadopago.com"

# Full columns from the official MercadoPago release report documentation.
# Update here if MP adds new columns in the future.
COLUMNS = [
    {"key": "DATE"},                         # Balance impact date
    {"key": "SOURCE_ID"},                    # MP transaction ID (key)
    {"key": "EXTERNAL_REFERENCE"},           # Your ID (key)
    {"key": "RECORD_TYPE"},                  # release / balance / total
    {"key": "DESCRIPTION"},                  # movement type

    {"key": "NET_CREDIT_AMOUNT"},            # incoming money
    {"key": "NET_DEBIT_AMOUNT"},             # outgoing money
    {"key": "BALANCE_AMOUNT"},               # balance after movement

    {"key": "GROSS_AMOUNT"},                 # gross amount
    {"key": "MP_FEE_AMOUNT"},               # MP commission
    {"key": "FINANCING_FEE_AMOUNT"},         # installment fee
    {"key": "SHIPPING_FEE_AMOUNT"},          # shipping fee
    {"key": "TAXES_AMOUNT"},                 # taxes

    {"key": "TRANSACTION_APPROVAL_DATE"},    # actual payment date
    {"key": "PAYMENT_METHOD"},               # visa, account_money, etc
    {"key": "PAYMENT_METHOD_TYPE"},          # credit_card, etc

    {"key": "ORDER_ID"},
    {"key": "ORDER_MP"},
    {"key": "EXTERNAL_POS_ID"},

    {"key": "METADATA"},                     # extra info (useful for integrations)
    {"key": "SALE_DETAIL"},                  # sale detail (useful for ecommerce)

    {"key": "CURRENCY"},
]

_COLUMNS_KEYS = {c["key"] for c in COLUMNS}


class MercadoPagoAgent(AgentBase):

    name = "mercadopago"

    def __init__(self, config: dict):
        super().__init__(config)
        self.accounts      = self.config.get("accounts", [])
        self.tz_display    = self.config.get("timezone", "GMT-03")
        self.separator     = self.config.get("separator", ";")
        self.poll_interval = self.config.get("poll_interval_seg", 10)
        self.poll_timeout  = self.config.get("poll_timeout_seg", 300)
        self.session       = httpx.Client(timeout=30, follow_redirects=True)

    # ── Base interface ────────────────────────────────────────────────────────

    def login(self) -> bool:
        """
        Validates all tokens. Returns True if at least one is valid.
        If all fail → InterventionRequired.
        """
        if not self.accounts:
            raise InterventionRequired(
                "MercadoPago: no accounts configured in config.json"
            )

        any_valid = False
        for account in self.accounts:
            alias = account.get("alias", "?")
            token = account.get("access_token", "")
            if not token:
                logger.warning(f"[{self.name}] [{alias}] empty access_token — skipping")
                continue

            resp = self.session.get(
                f"{BASE_URL}/v1/account/release_report/list",
                headers=self._headers(token),
            )

            if resp.status_code == 200:
                logger.info(f"[{self.name}] [{alias}] Token valid")
                any_valid = True
            elif resp.status_code in (401, 403):
                logger.error(
                    f"[{self.name}] [{alias}] Invalid or expired token "
                    f"(HTTP {resp.status_code})"
                )
            else:
                logger.warning(
                    f"[{self.name}] [{alias}] Unexpected HTTP {resp.status_code} "
                    f"— treating as invalid"
                )

        if not any_valid:
            raise InterventionRequired(
                "MercadoPago: all access_tokens are invalid or expired"
            )
        return True

    def list_files(self) -> list[dict]:
        """
        Per account with a valid token:
          - Configure report columns (idempotent)
          - Create report for the configured period
          - Wait until processed
          - Return item with metadata to download
        """
        # MP uses Argentina timezone (UTC-3): midnight ART = 03:00 UTC, end-of-day ART = next day 02:59:59 UTC
        from datetime import timedelta as _td
        end_date  = self.period_to.date() + _td(days=1)
        begin_str = f"{self.period_from.date().isoformat()}T03:00:00Z"
        end_str   = f"{end_date.isoformat()}T02:59:59Z"

        items = []
        for account in self.accounts:
            alias = account.get("alias", "mp")
            token = account.get("access_token", "")
            if not token:
                continue

            list_resp = self.session.get(
                f"{BASE_URL}/v1/account/release_report/list",
                headers=self._headers(token),
            )
            if list_resp.status_code not in (200,):
                logger.warning(
                    f"[{self.name}] [{alias}] Token not valid in list_files — skipping"
                )
                continue

            existing = self._find_in_list(
                list_resp.json() if list_resp.content else [], begin_str, end_str
            )
            if existing:
                status_mp = existing.get("status")
                file_name = existing.get("file_name")
                logger.info(
                    f"[{self.name}] [{alias}] Existing report in MP: "
                    f"id={existing.get('id')} status={status_mp} file={file_name}"
                )
                if file_name:
                    if db.already_downloaded(self.name, file_name):
                        logger.info(f"[{self.name}] [{alias}] Already downloaded: {file_name} — skipping")
                        continue
                    logger.info(
                        f"[{self.name}] [{alias}] File available (status='{status_mp}') — downloading directly"
                    )
                    items.append({
                        "name":         file_name,
                        "file_name":    file_name,
                        "access_token": token,
                        "alias":        alias,
                        "end":          self.period_to.date(),
                    })
                    continue

                elif status_mp in ("pending", "processing", "generating", "taken", "data-pending"):
                    logger.info(
                        f"[{self.name}] [{alias}] Report in progress "
                        f"(id={existing.get('id')}, status='{status_mp}') — waiting {self.poll_timeout}s..."
                    )
                    ready_report = self._wait_for_ready(
                        token, alias, existing.get("id")
                    )
                    if ready_report and ready_report.get("file_name"):
                        fn = ready_report["file_name"]
                        if db.already_downloaded(self.name, fn):
                            logger.info(f"[{self.name}] [{alias}] Already downloaded: {fn} — skipping")
                            continue
                        items.append({
                            "name":         fn,
                            "file_name":    fn,
                            "_content":     ready_report.get("_content"),
                            "access_token": token,
                            "alias":        alias,
                            "end":          self.period_to.date(),
                        })
                        continue
                    raise RuntimeError(
                        f"MercadoPago [{alias}]: report not ready yet "
                        f"(id={existing.get('id')}) — retrying next cycle"
                    )

            # Not found — configure columns and create new report
            self._configure_columns(token, alias)

            logger.info(
                f"[{self.name}] [{alias}] Creating report {self.period_from.date()} -> {self.period_to.date()}"
            )
            resp = self.session.post(
                f"{BASE_URL}/v1/account/release_report",
                headers=self._headers(token),
                json={"begin_date": begin_str, "end_date": end_str},
            )

            if resp.status_code not in (200, 201, 202):
                logger.error(
                    f"[{self.name}] [{alias}] Error creating report — "
                    f"HTTP {resp.status_code}: {resp.text[:300]}"
                )
                continue

            data      = resp.json() if resp.content else {}
            report_id = data.get("id")
            logger.info(
                f"[{self.name}] [{alias}] Report created: "
                f"id={report_id} status={data.get('status')}"
            )

            ready_report = self._wait_for_ready(token, alias, report_id)
            if not ready_report:
                raise RuntimeError(
                    f"MercadoPago [{alias}]: report id={report_id} not ready yet "
                    f"— retrying next cycle"
                )

            file_name = ready_report.get("file_name")
            if not file_name:
                logger.error(
                    f"[{self.name}] [{alias}] Report has no file_name — skipping"
                )
                continue

            items.append({
                "name":         file_name,
                "file_name":    file_name,
                "access_token": token,
                "alias":        alias,
                "end":          self.period_to.date(),
            })

        logger.info(f"[{self.name}] {len(items)} report(s) ready to download")
        return items

    def download(self, item: dict) -> Optional[Path]:
        """Downloads the report CSV and saves it to disk."""
        alias     = item["alias"]
        file_name = item["file_name"]
        token     = item["access_token"]

        if item.get("_content"):
            logger.info(f"[{self.name}] [{alias}] Using cached content of {file_name}")
            content = item["_content"]
        else:
            logger.info(f"[{self.name}] [{alias}] Downloading {file_name}")
            resp = self.session.get(
                f"{BASE_URL}/v1/account/release_report/{file_name}",
                headers={**self._headers(token), "Accept": "text/csv"},
                timeout=60,
            )
            if resp.status_code != 200:
                logger.error(
                    f"[{self.name}] [{alias}] Error downloading CSV — "
                    f"HTTP {resp.status_code}: {resp.text[:200]}"
                )
                return None
            content = resp.content

        if not self._verify_content(content, file_name):
            return None

        final_name = self.rename(file_name, {"alias": alias})
        path       = self.destination / final_name
        path.write_bytes(content)
        logger.info(
            f"[{self.name}] [{alias}] Saved: {path} ({path.stat().st_size:,} bytes)"
        )
        return path

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _headers(self, token: str) -> dict:
        return {
            "Authorization": f"Bearer {token}",
            "Content-Type":  "application/json",
            "User-Agent":    "atana-agents/1.0",
        }

    def _find_in_list(
        self, reports: list, begin_str: str, end_str: str
    ) -> Optional[dict]:
        """
        Searches the MP report list for one matching the period.
        Compares begin_date and end_date ignoring milliseconds and minor timezones.
        Returns the most recent matching report, or None.
        """
        begin_prefix = begin_str[:10]  # "YYYY-MM-DD"
        end_prefix   = end_str[:10]

        matches = [
            r for r in reports
            if (r.get("begin_date", "")[:10] == begin_prefix
                and r.get("end_date", "")[:10] == end_prefix)
        ]
        if not matches:
            return None

        order = {"processed": 0, "generating": 1, "pending": 2}
        matches.sort(key=lambda r: (
            order.get(r.get("status", ""), 9),
            r.get("date_created", ""),
        ), reverse=False)
        return matches[0]

    def _configure_columns(self, token: str, alias: str) -> None:
        """
        Configures the full column set for the report — idempotent.
        Flow:
          1. GET /config to read current configuration
          2. Compare current keys against _COLUMNS_KEYS
          3. If already matching → skip (idempotent, do not touch)
          4. If different → PUT to update existing config
          5. If no config existed → POST to create from scratch
        Silent failure — does not block report creation.
        """
        try:
            get_resp = self.session.get(
                f"{BASE_URL}/v1/account/release_report/config",
                headers=self._headers(token),
            )

            has_config    = get_resp.status_code == 200
            current_config = get_resp.json() if has_config else {}

            if has_config:
                current_keys = {c["key"] for c in current_config.get("columns", [])}
                if current_keys == _COLUMNS_KEYS:
                    logger.info(
                        f"[{self.name}] [{alias}] Column configuration already up to date — skipping"
                    )
                    return
                logger.info(
                    f"[{self.name}] [{alias}] Columns outdated — "
                    f"current: {len(current_keys)} | expected: {len(_COLUMNS_KEYS)} — updating with PUT"
                )
            else:
                logger.info(
                    f"[{self.name}] [{alias}] No existing config (HTTP {get_resp.status_code}) — creating with POST"
                )

            payload = {
                **current_config,
                "columns":                   COLUMNS,
                "file_name_prefix":          current_config.get("file_name_prefix", "conciliation-settlement-report"),
                "separator":                 current_config.get("separator", ";"),
                "display_timezone":          current_config.get("display_timezone", "GMT-03"),
                "report_translation":        current_config.get("report_translation", "es"),
                "include_withdrawal_at_end": current_config.get("include_withdrawal_at_end", False),
                "check_available_balance":   current_config.get("check_available_balance", False),
                "compensate_detail":         current_config.get("compensate_detail", False),
                "execute_after_withdrawal":  False,
                "scheduled":                 current_config.get("scheduled", False),
            }

            method = self.session.put if has_config else self.session.post
            verb   = "PUT" if has_config else "POST"

            resp = method(
                f"{BASE_URL}/v1/account/release_report/config",
                headers=self._headers(token),
                json=payload,
            )
            if resp.status_code in (200, 201):
                logger.info(
                    f"[{self.name}] [{alias}] Column config {verb} successful — "
                    f"{len(COLUMNS)} columns configured"
                )
            else:
                logger.warning(
                    f"[{self.name}] [{alias}] Could not configure columns — "
                    f"HTTP {resp.status_code}: {resp.text[:300]}"
                )
        except Exception as e:
            logger.warning(f"[{self.name}] [{alias}] Error configuring columns: {e}")

    def _wait_for_ready(self, token: str, alias: str, report_id: int) -> Optional[dict]:
        """
        Uses GET /v1/account/release_report/task/{report_id} to directly poll
        the report generation status.

        The task returns a 'file_name' field when the report is ready.
        Known statuses: 'generating', 'pending' → wait; 'processed' → download.

        Flow:
          1. GET /task/{report_id} → get status and file_name
          2. If status == 'processed' and file_name available → download
          3. Otherwise → wait self.poll_interval and retry
        """
        logger.info(
            f"[{self.name}] [{alias}] Polling task id={report_id} "
            f"(every {self.poll_interval}s, timeout {self.poll_timeout}s)"
        )
        deadline = time.monotonic() + self.poll_timeout

        while time.monotonic() < deadline:
            task_resp = self.session.get(
                f"{BASE_URL}/v1/account/release_report/task/{report_id}",
                headers=self._headers(token),
            )

            if task_resp.status_code != 200:
                logger.warning(
                    f"[{self.name}] [{alias}] Task poll HTTP {task_resp.status_code}: "
                    f"{task_resp.text[:200]}"
                )
                time.sleep(self.poll_interval)
                continue

            task      = task_resp.json() if task_resp.content else {}
            status    = task.get("status", "")
            file_name = task.get("file_name")

            logger.info(
                f"[{self.name}] [{alias}] Task id={report_id} -> "
                f"status='{status}' file='{file_name}'"
            )

            if status in ("failed", "deleted"):
                logger.error(
                    f"[{self.name}] [{alias}] Task id={report_id} ended with status='{status}' — aborting"
                )
                return None

            if status in ("pending", "processing"):
                logger.debug(
                    f"[{self.name}] [{alias}] Task in progress ('{status}') — waiting {self.poll_interval}s..."
                )
                time.sleep(self.poll_interval)
                continue

            if status == "processed":
                if not file_name:
                    logger.warning(
                        f"[{self.name}] [{alias}] Task processed but no file_name — waiting..."
                    )
                    time.sleep(self.poll_interval)
                    continue

                logger.info(
                    f"[{self.name}] [{alias}] Report processed. Downloading {file_name}..."
                )
                dl_resp = self.session.get(
                    f"{BASE_URL}/v1/account/release_report/{file_name}",
                    headers={**self._headers(token), "Accept": "text/csv"},
                    timeout=60,
                )
                logger.info(
                    f"[{self.name}] [{alias}] Download: "
                    f"HTTP {dl_resp.status_code} | "
                    f"{len(dl_resp.content):,} bytes | "
                    f"Content-Type: {dl_resp.headers.get('content-type', '?')}"
                )

                if dl_resp.status_code == 200 and self._verify_content(dl_resp.content, file_name):
                    logger.info(
                        f"[{self.name}] [{alias}] Download successful: {file_name}"
                    )
                    return {
                        "id":        report_id,
                        "file_name": file_name,
                        "status":    status,
                        "_content":  dl_resp.content,
                    }

                logger.warning(
                    f"[{self.name}] [{alias}] Download failed or invalid content — retrying"
                )
                time.sleep(self.poll_interval)
                continue

            logger.warning(
                f"[{self.name}] [{alias}] Unknown status '{status}' — waiting..."
            )
            time.sleep(self.poll_interval)

        logger.error(
            f"[{self.name}] [{alias}] Timeout: task id={report_id} "
            f"not available in {self.poll_timeout}s"
        )
        return None
