"""
shared/imap_reader.py
=====================
OTP reader via IMAP. Works with Gmail, Outlook, any hosting.
No external dependencies — stdlib imaplib only.
"""

import email
import imaplib
import re
import time
from email.header import decode_header
from loguru import logger


class ImapReader:

    def __init__(self, host: str, username: str, password: str, port: int = 993):
        self.host     = host
        self.username = username
        self.password = password
        self.port     = port

    def wait_for_code(
        self,
        sender:          str,
        subject_contains: str  = None,
        regex:           str  = r'\b(\d{4,8})\b',
        timeout:         int  = 60,
        poll_interval:   int  = 3,
    ) -> str:
        """
        Waits for an email and extracts the numeric code.

        Args:
            sender:           Expected sender email address
            subject_contains: Text that must appear in the subject (optional)
            regex:            Regex to extract the code from the body
            timeout:          Maximum wait time in seconds
            poll_interval:    Seconds between each attempt

        Returns:
            The extracted code as a string

        Raises:
            TimeoutError: If the code does not arrive within the timeout
        """
        logger.info(f"Waiting for OTP from {sender} (timeout: {timeout}s)")
        start_time = time.time()

        while time.time() - start_time < timeout:
            try:
                code = self._find_code(sender, subject_contains, regex)
                if code:
                    logger.info(f"OTP received: {code}")
                    return code
            except Exception as e:
                logger.warning(f"IMAP error: {e}")

            time.sleep(poll_interval)

        raise TimeoutError(f"OTP not received in {timeout}s from {sender}")

    def _find_code(
        self,
        sender:          str,
        subject_contains: str,
        regex:           str,
    ) -> str | None:
        """Connects to IMAP, searches for the email, and extracts the code."""

        mail = imaplib.IMAP4_SSL(self.host, self.port)
        mail.login(self.username, self.password)
        mail.select("INBOX")

        criteria = f'FROM "{sender}" UNSEEN'
        _, ids = mail.search(None, criteria)

        if not ids[0]:
            mail.logout()
            return None

        last_id = ids[0].split()[-1]
        _, data  = mail.fetch(last_id, "(RFC822)")

        if not data or not data[0]:
            mail.logout()
            return None

        msg = email.message_from_bytes(data[0][1])

        if subject_contains:
            subject = self._decode_header(msg.get("Subject", ""))
            if subject_contains.lower() not in subject.lower():
                mail.logout()
                return None

        body = self._get_body(msg)
        mail.logout()

        m = re.search(regex, body)
        return m.group(1) if m else None

    def _get_body(self, msg) -> str:
        """Extracts the text body from the email (plain text or HTML)."""
        body = ""

        if msg.is_multipart():
            for part in msg.walk():
                ct = part.get_content_type()
                if ct == "text/plain":
                    payload = part.get_payload(decode=True)
                    if payload:
                        body += payload.decode(
                            part.get_content_charset() or "utf-8",
                            errors="ignore"
                        )
        else:
            payload = msg.get_payload(decode=True)
            if payload:
                body = payload.decode(
                    msg.get_content_charset() or "utf-8",
                    errors="ignore"
                )

        return body

    def _decode_header(self, value: str) -> str:
        """Decodes email headers (may be base64-encoded)."""
        try:
            parts = decode_header(value)
            result = ""
            for text, charset in parts:
                if isinstance(text, bytes):
                    result += text.decode(charset or "utf-8", errors="ignore")
                else:
                    result += text
            return result
        except Exception:
            return value
