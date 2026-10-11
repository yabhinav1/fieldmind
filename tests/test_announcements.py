"""A supervisor tells the site something for a while; every device shows it, then drops it."""

import time

from fastapi.testclient import TestClient

from fieldmind.api import create_app

NOTICE = "Hot work permit needed in Bay 3 until 18:00; supervisor on radio channel 2"


def trio(fleet):
    return fleet("edge-s", role="supervisor"), fleet("edge-c"), fleet("edge-t", role="trainee")


def sync_all(*devices):
    for device in devices:
        device.sync.run_once()


def test_an_announcement_reaches_every_device_first_and_without_review(fleet):
    s, c, t = trio(fleet)
    s.service.capture("Conveyor C-3 belt tension adjusted, 12 mm sag")  # an ordinary note queued before it
    result = s.service.announce(NOTICE, hours=4)
    memory = result["memory"]
    assert memory["kind"] == "announcement" and memory["priority"] == 2 and memory["review"] is None
    assert 3.9 * 3600 < memory["expires_at"] - time.time() <= 4 * 3600
    assert [op["memory_id"] for op in s.journal.pending()][0] == memory["id"], "it goes out first"
    sync_all(s, c, t)
    for device in (c, t):
        shown = device.service.announcements()
        assert [a["text"] for a in shown] == [NOTICE], device.settings.device_id
        assert shown[0]["origin_device"] == "edge-s" and shown[0]["expires_at"] == memory["expires_at"]
    assert c.service.search("hot work permit")["results"][0]["id"] == memory["id"]
    assert any(e["type"] == "announce" and "Announced for 4 h" in e["message"] for e in s.journal.events())


def test_an_expired_announcement_is_removed_everywhere(fleet):
    s, c, t = trio(fleet)
    memory_id = s.service.announce(NOTICE, hours=1 / 3600)["memory"]["id"]  # one second
    sync_all(s, c)
    assert [a["id"] for a in c.service.announcements()] == [memory_id]
    time.sleep(1.2)
    assert c.service.announcements() == [], "hidden the moment it expires, before any sweep"
    assert NOTICE not in [m["text"] for m in c.service.list()]
    assert c.service.search("hot work permit")["results"] == []

    c.sync.set_forced_offline(True)
    assert c.sync.run_once()["status"] == "offline"
    assert c.service.replica.get(memory_id) is None, "an offline device forgets its copy on its own"

    sync_all(s)  # the author's device deletes it, so the cloud carries a tombstone
    assert s.service.find(memory_id) is None
    assert s.cloud.get(memory_id)["payload"]["status"] == "deleted"
    sync_all(t)
    assert t.service.replica.get(memory_id) is None and t.service.announcements() == []


def test_a_supervisor_can_take_an_announcement_down_early(fleet):
    s, c, _ = trio(fleet)
    memory_id = s.service.announce(NOTICE, hours=8)["memory"]["id"]
    sync_all(s, c)
    assert c.service.announcements()
    s.service.delete(memory_id)
    sync_all(s, c)
    assert c.service.announcements() == [] and c.service.find(memory_id) is None


def test_only_supervisors_announce_and_the_limits_hold(fleet):
    s, c, _ = trio(fleet)
    try:
        c.service.announce(NOTICE)
        raise AssertionError("a technician announced")
    except PermissionError:
        pass
    for bad in ("", "   "):
        try:
            s.service.announce(bad)
            raise AssertionError("empty announcement accepted")
        except ValueError:
            pass
    try:
        s.service.announce("SCADA panel password is hunter2 for the night shift")
        raise AssertionError("a credential was announced")
    except ValueError:
        pass
    assert s.service.announcements() == [], "nothing slipped through"
    assert s.service.policy.learned_count == 0, "announcing teaches the policy nothing"

    with TestClient(create_app(device=c), client=("127.0.0.1", 50000)) as client:
        assert client.get("/api/announcements").status_code == 401
        client.post("/api/auth/setup", json={"pin": "2468"})
        assert client.get("/api/announcements").json() == {"items": [], "can_announce": False}
        assert client.post("/api/announcements", json={"text": NOTICE}).status_code == 403
    with TestClient(create_app(device=s), client=("127.0.0.1", 50000)) as client:
        client.post("/api/auth/setup", json={"pin": "2468"})
        assert client.post("/api/announcements", json={"text": NOTICE, "hours": 0}).status_code == 400
        assert client.post("/api/announcements", json={"text": NOTICE, "hours": 2}).status_code == 200
        listed = client.get("/api/announcements").json()
        assert listed["can_announce"] and [a["text"] for a in listed["items"]] == [NOTICE]
        assert client.get("/api/status").json()["announcements"] == 1
