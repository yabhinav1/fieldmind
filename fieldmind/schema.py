"""The shape of a memory's payload, versioned.

Every memory carries ``schema``, the version of the payload layout it was written
with. A device upgrades older payloads as it reads them, and leaves alone any
payload newer than it understands: a device that is behind the fleet must never
guess at fields it does not know and merge them wrongly.
"""

from __future__ import annotations

from collections.abc import Callable

CURRENT = 1

# version -> function that turns a payload of that version into the next one
MIGRATIONS: dict[int, Callable[[dict], dict]] = {
    # 0: payloads written before the field existed. Nothing changed in layout.
    0: lambda payload: payload,
}


def version(payload: dict) -> int:
    try:
        return int(payload.get("schema", 0))
    except (TypeError, ValueError):
        return 0


def too_new(payload: dict) -> bool:
    return version(payload) > CURRENT


def upgrade(payload: dict) -> dict:
    """Bring a payload up to the current version in place and return it."""
    v = version(payload)
    while v < CURRENT:
        payload = MIGRATIONS[v](payload)
        v += 1
        payload["schema"] = v
    return payload
