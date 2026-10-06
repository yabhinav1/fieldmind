"""An encrypted backup carries what only the device holds to a replacement device."""

import pytest
from fastapi.testclient import TestClient

from fieldmind.api import create_app
from fieldmind.backup import MAGIC, export_bundle, import_bundle, read_bundle

FAULT = "Pump P-102 bearing vibration high at 7.2 mm/s, recommend replacement"
HEALTH = "Technician Ravi Kumar reported chest pain during night shift, sent to clinic"
MASKED = "Motor M-9 overheating, call Operator Anil Sharma on 9876543210 before restarting"
QUEUED = "Valve V-17 leaking hydraulic fluid near the flange, gasket looks worn"


def test_a_backup_restores_private_notes_queued_changes_and_learned_choices(fleet):
    a = fleet("edge-a")
    a.service.capture(FAULT)
    a.service.capture(HEALTH)
    a.service.capture(MASKED)
    a.sync.run_once()  # FAULT and MASKED reach the cloud
    a.sync.set_forced_offline(True)
    a.service.capture(QUEUED)  # still waiting
    a.service.capture("Cooling tower CT-1 fan gearbox oil level low, topped up 2 litres", scope="private")  # learned

    data = export_bundle(a, "correct horse battery")
    assert data.startswith(MAGIC)
    assert b"chest pain" not in data and b"Ravi" not in data, "the file is sealed"
    with pytest.raises(ValueError, match="passphrase"):
        read_bundle(data, "wrong passphrase!!")
    with pytest.raises(ValueError, match="at least 8"):
        export_bundle(a, "short")

    c = fleet("edge-c")  # the replacement device, empty
    stats = import_bundle(c, data, "correct horse battery")
    assert stats["memories"] == 5 and stats["queued"] == 1 and stats["learned"] == 1 and stats["from_device"] == "edge-a"
    assert c.service.search("chest pain")["results"][0]["text"] == HEALTH, "private notes came across"
    got = c.service.search("motor overheating")["results"][0]
    assert "9876543210" in got["text"] and "[phone removed]" in got["shared_text"], "the unmasked original and the masked copy"
    assert c.journal.counts()["pending"] == 1, "the change that was waiting is queued again"
    assert c.sync.run_once()["pushed"] == 1
    assert c.service.preview("Cooling tower CT-1 gearbox oil low again, topped up 1.5 litres")["decision"]["decided_by"] == "learned"

    again = import_bundle(c, data, "correct horse battery")
    assert again["memories"] == 0 and again["skipped"] == 5, "a second restore changes nothing"


def test_backup_and_restore_over_http(fleet):
    a = fleet("edge-a")
    a.service.capture(HEALTH)
    with TestClient(create_app(device=a), client=("127.0.0.1", 50000)) as client:
        client.post("/api/auth/setup", json={"pin": "2468"})
        assert client.post("/api/backup", json={"passphrase": "short"}).status_code == 400
        response = client.post("/api/backup", json={"passphrase": "correct horse battery"})
        assert response.status_code == 200 and response.headers["content-disposition"].endswith('.fmbackup"')
        data = response.content

    c = fleet("edge-c")
    with TestClient(create_app(device=c), client=("127.0.0.1", 50000)) as client:
        client.post("/api/auth/setup", json={"pin": "1357"})
        bad = client.post("/api/restore", files={"file": ("x.fmbackup", data)}, data={"passphrase": "nope nope nope"})
        assert bad.status_code == 400
        good = client.post("/api/restore", files={"file": ("x.fmbackup", data)}, data={"passphrase": "correct horse battery"})
        assert good.status_code == 200 and good.json()["memories"] == 1
        assert client.get("/api/memories?scope=private").json()["total"] == 1
