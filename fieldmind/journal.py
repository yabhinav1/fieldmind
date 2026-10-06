"""Durable device-side bookkeeping in SQLite: outbox, conflicts, activity and sync runs.

The outbox is what makes offline writes safe. Every change that must reach the
cloud is recorded here first, so it survives a restart and is replayed in
priority order when the link returns.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

from .vault import Vault

SCHEMA = """
CREATE TABLE IF NOT EXISTS outbox (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    memory_id TEXT NOT NULL,
    op TEXT NOT NULL,
    priority INTEGER NOT NULL DEFAULT 1,
    base_rev INTEGER NOT NULL DEFAULT 0,
    base_payload TEXT,
    state TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS outbox_state ON outbox(state, priority DESC, seq ASC);
CREATE TABLE IF NOT EXISTS conflicts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    memory_id TEXT NOT NULL,
    local_payload TEXT NOT NULL,
    cloud_payload TEXT NOT NULL,
    base_payload TEXT,
    fields TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'open',
    resolution TEXT,
    detected_at REAL NOT NULL,
    resolved_at REAL
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    type TEXT NOT NULL,
    level TEXT NOT NULL,
    message TEXT NOT NULL,
    data TEXT
);
CREATE TABLE IF NOT EXISTS sync_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at REAL NOT NULL,
    finished_at REAL,
    trigger TEXT,
    status TEXT,
    pushed INTEGER DEFAULT 0,
    pulled INTEGER DEFAULT 0,
    removed INTEGER DEFAULT 0,
    merged INTEGER DEFAULT 0,
    conflicts INTEGER DEFAULT 0,
    bytes_up INTEGER DEFAULT 0,
    bytes_down INTEGER DEFAULT 0,
    cloud_points INTEGER DEFAULT 0,
    error TEXT
);
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT);
"""


# How much history a device keeps. Pruning runs every PRUNE_EVERY inserts, so the
# tables never grow without bound on a device that runs for months.
KEEP_EVENTS = 20_000
KEEP_RUNS = 2_000
KEEP_FINISHED_OPS = 5_000
PRUNE_EVERY = 256


class Journal:
    def __init__(self, path: Path, keep_events: int = KEEP_EVENTS, keep_runs: int = KEEP_RUNS,
                 keep_finished_ops: int = KEEP_FINISHED_OPS, vault: Vault | None = None):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        self.vault = vault or Vault(None)
        self._keep = {"events": keep_events, "sync_runs": keep_runs, "outbox": keep_finished_ops}
        self._writes = 0
        with self._lock:
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.executescript(SCHEMA)
            self.prune()

    def prune(self) -> None:
        """Drop the oldest activity, sync runs and finished outbox entries beyond the retention caps."""
        closed = "state NOT IN ('pending', 'conflict', 'failed')"
        with self._lock:
            self._db.execute("DELETE FROM events WHERE id <= (SELECT id FROM events ORDER BY id DESC LIMIT 1 OFFSET ?)",
                             (self._keep["events"],))
            self._db.execute("DELETE FROM sync_runs WHERE id <= (SELECT id FROM sync_runs ORDER BY id DESC LIMIT 1 OFFSET ?)",
                             (self._keep["sync_runs"],))
            self._db.execute(f"DELETE FROM outbox WHERE {closed} AND seq <= "
                             f"(SELECT seq FROM outbox WHERE {closed} ORDER BY seq DESC LIMIT 1 OFFSET ?)",
                             (self._keep["outbox"],))

    def _maybe_prune(self) -> None:
        self._writes += 1
        if self._writes % PRUNE_EVERY == 0:
            self.prune()

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def _all(self, sql: str, args: tuple = ()) -> list[dict]:
        with self._lock:
            return [_row(r) for r in self._db.execute(sql, args).fetchall()]

    def _one(self, sql: str, args: tuple = ()) -> dict | None:
        rows = self._all(sql, args)
        return rows[0] if rows else None

    def _run(self, sql: str, args: tuple = ()) -> int:
        with self._lock:
            return self._db.execute(sql, args).lastrowid

    # -- key/value --------------------------------------------------------

    def get(self, key: str, default: Any = None) -> Any:
        row = self._one("SELECT value FROM kv WHERE key = ?", (key,))
        return default if row is None else json.loads(row["value"])

    def set(self, key: str, value: Any) -> None:
        self._run("INSERT INTO kv(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                  (key, json.dumps(value)))

    # -- activity ---------------------------------------------------------

    def log(self, type_: str, message: str, level: str = "info", **data: Any) -> None:
        """Record an activity line. Messages quote note text, so they are sealed on disk."""
        self._run("INSERT INTO events(ts, type, level, message, data) VALUES(?, ?, ?, ?, ?)",
                  (time.time(), type_, level, self.vault.seal(message), json.dumps(data) if data else None))
        self._maybe_prune()

    def events(self, after: int = 0, limit: int = 200) -> list[dict]:
        rows = self._all("SELECT * FROM events WHERE id > ? ORDER BY id DESC LIMIT ?", (after, limit))
        for row in rows:
            row["message"] = self.vault.unseal(row["message"])
        return rows[::-1]

    # -- outbox -----------------------------------------------------------

    def enqueue(self, memory_id: str, op: str, priority: int, base_rev: int, base_payload: dict | None) -> None:
        """Queue a change. Repeated edits of one memory collapse into a single entry
        that keeps the original base, so the cloud sees one change against the
        version this device last agreed on. An edit also revives a parked entry."""
        now = time.time()
        with self._lock:
            open_op = self._one(
                "SELECT * FROM outbox WHERE memory_id = ? AND state IN ('pending', 'conflict', 'failed') ORDER BY seq DESC LIMIT 1",
                (memory_id,),
            )
            if open_op:
                self._run(
                    "UPDATE outbox SET op = ?, priority = MAX(priority, ?), state = 'pending', attempts = 0, "
                    "last_error = NULL, updated_at = ? WHERE seq = ?",
                    (op, priority, now, open_op["seq"]),
                )
                return
            self._run(
                "INSERT INTO outbox(memory_id, op, priority, base_rev, base_payload, created_at, updated_at) VALUES(?, ?, ?, ?, ?, ?, ?)",
                (memory_id, op, priority, base_rev, json.dumps(base_payload) if base_payload else None, now, now),
            )

    def rebase(self, memory_id: str, base_rev: int, base_payload: dict | None) -> None:
        self._run(
            "UPDATE outbox SET base_rev = ?, base_payload = ?, state = 'pending', last_error = NULL, updated_at = ? "
            "WHERE memory_id = ? AND state IN ('pending', 'conflict')",
            (base_rev, json.dumps(base_payload) if base_payload else None, time.time(), memory_id),
        )

    def pending(self, limit: int | None = None) -> list[dict]:
        sql = "SELECT * FROM outbox WHERE state = 'pending' ORDER BY priority DESC, seq ASC"
        if limit:
            sql += f" LIMIT {int(limit)}"
        return self._all(sql)

    def outbox(self, limit: int = 100) -> list[dict]:
        return self._all(
            "SELECT * FROM outbox WHERE state IN ('pending', 'conflict', 'failed') ORDER BY priority DESC, seq ASC LIMIT ?",
            (limit,))

    def open_op(self, memory_id: str) -> dict | None:
        return self._one(
            "SELECT * FROM outbox WHERE memory_id = ? AND state IN ('pending', 'conflict') ORDER BY seq DESC LIMIT 1",
            (memory_id,))

    def finish(self, seq: int, state: str = "done", error: str | None = None,
               if_updated_at: float | None = None) -> bool:
        """Close an outbox entry. With ``if_updated_at``, only if nothing touched the
        entry since then; a memory edited while its upload was in flight stays queued."""
        sql = "UPDATE outbox SET state = ?, last_error = ?, attempts = attempts + 1, updated_at = ? WHERE seq = ?"
        args: tuple = (state, error, time.time(), seq)
        if if_updated_at is not None:
            sql += " AND updated_at = ?"
            args += (if_updated_at,)
        with self._lock:
            changed = self._db.execute(sql, args).rowcount
        self._maybe_prune()
        return changed > 0

    def fail(self, seq: int, error: str) -> int:
        """Record a failed attempt. Returns how many attempts the entry has had."""
        with self._lock:
            self._run("UPDATE outbox SET attempts = attempts + 1, last_error = ?, updated_at = ? WHERE seq = ?",
                      (error, time.time(), seq))
            row = self._one("SELECT attempts FROM outbox WHERE seq = ?", (seq,))
        return int(row["attempts"]) if row else 0

    def defer(self, seq: int, note: str) -> None:
        """Leave an entry for the next cycle without counting it as a failure (for example a lost compare-and-swap)."""
        self._run("UPDATE outbox SET last_error = ?, updated_at = ? WHERE seq = ?", (note, time.time(), seq))

    def cancel(self, memory_id: str) -> None:
        self._run("UPDATE outbox SET state = 'cancelled', updated_at = ? WHERE memory_id = ? AND state IN ('pending', 'conflict', 'failed')",
                  (time.time(), memory_id))

    def failed(self) -> list[dict]:
        return self._all("SELECT * FROM outbox WHERE state = 'failed' ORDER BY seq ASC")

    def retry_failed(self) -> list[str]:
        """Put parked entries back in the queue. Returns the memory ids affected."""
        with self._lock:
            ids = [row["memory_id"] for row in self.failed()]
            self._run("UPDATE outbox SET state = 'pending', attempts = 0, last_error = NULL, updated_at = ? WHERE state = 'failed'",
                      (time.time(),))
        return ids

    def counts(self) -> dict:
        row = self._one(
            "SELECT SUM(state = 'pending') AS pending, SUM(state = 'conflict') AS conflict, "
            "SUM(state = 'failed') AS failed, SUM(state = 'done') AS done FROM outbox")
        return {k: int(v or 0) for k, v in (row or {}).items()}

    # -- conflicts --------------------------------------------------------

    def add_conflict(self, memory_id: str, local: dict, cloud: dict, base: dict | None, fields: list[str]) -> int:
        with self._lock:
            existing = self._one("SELECT id FROM conflicts WHERE memory_id = ? AND state = 'open'", (memory_id,))
            if existing:
                self._run("UPDATE conflicts SET local_payload = ?, cloud_payload = ?, base_payload = ?, fields = ? WHERE id = ?",
                          (json.dumps(local), json.dumps(cloud), json.dumps(base) if base else None, json.dumps(fields), existing["id"]))
                return existing["id"]
            return self._run(
                "INSERT INTO conflicts(memory_id, local_payload, cloud_payload, base_payload, fields, detected_at) VALUES(?, ?, ?, ?, ?, ?)",
                (memory_id, json.dumps(local), json.dumps(cloud), json.dumps(base) if base else None, json.dumps(fields), time.time()),
            )

    def conflicts(self, state: str | None = "open") -> list[dict]:
        if state:
            return self._all("SELECT * FROM conflicts WHERE state = ? ORDER BY id DESC", (state,))
        return self._all("SELECT * FROM conflicts ORDER BY id DESC LIMIT 50")

    def conflict(self, conflict_id: int) -> dict | None:
        return self._one("SELECT * FROM conflicts WHERE id = ?", (conflict_id,))

    def resolve_conflict(self, conflict_id: int, resolution: str) -> None:
        self._run("UPDATE conflicts SET state = 'resolved', resolution = ?, resolved_at = ? WHERE id = ?",
                  (resolution, time.time(), conflict_id))

    def close_conflicts_for(self, memory_id: str, resolution: str) -> None:
        self._run("UPDATE conflicts SET state = 'resolved', resolution = ?, resolved_at = ? WHERE memory_id = ? AND state = 'open'",
                  (resolution, time.time(), memory_id))

    # -- sync runs --------------------------------------------------------

    def start_run(self, trigger: str) -> int:
        return self._run("INSERT INTO sync_runs(started_at, trigger, status) VALUES(?, ?, 'running')", (time.time(), trigger))

    def finish_run(self, run_id: int, status: str, error: str | None = None, **stats: int) -> None:
        allowed = ("pushed", "pulled", "removed", "merged", "conflicts", "bytes_up", "bytes_down", "cloud_points")
        sets = ", ".join(f"{k} = ?" for k in allowed)
        self._run(f"UPDATE sync_runs SET finished_at = ?, status = ?, error = ?, {sets} WHERE id = ?",
                  (time.time(), status, error, *[int(stats.get(k, 0)) for k in allowed], run_id))
        self._maybe_prune()

    def discard_run(self, run_id: int) -> None:
        self._run("DELETE FROM sync_runs WHERE id = ?", (run_id,))

    def runs(self, limit: int = 20) -> list[dict]:
        return self._all("SELECT * FROM sync_runs ORDER BY id DESC LIMIT ?", (limit,))

    def last_run(self, status: str | None = None) -> dict | None:
        if status:
            return self._one("SELECT * FROM sync_runs WHERE status = ? ORDER BY id DESC LIMIT 1", (status,))
        return self._one("SELECT * FROM sync_runs ORDER BY id DESC LIMIT 1")

    def totals(self) -> dict:
        row = self._one(
            "SELECT COUNT(*) AS runs, SUM(pushed) AS pushed, SUM(pulled) AS pulled, SUM(merged) AS merged, "
            "SUM(conflicts) AS conflicts, SUM(bytes_up) AS bytes_up, SUM(bytes_down) AS bytes_down "
            "FROM sync_runs WHERE status = 'ok'")
        return {k: int(v or 0) for k, v in (row or {}).items()}


JSON_COLUMNS = ("data", "base_payload", "local_payload", "cloud_payload", "fields")


def _row(row: sqlite3.Row) -> dict:
    out = dict(row)
    for key in JSON_COLUMNS:
        if out.get(key):
            out[key] = json.loads(out[key])
    return out
