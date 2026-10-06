"""Encrypted device backup and restore.

A device holds things that exist nowhere else: its private notes, the unmasked
originals of masked notes, its photos and what its policy learned. A backup is
one file holding all of that, sealed with a passphrase the person chooses (not
the device key, which stays on the device). Restoring it on a replacement device
re-embeds every note, so the file carries text and payloads, never vectors.

Shared notes already in the cloud are restored as synced; notes that were still
waiting to upload are queued again. The replacement device keeps its own PIN.
"""

from __future__ import annotations

import base64
import hashlib
import json
import time
import zlib

MAGIC = b"FMB1"
ITERATIONS = 390_000
MIN_PASSPHRASE = 8


def _key(passphrase: str, salt: bytes) -> bytes:
    raw = hashlib.pbkdf2_hmac("sha256", passphrase.encode(), salt, ITERATIONS)
    return base64.urlsafe_b64encode(raw)


def export_bundle(device, passphrase: str) -> bytes:
    """Everything this device alone holds, as an encrypted file."""
    import os

    from cryptography.fernet import Fernet

    if len(passphrase or "") < MIN_PASSPHRASE:
        raise ValueError(f"Use a passphrase of at least {MIN_PASSPHRASE} characters.")
    service = device.service
    memories = [{"id": r["id"], "payload": r["payload"]} for r in service.local.scroll()]
    photos = []
    if device.photos:
        for record in device.photos.shard.scroll():
            data = device.photos.read(record["id"])
            if data is not None:
                photos.append({"id": record["id"], "payload": record["payload"], "image": base64.b64encode(data).decode()})
    bundle = {
        "version": 1,
        "device": device.settings.device_id,
        "site": device.settings.site,
        "exported_at": time.time(),
        "memories": memories,
        "photos": photos,
        "policy_examples": device.journal.get("policy_examples", []),
        "outbox": [op for op in device.journal.outbox(10_000) if op["state"] in ("pending", "failed")],
    }
    salt = os.urandom(16)
    body = zlib.compress(json.dumps(bundle, default=str).encode(), 9)
    return MAGIC + salt + Fernet(_key(passphrase, salt)).encrypt(body)


def read_bundle(data: bytes, passphrase: str) -> dict:
    from cryptography.fernet import Fernet, InvalidToken

    if not data.startswith(MAGIC) or len(data) < 21:
        raise ValueError("That is not a FieldMind backup file.")
    salt, token = data[4:20], data[20:]
    try:
        body = Fernet(_key(passphrase, salt)).decrypt(token)
    except InvalidToken:
        raise ValueError("Wrong passphrase, or the file is damaged.") from None
    return json.loads(zlib.decompress(body))


def import_bundle(device, data: bytes, passphrase: str) -> dict:
    """Bring a backup's content into this device. Memories already present are left alone."""
    bundle = read_bundle(data, passphrase)
    service, journal = device.service, device.journal
    stats = {"memories": 0, "skipped": 0, "photos": 0, "queued": 0, "learned": 0, "from_device": bundle.get("device")}
    queued_ops = {op["memory_id"]: op for op in bundle.get("outbox", [])}

    with service.lock:
        for item in bundle.get("memories", []):
            memory_id, payload = item["id"], dict(item["payload"])
            if service.local.get(memory_id):
                stats["skipped"] += 1
                continue
            dense, sparse = service.embedder.document(payload.get("text", ""))
            service.local.upsert(memory_id, dense, sparse, payload)
            stats["memories"] += 1
            if payload.get("scope") != "private" and payload.get("sync_state") in ("pending", "failed"):
                op = queued_ops.get(memory_id)
                payload["sync_state"] = "pending"
                service.local.set_payload(memory_id, payload)
                journal.enqueue(memory_id, (op or {}).get("op", "upsert"), payload.get("priority", 1),
                                (op or {}).get("base_rev", payload.get("base_rev", 0)), (op or {}).get("base_payload"))
                stats["queued"] += 1
        service.local.flush()

        if device.photos:
            for item in bundle.get("photos", []):
                if device.photos.get(item["id"]):
                    continue
                stored = device.photos.store_remote({"id": item["id"], "payload": {**item["payload"], "image": item["image"]},
                                                     "vectors": {}})
                if stored:
                    device.photos.set_state(item["id"], sync_state=item["payload"].get("sync_state", "private"),
                                            scope=item["payload"].get("scope", "private"))
                    stats["photos"] += 1
                    if item["payload"].get("sync_state") == "pending":
                        journal.enqueue(item["id"], "photo", 1, 0, {"memory_id": item["payload"].get("memory_id")})
            device.photos.shard.flush()

        examples = bundle.get("policy_examples") or []
        if examples:
            known = journal.get("policy_examples", [])
            merged = known + [e for e in examples if e not in known]
            journal.set("policy_examples", merged[-200:])
            service.policy._learned = list(merged[-200:])
            service.policy._learned_matrix = service.policy._matrix()
            stats["learned"] = len(merged) - len(known)

    journal.log("system", f"Restored {stats['memories']} memories and {stats['photos']} photos from a backup of "
                          f"{bundle.get('device')}; {stats['queued']} queued for the cloud.", level="good", **{k: v for k, v in stats.items() if k != "from_device"})
    return stats
