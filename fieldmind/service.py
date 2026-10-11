"""The device's memory: capture, edit, search and answer, all without a network."""

from __future__ import annotations

import copy
import math
import re
import threading
import time
import uuid
from collections import deque
from typing import Any

from . import schema
from .config import DENSE, SPARSE, Settings
from .embedder import Embedder
from .journal import Journal
from .llm import LocalModel
from .photos import PhotoStore, image_strength
from .policy import PRIVATE, REDACTED, SHARED, PolicyEngine, find_asset
from .reranker import Reranker
from .review import APPROVED, PENDING, REJECTED, REVIEW_FIELDS, ROLES, TRUST_AFTER
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
# A photo that looks like what the question describes adds to its memory's score.
IMAGE_WEIGHT = 0.4
# Memory evolves: when two notes match equally well, the more recent one should
# come first. A note gets up to this much extra, fading over RECENCY_DAYS.
RECENCY_WEIGHT = 0.08
RECENCY_DAYS = 30.0

# Reference material is published centrally. A field note can relate to it but
# never replaces it.
PROTECTED_KINDS = ("reference",)

# Fields that travel to the cloud. Everything else is device bookkeeping.
CLOUD_FIELDS = (
    "text", "kind", "asset", "tags", "site", "scope", "category", "priority",
    "device_id", "origin_device", "author", "created_at", "updated_at", "rev",
    "status", "supersedes", "superseded_by", "relation", "redacted", "schema", "expires_at",
) + REVIEW_FIELDS

# An announcement from a supervisor: shared, sent first, shown on every device's
# Work page until it expires, then removed everywhere.
ANNOUNCEMENT = "announcement"
MAX_ANNOUNCEMENT_HOURS = 24 * 14
# Fields a person can change, and therefore the ones that can conflict.
CONTENT_FIELDS = ("text", "kind", "asset", "tags", "status", "supersedes", "superseded_by", "relation")

SCOPE_WORDS = {PRIVATE: "private", SHARED: "shared", REDACTED: "shared with details masked"}
CATEGORY_TITLE = {
    "safety_hazard": "Safety hazards", "equipment_fault": "Equipment faults", "procedure_fix": "Work done and procedures",
    "routine_reading": "Routine readings", "personal_health": "Personal and health", "people_hr": "People matters",
    "scratch_note": "Personal notes", "other": "Other",
}

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
                 journal: Journal, policy: PolicyEngine, reranker: Reranker | None = None,
                 photos: PhotoStore | None = None):
        self.settings = settings
        self.embedder = embedder
        self.local = local
        self.replica = replica
        self.journal = journal
        self.policy = policy
        self.reranker = reranker if reranker is not None and reranker.available else None
        self.photos = photos
        self.llm = LocalModel(settings.ollama_url, settings.ollama_model)
        # Every change to the local shard and the outbox happens under this lock, so
        # a capture from the dashboard and a sync cycle never interleave half-way.
        self.lock = threading.RLock()
        # Recent search and answer times, for /metrics.
        self.search_ms: deque[float] = deque(maxlen=500)
        self.answer_ms: deque[float] = deque(maxlen=200)
        self.searches = 0
        self.answers = 0

    # -- roles and review -------------------------------------------------

    @property
    def role(self) -> str:
        return self.settings.role if self.settings.role in ROLES else "technician"

    @property
    def trusted(self) -> bool:
        return bool(self.journal.get("trusted"))

    @property
    def approval_streak(self) -> int:
        return int(self.journal.get("approval_streak", 0) or 0)

    def needs_review(self) -> bool:
        return self.role == "trainee" and not self.trusted

    def _stamp_review(self, payload: dict) -> None:
        """Mark a shared memory for review, or as cleared, according to this device's role."""
        payload["role"] = self.role
        for field in ("reviewed_by", "reviewed_at", "review_reason"):
            payload.pop(field, None)
        if payload.get("scope") == PRIVATE:
            payload.pop("review", None)
        elif self.needs_review():
            payload["review"] = PENDING
        elif self.role == "trainee":
            payload.update(review=APPROVED, reviewed_by="trusted", reviewed_at=time.time())
        else:
            payload.pop("review", None)

    def approvals(self) -> list[dict]:
        """Memories from this site waiting for this supervisor's decision."""
        if self.role != "supervisor":
            return []
        out = []
        for record in self.replica.scroll(build_filter({"review": PENDING})):
            if self.journal.open_op(record["id"]):
                continue  # decided here already, waiting to be sent
            out.append(self._present(record))
        out.sort(key=lambda m: (-m["priority"], m["created_at"]))
        return out

    def review(self, memory_id: str, decision: str, reason: str = "") -> dict:
        """A supervisor's decision on a memory awaiting review. Works offline: the
        decision is applied to this device's copy now and sent to the cloud later."""
        if self.role != "supervisor":
            raise PermissionError("Only a supervisor's device can approve or reject notes.")
        if decision not in (APPROVED, REJECTED):
            raise ValueError(f"Unknown decision: {decision}")
        reason = (reason or "").strip()
        if decision == REJECTED and not reason:
            raise ValueError("Say why the note is rejected; the author will see the reason.")
        with self.lock:
            record = self.replica.get(memory_id)
            if not record or record["payload"].get("review") != PENDING:
                raise NotFound(memory_id)
            payload = record["payload"]
            cloud_copy = {k: v for k, v in payload.items() if k not in ("sync_state", "base_rev", "via_peer")}
            self.journal.enqueue(memory_id, "review", payload.get("priority", 1), payload.get("rev", 0),
                                 {"decision": decision, "reason": reason, "payload": cloud_copy})
            if decision == APPROVED:
                payload.update(review=APPROVED, reviewed_by=self.settings.device_id, reviewed_at=time.time())
                self.replica.set_payload(memory_id, payload)
            else:
                self.replica.delete([memory_id])
            self.replica.flush()
        verb = "Approved" if decision == APPROVED else f"Rejected ({reason})"
        self.journal.log("review", f"{verb}: {_clip(payload.get('text', ''))}", level="good" if decision == APPROVED else "warn",
                         memory_id=memory_id, decision=decision, author_device=payload.get("origin_device"))
        return {"status": decision, "memory_id": memory_id, "queued": True}

    def note_review_outcome(self, memory_id: str, payload: dict, before: str | None, cloud_payload: dict) -> None:
        """This device's memory was approved or rejected elsewhere: keep score and tell the person."""
        outcome = cloud_payload.get("review")
        who = cloud_payload.get("reviewed_by") or "a supervisor"
        if outcome == APPROVED and before == PENDING:
            streak = self.approval_streak + 1
            self.journal.set("approval_streak", streak)
            self.journal.log("review", f"Approved by {who}: {_clip(payload.get('text', ''))}", level="good",
                             memory_id=memory_id, streak=streak)
            if streak >= TRUST_AFTER and not self.trusted and self.role == "trainee":
                self.journal.set("trusted", True)
                self.journal.log("review", f"{streak} approvals in a row: this device is now trusted and its notes "
                                           "no longer wait for approval.", level="good")
        elif outcome == REJECTED:
            self.journal.set("approval_streak", 0)
            if self.trusted:
                self.journal.set("trusted", False)
            reason = cloud_payload.get("review_reason") or ""
            self.journal.log("review", f"Rejected by {who}{f' ({reason})' if reason else ''}, kept here as private: "
                                       f"{_clip(payload.get('text', ''))}", level="warn", memory_id=memory_id, reason=reason)

    # -- lookup -----------------------------------------------------------

    def find(self, memory_id: str, with_vector: bool = False) -> dict | None:
        return self.local.get(memory_id, with_vector) or self.replica.get(memory_id, with_vector)

    def get(self, memory_id: str) -> dict:
        record = self.find(memory_id)
        if not record:
            raise NotFound(memory_id)
        out = self._present(record)
        if self.photos:
            out["photos"] = self.photos.for_memory(memory_id)
        return out

    def revisions(self, memory_id: str) -> list[dict]:
        """Every version of a memory this device saw, newest first, with who changed it."""
        if not self.find(memory_id):
            raise NotFound(memory_id)
        return [{"rev": r["rev"], "ts": r["ts"], "device_id": r["device_id"], "author": r["author"], "note": r["note"],
                 **{k: r["snapshot"].get(k) for k in ("text", "kind", "asset", "tags", "scope", "status", "review")}}
                for r in self.journal.revisions(memory_id)]

    def restore(self, memory_id: str, rev: int) -> dict:
        """Bring an earlier version's content back as a new revision (nothing is rewritten)."""
        match = next((r for r in self.journal.revisions(memory_id) if r["rev"] == rev), None)
        if not match:
            raise NotFound(memory_id)
        snapshot = match["snapshot"]
        with self.lock:
            result = self._mutate(memory_id, {"text": snapshot.get("text") or "", "kind": snapshot.get("kind") or "observation",
                                              "asset": snapshot.get("asset"), "tags": sorted(set(snapshot.get("tags") or []))},
                                  note=f"Restored revision {rev}.")
            self.local.flush()
        return result

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
        return [self._present(r) for r in records if not _expired(r["payload"])]

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
            "awaiting_review": count(self.local, review=PENDING),
            "approvals": len(self.approvals()),
            "failed": count(self.local, sync_state="failed"),
            "superseded": count(self.local, status="superseded") + count(self.replica, status="superseded"),
            "photos": self.photos.shard.count() if self.photos else 0,
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

    # -- announcements ----------------------------------------------------

    def announce(self, text: str, hours: float = 8.0) -> dict:
        """A supervisor tells the whole site something for a while. It travels like a
        safety note (first, never waiting for approval) and disappears from every
        device when the time is up."""
        if self.role != "supervisor":
            raise PermissionError("Only a supervisor's device can announce to the site.")
        text = text.strip()
        if not text:
            raise ValueError("An announcement needs some text.")
        if not 0 < hours <= MAX_ANNOUNCEMENT_HOURS:
            raise ValueError(f"An announcement lasts between a minute and {MAX_ANNOUNCEMENT_HOURS // 24} days.")
        with self.lock:
            result = self._capture(text, ANNOUNCEMENT, None, [], SHARED, link=False, allow_duplicate=True,
                                   supersede=False, learn=False, expires_at=time.time() + hours * 3600, mask=False)
        if not result["created"]:
            return result
        if result["memory"]["scope"] == PRIVATE:
            # A credential in an announcement: the policy kept it on this device.
            raise ValueError("That text contains a credential, so it cannot be announced.")
        self.journal.log("announce", f"Announced for {_duration(hours * 3600)}: {_clip(text)}", level="good",
                         memory_id=result["memory"]["id"], hours=hours)
        return result

    def announcements(self) -> list[dict]:
        """Announcements in force on this site, newest first."""
        now = time.time()
        out = []
        for shard in (self.local, self.replica):
            for record in shard.scroll(build_filter({"kind": ANNOUNCEMENT, "status": "active"})):
                expires = record["payload"].get("expires_at") or 0
                if expires > now and record["payload"].get("scope") != PRIVATE:
                    out.append({**self._present(record), "expires_at": expires})
        out.sort(key=lambda m: m["created_at"], reverse=True)
        return out

    def sweep_expired(self) -> int:
        """Drop announcements whose time is up: this device's own are deleted (so the
        tombstone reaches everyone), copies from others are simply forgotten."""
        now = time.time()
        gone = 0
        with self.lock:
            for record in self.local.scroll(build_filter({"kind": ANNOUNCEMENT}), fields=["expires_at", "text"]):
                if 0 < (record["payload"].get("expires_at") or 0) <= now:
                    self.delete(record["id"])
                    gone += 1
            stale = [r["id"] for r in self.replica.scroll(build_filter({"kind": ANNOUNCEMENT}), fields=["expires_at"])
                     if 0 < (r["payload"].get("expires_at") or 0) <= now]
            if stale:
                self.replica.delete(stale)
                self.replica.flush()
                gone += len(stale)
        if gone:
            self.journal.log("announce", f"{gone} announcement{'s' if gone != 1 else ''} expired and were removed.")
        return gone

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
                 link: bool, allow_duplicate: bool, supersede: bool, learn: bool = True,
                 expires_at: float | None = None, mask: bool = True) -> dict:
        decision = self.policy.decide(text, override=scope)
        if not mask and decision.scope == REDACTED:
            # A supervisor's announcement names people on purpose; only a credential
            # still keeps it on the device.
            decision.scope, decision.shared_text = SHARED, None
        asset = asset or find_asset(text)
        related = self.related(text, asset) if link else []

        duplicate = next((r for r in related if r["relation"] == "duplicate"), None)
        if duplicate and not allow_duplicate:
            self.journal.log("dedupe", f"Skipped a duplicate of an existing memory about {asset}.",
                             memory_id=duplicate["id"])
            return {"created": False, "duplicate_of": duplicate, "decision": decision.to_dict()}

        now = time.time()
        memory_id = str(uuid.uuid4())
        if decision.decided_by == "user" and learn:
            self.policy.learn(text, decision.scope)

        payload = {
            "schema": schema.CURRENT,
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
        if expires_at:
            payload["expires_at"] = expires_at
            payload["priority"] = max(payload["priority"], 2)  # announcements go first and never wait
        self._stamp_review(payload)

        # A follow-up replaces what the device believed before, as long as doing so
        # would not reveal a private note to the fleet.
        target = next((r for r in related if r["relation"] in ("updates", "resolves")), None)
        if supersede and target and (decision.scope != PRIVATE or target["scope"] == PRIVATE):
            payload["supersedes"] = target["id"]
            payload["relation"] = target["relation"]

        dense, sparse = self.embedder.document(text)
        self.local.upsert(memory_id, dense, sparse, payload)
        self.journal.record_revision(memory_id, payload, "Written here")
        if decision.scope != PRIVATE:
            self.journal.enqueue(memory_id, "upsert", payload["priority"], 0, None)
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

    # -- photos -----------------------------------------------------------

    def attach_photo(self, data: bytes, caption: str, scope: str | None = None, supersede: bool = True) -> dict:
        """Save a photo with what it shows. The caption is captured as a memory and
        decides the scope; the photo is attached and follows it. A caption that
        repeats an existing note attaches the photo to that note instead."""
        if not self.photos:
            raise ValueError("Photos are turned off on this device (FIELDMIND_PHOTOS=1 turns them on).")
        if not caption.strip():
            raise ValueError("Say what the photo shows.")
        with self.lock:
            result = self.capture(caption, kind="photo", scope=scope, supersede=supersede)
            memory = result["memory"] if result["created"] else result["duplicate_of"]
            photo = self.photos.add(memory, data)
            if photo["sync_state"] == "pending":
                self.journal.enqueue(photo["id"], "photo", memory.get("priority", 1), 0, {"memory_id": memory["id"]})
            self.journal.log("capture", f"Photo attached, {SCOPE_WORDS[memory['scope']]}: {_clip(memory['text'])}",
                             memory_id=memory["id"], photo_id=photo["id"])
            return {**result, "memory": self.get(memory["id"]), "photo": photo}

    def _rescope_photos(self, memory_id: str, scope: str) -> None:
        """A memory's scope changed, so its photos change with it: withdrawn from the
        cloud when it turns private, queued for upload when it is shared again."""
        if not self.photos:
            return
        for before, after in zip(self.photos.for_memory(memory_id), self.photos.rescope(memory_id, scope), strict=True):
            if after["sync_state"] == "private":
                if before["sync_state"] == "synced":
                    self.journal.enqueue(after["id"], "photo_delete", 1, 0, {"memory_id": memory_id})
                else:
                    self.journal.cancel(after["id"])
            elif after["sync_state"] == "pending":
                self.journal.enqueue(after["id"], "photo", 1, 0, {"memory_id": memory_id})

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
            if self.photos:
                for photo in self.photos.remove_for(memory_id):
                    if photo["sync_state"] in ("synced", "replica"):
                        self.journal.enqueue(photo["id"], "photo_delete", 1, 0, {"memory_id": memory_id})
                    else:
                        self.journal.cancel(photo["id"])
            self.local.delete([memory_id])
            self.replica.delete([memory_id])
            self.journal.forget_revisions(memory_id)
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
        if text_changed or payload["scope"] != before_scope or payload.get("review") == REJECTED:
            # What the supervisor saw is no longer what is being shared.
            self._stamp_review(payload)

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
        self.journal.record_revision(memory_id, payload, note or "Changed here")
        if record["shard"] == "replica":
            self.replica.delete([memory_id])
        if payload["scope"] != before_scope:
            self._rescope_photos(memory_id, payload["scope"])

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

        merged: dict[str, dict] = {}
        for signal, hits in (("semantic", dense_hits), ("keyword", sparse_hits)):
            for hit in hits:
                entry = merged.setdefault(hit["id"], {"hit": hit, "matched": {}, "strength": {}})
                entry["matched"][signal] = round(hit["score"], 4)
                entry["strength"][signal] = round(_strength(signal, hit["score"]), 4)

        # Photos that look like what the question describes count for their memory.
        if self.photos and dense_q is not None and self.photos.available:
            for photo in self.photos.search(query, depth):
                memory_id = photo["memory_id"]
                entry = merged.get(memory_id)
                if entry is None:
                    record = self.find(memory_id)
                    if not record or not _passes(record["payload"], must, must_not):
                        continue
                    entry = merged[memory_id] = {"hit": record, "matched": {}, "strength": {}}
                if photo["score"] > entry["matched"].get("image", -1):
                    entry["matched"]["image"] = round(photo["score"], 4)
                    entry["strength"]["image"] = round(image_strength(photo["score"]), 4)
        t2 = time.perf_counter()

        weights = {"semantic": SEMANTIC_WEIGHT, "keyword": KEYWORD_WEIGHT, "image": IMAGE_WEIGHT}
        if mode != "hybrid":
            weights = {"semantic": 1.0, "keyword": 1.0, "image": 1.0}
        results = []
        for entry in merged.values():
            if _expired(entry["hit"]["payload"]):
                continue
            score = sum(weights[s] * v for s, v in entry["strength"].items())
            if entry["hit"]["payload"].get("status") == "superseded":
                score *= 0.5
            age_days = max(0.0, time.time() - (entry["hit"]["payload"].get("updated_at") or 0)) / 86400
            score *= 1 + RECENCY_WEIGHT * math.exp(-age_days / RECENCY_DAYS)
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

        results = results[:limit]
        if self.photos:
            photos = self.photos.for_memories([r["id"] for r in results])
            for result in results:
                result["photos"] = photos.get(result["id"], [])

        return {
            "query": query,
            "mode": mode,
            "reranked": reranked,
            "results": results,
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

    # -- report -----------------------------------------------------------

    def report(self, hours: float = 24, include_private: bool = False) -> str:
        """A shift report in Markdown: what this device recorded in the last ``hours``,
        grouped by kind, hazards first. Private notes are left out unless asked for;
        masked notes appear as the fleet sees them."""
        since = time.time() - hours * 3600
        notes = [m for m in self.list(source="local") if m["updated_at"] >= since and m["status"] == "active"]
        if not include_private:
            notes = [m for m in notes if m["scope"] != PRIVATE]
        groups: dict[str, list[dict]] = {}
        for note in notes:
            groups.setdefault(note.get("category") or "other", []).append(note)
        order = ["safety_hazard", "equipment_fault", "procedure_fix", "routine_reading", "personal_health", "people_hr",
                 "scratch_note", "other"]
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime())
        lines = [f"# Shift report: {self.settings.device_id} ({self.settings.site})", "",
                 f"Last {hours:g} hours, written {when} by {self.settings.author}. "
                 f"{len(notes)} note{'s' if len(notes) != 1 else ''}"
                 + ("" if include_private else "; private notes are not included") + ".", ""]
        for key in order:
            items = groups.get(key)
            if not items:
                continue
            lines.append(f"## {CATEGORY_TITLE.get(key, key.replace('_', ' ').title())}")
            lines.append("")
            for m in sorted(items, key=lambda m: (-(m.get("priority") or 0), m["updated_at"])):
                text = m["shared_text"] if (m["scope"] == REDACTED and not include_private and m.get("shared_text")) else m["text"]
                asset = f"**{m['asset']}** · " if m.get("asset") else ""
                flags = []
                if m.get("priority") == 2:
                    flags.append("urgent")
                if m.get("photos"):
                    flags.append(f"{len(m['photos'])} photo{'s' if len(m['photos']) != 1 else ''}")
                if m.get("sync_state") == "pending":
                    flags.append("not yet synced")
                if m.get("relation") in ("updates", "resolves") and m.get("supersedes"):
                    flags.append("resolves an earlier note" if m["relation"] == "resolves" else "updates an earlier note")
                suffix = f" _({', '.join(flags)})_" if flags else ""
                lines.append(f"- {asset}{text}{suffix}  ")
                lines.append(f"  {time.strftime('%H:%M', time.localtime(m['updated_at']))}, {m.get('author') or m.get('device_id')}")
            lines.append("")
        if not notes:
            lines.append("_Nothing recorded in this period._")
        return "\n".join(lines).rstrip() + "\n"

    # -- helpers ----------------------------------------------------------

    def _shards(self, source: str | None) -> list[Shard]:
        if source == "local":
            return [self.local]
        if source == "replica":
            return [self.replica]
        return [self.local, self.replica]

    def _present(self, record: dict) -> dict:
        payload = schema.upgrade(record["payload"])
        shard = record.get("shard", "local")
        return {
            "id": record["id"],
            "schema": payload["schema"],
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
            "via_peer": payload.get("via_peer"),
            "expires_at": payload.get("expires_at"),
            "review": payload.get("review"),
            "reviewed_by": payload.get("reviewed_by"),
            "reviewed_at": payload.get("reviewed_at"),
            "review_reason": payload.get("review_reason"),
            "role": payload.get("role"),
            "source": shard,
            "mine": payload.get("origin_device") == self.settings.device_id,
        }


def _strength(signal: str, score: float) -> float:
    """Map a raw score onto 0..1 so the two signals can be weighed against each other."""
    if signal == "semantic":
        return min(1.0, max(0.0, (score - SEMANTIC_FLOOR) / (SEMANTIC_CEILING - SEMANTIC_FLOOR)))
    return score / (score + KEYWORD_HALF)


def _passes(payload: dict, must: dict, must_not: dict | None) -> bool:
    """Apply the same keyword filter the shards were given to a record found another way."""
    for key, value in must.items():
        if value is not None and value != "" and payload.get(key) != value:
            return False
    for key, value in (must_not or {}).items():
        values = value if isinstance(value, (list, tuple, set)) else [value]
        if payload.get(key) in values:
            return False
    return True


def _expired(payload: dict) -> bool:
    expires = payload.get("expires_at") or 0
    return 0 < expires <= time.time()


def _duration(seconds: float) -> str:
    if seconds < 3600:
        return f"{max(1, round(seconds / 60))} min"
    if seconds < 86400:
        return f"{round(seconds / 3600)} h"
    return f"{round(seconds / 86400)} days"


def _clip(text: str, length: int = 70) -> str:
    text = " ".join(text.split())
    return text if len(text) <= length else text[: length - 1] + "…"
