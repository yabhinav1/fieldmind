"""Device lock: a PIN protects everything the dashboard and API can read or change.

The PIN is stored as a salted PBKDF2 hash in the device journal. Sessions live in
memory, so restarting the device locks it again.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import threading
import time

from .journal import Journal

ITERATIONS = 200_000
SESSION_SECONDS = 12 * 3600
MAX_FAILURES = 5
LOCKOUT_SECONDS = 30
MIN_LENGTH = 4


class Locked(Exception):
    def __init__(self, seconds: int):
        super().__init__(f"Too many wrong attempts. Try again in {seconds}s.")
        self.seconds = seconds


class Auth:
    def __init__(self, journal: Journal, preset_pin: str | None = None):
        self._journal = journal
        self._sessions: dict[str, float] = {}
        self._failures = 0
        self._locked_until = 0.0
        self._lock = threading.Lock()
        if preset_pin:
            self.set_pin(preset_pin)

    @property
    def configured(self) -> bool:
        return self._journal.get("pin") is not None

    def set_pin(self, pin: str) -> None:
        pin = (pin or "").strip()
        if len(pin) < MIN_LENGTH:
            raise ValueError(f"Use at least {MIN_LENGTH} characters.")
        salt = secrets.token_bytes(16)
        self._journal.set("pin", {"salt": salt.hex(), "hash": self._hash(pin, salt, ITERATIONS).hex(),
                                  "iterations": ITERATIONS})

    def locked_for(self) -> int:
        return max(0, int(self._locked_until - time.time() + 0.999))

    def login(self, pin: str) -> str:
        """Check the PIN and start a session. Raises ``Locked`` or ``PermissionError``."""
        with self._lock:
            if self.locked_for():
                raise Locked(self.locked_for())
            record = self._journal.get("pin")
            expected = bytes.fromhex(record["hash"]) if record else b""
            given = self._hash((pin or "").strip(), bytes.fromhex(record["salt"]), record["iterations"]) if record else b"x"
            if not record or not hmac.compare_digest(given, expected):
                self._failures += 1
                if self._failures >= MAX_FAILURES:
                    self._failures = 0
                    self._locked_until = time.time() + LOCKOUT_SECONDS
                    self._journal.log("security", "Device locked for 30s after repeated wrong PIN attempts.", level="warn")
                    raise Locked(LOCKOUT_SECONDS)
                raise PermissionError("Wrong PIN.")
            self._failures = 0
            return self._issue()

    def _issue(self) -> str:
        now = time.time()
        for token in [t for t, expires in self._sessions.items() if expires < now]:
            del self._sessions[token]
        token = secrets.token_urlsafe(32)
        self._sessions[token] = now + SESSION_SECONDS
        return token

    def start_session(self) -> str:
        with self._lock:
            return self._issue()

    def valid(self, token: str | None) -> bool:
        return bool(token) and self._sessions.get(token, 0) > time.time()

    def logout(self, token: str | None) -> None:
        self._sessions.pop(token or "", None)

    @staticmethod
    def _hash(pin: str, salt: bytes, iterations: int) -> bytes:
        return hashlib.pbkdf2_hmac("sha256", pin.encode(), salt, iterations)
