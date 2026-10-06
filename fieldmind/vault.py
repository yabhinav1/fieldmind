"""Seals what must not be readable from the device's files alone.

Qdrant Edge and SQLite write payloads and log lines to disk as they are. The
vault encrypts the text of private and masked memories before it reaches the
shard, and every activity-log line before it reaches the journal, with a key
kept in a file only the device's user can read. Vectors are not encrypted: they
are needed for search and cannot be turned back into text.

This protects against the shard and journal files being copied off the device
without the key file. It does not protect against someone who can read the key
file too; use full-disk encryption for that.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

MARK = "enc1:"


class Vault:
    def __init__(self, key: bytes | None):
        self._fernet = None
        if key is not None:
            from cryptography.fernet import Fernet

            self._fernet = Fernet(key)

    @classmethod
    def open(cls, key_file: Path | None) -> Vault:
        """Load the key, creating one on first use. ``None`` gives a vault that seals nothing."""
        if key_file is None:
            return cls(None)
        try:
            from cryptography.fernet import Fernet
        except ImportError:
            return cls(None)
        if key_file.exists():
            return cls(key_file.read_bytes().strip())
        key_file.parent.mkdir(parents=True, exist_ok=True)
        key = Fernet.generate_key()
        fd = os.open(key_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, stat.S_IRUSR | stat.S_IWUSR)
        with os.fdopen(fd, "wb") as out:
            out.write(key)
        return cls(key)

    @property
    def active(self) -> bool:
        return self._fernet is not None

    def seal(self, text: str | None) -> str | None:
        if not self.active or text is None or text.startswith(MARK):
            return text
        return MARK + self._fernet.encrypt(text.encode()).decode()

    def unseal(self, text: str | None) -> str | None:
        if text is None or not text.startswith(MARK):
            return text
        if not self.active:
            return "[sealed; key file missing]"
        try:
            return self._fernet.decrypt(text[len(MARK):].encode()).decode()
        except Exception:
            return "[sealed; wrong key]"

    def seal_bytes(self, data: bytes) -> bytes:
        return MARK.encode() + self._fernet.encrypt(data) if self.active else data

    def unseal_bytes(self, data: bytes) -> bytes | None:
        """The original bytes, or None when they are sealed and the key is missing or wrong."""
        if not data.startswith(MARK.encode()):
            return data
        if not self.active:
            return None
        try:
            return self._fernet.decrypt(data[len(MARK):])
        except Exception:
            return None
