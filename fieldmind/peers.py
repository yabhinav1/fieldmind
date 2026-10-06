"""Device-to-device exchange for when the cloud is out of reach.

Two devices on the same network can still learn from each other. Each device
offers the memories it is allowed to share (its own non-private notes, with the
same masking the cloud would get, plus the cloud knowledge it already holds), and
a device with no cloud link pulls from its peers into its replica shard. Nothing
a peer sends ever lands in the local shard: ownership stays with the device that
wrote the note, and the cloud remains the place where revisions are settled.

Peers authenticate each other with one shared fleet token; the device PIN is for
people, not devices.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

import httpx

from .policy import PRIVATE, REDACTED
from .service import MemoryService, _clip, cloud_view

HEADER = "x-fieldmind-peer"
Fetch = Callable[[str, str, dict | None], Any]
"""``fetch(peer_url, path, json_body) -> parsed JSON`` so tests can wire peers in-process."""


def http_fetch(token: str, timeout: float = 4.0) -> Fetch:
    def fetch(peer: str, path: str, body: dict | None) -> Any:
        headers = {HEADER: token}
        with httpx.Client(timeout=timeout, headers=headers) as client:
            response = client.post(f"{peer}{path}", json=body) if body is not None else client.get(f"{peer}{path}")
            response.raise_for_status()
            return response.json()
    return fetch


class PeerExchange:
    def __init__(self, service: MemoryService, fetch: Fetch | None = None):
        self.service = service
        self.settings = service.settings
        self.journal = service.journal
        self._fetch = fetch or http_fetch(self.settings.peer_token or "")
        self.seen: dict[str, dict] = {}

    @property
    def enabled(self) -> bool:
        return bool(self.settings.peers and self.settings.peer_token)

    # -- what this device offers ------------------------------------------

    def manifest(self) -> dict:
        items: dict[str, dict] = {}
        for record in self.service.local.scroll(fields=["rev", "scope", "status", "withdrawn", "sync_state"]):
            p = record["payload"]
            if p.get("scope") == PRIVATE or p.get("withdrawn") or p.get("sync_state") == "private":
                continue
            items[record["id"]] = {"rev": p.get("rev", 0), "status": p.get("status", "active"), "from": "local"}
        for record in self.service.replica.scroll(fields=["rev", "status"]):
            p = record["payload"]
            items.setdefault(record["id"], {"rev": p.get("rev", 0), "status": p.get("status", "active"), "from": "replica"})
        return {"device": self.settings.device_id, "site": self.settings.site, "items": items}

    def records(self, ids: list[str]) -> list[dict]:
        """Shareable copies of the requested memories, exactly as the cloud would receive them."""
        out = []
        for memory_id in ids[:256]:
            record = self.service.local.get(memory_id, with_vector=True)
            if record:
                payload = record["payload"]
                if payload.get("scope") == PRIVATE or payload.get("withdrawn"):
                    continue
                shared = cloud_view(payload)
                if payload.get("scope") == REDACTED:
                    # Never hand out vectors computed from text the fleet may not see.
                    dense, sparse = self.service.embedder.document(shared["text"])
                else:
                    dense, sparse = record["dense"], record["sparse"]
            else:
                record = self.service.replica.get(memory_id, with_vector=True)
                if not record:
                    continue
                shared = {k: v for k, v in record["payload"].items() if k not in ("sync_state", "base_rev", "via_peer")}
                dense, sparse = record["dense"], record["sparse"]
            out.append({"id": memory_id, "payload": shared, "dense": list(dense),
                        "sparse": {"indices": list(sparse.indices), "values": list(sparse.values)} if sparse else None})
        return out

    # -- what this device takes -------------------------------------------

    def exchange(self) -> dict:
        """Pull from every configured peer. Returns per-peer counts; never raises."""
        results: dict[str, dict] = {}
        if not self.enabled:
            return results
        for peer in self.settings.peers:
            try:
                results[peer] = self._pull_from(peer)
            except Exception as error:
                results[peer] = {"ok": False, "error": _clip(str(error), 80)}
        return results

    def _pull_from(self, peer: str) -> dict:
        from qdrant_edge import SparseVector

        service = self.service
        manifest = self._fetch(peer, "/api/peer/manifest", None)
        device = manifest.get("device", peer)
        local = {r["id"] for r in service.local.scroll(fields=["rev"])}
        replica = {r["id"]: r["payload"].get("rev", 0) for r in service.replica.scroll(fields=["rev"])}
        wanted, removed = [], 0
        for memory_id, entry in manifest.get("items", {}).items():
            if memory_id in local:
                continue  # this device's own memory; the cloud settles revisions, not peers
            if entry.get("status") == "deleted":
                if memory_id in replica:
                    service.replica.delete([memory_id])
                    removed += 1
            elif replica.get(memory_id, -1) < entry.get("rev", 0):
                wanted.append(memory_id)

        received = 0
        for start in range(0, len(wanted), 64):
            for record in self._fetch(peer, "/api/peer/memories", {"ids": wanted[start:start + 64]}):
                payload = {**record["payload"], "sync_state": "replica", "base_rev": record["payload"].get("rev", 0),
                           "via_peer": device}
                sparse = record.get("sparse")
                sparse = SparseVector(sparse["indices"], sparse["values"]) if sparse else SparseVector([], [])
                service.replica.upsert(record["id"], record["dense"], sparse, payload)
                received += 1
        if received or removed:
            service.replica.flush()
            self.journal.log("peer", f"Received {received} memories from {device} over the local network"
                                     f"{f', removed {removed}' if removed else ''}.", level="good",
                             peer=device, received=received, removed=removed)
        self.seen[peer] = {"device": device, "site": manifest.get("site"), "at": time.time(), "received": received}
        return {"ok": True, "device": device, "received": received, "removed": removed}

    def status(self) -> list[dict]:
        return [{"url": peer, **self.seen.get(peer, {})} for peer in self.settings.peers]
