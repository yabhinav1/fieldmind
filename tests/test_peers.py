"""Devices on the same network learn from each other when the cloud is out of reach."""

from fastapi.testclient import TestClient
from qdrant_client import QdrantClient

from fieldmind.api import create_app
from fieldmind.config import Settings
from fieldmind.demo import seed_cloud
from fieldmind.peers import HEADER
from fieldmind.runtime import build

FAULT = "Pump P-102 bearing vibration high at 7.2 mm/s, recommend replacement"
HEALTH = "Technician Ravi Kumar reported chest pain during night shift, sent to clinic"
MASKED = "Motor M-9 overheating, call Operator Anil Sharma on 9876543210 before restarting"


def pair(tmp_path, embedder, names, reranker, cloud_client=None):
    """Two devices that can reach each other in-process, sharing one (possibly absent) cloud."""
    devices = {}

    def fetch(peer, path, body):
        target = devices[peer]
        if path == "/api/peer/manifest":
            return target.peers.manifest()
        return target.peers.records(body["ids"])

    for name, other in (("edge-a", "edge-b"), ("edge-b", "edge-a")):
        settings = Settings(device_id=name, site="plant-1", data_root=tmp_path, sync_batch=100, ollama_model="",
                            peers=[f"peer://{other}"], peer_token="fleet-test")
        devices[f"peer://{name}"] = build(settings, embedder=embedder, cloud_client=cloud_client or QdrantClient(":memory:"),
                                          names=names, reranker=reranker, peer_fetch=fetch)
    return devices["peer://edge-a"], devices["peer://edge-b"]


def test_shareable_notes_cross_the_room_without_a_cloud(tmp_path, embedder, names, reranker):
    a, b = pair(tmp_path, embedder, names, reranker)
    a.cloud.reachable = lambda: False
    b.cloud.reachable = lambda: False
    a.service.capture(FAULT)
    a.service.capture(HEALTH)
    a.service.capture(MASKED)

    result = b.sync.run_once()
    assert result["status"] == "offline" and result["peers"]["peer://edge-a"]["received"] == 2
    texts = [m["text"] for m in b.service.list(source="replica")]
    assert any("7.2 mm/s" in t for t in texts) and not any("chest pain" in t for t in texts)
    shared = next(t for t in texts if "Motor M-9" in t)
    assert "Anil" not in shared and "9876543210" not in shared, "masking is the same as for the cloud"
    assert b.service.search("pump bearing vibration")["results"][0]["via_peer"] == "edge-a"
    assert b.sync.run_once()["peers"]["peer://edge-a"]["received"] == 0, "nothing new travels twice"
    assert a.service.get(a.service.list(source="local")[0]["id"])["sync_state"] == "pending", "the outbox still waits for the cloud"

    for device in (a, b):
        device.close()


def test_cloud_knowledge_is_relayed_and_the_cloud_settles_it_later(tmp_path, embedder, names, reranker):
    cloud = QdrantClient(":memory:")
    a, b = pair(tmp_path, embedder, names, reranker, cloud_client=cloud)
    seed_cloud(a.cloud, embedder)
    assert a.sync.run_once()["pulled"] == 8  # a has the manuals, b never saw the cloud
    memory_id = a.service.capture(FAULT)["memory"]["id"]

    b.cloud.reachable = lambda: False
    assert b.sync.run_once()["peers"]["peer://edge-a"]["received"] == 9
    assert b.service.stats()["replica"] == 9

    # The cloud comes back for b before a has uploaded its note: the peer-learned
    # note is kept, the manuals are now the cloud's, and a's upload reconciles.
    b.cloud.reachable = lambda: True
    result = b.sync.run_once()
    assert result["status"] == "ok" and result["removed"] == 0
    assert b.service.find(memory_id) is not None
    a.sync.run_once()
    b.sync.run_once()
    assert b.service.get(memory_id)["via_peer"] is None, "once the cloud holds it, it is the cloud's copy"

    for device in (a, b):
        device.close()


def test_peer_routes_need_the_fleet_token_not_the_pin(fleet):
    device = fleet("edge-a")
    device.settings.peer_token = "fleet-test"
    device.service.capture(FAULT)
    with TestClient(create_app(device=device), client=("192.168.1.50", 50000)) as client:
        assert client.get("/api/peer/manifest").status_code == 403
        assert client.get("/api/peer/manifest", headers={HEADER: "wrong"}).status_code == 403
        manifest = client.get("/api/peer/manifest", headers={HEADER: "fleet-test"}).json()
        assert manifest["device"] == "edge-a" and len(manifest["items"]) == 1
        records = client.post("/api/peer/memories", json={"ids": list(manifest["items"])}, headers={HEADER: "fleet-test"}).json()
        assert records[0]["payload"]["text"] == FAULT and len(records[0]["dense"]) == 384
        assert client.get("/api/status", headers={HEADER: "fleet-test"}).status_code == 401, "the token opens nothing else"


def test_peer_routes_are_off_without_a_token(fleet):
    with TestClient(create_app(device=fleet("edge-a")), client=("127.0.0.1", 50000)) as client:
        assert client.get("/api/peer/manifest").status_code == 403
