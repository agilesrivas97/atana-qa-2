"""
shared/session_store.py
=======================
Session persistence (JWT, cookies, tokens) in SQL Server.
Data is encrypted with Fernet before saving.
"""

import json
from datetime import datetime, timedelta
from typing import Optional

from cryptography.fernet import Fernet
from loguru import logger  # used in get/save/invalidate

from dispatcher import db


_fernet: Optional[Fernet] = None


def init(key: str = None):
    """Initialize the encryptor. Raises if no key is provided."""
    global _fernet
    if key:
        _fernet = Fernet(key.encode())
    else:
        raise RuntimeError(
            "session_key not configured — generate one with:\n"
            "  python -c \"from cryptography.fernet import Fernet; "
            "print(Fernet.generate_key().decode())\"\n"
            "and set it in system_config (key_config='session_key')"
        )


class SessionStore:

    def __init__(self, provider: str):
        self.provider = provider

    def save(self, data: dict, expires_in_hours: int = 8):
        """Encrypts and saves the session to SQL Server."""
        if _fernet is None:
            logger.warning("SessionStore not initialized — call init() first")
            return

        try:
            json_bytes     = json.dumps(data).encode()
            encrypted_data = _fernet.encrypt(json_bytes)
            expires_at     = datetime.now() + timedelta(hours=expires_in_hours)

            db.save_session(self.provider, encrypted_data, expires_at)
            logger.debug(f"[{self.provider}] Session saved (expires in {expires_in_hours}h)")

        except Exception as e:
            logger.error(f"[{self.provider}] Error saving session: {e}")

    def get(self) -> Optional[dict]:
        """Reads and decrypts the session from SQL Server."""
        if _fernet is None:
            return None

        try:
            encrypted_data = db.get_session(self.provider)
            if not encrypted_data:
                return None

            json_bytes = _fernet.decrypt(encrypted_data)
            return json.loads(json_bytes)

        except Exception as e:
            logger.debug(f"[{self.provider}] Session not available: {e}")
            return None

    def is_valid(self) -> bool:
        """Returns True if there is a valid, non-expired session."""
        return self.get() is not None

    def invalidate(self):
        """Invalidates the current session."""
        try:
            db.invalidate_session(self.provider)
            logger.info(f"[{self.provider}] Session invalidated")
        except Exception as e:
            logger.error(f"[{self.provider}] Error invalidating session: {e}")
