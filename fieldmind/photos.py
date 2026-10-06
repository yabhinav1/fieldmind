"""Photos as memories.

A technician photographs what they see and says what it is. The caption becomes
an ordinary memory and goes through the same sharing policy as any note; the
photo is attached to it and follows its scope. A third Qdrant Edge shard holds
one CLIP vector per photo, so photos are found by what they show as well as by
their caption. Thumbnails live as files next to the shard, sealed with the
device key; EXIF data (position, time, camera) is stripped before anything is
stored. Only thumbnails ever leave the device.
"""

from __future__ import annotations

import base64
import time
import uuid
from pathlib import Path

from qdrant_edge import Distance, EdgeConfig, EdgeOptimizersConfig, EdgeVectorParams, FieldCondition, Filter, MatchAny

from . import schema
from .config import Settings
from .policy import PRIVATE
from .store import Shard, build_filter
from .vault import Vault
from .vision import IMAGE_DIM, ImageEmbedder, prepare

IMAGE = "image"
MEDIA_INDEXED = ("memory_id", "scope", "sync_state", "status", "site")
# Fields that travel to the cloud with a photo. The thumbnail itself is added as base64.
MEDIA_CLOUD_FIELDS = ("schema", "memory_id", "site", "device_id", "origin_device", "created_at", "updated_at",
                      "width", "height", "status", "rev")
# CLIP text-to-image cosine: unrelated pairs sit near 0.15, a clear match near 0.32.
IMAGE_FLOOR, IMAGE_CEILING = 0.15, 0.32


def media_config() -> EdgeConfig:
    return EdgeConfig(vectors={IMAGE: EdgeVectorParams(size=IMAGE_DIM, distance=Distance.Cosine)},
                      optimizers=EdgeOptimizersConfig(default_segment_number=1))


def image_strength(score: float) -> float:
    return min(1.0, max(0.0, (score - IMAGE_FLOOR) / (IMAGE_CEILING - IMAGE_FLOOR)))


class PhotoStore:
    def __init__(self, settings: Settings, vault: Vault, embedder: ImageEmbedder):
        self.settings = settings
        self.vault = vault
        self.embedder = embedder
        self.dir = settings.media_dir
        self.dir.mkdir(parents=True, exist_ok=True)
        self.shard = Shard(settings.media_shard_dir, "media", vault, config=media_config(), indexed=MEDIA_INDEXED)

    @property
    def available(self) -> bool:
        return self.embedder.available

    def close(self) -> None:
        self.shard.close()

    # -- files ------------------------------------------------------------

    def path(self, photo_id: str) -> Path:
        return self.dir / f"{photo_id}.jpg"

    def read(self, photo_id: str) -> bytes | None:
        path = self.path(photo_id)
        return self.vault.unseal_bytes(path.read_bytes()) if path.exists() else None

    def _write(self, photo_id: str, data: bytes) -> None:
        self.path(photo_id).write_bytes(self.vault.seal_bytes(data))

    # -- adding -----------------------------------------------------------

    def add(self, memory: dict, data: bytes) -> dict:
        """Attach a photo to a memory. The memory decides the scope; the photo follows it."""
        if not self.embedder.ready(wait=60):
            raise RuntimeError(f"The vision model is not loaded: {self.embedder.error or 'still loading'}")
        thumbnail, width, height = prepare(data)
        vector = self.embedder.image(thumbnail)
        photo_id = str(uuid.uuid4())
        now = time.time()
        private = memory["scope"] == PRIVATE
        payload = {
            "schema": schema.CURRENT, "memory_id": memory["id"], "scope": memory["scope"],
            "sync_state": "private" if private else "pending",
            "site": self.settings.site, "device_id": self.settings.device_id, "origin_device": self.settings.device_id,
            "created_at": now, "updated_at": now, "width": width, "height": height, "bytes": len(thumbnail),
            "status": "active", "rev": 1,
        }
        self._write(photo_id, thumbnail)
        self.shard.upsert_point(photo_id, {IMAGE: vector}, payload)
        self.shard.flush()
        return self._present({"id": photo_id, "payload": payload})

    def store_remote(self, record: dict) -> dict | None:
        """Keep a photo another device shared: decode its thumbnail and file it as a replica."""
        payload = dict(record["payload"])
        image = payload.pop("image", None)
        if not image:
            return None
        data = base64.b64decode(image)
        self._write(record["id"], data)
        payload.update(sync_state="replica", bytes=len(data), scope="shared")
        vector = record.get("vectors", {}).get(IMAGE) if record.get("vectors") else None
        if vector is None:
            if not self.embedder.ready(wait=60):
                return None
            vector = self.embedder.image(data)
        self.shard.upsert_point(record["id"], {IMAGE: vector}, payload)
        return self._present({"id": record["id"], "payload": payload})

    # -- reading ----------------------------------------------------------

    def get(self, photo_id: str) -> dict | None:
        record = self.shard.get(photo_id)
        return self._present(record) if record else None

    def for_memory(self, memory_id: str) -> list[dict]:
        return self.for_memories([memory_id]).get(memory_id, [])

    def for_memories(self, memory_ids: list[str]) -> dict[str, list[dict]]:
        out: dict[str, list[dict]] = {}
        if not memory_ids:
            return out
        flt = Filter(must=[FieldCondition(key="memory_id", match=MatchAny(list(memory_ids))),
                           FieldCondition(key="status", match=MatchAny(["active"]))])
        for record in self.shard.scroll(flt):
            out.setdefault(record["payload"]["memory_id"], []).append(self._present(record))
        for photos in out.values():
            photos.sort(key=lambda p: p["created_at"])
        return out

    def index(self) -> dict[str, dict]:
        """``photo_id -> {memory_id, sync_state}`` for every photo on the device."""
        return {r["id"]: r["payload"] for r in self.shard.scroll(fields=["memory_id", "sync_state", "status"])}

    def search(self, query: str, limit: int) -> list[dict]:
        """Photos that look like what the text describes, with raw CLIP cosine scores."""
        if not self.embedder.ready() or self.shard.count() == 0:
            return []
        vector = self.embedder.text(query)
        flt = build_filter({"status": "active"})
        return [{**self._present(hit), "score": hit["score"]} for hit in self.shard.nearest(vector, IMAGE, limit, flt)]

    def cloud_view(self, photo: dict, caption: str) -> tuple[dict, list[float]] | None:
        """What leaves the device for a photo: the thumbnail, its vector and bookkeeping, plus the shared caption."""
        record = self.shard.get(photo["id"], with_vector=True)
        data = self.read(photo["id"])
        if not record or data is None:
            return None
        payload = {k: record["payload"][k] for k in MEDIA_CLOUD_FIELDS if k in record["payload"]}
        payload.update(caption=caption, image=base64.b64encode(data).decode(), updated_at=time.time())
        return payload, record["vectors"][IMAGE]

    # -- changes ----------------------------------------------------------

    def set_state(self, photo_id: str, **changes) -> None:
        record = self.shard.get(photo_id)
        if record:
            record["payload"].update(changes, updated_at=time.time())
            self.shard.set_payload(photo_id, record["payload"])

    def rescope(self, memory_id: str, scope: str) -> list[dict]:
        """A memory's scope changed; its photos follow. Returns the photos as they now stand."""
        out = []
        for photo in self.for_memory(memory_id):
            state = "private" if scope == PRIVATE else ("pending" if photo["sync_state"] in ("private", "pending") else photo["sync_state"])
            self.set_state(photo["id"], scope=scope, sync_state=state)
            out.append({**photo, "scope": scope, "sync_state": state})
        return out

    def remove(self, photo_ids: list[str]) -> None:
        for photo_id in photo_ids:
            self.path(photo_id).unlink(missing_ok=True)
        self.shard.delete(photo_ids)

    def remove_for(self, memory_id: str) -> list[dict]:
        photos = self.for_memory(memory_id)
        self.remove([p["id"] for p in photos])
        return photos

    def _present(self, record: dict) -> dict:
        p = record["payload"]
        return {
            "id": record["id"], "memory_id": p.get("memory_id"), "scope": p.get("scope"),
            "sync_state": p.get("sync_state"), "width": p.get("width"), "height": p.get("height"),
            "bytes": p.get("bytes"), "device_id": p.get("device_id"), "origin_device": p.get("origin_device"),
            "created_at": p.get("created_at", 0), "url": f"/api/photos/{record['id']}",
        }
