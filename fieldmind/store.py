"""Thin wrapper around a Qdrant Edge shard.

A device holds two shards:

* ``local``   - memories written or edited on this device (private and shared)
* ``replica`` - a read-only copy of cloud knowledge that other devices produced

Keeping them apart means the replica can be rebuilt from the cloud at any time
without touching anything the device has not shared.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any, Iterable

from qdrant_edge import (
    CountRequest,
    Distance,
    EdgeConfig,
    EdgeShard,
    EdgeSparseVectorParams,
    EdgeVectorParams,
    FieldCondition,
    Filter,
    MatchAny,
    MatchValue,
    Modifier,
    PayloadSchemaType,
    Point,
    Query,
    QueryRequest,
    ScrollRequest,
    SparseVector,
    UpdateOperation,
)

from .config import DENSE, DENSE_DIM, SPARSE

INDEXED_FIELDS = ("scope", "sync_state", "status", "kind", "asset", "site", "device_id")


def build_filter(
    must: dict[str, Any] | None = None, must_not: dict[str, Any] | None = None
) -> Filter | None:
    """Build a keyword filter from ``{field: value}``; a list value means "any of"."""

    def conditions(spec: dict[str, Any] | None) -> list[FieldCondition]:
        out = []
        for key, value in (spec or {}).items():
            if value is None or value == "" or value == []:
                continue
            match = MatchAny(list(value)) if isinstance(value, (list, tuple, set)) else MatchValue(value)
            out.append(FieldCondition(key=key, match=match))
        return out

    must_c, not_c = conditions(must), conditions(must_not)
    if not must_c and not not_c:
        return None
    return Filter(must=must_c or None, must_not=not_c or None)


class Shard:
    def __init__(self, path: Path, name: str):
        self.name = name
        self.path = path
        self._lock = threading.RLock()
        self._open()

    def _open(self) -> None:
        self.path.mkdir(parents=True, exist_ok=True)
        config = EdgeConfig(
            vectors={DENSE: EdgeVectorParams(size=DENSE_DIM, distance=Distance.Cosine)},
            sparse_vectors={SPARSE: EdgeSparseVectorParams(modifier=Modifier.Idf)},
        )
        # load() creates the shard when the directory is empty.
        self._shard = EdgeShard.load(str(self.path), config)
        existing = self._shard.info().payload_schema
        for name in INDEXED_FIELDS:
            if name not in existing:
                self._shard.update(UpdateOperation.create_field_index(name, PayloadSchemaType.Keyword))

    # -- writes -----------------------------------------------------------

    def upsert(self, memory_id: str, dense: list[float], sparse: SparseVector, payload: dict) -> None:
        point = Point(memory_id, {DENSE: dense, SPARSE: sparse}, payload)
        with self._lock:
            self._shard.update(UpdateOperation.upsert_points([point]))

    def set_payload(self, memory_id: str, payload: dict) -> None:
        """Replace the whole payload, leaving the vectors untouched."""
        with self._lock:
            self._shard.update(UpdateOperation.overwrite_payload([memory_id], payload))

    def delete(self, memory_ids: Iterable[str]) -> None:
        ids = list(memory_ids)
        if ids:
            with self._lock:
                self._shard.update(UpdateOperation.delete_points(ids))

    def clear(self) -> None:
        with self._lock:
            ids = [r["id"] for r in self.scroll()]
            self.delete(ids)

    def flush(self) -> None:
        with self._lock:
            self._shard.flush()

    def close(self) -> None:
        with self._lock:
            self._shard.flush()
            self._shard.close()

    # -- reads ------------------------------------------------------------

    def get(self, memory_id: str, with_vector: bool = False) -> dict | None:
        with self._lock:
            records = self._shard.retrieve([memory_id], True, with_vector)
        return self._record(records[0], with_vector) if records else None

    def has(self, memory_id: str) -> bool:
        with self._lock:
            return bool(self._shard.retrieve([memory_id], False, False))

    def scroll(self, flt: Filter | None = None, with_vector: bool = False) -> list[dict]:
        out: list[dict] = []
        offset = None
        with self._lock:
            while True:
                records, offset = self._shard.scroll(
                    ScrollRequest(offset=offset, limit=256, filter=flt, with_payload=True, with_vector=with_vector)
                )
                out.extend(self._record(r, with_vector) for r in records)
                if offset is None:
                    return out

    def count(self, flt: Filter | None = None) -> int:
        with self._lock:
            return self._shard.count(CountRequest(exact=True, filter=flt))

    def nearest(self, vector: list[float] | SparseVector, using: str, limit: int, flt: Filter | None = None) -> list[dict]:
        """Single-vector search that returns raw scores (cosine for dense, BM25 for sparse)."""
        request = QueryRequest(limit=limit, query=Query.Nearest(vector, using=using), filter=flt, with_payload=True)
        with self._lock:
            hits = self._shard.query(request)
        return [{"id": str(h.id), "score": float(h.score), "payload": h.payload or {}, "shard": self.name} for h in hits]

    def info(self) -> dict:
        with self._lock:
            info = self._shard.info()
        return {
            "name": self.name,
            "points": info.points_count,
            "segments": info.segments_count,
            "indexed_vectors": info.indexed_vectors_count,
            "disk_bytes": _dir_size(self.path),
        }

    def _record(self, record, with_vector: bool) -> dict:
        out = {"id": str(record.id), "payload": record.payload or {}, "shard": self.name}
        if with_vector and record.vector:
            out["dense"] = record.vector.get(DENSE)
            out["sparse"] = record.vector.get(SPARSE)
        return out


def _dir_size(path: Path) -> int:
    total = 0
    for item in path.rglob("*"):
        try:
            if item.is_file():
                total += item.stat().st_size
        except OSError:
            pass
    return total
