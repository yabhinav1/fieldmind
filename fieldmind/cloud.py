"""The cloud side: a Qdrant Server collection shared by every device in the fleet."""

from __future__ import annotations

import secrets
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import httpx
from qdrant_client import QdrantClient
from qdrant_client import models as m
from qdrant_edge import SparseVector

from .config import DENSE, DENSE_DIM, SPARSE

# A deleted memory stays in the cloud as a tombstone so other devices learn about
# the deletion. Its content is wiped and its vector replaced with this placeholder.
TOMBSTONE_VECTOR = [1.0] + [0.0] * (DENSE_DIM - 1)


class CloudUnavailable(Exception):
    pass


class Cloud:
    def __init__(self, url: str, api_key: str | None, collection: str,
                 client: QdrantClient | None = None, timeout: float = 5.0):
        self.url = url.rstrip("/")
        self.api_key = api_key
        self.collection = collection
        self._timeout = timeout
        self._client = client
        self._injected = client is not None
        self._ready = False

    # -- connection -------------------------------------------------------

    def reachable(self) -> bool:
        if self._injected:
            return True
        try:
            headers = {"api-key": self.api_key} if self.api_key else {}
            return httpx.get(f"{self.url}/", headers=headers, timeout=1.5).status_code == 200
        except httpx.HTTPError:
            return False

    @property
    def client(self) -> QdrantClient:
        if self._client is None:
            self._client = QdrantClient(url=self.url, api_key=self.api_key, timeout=self._timeout,
                                        check_compatibility=False)
        return self._client

    def ensure(self) -> None:
        if self._ready:
            return
        if not self.client.collection_exists(self.collection):
            self.client.create_collection(
                self.collection,
                vectors_config={DENSE: m.VectorParams(size=DENSE_DIM, distance=m.Distance.COSINE)},
                sparse_vectors_config={SPARSE: m.SparseVectorParams(modifier=m.Modifier.IDF)},
                # Devices restore from shard snapshots and pay disk for every segment
                # they unpack, so keep the shard in one segment.
                optimizers_config=m.OptimizersConfigDiff(default_segment_number=1),
            )
        if not self._injected:
            for name in ("site", "status", "device_id", "asset"):
                self.client.create_payload_index(self.collection, name, m.PayloadSchemaType.KEYWORD)
            self.client.create_payload_index(self.collection, "rev", m.PayloadSchemaType.INTEGER)
        self._ready = True

    def forget_connection(self) -> None:
        self._ready = False

    # -- reads ------------------------------------------------------------

    def get(self, memory_id: str, with_vectors: bool = False) -> dict | None:
        records = self.get_many([memory_id], with_vectors)
        return records[0] if records else None

    def get_many(self, ids: Iterable[str], with_vectors: bool = True) -> list[dict]:
        ids = list(ids)
        if not ids:
            return []
        records = self.client.retrieve(self.collection, ids, with_payload=True, with_vectors=with_vectors)
        return [self._record(r, with_vectors) for r in records]

    def manifest(self, site: str | None = None) -> dict[str, dict]:
        """Every memory this device may see, as ``id -> {rev, status, ...}``.

        No text or vectors are transferred, so comparing the manifest with what the
        device already holds costs very little bandwidth.
        """
        flt = None
        if site:
            flt = m.Filter(should=[
                m.FieldCondition(key="site", match=m.MatchValue(value=site)),
                m.FieldCondition(key="site", match=m.MatchValue(value="global")),
            ])
        out, offset = {}, None
        while True:
            records, offset = self.client.scroll(
                self.collection, scroll_filter=flt, limit=512, offset=offset,
                with_payload=["rev", "status", "tombstone", "device_id"], with_vectors=False)
            for record in records:
                out[str(record.id)] = record.payload or {}
            if offset is None:
                return out

    def browse(self, limit: int = 200) -> list[dict]:
        records, _ = self.client.scroll(self.collection, limit=limit, with_payload=True, with_vectors=False)
        rows = [self._record(r, False) for r in records]
        rows.sort(key=lambda r: r["payload"].get("updated_at", 0), reverse=True)
        return rows

    def count(self, live_only: bool = True) -> int:
        flt = m.Filter(must_not=[m.FieldCondition(key="status", match=m.MatchValue(value="deleted"))]) if live_only else None
        return self.client.count(self.collection, count_filter=flt, exact=True).count

    @property
    def supports_snapshots(self) -> bool:
        return not self._injected

    def download_snapshot(self, target: Path) -> int:
        """Stream a snapshot of the collection's shard to ``target``. Returns its size in bytes."""
        headers = {"api-key": self.api_key} if self.api_key else {}
        url = f"{self.url}/collections/{self.collection}/shards/0/snapshot"
        size = 0
        with httpx.stream("GET", url, headers=headers, timeout=120) as response:
            response.raise_for_status()
            with open(target, "wb") as out:
                for chunk in response.iter_bytes(1 << 16):
                    out.write(chunk)
                    size += len(chunk)
        return size

    # -- writes -----------------------------------------------------------

    def write(self, memory_id: str, dense: list[float], sparse: SparseVector | None, payload: dict,
              expect_rev: int | None) -> bool:
        """Compare-and-swap upsert.

        With ``expect_rev`` set, an existing point is only replaced while it is still
        at that revision, so two devices cannot silently overwrite each other.
        Returns whether this write is the one now stored: every write carries a
        fresh ``write_id``, so the read-back cannot mistake another device's write
        for this one even if both carry the same revision and timestamp.
        """
        guard = None
        if expect_rev is not None:
            guard = m.Filter(must=[m.FieldCondition(key="rev", match=m.MatchValue(value=expect_rev))])
        vector: dict[str, Any] = {DENSE: dense}
        if sparse is not None and list(sparse.indices):
            vector[SPARSE] = m.SparseVector(indices=list(sparse.indices), values=list(sparse.values))
        write_id = secrets.token_hex(8)
        point = m.PointStruct(id=memory_id, vector=vector, payload={**payload, "write_id": write_id})
        self.client.upsert(self.collection, [point], wait=True, update_filter=guard)
        stored = self.get(memory_id)
        return bool(stored) and stored["payload"].get("write_id") == write_id

    def patch(self, memory_id: str, payload: dict, expect_rev: int) -> bool:
        """Compare-and-swap replacement of the payload alone, leaving the vectors in place.

        Used when an edit changed tags, kind or status but not the text, so the
        vectors the cloud already holds are still right and need not travel again.
        """
        guard = m.Filter(must=[m.HasIdCondition(has_id=[memory_id]),
                               m.FieldCondition(key="rev", match=m.MatchValue(value=expect_rev))])
        write_id = secrets.token_hex(8)
        self.client.overwrite_payload(self.collection, {**payload, "write_id": write_id}, points=guard, wait=True)
        stored = self.get(memory_id)
        return bool(stored) and stored["payload"].get("write_id") == write_id

    def tombstone(self, memory_id: str, payload: dict, expect_rev: int | None) -> bool:
        return self.write(memory_id, TOMBSTONE_VECTOR, None, payload, expect_rev)

    def reset(self) -> None:
        if self.client.collection_exists(self.collection):
            self.client.delete_collection(self.collection)
        self._ready = False
        self.ensure()

    @staticmethod
    def _record(record, with_vectors: bool) -> dict:
        out = {"id": str(record.id), "payload": record.payload or {}}
        if with_vectors and record.vector:
            out["dense"] = record.vector.get(DENSE)
            sparse = record.vector.get(SPARSE)
            out["sparse"] = SparseVector(list(sparse.indices), list(sparse.values)) if sparse else SparseVector([], [])
        return out
