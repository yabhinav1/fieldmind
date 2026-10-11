"""Every revision of a note is kept on the device, with who changed it, and can be brought back."""

from fastapi.testclient import TestClient

from fieldmind.api import create_app

FAULT = "Pump P-102 bearing vibration high at 7.2 mm/s, recommend replacement"


def test_revisions_record_who_changed_what_and_an_old_version_can_be_restored(fleet):
    a, b = fleet("edge-a"), fleet("edge-b")
    memory_id = a.service.capture(FAULT)["memory"]["id"]
    a.service.edit(memory_id, text=FAULT + ", drive end", tags=["night"])
    a.sync.run_once()
    b.sync.run_once()
    b.service.edit(memory_id, text=FAULT + ", drive end, bearing ordered")
    b.sync.run_once()
    a.sync.run_once()

    revisions = a.service.revisions(memory_id)
    assert [r["rev"] for r in revisions] == [3, 2, 1]
    assert revisions[0]["device_id"] == "edge-b" and revisions[0]["note"] == "Changed on edge-b"
    assert revisions[1]["text"] == FAULT + ", drive end" and revisions[1]["tags"] == ["night"]
    assert revisions[2]["text"] == FAULT and revisions[2]["note"] == "Written here" and revisions[2]["device_id"] == "edge-a"
    assert a.journal._one("SELECT snapshot FROM revisions WHERE memory_id = ? LIMIT 1", (memory_id,))["snapshot"].startswith("enc1:"), \
        "revision text is sealed on disk like the activity log"

    restored = a.service.restore(memory_id, 1)
    assert restored["text"] == FAULT and restored["rev"] == 4 and restored["sync_state"] == "pending"
    assert a.service.revisions(memory_id)[0]["note"] == "Restored revision 1."
    a.sync.run_once()
    b.sync.run_once()
    assert b.service.get(memory_id)["text"] == FAULT, "a restore is an ordinary change and syncs like one"
    assert len(b.service.revisions(memory_id)) == 2, "edge-b records the versions it held as its own: its edit and the restore"

    a.service.delete(memory_id)
    assert a.journal.revisions(memory_id) == []


def test_revisions_over_http(fleet):
    device = fleet("edge-a")
    memory_id = device.service.capture(FAULT)["memory"]["id"]
    device.service.edit(memory_id, text=FAULT + " today")
    with TestClient(create_app(device=device), client=("127.0.0.1", 50000)) as client:
        assert client.get(f"/api/memories/{memory_id}/revisions").status_code == 401
        client.post("/api/auth/setup", json={"pin": "2468"})
        listed = client.get(f"/api/memories/{memory_id}/revisions").json()["revisions"]
        assert [r["rev"] for r in listed] == [2, 1]
        assert client.post(f"/api/memories/{memory_id}/revisions/1/restore").json()["text"] == FAULT
        assert client.post(f"/api/memories/{memory_id}/revisions/9/restore").status_code == 404
