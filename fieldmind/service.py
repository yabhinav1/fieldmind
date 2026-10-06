"""The device's memory: capture, edit, search and answer, all without a network."""

from __future__ import annotations

import copy
import re
import threading
import time
import uuid
from collections import deque
from typing import Any

from .config import DENSE, SPARSE, Settings
from .embedder import Embedder
from .journal import Journal
from .llm import LocalModel
from .policy import PRIVATE, REDACTED, SHARED, PolicyEngine, find_asset
from .reranker import Reranker
from .store import Shard, build_filter

# Cosine similarity bands for two notes about the same asset, measured on this
# embedding model: rewording scores ~0.99, a follow-up on the same issue
# 0.82-0.93, a different issue on the same asset ~0.74.
DUPLICATE_AT = 0.97
UPDATE_AT = 0.80
RELATED_AT = 0.60

# Search fusion. This embedding model scores unrelated text around 0.5 and a
# strong match around 0.85, so cosine is rescaled to that band. Keyword scores
# saturate, which stops one shared common word from outranking a real match.
SEMANTIC_FLOOR, SEMANTIC_CEILING = 0.50, 0.85
KEYWORD_HALF = 4.0
SEMANTIC_WEIGHT, KEYWORD_WEIGHT = 0.75, 0.25
MIN_SCORE = 0.05
ANSWER_AT = 0.20
# When reranking, the cross-encoder's verdict and the fused score share the final
# score. The fused score keeps keyword-exact matches from being buried.
RERANK_DEPTH = 20
RERANK_WEIGHT = 0.6

# Reference material is published centrally. A field note can relate to it but
# never replaces it.
PROTECTED_KINDS = ("reference",)

# Fields that travel to the cloud. Everything else is device bookkeeping.
CLOUD_FIELDS = (
    "text", "kind", "asset", "tags", "site", "scope", "category", "priority",
    "device_id", "origin_device", "author", "created_at", "updated_at", "rev",
    "status", "supersedes", "superseded_by", "relation", "redacted",
)
# Fields a person can change, and therefore the ones that can conflict.
CONTENT_FIELDS = ("text", "kind", "asset", "tags", "status", "supersedes", "superseded_by", "relation")

SCOPE_WORDS = {PRIVATE: "private", SHARED: "shared", REDACTED: "shared with details masked"}

RESOLVED_WORDS = re.compile(
    r"(?i)\b(fixed|repaired|replaced|resolved|cleaned|cleared|restored|normal|ok|okay|working|healthy|no leak|back to)\b")


class NotFound(Exception):
    pass


def cloud_view(payload: dict) -> dict:
    """The version of a memory that is allowed to leave the device."""
    out = {k: copy.deepcopy(payload[k]) for k in CLOUD_FIELDS if k in payload}
    if payload.get("scope") == REDACTED:
        out["text"] = payload.get("shared_text") or payload.get("text", "")
        out["redacted"] = True
    return out


class MemoryService:
    def __init__(self, settings: Settings, embedder: Embedder, local: Shard, replica: Shard,
                 journal: Journal, policy: PolicyEngine, reranker: Reranker | None = None):
        self.settings = settings
        self.embedder = embedder
        self.local = local
        self.replica = replica
        self.journal = journal
        self.policy = policy
        self.reranker = reranker if reranker is not None and reranker.available else None
        self.llm = LocalModel(settings.ollama_url, settings.ollama_model)
        # Every change to the local shard and the outbox happens under this lock, so
        # a capture from the dashboard and a sync cycle never interleave half-way.
        self.lock = threading.RLock()
        # Recent search and answer times, for /metrics.
        self.search_ms: deque[float] = deque(maxlen=500)
        self.answer_ms: deque[float] = deque(maxlen=200)
        self.searches = 0
        self.answers = 0

    # -- lookup -----------------------------------------------------------

    def find(self, memory_id: str, with_vector: bool = False) -> dict | None:
        return self.local.get(memory_id, with_vector) or self.replica.get(memory_id, with_vector)

    def get(self, memory_id: str) -> dict:
        record = self.find(memory_id)
        if not record:
            raise NotFound(memory_id)
        return self._present(record)

    def history(self, memory_id: str) -> list[dict]:
        """The chain of memories this one replaced, newest first."""
        chain, seen, current = [], set(), self.find(memory_id)
        while current and current["id"] not in seen:
            seen.add(current["id"])
            chain.append(self._present(current))
            previous = current["payload"].get("supersedes")
            current = self.find(previous) if previous else None
        return chain

    def list(self, scope: str | None = None, sync_state: str | None = None, kind: str | None = None,
             asset: str | None = None, source: str | None = None, status: str | None = None,
             text: str | None = None) -> list[dict]:
        flt = build_filter({"scope": scope, "sync_state": sync_state, "kind": kind, "asset": asset, "status": status})
        records = []
        for shard in self._shards(source):
            records.extend(shard.scroll(flt))
        if text:
            needle = text.lower()
            records = [r for r in records if needle in (r["payload"].get("text") or "").lower()]
        records.sort(key=lambda r: r["payload"].get("updated_at", 0), reverse=True)
        return [self._present(r) for r in records]

    def stats(self) -> dict:
        def count(shard: Shard, **must: Any) -> int:
            return shard.count(build_filter(must))

        return {
            "local": self.local.count(),
            "replica": self.replica.count(),
            "private": count(self.local, scope=PRIVATE),
            "shared": count(self.local, scope=[SHARED, REDACTED]),
            "redacted": count(self.local, scope=REDACTED),
            "pending": count(self.local, sync_state="pending"),
            "synced": count(self.local, sync_state="synced"),
            "conflict": count(self.local, sync_state="conflict"),
            "failed": count(self.local, sync_state="failed"),
            "superseded": count(self.local, status="superseded") + count(self.replica, status="superseded"),
        }

    # -- capture ----------------------------------------------------------

    def preview(self, text: str, asset: str | None = None, scope: str | None = None) -> dict:
        """What would happen if this note were saved: the policy decision and what it relates to."""
        started = time.perf_counter()
        decision = self.policy.decide(text, override=scope)
        asset = asset or find_asset(text)
        related = self.related(text, asset)
        return {
            "decision": decision.to_dict(),
            "asset": asset,
            "related": related,
            "took_ms": round((time.perf_counter() - started) * 1000, 1),
        }

    def related(self, text: str, asset: str | None, exclude: str | None = None, limit: int = 3) -> list[dict]:
        if not asset:
            return []
        vector = self.embedder.dense([text])[0]
        flt = build_filter({"asset": asset, "status": "active"})
        hits = []
        for shard in (self.local, self.replica):
            hits.extend(shard.nearest(vector, DENSE, limit, flt))
        hits.sort(key=lambda h: h["score"], reverse=True)
        out = []
        for hit in hits:
            if hit["id"] == exclude or hit["score"] < RELATED_AT:
                continue
            if hit["payload"].get("kind") in PROTECTED_KINDS:
                relation = "related"
            elif hit["score"] >= DUPLICATE_AT:
                relation = "duplicate"
            elif hit["score"] >= UPDATE_AT:
                relation = "resolves" if RESOLVED_WORDS.search(text) else "updates"
            else:
                relation = "related"
            out.append({**self._present(hit), "similarity": round(hit["score"], 3), "relation": relation})
        return out[:limit]

    def capture(self, text: str, kind: str = "observation", asset: str | None = None,
                tags: list[str] | None = None, scope: str | None = None,
                link: bool = True, allow_duplicate: bool = False, supersede: bool = True) -> dict:
        """Save a note. ``supersede=False`` keeps an earlier note about the same issue
        current even when this one reads as a follow-up; the preview shows what would
        be replaced so the person can decide."""
        text = text.strip()
        if not text:
            raise ValueError("A memory needs some text.")
        with self.lock:
            return self._capture(text, kind, asset, tags, scope, link, allow_duplicate, supersede)

    def _capture(self, text: str, kind: str, asset: str | None, tags: list[str] | None, scope: str | None,
                 link: bool, allow_duplicate: bool, supersede: bool) -> dict:
        decision = self.policy.decide(text, override=scope)
        asset = asset or find_asset(text)
        related = self.related(text, asset) if link else []

        duplicate = next((r for r in related if r["relation"] == "duplicate"), None)
        if duplicate and not allow_duplicate:
            self.journal.log("dedupe", f"Skipped a duplicate of an existing memory about {asset}.",
                             memory_id=duplicate["id"])
            return {"created": False, "duplicate_of": duplicate, "decision": decision.to_dict()}

        now = time.time()
        memory_id = str(uuid.uuid4())
        if decision.decided_by == "user":
            self.policy.learn(text, decision.scope)

        payload = {
            "text": text,
            "kind": kind,
            "asset": asset,
            "tags": sorted(set(tags or [])),
            "site": self.settings.site,
            "scope": decision.scope,
            "category": decision.category,
            "priority": decision.priority,
            "policy": decision.summary(),
            "device_id": self.settings.device_id,
            "origin_device": self.settings.device_id,
            "author": self.settings.author,
            "created_at": now,
            "updated_at": now,
            "rev": 1,
            "base_rev": 0,
            "status": "active",
            "sync_state": "private" if decision.scope == PRIVATE else "pending",
        }
        if decision.shared_text:
            payload["shared_text"] = decision.shared_text

        # A follow-up replaces what the device believed before, as long as doing so
        # would not reveal a private note to the fleet.
        target = next((r for r in related if r["relation"] in ("updates", "resolves")), None)
        if supersede and target and (decision.scope != PRIVATE or target["scope"] == PRIVATE):
            payload["supersedes"] = target["id"]
            payload["relation"] = target["relation"]

        dense, sparse = self.embedder.document(text)
        self.local.upsert(memory_id, dense, sparse, payload)
        if decision.scope != PRIVATE:
            self.journal.enqueue(memory_id, "upsert", decision.priority, 0, None)
        self.journal.log(
            "capture",
            f"Saved as {SCOPE_WORDS[decision.scope]}: {_clip(text)}",
            memory_id=memory_id, scope=decision.scope, category=decision.category, reasons=decision.reasons,
        )

        if payload.get("supersedes"):
            self._mutate(payload["supersedes"], {"status": "superseded", "superseded_by": memory_id},
                         note=f"Replaced by a newer memory about {asset}.")
        self.local.flush()
        return {"created": True, "memory": self.get(memory_id), "decision": decision.to_dict(), "related": related}

    # -- change -----------------------------------------------------------

    def edit(self, memory_id: str, text: str | None = None, kind: str | None = None,
             asset: str | None = None, tags: list[str] | None = None, scope: str | None = None) -> dict:
        changes: dict[str, Any] = {}
        if text is not None:
            changes["text"] = text.strip()
        if kind is not None:
            changes["kind"] = kind
        if asset is not None:
            changes["asset"] = asset or None
        if tags is not None:
            changes["tags"] = sorted(set(tags))
        with self.lock:
            result = self._mutate(memory_id, changes, scope=scope, note="Edited on this device.")
            self.local.flush()
        return result

    def delete(self, memory_id: str) -> None:
        with self.lock:
            record = self.find(memory_id)
            if not record:
                raise NotFound(memory_id)
            payload = record["payload"]
            open_op = self.journal.open_op(memory_id)
            in_cloud = (payload.get("base_rev", 0) > 0 and not payload.get("withdrawn")) or record["shard"] == "replica"
            if in_cloud:
                base = open_op["base_payload"] if open_op else cloud_view(payload)
                base_rev = open_op["base_rev"] if open_op else payload.get("rev", 0)
                self.journal.enqueue(memory_id, "delete", 1, base_rev, base)
            else:
                self.journal.cancel(memory_id)
            self.journal.close_conflicts_for(memory_id, "deleted")
            self.local.delete([memory_id])
            self.replica.delete([memory_id])
            self.local.flush()
        self.journal.log("delete", f"Deleted: {_clip(payload.get('text', ''))}", memory_id=memory_id,
                         queued=in_cloud)

    def _mutate(self, memory_id: str, changes: dict, scope: str | None = None, note: str = "") -> dict:
        record = self.find(memory_id, with_vector=True)
        if not record:
            raise NotFound(memory_id)
        payload = copy.deepcopy(record["payload"])
        open_op = self.journal.open_op(memory_id)

        # The version the cloud and this device last agreed on. It is the reference
        # point for detecting and merging concurrent edits later.
        if open_op:
            base_rev, base = open_op["base_rev"], open_op["base_payload"]
        elif record["shard"] == "replica" or payload.get("sync_state") == "synced":
            base_rev, base = payload.get("rev", 0), cloud_view(payload)
        else:
            base_rev, base = payload.get("base_rev", 0), None

        before_scope = payload.get("scope", PRIVATE)
        text_changed = "text" in changes and changes["text"] != payload.get("text")
        changed = {k: v for k, v in changes.items() if payload.get(k) != v}
        if not changed and (scope is None or scope == before_scope):
            return self._present(record)
        payload.update(changed)

        if text_changed or scope is not None:
            keep = scope if scope is not None else (before_scope if payload.get("policy", {}).get("decided_by") == "user" else None)
            decision = self.policy.decide(payload["text"], override=keep)
            if scope is not None and decision.decided_by == "user":
                self.policy.learn(payload["text"], decision.scope)
            payload.update(scope=decision.scope, category=decision.category, priority=decision.priority,
                           policy=decision.summary())
            payload.pop("shared_text", None)
            if decision.shared_text:
                payload["shared_text"] = decision.shared_text
            if text_changed and not payload.get("asset"):
                payload["asset"] = find_asset(payload["text"])

        payload["rev"] = payload.get("rev", 0) + 1
        payload["base_rev"] = base_rev
        payload["updated_at"] = time.time()
        payload["device_id"] = self.settings.device_id
        payload.setdefault("origin_device", self.settings.device_id)

        now_private = payload["scope"] == PRIVATE
        if now_private and base_rev > 0 and not payload.get("withdrawn"):
            # It was shared before: keep it here, withdraw it from the cloud.
            payload["sync_state"] = "pending"
            self.journal.enqueue(memory_id, "retract", 1, base_rev, base)
        elif now_private:
            payload["sync_state"] = "private"
            self.journal.cancel(memory_id)
        else:
            payload["sync_state"] = "pending"
            self.journal.enqueue(memory_id, "upsert", payload.get("priority", 1), base_rev, base)

        if text_changed:
            dense, sparse = self.embedder.document(payload["text"])
        else:
            dense, sparse = record["dense"], record["sparse"]
        self.local.upsert(memory_id, dense, sparse, payload)
        if record["shard"] == "replica":
            self.replica.delete([memory_id])

        self.journal.log("edit", f"{note or 'Changed'} {_clip(payload['text'])}", memory_id=memory_id,
                         fields=sorted(changed) + (["scope"] if payload["scope"] != before_scope else []),
                         rev=payload["rev"])
        return self._present({"id": memory_id, "payload": payload, "shard": "local"})

    # -- search -----------------------------------------------------------

    def search(self, query: str, mode: str = "hybrid", limit: int = 8, scope: str | None = None,
               kind: str | None = None, asset: str | None = None, source: str | None = None,
               include_superseded: bool = False, rerank: bool = False) -> dict:
        """Federated search over both shards.

        Each shard is asked separately for semantic and keyword matches. Raw scores
        are comparable across shards, so every memory gets one semantic and one
        keyword score, which are then combined into a single relevance score. With
        ``rerank``, a cross-encoder then reads the question with each of the best
        candidates and the two verdicts are blended.
        """
        query = query.strip()
        must = {"scope": scope, "kind": kind, "asset": asset}
        must_not = None if include_superseded else {"status": ["superseded", "deleted"]}
        flt = build_filter(must, must_not)
        depth = max(limit * 3, 20)

        t0 = time.perf_counter()
        dense_q = self.embedder.dense_query(query) if mode in ("hybrid", "semantic") else None
        sparse_q = self.embedder.sparse_query(query) if mode in ("hybrid", "keyword") else None
        t1 = time.perf_counter()

        dense_hits, sparse_hits = [], []
        for shard in self._shards(source):
            if dense_q is not None:
                dense_hits.extend(shard.nearest(dense_q, DENSE, depth, flt))
            if sparse_q is not None and sparse_q.indices:
                sparse_hits.extend(shard.nearest(sparse_q, SPARSE, depth, flt))
        t2 = time.perf_counter()

        merged: dict[str, dict] = {}
        for signal, hits in (("semantic", dense_hits), ("keyword", sparse_hits)):
            for hit in hits:
                entry = merged.setdefault(hit["id"], {"hit": hit, "matched": {}, "strength": {}})
                entry["matched"][signal] = round(hit["score"], 4)
                entry["strength"][signal] = round(_strength(signal, hit["score"]), 4)

        weights = {"semantic": SEMANTIC_WEIGHT, "keyword": KEYWORD_WEIGHT}
        if mode != "hybrid":
            weights = {"semantic": 1.0, "keyword": 1.0}
        results = []
        for entry in merged.values():
            score = sum(weights[s] * v for s, v in entry["strength"].items())
            if entry["hit"]["payload"].get("status") == "superseded":
                score *= 0.5
            if score >= MIN_SCORE:
                results.append({**self._present(entry["hit"]), "score": round(score, 4),
                                "matched": entry["matched"], "strength": entry["strength"]})
        results.sort(key=lambda r: (r["score"], r["updated_at"]), reverse=True)

        reranked = rerank and self.reranker is not None and bool(results)
        if reranked:
            head = results[:RERANK_DEPTH]
            for result, verdict in zip(head, self.reranker.score(query, [r["text"] for r in head]), strict=True):
                result["matched"]["rerank"] = round(verdict, 4)
                result["strength"]["rerank"] = round(verdict, 4)
                result["score"] = round((1 - RERANK_WEIGHT) * result["score"] + RERANK_WEIGHT * verdict, 4)
            head.sort(key=lambda r: (r["score"], r["updated_at"]), reverse=True)
            results[:RERANK_DEPTH] = head
        t3 = time.perf_counter()
        self.search_ms.append((t3 - t0) * 1000)
        self.searches += 1

        return {
            "query": query,
            "mode": mode,
            "reranked": reranked,
            "results": results[:limit],
            "candidates": len(results),
            "searched": {"local": self.local.count(), "replica": self.replica.count()},
            "timing_ms": {
                "embed": round((t1 - t0) * 1000, 2),
                "search": round((t2 - t1) * 1000, 2),
                "rerank": round((t3 - t2) * 1000, 2) if reranked else 0,
                "total": round((t3 - t0) * 1000, 2),
            },
            "network_calls": 0,
        }

    # -- answer -----------------------------------------------------------

    def ask(self, question: str, limit: int = 6) -> dict:
        """Answer from device memory. A local language model phrases the answer when
        one is running and its answer matches the notes it cites; otherwise the
        answer is composed from the memories themselves."""
        started = time.perf_counter()
        found = self.search(question, limit=limit, rerank=True)
        relevant = [r for r in found["results"] if r["score"] >= ANSWER_AT][:3]
        points = []
        for result in relevant:
            earlier = [h for h in self.history(result["id"])[1:3]]
            points.append({"memory": result, "earlier": earlier})

        engine, text, note = "device memory", None, None
        if points:
            text, problem = self.llm.answer(question, points)
            if text:
                engine = f"on-device model {self.llm.model}"
                note = "Numbers checked against the cited notes."
            elif problem == "ungrounded":
                note = "The model's wording did not match its sources, so the notes are quoted instead."
                self.journal.log("answer", "Discarded a generated answer that did not match its sources.", level="warn")
        if text is None:
            text = self._compose(points)
        self.answer_ms.append((time.perf_counter() - started) * 1000)
        self.answers += 1
        return {
            "question": question,
            "answer": text,
            "engine": engine,
            "note": note,
            "points": points,
            "timing_ms": {**found["timing_ms"], "total": round((time.perf_counter() - started) * 1000, 2)},
            "network_calls": 0,
        }

    @staticmethod
    def _compose(points: list[dict]) -> str:
        if not points:
            return "Nothing in this device's memory matches that yet."
        lines = []
        for point in points:
            memory = point["memory"]
            subject = f"{memory['asset']}: " if memory["asset"] else ""
            line = f"{subject}{memory['text']}"
            if point["earlier"]:
                verb = "This resolves" if memory.get("relation") == "resolves" else "This replaces"
                earlier = point["earlier"][0]["text"]
                line += f' {verb} an earlier note: "{earlier}"'
            lines.append(line)
        return "\n".join(lines)

    # -- helpers ----------------------------------------------------------

    def _shards(self, source: str | None) -> list[Shard]:
        if source == "local":
            return [self.local]
        if source == "replica":
            return [self.replica]
        return [self.local, self.replica]

    def _present(self, record: dict) -> dict:
        payload = record["payload"]
        shard = record.get("shard", "local")
        return {
            "id": record["id"],
            "text": payload.get("text", ""),
            "shared_text": payload.get("shared_text"),
            "redacted": bool(payload.get("redacted")),
            "kind": payload.get("kind"),
            "asset": payload.get("asset"),
            "tags": payload.get("tags") or [],
            "site": payload.get("site"),
            "scope": payload.get("scope"),
            "category": payload.get("category"),
            "priority": payload.get("priority", 1),
            "policy": payload.get("policy"),
            "device_id": payload.get("device_id"),
            "origin_device": payload.get("origin_device"),
            "author": payload.get("author"),
            "created_at": payload.get("created_at", 0),
            "updated_at": payload.get("updated_at", 0),
            "rev": payload.get("rev", 1),
            "base_rev": payload.get("rev", 1) if shard == "replica" else payload.get("base_rev", 0),
            "status": payload.get("status", "active"),
            "supersedes": payload.get("supersedes"),
            "superseded_by": payload.get("superseded_by"),
            "relation": payload.get("relation"),
            "sync_state": "replica" if shard == "replica" else payload.get("sync_state", "private"),
            "source": shard,
            "mine": payload.get("origin_device") == self.settings.device_id,
        }


def _strength(signal: str, score: float) -> float:
    """Map a raw score onto 0..1 so the two signals can be weighed against each other."""
    if signal == "semantic":
        return min(1.0, max(0.0, (score - SEMANTIC_FLOOR) / (SEMANTIC_CEILING - SEMANTIC_FLOOR)))
    return score / (score + KEYWORD_HALF)


def _clip(text: str, length: int = 70) -> str:
    text = " ".join(text.split())
    return text if len(text) <= length else text[: length - 1] + "…"
