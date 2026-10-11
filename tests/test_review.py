"""Who may share without asking: a trainee's notes wait for a supervisor."""

import uuid

from fastapi.testclient import TestClient
from qdrant_client import QdrantClient

from fieldmind.api import create_app
from fieldmind.config import Settings
from fieldmind.review import TRUST_AFTER
from fieldmind.runtime import build

FAULT = "Pump P-102 bearing vibration high at 7.2 mm/s, recommend replacement"
GAS = "Smell of gas near compressor K-4, isolated the line and cleared the bay"
MASKED = "Motor M-9 overheating, call Operator Anil Sharma on 9876543210 before restarting"


def texts(device):
    return [m["text"] for m in device.service.list()]


def trio(fleet):
    """A trainee, a supervisor and an ordinary technician on one site."""
    return fleet("edge-t", role="trainee"), fleet("edge-s", role="supervisor"), fleet("edge-c")


def sync_all(*devices):
    for device in devices:
        device.sync.run_once()


def test_a_trainee_note_waits_for_a_supervisor_and_approval_works_offline(fleet):
    t, s, c = trio(fleet)
    memory = t.service.capture(FAULT)["memory"]
    assert memory["review"] == "pending" and memory["role"] == "trainee"
    sync_all(t, c, s)
    assert FAULT not in texts(c), "an equal does not see it until a supervisor agrees"
    assert FAULT not in [r["text"] for r in c.service.search("pump vibration")["results"]]
    assert [p["id"] for p in s.service.approvals()] == [memory["id"]], "the supervisor has it in the queue"
    assert FAULT in texts(s), "and can search it like any note"
    assert c.service.stats()["approvals"] == 0 and s.service.stats()["approvals"] == 1

    s.sync.set_forced_offline(True)
    assert s.service.review(memory["id"], "approved")["queued"] is True
    assert s.service.approvals() == [] and s.journal.counts()["pending"] == 1
    s.sync.set_forced_offline(False)
    sync_all(s, c, t)
    assert FAULT in texts(c), "approved: now the whole site has it"
    mine = t.service.get(memory["id"])
    assert mine["review"] == "approved" and mine["reviewed_by"] == "edge-s" and mine["sync_state"] == "synced"
    assert t.service.approval_streak == 1
    assert any(e["type"] == "review" and "Approved by edge-s" in e["message"] for e in t.journal.events())


def test_a_rejected_note_comes_back_private_with_the_reason_and_can_be_resubmitted(fleet):
    t, s, c = trio(fleet)
    memory_id = t.service.capture(MASKED)["memory"]["id"]
    sync_all(t, s)
    s.service.review(memory_id, "rejected", "Wrong motor, M-9 was replaced last week")
    sync_all(s, c, t)
    assert MASKED not in texts(c)
    mine = t.service.get(memory_id)
    assert mine["scope"] == "private" and mine["sync_state"] == "private"
    assert mine["review"] == "rejected" and mine["review_reason"] == "Wrong motor, M-9 was replaced last week"
    assert mine["text"] == MASKED, "the author keeps the full original"
    assert t.service.approval_streak == 0
    stored = t.cloud.get(memory_id)["payload"]
    assert stored["status"] == "deleted" and stored["review"] == "rejected" and "Anil" not in stored.get("text", "")

    t.service.edit(memory_id, text="Motor M-9 overheating, call Operator Anil Sharma on 9876543210; checked nameplate", scope="shared")
    again = t.service.get(memory_id)
    assert again["review"] == "pending" and again["review_reason"] is None and again["scope"] == "redacted"
    sync_all(t, s, c)
    assert [p["id"] for p in s.service.approvals()] == [memory_id], "back in the queue with the new text"
    assert "checked nameplate" not in " ".join(texts(c))


def test_safety_hazards_reach_everyone_at_once_and_are_reviewed_afterwards(fleet):
    t, s, c = trio(fleet)
    memory = t.service.capture(GAS)["memory"]
    assert memory["priority"] == 2 and memory["review"] == "pending"
    sync_all(t, c, s)
    assert GAS in texts(c), "a hazard is not held back"
    assert [p["id"] for p in s.service.approvals()] == [memory["id"]]


def test_technicians_and_supervisors_share_without_review(fleet):
    t, s, c = trio(fleet)
    assert c.service.capture(FAULT)["memory"]["review"] is None
    assert s.service.capture(GAS)["memory"]["review"] is None
    sync_all(c, s, t)
    assert FAULT in texts(t) and GAS in texts(t) and FAULT in texts(s)
    assert s.service.approvals() == []


def test_trust_is_earned_after_a_run_of_approvals_and_lost_on_a_rejection(fleet):
    t, s, c = trio(fleet)
    for n in range(TRUST_AFTER):
        memory_id = t.service.capture(f"Conveyor C-{n + 1} belt tension adjusted, {n + 10} mm sag", link=False)["memory"]["id"]
        sync_all(t, s)
        s.service.review(memory_id, "approved")
        sync_all(s, t)
        assert t.service.approval_streak == n + 1
    assert t.service.trusted and not t.service.needs_review()
    assert any("now trusted" in e["message"] for e in t.journal.events())

    memory = t.service.capture(FAULT)["memory"]
    assert memory["review"] == "approved" and memory["reviewed_by"] == "trusted"
    sync_all(t, c, s)
    assert FAULT in texts(c) and s.service.approvals() == []

    fresh = fleet("edge-u", role="trainee")
    memory_id = fresh.service.capture(MASKED)["memory"]["id"]
    sync_all(fresh, s)
    s.service.review(memory_id, "approved")
    sync_all(s, fresh)
    assert fresh.service.approval_streak == 1
    memory_id = fresh.service.capture(GAS)["memory"]["id"]
    sync_all(fresh, s)
    s.service.review(memory_id, "rejected", "Already cleared by the night shift")
    sync_all(s, fresh)
    assert fresh.service.approval_streak == 0 and not fresh.service.trusted


def test_an_edit_after_approval_goes_back_for_review(fleet):
    t, s, c = trio(fleet)
    memory_id = t.service.capture(FAULT)["memory"]["id"]
    sync_all(t, s)
    s.service.review(memory_id, "approved")
    sync_all(s, c, t)
    assert FAULT in texts(c)

    t.service.edit(memory_id, text=FAULT + ", drive end")
    assert t.service.get(memory_id)["review"] == "pending"
    t.service.edit(memory_id, tags=["night"])
    assert t.service.get(memory_id)["review"] == "pending", "a tag change keeps the pending state"
    sync_all(t, c, s)
    assert FAULT not in " ".join(texts(c)), "withdrawn from equals until approved again"
    assert [p["text"] for p in s.service.approvals()] == [FAULT + ", drive end"]


def test_a_decision_on_a_note_the_author_changed_meanwhile_is_dropped(fleet):
    t, s, c = trio(fleet)
    memory_id = t.service.capture(FAULT)["memory"]["id"]
    sync_all(t, s)
    s.sync.set_forced_offline(True)
    s.service.review(memory_id, "approved")
    t.service.edit(memory_id, text=FAULT + ", confirmed with a second meter")
    sync_all(t)
    s.sync.set_forced_offline(False)
    sync_all(s, c)
    assert FAULT not in " ".join(texts(c)), "the stale approval did not release the new text"
    assert [p["text"] for p in s.service.approvals()] == [FAULT + ", confirmed with a second meter"]
    assert s.journal.counts()["pending"] == 0


def test_only_a_supervisor_can_decide_and_a_rejection_needs_a_reason(fleet):
    t, s, c = trio(fleet)
    memory_id = t.service.capture(FAULT)["memory"]["id"]
    sync_all(t, s)
    with TestClient(create_app(device=c), client=("127.0.0.1", 50000)) as client:
        assert client.get("/api/approvals").status_code == 401
        client.post("/api/auth/setup", json={"pin": "2468"})
        assert client.get("/api/approvals").json() == {"role": "technician", "items": []}
        assert client.post(f"/api/approvals/{memory_id}", json={"decision": "approved"}).status_code == 403
    with TestClient(create_app(device=s), client=("127.0.0.1", 50000)) as client:
        client.post("/api/auth/setup", json={"pin": "2468"})
        listed = client.get("/api/approvals").json()
        assert listed["role"] == "supervisor" and [m["id"] for m in listed["items"]] == [memory_id]
        assert client.get("/api/status").json()["open_approvals"] == 1
        assert client.post(f"/api/approvals/{memory_id}", json={"decision": "rejected"}).status_code == 400
        assert client.post(f"/api/approvals/{memory_id}", json={"decision": "maybe"}).status_code == 400
        assert client.post(f"/api/approvals/{uuid.uuid4()}", json={"decision": "approved"}).status_code == 404
        assert client.post(f"/api/approvals/{memory_id}", json={"decision": "approved"}).json()["status"] == "approved"
        assert client.get("/api/sync").json()["approvals"] == []


def test_peers_hand_pending_notes_only_to_supervisors(tmp_path, embedder, names, reranker):
    devices = {}

    def fetch(peer, path, body):
        target = devices[peer]
        return target.peers.manifest() if path == "/api/peer/manifest" else target.peers.records(body["ids"])

    for name, role in (("edge-t", "trainee"), ("edge-c", "technician"), ("edge-s", "supervisor")):
        settings = Settings(device_id=name, site="plant-1", data_root=tmp_path, sync_batch=100, ollama_model="", role=role,
                            peers=[f"peer://{other}" for other in ("edge-t", "edge-c", "edge-s") if other != name],
                            peer_token="fleet-test")
        devices[f"peer://{name}"] = build(settings, embedder=embedder, cloud_client=QdrantClient(":memory:"),
                                          names=names, reranker=reranker, peer_fetch=fetch)
    t, c, s = devices["peer://edge-t"], devices["peer://edge-c"], devices["peer://edge-s"]
    try:
        for device in (t, c, s):
            device.cloud.reachable = lambda: False
        t.service.capture(FAULT)
        t.service.capture(GAS)
        c.peers.exchange()
        s.peers.exchange()
        assert FAULT not in texts(c) and GAS in texts(c)
        assert FAULT in texts(s) and GAS in texts(s)
    finally:
        for device in (t, c, s):
            device.close()
