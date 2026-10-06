"""Device lock: a PIN protects everything the dashboard and API can read or change.

The PIN is stored as a salted PBKDF2 hash in the device journal. Sessions and the
wrong-attempt counter live there too, so a restart neither logs everyone out nor
gives an attacker a fresh set of guesses.
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
MAX_SESSIONS = 50


class Locked(Exception):
    def __init__(self, seconds: int):
        super().__init__(f"Too many wrong attempts. Try again in {seconds}s.")
        self.seconds = seconds


class Auth:
    def __init__(self, journal: Journal, preset_pin: str | None = None):
        self._journal = journal
        self._lock = threading.Lock()
        # A preset PIN only seeds a device that has none. A PIN changed on the
        # device afterwards must survive restarts.
        if preset_pin and not self.configured:
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

    # -- lockout ------------------------------------------------------------

    def _guard(self) -> dict:
        return self._journal.get("pin_guard", {"failures": 0, "locked_until": 0.0})

    def locked_for(self) -> int:
        return max(0, int(self._guard()["locked_until"] - time.time() + 0.999))

    def login(self, pin: str) -> str:
        """Check the PIN and start a session. Raises ``Locked`` or ``PermissionError``."""
        with self._lock:
            if self.locked_for():
                raise Locked(self.locked_for())
            record = self._journal.get("pin")
            expected = bytes.fromhex(record["hash"]) if record else b""
            given = self._hash((pin or "").strip(), bytes.fromhex(record["salt"]), record["iterations"]) if record else b"x"
            if not record or not hmac.compare_digest(given, expected):
                guard = self._guard()
                guard["failures"] += 1
                if guard["failures"] >= MAX_FAILURES:
                    guard = {"failures": 0, "locked_until": time.time() + LOCKOUT_SECONDS}
                    self._journal.set("pin_guard", guard)
                    self._journal.log("security", f"Device locked for {LOCKOUT_SECONDS}s after repeated wrong PIN attempts.",
                                      level="warn")
                    raise Locked(LOCKOUT_SECONDS)
                self._journal.set("pin_guard", guard)
                raise PermissionError("Wrong PIN.")
            self._journal.set("pin_guard", {"failures": 0, "locked_until": 0.0})
            return self._issue()

    # -- sessions -----------------------------------------------------------

    def _sessions(self) -> dict[str, float]:
        now = time.time()
        return {t: e for t, e in self._journal.get("sessions", {}).items() if e > now}

    def _issue(self) -> str:
        sessions = self._sessions()
        token = secrets.token_urlsafe(32)
        sessions[token] = time.time() + SESSION_SECONDS
        if len(sessions) > MAX_SESSIONS:
            for old in sorted(sessions, key=sessions.get)[: len(sessions) - MAX_SESSIONS]:
                del sessions[old]
        self._journal.set("sessions", sessions)
        return token

    def start_session(self) -> str:
        with self._lock:
            return self._issue()

    def valid(self, token: str | None) -> bool:
        return bool(token) and self._journal.get("sessions", {}).get(token, 0) > time.time()

    def logout(self, token: str | None) -> None:
        with self._lock:
            sessions = self._sessions()
            sessions.pop(token or "", None)
            self._journal.set("sessions", sessions)

    def logout_all(self, except_token: str | None = None) -> None:
        with self._lock:
            sessions = self._sessions()
            keep = {except_token: sessions[except_token]} if except_token in sessions else {}
            self._journal.set("sessions", keep)

    @staticmethod
    def _hash(pin: str, salt: bytes, iterations: int) -> bytes:
        return hashlib.pbkdf2_hmac("sha256", pin.encode(), salt, iterations)
