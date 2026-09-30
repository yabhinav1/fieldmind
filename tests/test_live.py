"""Behaviour that needs a real Qdrant Server. Skipped when none is running on localhost:6333."""

import time
import uuid

import httpx
import pytest

from fieldmind.cloud import Cloud
from fieldmind.config import Settings
from fieldmind.demo import seed_cloud
from fieldmind.runtime import build

URL = "http://localhost:6333"


def server_running() -> bool:
    try:
        return httpx.get(URL, timeout=1.5).status_code == 200
    except httpx.HTTPError:
        return False


pytestmark = pytest.mark.skipif(not server_running(), reason="Qdrant Server is not running")


@pytest.fixture
def live(tmp_path, embedder, names):
    import shutil

    collection = f"test_{uuid.uuid4().hex[:10]}"
    devices = []

    def make(name: str, **overrides):
        settings = Settings(device_id=name, site="plant-1", data_root=tmp_path, cloud_url=URL,
                            collection=collection, sync_batch=100, **overrides)
        device = build(settings, embedder=embedder, names=names)
        devices.append(device)
        return device

    yield make
    for device in devices:
        device.close()
    Cloud(URL, None, collection).client.delete_collection(collection)
    shutil.rmtree(tmp_path, ignore_errors=True)


def test_far_behind_device_restores_replica_from_a_snapshot(live, embedder):
    a = live("edge-a")
    seed_cloud(a.cloud, embedder)
    shared = a.service.capture("Pump P-102 bearing vibration high at 7.2 mm/s, recommend replacement")["memory"]
    gone = a.service.capture("Conveyor C-3 belt tracking drifting left, adjusted the tail pulley")["memory"]
    a.sync.run_once()
    a.service.delete(gone["id"])
    a.sync.run_once()  # leaves a tombstone in the cloud
    dense, sparse = embedder.document("Chiller CH-1 refrigerant low at the other plant")
    a.cloud.write(str(uuid.uuid4()), dense, sparse,
                  {"text": "Chiller CH-1 refrigerant low at the other plant", "site": "plant-9", "rev": 1,
                   "status": "active", "device_id": "edge-z", "updated_at": time.time()}, None)

    b = live("edge-b", snapshot_min_points=1)
    result = b.sync.run_once()
    assert result["status"] == "ok" and result["pulled"] == 9  # 8 manuals and the pump note
    assert any("cloud snapshot" in e["message"] for e in b.journal.events())

    texts = [m["text"] for m in b.service.list(source="replica")]
    assert len(texts) == 9 and all(texts)
    assert not any("CH-1" in t or "C-3 belt" in t for t in texts)  # other site and tombstone trimmed

    b.sync.set_forced_offline(True)
    top = b.service.search("what vibration level is unacceptable")["results"][0]
    assert top["source"] == "replica" and "7.1 mm/s" in top["text"]
    assert b.service.search("P-102 bearing")["results"][0]["id"] == shared["id"]

    # The restored replica keeps working with ordinary incremental sync and edits.
    b.sync.set_forced_offline(False)
    assert b.sync.run_once()["pulled"] == 0
    b.service.edit(shared["id"], tags=["bearing"])
    assert b.sync.run_once()["pushed"] == 1
    a.sync.run_once()
    assert a.service.get(shared["id"])["tags"] == ["bearing"]


def test_rebuild_uses_a_snapshot_and_leaves_private_notes_alone(live, embedder):
    a = live("edge-a")
    seed_cloud(a.cloud, embedder)
    a.service.capture("Remind me to call home after shift and pick up my keys")
    a.service.capture("Pump P-102 bearing vibration high at 7.2 mm/s, recommend replacement")
    a.sync.run_once()

    result = a.sync.rebuild_replica()
    assert result["method"] == "snapshot" and result["pulled"] == 8
    stats = a.service.stats()
    assert (stats["replica"], stats["local"], stats["private"]) == (8, 2, 1)


def test_real_outage_is_detected_without_the_switch(live):
    a = live("edge-a")
    assert a.sync.check_link() is True
    a.cloud.url = "http://localhost:6399"  # nothing listens here
    assert a.sync.check_link() is False
    a.service.capture("Valve V-17 leaking hydraulic fluid near the flange, gasket looks worn")
    assert a.sync.run_once()["status"] == "offline"
    assert a.journal.counts()["pending"] == 1
