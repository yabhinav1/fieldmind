"""Edge-to-cloud synchronisation.

Push: replay the outbox in priority order, each write guarded by a
compare-and-swap on the revision the device last saw. If the cloud has moved on,
the change is merged field by field; only a true clash needs a person.

Pull: compare a lightweight manifest (ids and revisions) with what the device
holds and download only what changed.
"""

from __future__ import annotations

import copy
import json
import threading
import time

from qdrant_edge import SparseVector

from .cloud import Cloud
from .journal import Journal
from .policy import PRIVATE, REDACTED
from .service import CONTENT_FIELDS, MemoryService, _clip, cloud_view


def three_way(base: dict | None, mine: dict, theirs: dict) -> tuple[dict, list[str]]:
    """Merge two edits of the same memory. Returns the merge and the fields that clash."""
    base = base or {}
    merged, clashes = {}, []
    for field in CONTENT_FIELDS:
        a, b, o = mine.get(field), theirs.get(field), base.get(field)
        if a == b:
            merged[field] = a
        elif a == o:
            merged[field] = b
        elif b == o:
            merged[field] = a
        elif field == "tags":
            old = set(o or [])
            added = (set(a or []) | set(b or [])) - old
            kept = old & set(a or []) & set(b or [])
            merged[field] = sorted(kept | added)
        else:
            merged[field] = a
            clashes.append(field)
    return merged, clashes


def same_content(a: dict, b: dict) -> bool:
    return all(a.get(f) == b.get(f) for f in CONTENT_FIELDS)


def _size(payload: dict, sparse: SparseVector | None, dense: bool = True) -> int:
    vectors = (4 * 384 if dense else 0) + (8 * len(sparse.indices) if sparse is not None else 0)
    return len(json.dumps(payload, default=str)) + vectors


class SyncEngine:
    def __init__(self, service: MemoryService, journal: Journal, cloud: Cloud):
        self.service = service
        self.journal = journal
        self.cloud = cloud
        self.settings = service.settings
        self._run_lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._reachable = False
        self._checked_at = 0.0
        self._was_online: bool | None = None
        self._offline_since: float | None = time.time()

    # -- link -------------------------------------------------------------

    @property
    def forced_offline(self) -> bool:
        return bool(self.journal.get("forced_offline", False))

    @property
    def auto_sync(self) -> bool:
        return bool(self.journal.get("auto_sync", True))

    def set_forced_offline(self, value: bool) -> None:
        self.journal.set("forced_offline", bool(value))
        self.journal.log("link", "Network disabled on this device." if value else "Network enabled on this device.",
                         level="warn" if value else "info")
        self.check_link()

    def set_auto_sync(self, value: bool) -> None:
        self.journal.set("auto_sync", bool(value))
        self.journal.log("sync", f"Automatic sync turned {'on' if value else 'off'}.")

    def check_link(self) -> bool:
        self._reachable = False if self.forced_offline else self.cloud.reachable()
        self._checked_at = time.time()
        online = self._reachable
        if online != self._was_online:
            if online:
                waited = f" after {_duration(time.time() - self._offline_since)} offline" if self._offline_since else ""
                self.journal.log("link", f"Cloud link is up{waited}.", level="good")
                self._offline_since = None
            else:
                if self._was_online is not None:
                    self.journal.log("link", "Cloud link lost. Working from device memory.", level="warn")
                self._offline_since = time.time()
                self.cloud.forget_connection()
            self._was_online = online
        return online

    def link(self) -> dict:
        return {
            "online": self._reachable,
            "forced_offline": self.forced_offline,
            "auto_sync": self.auto_sync,
            "cloud_url": self.cloud.url,
            "checked_at": self._checked_at,
            "offline_since": self._offline_since,
        }

    # -- background loop --------------------------------------------------

    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, name="fieldmind-sync", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                if self.check_link() and self.auto_sync:
                    self.run_once("auto")
            except Exception as error:  # the loop must survive any single failure
                self.journal.log("sync", f"Background sync error: {error}", level="error")
            self._stop.wait(self.settings.sync_interval)

    # -- one sync cycle ---------------------------------------------------

    def run_once(self, trigger: str = "manual") -> dict:
        if not self._run_lock.acquire(blocking=False):
            return {"status": "busy"}
        try:
            if not self.check_link():
                return {"status": "offline", "pending": self.journal.counts()["pending"]}
            stats = dict(pushed=0, pulled=0, removed=0, merged=0, conflicts=0, bytes_up=0, bytes_down=0, cloud_points=0)
            quiet = trigger == "auto" and not self.journal.pending(1)
            run_id = self.journal.start_run(trigger)
            try:
                self.cloud.ensure()
                self._push(stats)
                self._pull(stats)
                self.service.local.flush()
                self.service.replica.flush()
            except Exception as error:
                self.journal.finish_run(run_id, "failed", str(error), **stats)
                self.journal.log("sync", f"Sync failed: {error}", level="error")
                self.cloud.forget_connection()
                return {"status": "failed", "error": str(error), **stats}
            moved = stats["pushed"] + stats["pulled"] + stats["removed"] + stats["merged"] + stats["conflicts"]
            self.journal.set("last_sync_at", time.time())
            if moved or not quiet:
                self.journal.finish_run(run_id, "ok", **stats)
                self.journal.log("sync", _summary(stats), level="good" if not stats["conflicts"] else "warn", **stats)
            else:
                # A background check that found nothing to do is not worth a history entry.
                self.journal.discard_run(run_id)
            return {"status": "ok", **stats, "pending": self.journal.counts()["pending"]}
        finally:
            self._run_lock.release()

    # -- push -------------------------------------------------------------

    def _push(self, stats: dict) -> None:
        for op in self.journal.pending(self.settings.sync_batch):
            try:
                if op["op"] == "upsert":
                    self._push_upsert(op, stats)
                else:
                    self._push_removal(op, stats)
            except Exception as error:
                self.journal.fail(op["seq"], str(error))
                raise

    def _push_upsert(self, op: dict, stats: dict) -> None:
        memory_id = op["memory_id"]
        record = self.service.local.get(memory_id, with_vector=True)
        if not record or record["payload"].get("scope") == PRIVATE:
            self.journal.finish(op["seq"], "cancelled")
            return
        payload = record["payload"]
        mine = cloud_view(payload)
        theirs_record = self.cloud.get(memory_id)
        theirs = theirs_record["payload"] if theirs_record else None

        if theirs is None:
            expect = None
        elif theirs.get("rev") == op["base_rev"]:
            expect = op["base_rev"]
        elif same_content(mine, theirs):
            self._mark_synced(memory_id, payload, theirs.get("rev", payload["rev"]))
            self.journal.finish(op["seq"])
            return
        elif theirs.get("status") == "deleted":
            # Deleted elsewhere while this device kept working on it: the edit wins.
            mine["rev"] = theirs.get("rev", 0) + 1
            expect = theirs.get("rev")
            self.journal.log("sync", f"Restored a memory that was deleted elsewhere: {_clip(mine['text'])}",
                             level="warn", memory_id=memory_id)
        else:
            merged, clashes = three_way(op["base_payload"], mine, theirs)
            if clashes:
                self._hold_conflict(op, payload, mine, theirs, clashes, stats)
                return
            mine.update(merged)
            mine["rev"] = max(theirs.get("rev", 0), mine["rev"]) + 1
            mine["updated_at"] = time.time()
            expect = theirs.get("rev")
            self._apply_cloud_content(payload, mine)
            record["dense"], record["sparse"] = self.service.embedder.document(payload["text"])
            self.service.local.upsert(memory_id, record["dense"], record["sparse"], payload)
            stats["merged"] += 1
            self.journal.log("merge", f"Merged edits from {theirs.get('device_id')} automatically: {_clip(mine['text'])}",
                             level="good", memory_id=memory_id, fields=sorted(merged))

        if payload.get("scope") == REDACTED or mine["text"] != payload.get("text"):
            # Never upload vectors computed from text the cloud is not allowed to see.
            dense, sparse = self.service.embedder.document(mine["text"])
        else:
            dense, sparse = record["dense"], record["sparse"]

        if not self.cloud.write(memory_id, dense, sparse, mine, expect):
            self.journal.fail(op["seq"], "The cloud copy changed during upload; retrying.")
            return
        payload["rev"] = mine["rev"]
        self._mark_synced(memory_id, payload, mine["rev"])
        self.journal.finish(op["seq"])
        self.journal.close_conflicts_for(memory_id, "superseded")
        stats["pushed"] += 1
        stats["bytes_up"] += _size(mine, sparse)

    def _push_removal(self, op: dict, stats: dict) -> None:
        memory_id = op["memory_id"]
        retract = op["op"] == "retract"
        theirs_record = self.cloud.get(memory_id)
        theirs = theirs_record["payload"] if theirs_record else None
        local = self.service.local.get(memory_id)

        if theirs is None or theirs.get("status") == "deleted":
            self._after_removal(op, local, theirs.get("rev", 0) if theirs else 0)
            return
        if not retract and theirs.get("rev") != op["base_rev"]:
            # Edited elsewhere after this device deleted it: keep the newer edit.
            self.journal.finish(op["seq"], "cancelled", "Edited elsewhere; deletion dropped.")
            self.journal.log("sync", f"Kept a memory that was edited elsewhere after deletion here: {_clip(theirs.get('text', ''))}",
                             level="warn", memory_id=memory_id)
            return

        rev = theirs.get("rev", 0) + 1
        tombstone = {
            "status": "deleted", "tombstone": op["op"], "text": "", "rev": rev,
            "site": theirs.get("site"), "asset": theirs.get("asset"),
            "device_id": self.settings.device_id, "origin_device": theirs.get("origin_device"),
            "updated_at": time.time(),
        }
        if not self.cloud.tombstone(memory_id, tombstone, None if retract else op["base_rev"]):
            self.journal.fail(op["seq"], "The cloud copy changed during removal; retrying.")
            return
        stats["pushed"] += 1
        stats["bytes_up"] += _size(tombstone, None, dense=False)
        self._after_removal(op, local, rev)
        verb = "Withdrew from the cloud" if retract else "Deleted from the cloud"
        self.journal.log("sync", f"{verb}: {_clip((op['base_payload'] or {}).get('text', ''))}", memory_id=memory_id)

    def _after_removal(self, op: dict, local: dict | None, rev: int) -> None:
        if op["op"] == "retract" and local:
            payload = local["payload"]
            payload.update(sync_state="private", base_rev=rev, rev=max(rev, payload.get("rev", 0)), withdrawn=True)
            self.service.local.set_payload(op["memory_id"], payload)
        self.journal.finish(op["seq"])

    def _hold_conflict(self, op: dict, payload: dict, mine: dict, theirs: dict, clashes: list[str], stats: dict) -> None:
        self.journal.add_conflict(op["memory_id"], mine, theirs, op["base_payload"], clashes)
        self.journal.finish(op["seq"], "conflict", "Waiting for a decision.")
        if payload.get("sync_state") != "conflict":
            payload["sync_state"] = "conflict"
            self.service.local.set_payload(op["memory_id"], payload)
            stats["conflicts"] += 1
            self.journal.log(
                "conflict",
                f"{theirs.get('device_id')} changed the same memory ({', '.join(clashes)}): {_clip(mine['text'])}",
                level="warn", memory_id=op["memory_id"], fields=clashes)

    def _mark_synced(self, memory_id: str, payload: dict, rev: int) -> None:
        payload.update(sync_state="synced", base_rev=rev, rev=rev)
        payload.pop("withdrawn", None)
        self.service.local.set_payload(memory_id, payload)

    @staticmethod
    def _apply_cloud_content(payload: dict, cloud_payload: dict) -> None:
        """Copy cloud content into a local payload, keeping an unshared original if there is one."""
        keeps_original = payload.get("scope") == REDACTED and cloud_payload.get("text") == payload.get("shared_text")
        for field in CONTENT_FIELDS:
            if field == "text" and keeps_original:
                continue
            if field in cloud_payload:
                payload[field] = copy.deepcopy(cloud_payload[field])
        if payload.get("scope") == REDACTED and not keeps_original:
            payload["shared_text"] = cloud_payload.get("text")

    # -- pull -------------------------------------------------------------

    def _pull(self, stats: dict) -> None:
        service, journal = self.service, self.journal
        manifest = self.cloud.manifest(self.settings.site)
        stats["cloud_points"] = sum(1 for v in manifest.values() if v.get("status") != "deleted")
        local = {r["id"]: r["payload"] for r in service.local.scroll()}
        replica = {r["id"]: r["payload"].get("rev", 0) for r in service.replica.scroll()}
        wanted: dict[str, str] = {}

        for memory_id, entry in manifest.items():
            deleted = entry.get("status") == "deleted"
            rev = entry.get("rev", 0)
            if memory_id in local:
                mine = local[memory_id]
                if journal.open_op(memory_id) or mine.get("scope") == PRIVATE:
                    continue
                if deleted:
                    service.local.delete([memory_id])
                    stats["removed"] += 1
                    journal.log("sync", f"Removed, deleted by {entry.get('device_id')}: {_clip(mine.get('text', ''))}",
                                memory_id=memory_id)
                elif rev > mine.get("rev", 0):
                    wanted[memory_id] = "local"
            elif deleted:
                if memory_id in replica:
                    service.replica.delete([memory_id])
                    stats["removed"] += 1
            elif replica.get(memory_id) != rev:
                wanted[memory_id] = "replica"

        gone = [i for i in replica if i not in manifest]
        service.replica.delete(gone)
        stats["removed"] += len(gone)

        for memory_id, mine in local.items():
            # The cloud has no trace of something this device already delivered
            # (for example the collection was rebuilt). Send it again.
            if memory_id not in manifest and mine.get("sync_state") == "synced" and not journal.open_op(memory_id):
                mine.update(sync_state="pending", base_rev=0)
                service.local.set_payload(memory_id, mine)
                journal.enqueue(memory_id, "upsert", mine.get("priority", 1), 0, None)
                journal.log("sync", f"Cloud copy missing, queued again: {_clip(mine.get('text', ''))}",
                            level="warn", memory_id=memory_id)

        ids = list(wanted)
        for start in range(0, len(ids), 64):
            for record in self.cloud.get_many(ids[start:start + 64], with_vectors=True):
                target = wanted[record["id"]]
                cloud_payload = record["payload"]
                stats["bytes_down"] += _size(cloud_payload, record.get("sparse"))
                stats["pulled"] += 1
                if target == "replica":
                    payload = {**cloud_payload, "sync_state": "replica", "base_rev": cloud_payload.get("rev", 0)}
                    service.replica.upsert(record["id"], record["dense"], record["sparse"], payload)
                    continue
                payload = local[record["id"]]
                self._apply_cloud_content(payload, cloud_payload)
                payload.update(rev=cloud_payload.get("rev", 0), base_rev=cloud_payload.get("rev", 0),
                               updated_at=cloud_payload.get("updated_at"), device_id=cloud_payload.get("device_id"),
                               sync_state="synced")
                dense, sparse = service.embedder.document(payload["text"])
                service.local.upsert(record["id"], dense, sparse, payload)
                journal.log("sync", f"Updated by {cloud_payload.get('device_id')}: {_clip(payload['text'])}",
                            memory_id=record["id"])

    # -- conflicts --------------------------------------------------------

    def resolve(self, conflict_id: int, choice: str, text: str | None = None) -> dict:
        """Settle a conflict. Works offline: the decision is applied locally and synced later."""
        conflict = self.journal.conflict(conflict_id)
        if not conflict or conflict["state"] != "open":
            raise KeyError(conflict_id)
        if choice not in ("mine", "theirs", "both", "merge"):
            raise ValueError(f"Unknown choice: {choice}")
        if choice == "merge" and not (text or "").strip():
            raise ValueError("A merged text is required.")
        service = self.service
        memory_id = conflict["memory_id"]
        theirs = conflict["cloud_payload"]
        record = service.local.get(memory_id, with_vector=True)
        if not record:
            self.journal.resolve_conflict(conflict_id, "gone")
            return {"status": "gone"}
        payload = record["payload"]

        if choice == "both":
            service.capture(payload["text"], kind=payload.get("kind") or "observation", asset=payload.get("asset"),
                            tags=payload.get("tags"), scope=payload.get("scope"), link=False, allow_duplicate=True)
            choice_applied = "theirs"
        else:
            choice_applied = choice

        if choice_applied == "theirs":
            self._apply_cloud_content(payload, theirs)
            payload.update(rev=theirs.get("rev", 0), base_rev=theirs.get("rev", 0), sync_state="synced",
                           updated_at=theirs.get("updated_at"), device_id=theirs.get("device_id"))
            self.journal.cancel(memory_id)
        else:
            if choice_applied == "merge":
                payload["text"] = text.strip()
                decision = service.policy.decide(payload["text"], override=payload.get("scope"))
                payload.pop("shared_text", None)
                if decision.shared_text:
                    payload["shared_text"] = decision.shared_text
            payload.update(rev=max(theirs.get("rev", 0), payload.get("rev", 0)) + 1, base_rev=theirs.get("rev", 0),
                           sync_state="pending", updated_at=time.time(), device_id=self.settings.device_id)
            self.journal.rebase(memory_id, theirs.get("rev", 0), theirs)

        dense, sparse = service.embedder.document(payload["text"])
        service.local.upsert(memory_id, dense, sparse, payload)
        service.local.flush()
        self.journal.resolve_conflict(conflict_id, choice)
        label = {"mine": "kept this device's version", "theirs": f"accepted the version from {theirs.get('device_id')}",
                 "both": "kept both as separate memories", "merge": "saved a combined version"}[choice]
        self.journal.log("conflict", f"Conflict settled, {label}: {_clip(payload['text'])}", level="good",
                         memory_id=memory_id, choice=choice)
        return {"status": "resolved", "choice": choice, "memory": service.get(memory_id)}

    # -- replica ----------------------------------------------------------

    def rebuild_replica(self) -> dict:
        """Throw away the cloud replica and download it again. Device-authored memory is untouched."""
        before = self.service.replica.count()
        self.service.replica.clear()
        self.journal.log("sync", f"Cleared the cloud replica ({before} memories). It refills on the next sync.")
        return {"cleared": before, "sync": self.run_once("rebuild")}


def _summary(stats: dict) -> str:
    parts = []
    if stats["pushed"]:
        parts.append(f"sent {stats['pushed']}")
    if stats["pulled"]:
        parts.append(f"received {stats['pulled']}")
    if stats["merged"]:
        parts.append(f"merged {stats['merged']}")
    if stats["removed"]:
        parts.append(f"removed {stats['removed']}")
    if stats["conflicts"]:
        parts.append(f"{stats['conflicts']} need a decision")
    if not parts:
        return "Sync complete, already up to date."
    return "Sync complete: " + ", ".join(parts) + "."


def _duration(seconds: float) -> str:
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m {seconds % 60}s"
    return f"{seconds // 3600}h {seconds % 3600 // 60}m"
