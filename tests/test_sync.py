"""End-to-end behaviour of two devices and one cloud."""

import time
import uuid

from fieldmind.demo import seed_cloud

FAULT = "Pump P-102 bearing vibration high at 7.2 mm/s, recommend replacement"
HEALTH = "Technician Ravi Kumar reported chest pain during night shift, sent to clinic"
MASKED = "Motor M-9 overheating, call Operator Anil Sharma on 9876543210 before restarting"


def cloud_texts(device):
    device.cloud.ensure()
    return [r["payload"].get("text") for r in device.cloud.browse() if r["payload"].get("status") != "deleted"]


def test_works_fully_offline(fleet):
    a = fleet("edge-a")
    a.sync.set_forced_offline(True)
    memory = a.service.capture(FAULT)["memory"]
    assert memory["sync_state"] == "pending"

    found = a.service.search("which pump is shaking")
    assert found["results"][0]["id"] == memory["id"]
    assert found["network_calls"] == 0
    assert a.sync.run_once()["status"] == "offline"
    assert a.journal.counts()["pending"] == 1


def test_queued_changes_survive_restart(fleet, tmp_path, embedder):
    a = fleet("edge-a")
    a.sync.set_forced_offline(True)
    memory_id = a.service.capture(FAULT)["memory"]["id"]
    a.close()

    again = fleet("edge-a")
    assert again.service.get(memory_id)["text"] == FAULT
    assert [op["memory_id"] for op in again.journal.pending()] == [memory_id]
    assert again.sync.forced_offline is True


def test_shared_memory_reaches_other_devices(fleet):
    a, b = fleet("edge-a"), fleet("edge-b")
    memory_id = a.service.capture(FAULT)["memory"]["id"]
    assert a.sync.run_once()["pushed"] == 1
    assert a.service.get(memory_id)["sync_state"] == "synced"

    assert b.sync.run_once()["pulled"] == 1
    got = b.service.get(memory_id)
    assert got["source"] == "replica" and got["origin_device"] == "edge-a"
    assert b.service.search("bearing vibration")["results"][0]["id"] == memory_id
    # Nothing changed, so a second pull transfers nothing.
    assert b.sync.run_once()["pulled"] == 0


def test_private_memory_never_reaches_the_cloud(fleet):
    a = fleet("edge-a")
    a.service.capture(HEALTH)
    a.service.capture("SCADA panel login password is Plant@2026")
    a.service.capture(FAULT)
    a.sync.run_once()
    assert cloud_texts(a) == [FAULT]
    assert a.service.search("chest pain")["results"][0]["scope"] == "private"


def test_masked_copy_goes_up_original_stays(fleet):
    a, b = fleet("edge-a"), fleet("edge-b")
    memory_id = a.service.capture(MASKED)["memory"]["id"]
    a.sync.run_once()
    b.sync.run_once()

    assert "9876543210" in a.service.get(memory_id)["text"]
    shared = b.service.get(memory_id)["text"]
    assert "9876543210" not in shared and "Anil" not in shared
    assert "Motor M-9 overheating" in shared


def test_urgent_memories_are_sent_first(fleet):
    a = fleet("edge-a")
    a.settings.sync_batch = 1
    a.service.capture("Daily round done, boiler B-2 pressure 6.1 bar, normal")
    a.service.capture("Exposed live cable found behind panel MCC-2, area barricaded")
    a.sync.run_once()
    assert "Exposed live cable" in cloud_texts(a)[0]
    assert a.journal.counts()["pending"] == 1


def test_follow_up_replaces_earlier_belief(fleet):
    a = fleet("edge-a")
    first = a.service.capture(FAULT)["memory"]["id"]
    second = a.service.capture("Pump P-102 bearing replaced, vibration now 1.8 mm/s, normal")["memory"]
    assert second["supersedes"] == first and second["relation"] == "resolves"
    assert a.service.get(first)["status"] == "superseded"

    ids = [r["id"] for r in a.service.search("P-102 bearing vibration")["results"]]
    assert second["id"] in ids and first not in ids
    assert [h["id"] for h in a.service.history(second["id"])] == [second["id"], first]


def test_the_person_can_keep_the_earlier_note_current(fleet):
    a = fleet("edge-a")
    first = a.service.capture(FAULT)["memory"]["id"]
    preview = a.service.preview("Pump P-102 bearing replaced, vibration now 1.8 mm/s, normal")
    assert preview["related"][0]["relation"] == "resolves", "the preview shows what would be replaced"
    second = a.service.capture("Pump P-102 bearing replaced, vibration now 1.8 mm/s, normal", supersede=False)["memory"]
    assert second["supersedes"] is None
    assert a.service.get(first)["status"] == "active"


def test_unrelated_issue_on_same_asset_is_kept(fleet):
    a = fleet("edge-a")
    first = a.service.capture(FAULT)["memory"]["id"]
    a.service.capture("Pump P-102 mechanical seal leaking water at the shaft")
    assert a.service.get(first)["status"] == "active"


def test_duplicates_are_not_stored_twice(fleet):
    a = fleet("edge-a")
    first = a.service.capture(FAULT)["memory"]["id"]
    again = a.service.capture("Pump P-102 bearing vibration high at 7.2 mm/s, recommend replacing")
    assert again["created"] is False and again["duplicate_of"]["id"] == first
    assert a.service.stats()["local"] == 1


def test_an_edit_that_keeps_the_text_sends_no_vectors(fleet):
    a, b = fleet("edge-a"), fleet("edge-b")
    memory_id = a.service.capture(FAULT)["memory"]["id"]
    full = a.sync.run_once()["bytes_up"]
    b.sync.run_once()

    a.service.edit(memory_id, tags=["bearing", "p-series"])
    patched = a.sync.run_once()
    assert patched["pushed"] == 1 and patched["bytes_up"] < 4 * 384 < full, "the payload went up without the vectors"
    assert a.service.get(memory_id)["sync_state"] == "synced"

    b.sync.run_once()
    got = b.service.get(memory_id)
    assert got["tags"] == ["bearing", "p-series"] and got["text"] == FAULT
    assert b.service.search("pump bearing vibration")["results"][0]["id"] == memory_id, "the cloud vectors still match"


def test_memories_from_a_newer_fieldmind_are_held_back(fleet, embedder):
    a = fleet("edge-a")
    a.cloud.ensure()
    now = time.time()
    for text, version in (("Chiller CH-1 refrigerant low", 99), ("Legacy note about valve V-17", None)):
        dense, sparse = embedder.document(text)
        payload = {"text": text, "site": "plant-1", "rev": 1, "status": "active", "device_id": "edge-z", "updated_at": now}
        if version:
            payload["schema"] = version
        a.cloud.write(str(uuid.uuid4()), dense, sparse, payload, None)

    assert a.sync.run_once()["pulled"] == 1
    texts = [m["text"] for m in a.service.list(source="replica")]
    assert texts == ["Legacy note about valve V-17"]
    assert a.service.list(source="replica")[0]["schema"] == 1, "an old payload is read at the current version"
    assert any("newer FieldMind" in e["message"] for e in a.journal.events())


def test_edits_to_different_fields_merge_automatically(fleet):
    a, b = fleet("edge-a"), fleet("edge-b")
    memory_id = a.service.capture(FAULT)["memory"]["id"]
    a.sync.run_once()
    b.sync.run_once()

    a.service.edit(memory_id, tags=["bearing"])
    b.service.edit(memory_id, text="Pump P-102 bearing vibration high at 7.9 mm/s, stop the pump")
    a.sync.run_once()
    result = b.sync.run_once()
    assert result["merged"] == 1 and result["conflicts"] == 0

    a.sync.run_once()
    for device in (a, b):
        memory = device.service.get(memory_id)
        assert memory["tags"] == ["bearing"] and "7.9 mm/s" in memory["text"]
        assert memory["sync_state"] == "synced"


def clash(fleet):
    a, b = fleet("edge-a"), fleet("edge-b")
    memory_id = a.service.capture(FAULT)["memory"]["id"]
    a.sync.run_once()
    b.sync.run_once()

    a.service.edit(memory_id, text="Pump P-102 bearing vibration 7.2 mm/s, replace at next shutdown")
    b.service.edit(memory_id, text="Pump P-102 bearing vibration 7.2 mm/s, stop the pump immediately")
    a.sync.run_once()
    assert b.sync.run_once()["conflicts"] == 1

    conflict = b.journal.conflicts("open")[0]
    assert conflict["fields"] == ["text"]
    assert b.service.get(memory_id)["sync_state"] == "conflict"
    # The cloud still holds the first writer's version; nothing was overwritten.
    assert "next shutdown" in cloud_texts(a)[0]
    return a, b, memory_id, conflict


def test_clashing_edits_wait_for_a_decision(fleet):
    clash(fleet)


def test_conflict_keep_mine(fleet):
    a, b, memory_id, conflict = clash(fleet)
    b.sync.set_forced_offline(True)
    b.sync.resolve(conflict["id"], "mine")  # decided while offline
    b.sync.set_forced_offline(False)
    assert b.sync.run_once()["pushed"] == 1
    a.sync.run_once()
    assert "stop the pump immediately" in a.service.get(memory_id)["text"]
    assert not b.journal.conflicts("open")


def test_conflict_accept_theirs(fleet):
    a, b, memory_id, conflict = clash(fleet)
    b.sync.resolve(conflict["id"], "theirs")
    assert "next shutdown" in b.service.get(memory_id)["text"]
    assert b.journal.counts()["pending"] == 0
    assert b.sync.run_once()["pushed"] == 0


def test_conflict_keep_both(fleet):
    a, b, memory_id, conflict = clash(fleet)
    b.sync.resolve(conflict["id"], "both")
    b.sync.run_once()
    texts = cloud_texts(a)
    assert any("next shutdown" in t for t in texts) and any("stop the pump immediately" in t for t in texts)


def test_delete_reaches_other_devices(fleet):
    a, b = fleet("edge-a"), fleet("edge-b")
    memory_id = a.service.capture(FAULT)["memory"]["id"]
    a.sync.run_once()
    b.sync.run_once()
    a.service.delete(memory_id)
    a.sync.run_once()
    assert b.sync.run_once()["removed"] == 1
    assert b.service.find(memory_id) is None
    assert cloud_texts(a) == []


def test_withdrawing_a_shared_memory(fleet):
    a, b = fleet("edge-a"), fleet("edge-b")
    memory_id = a.service.capture(FAULT)["memory"]["id"]
    a.sync.run_once()
    b.sync.run_once()

    a.service.edit(memory_id, scope="private")
    a.sync.run_once()
    b.sync.run_once()
    assert a.service.get(memory_id)["sync_state"] == "private"
    assert a.service.get(memory_id)["text"] == FAULT
    assert b.service.find(memory_id) is None
    assert cloud_texts(a) == []

    a.service.edit(memory_id, scope="shared")
    a.sync.run_once()
    b.sync.run_once()
    assert b.service.get(memory_id)["text"] == FAULT


def test_device_recovers_when_cloud_is_rebuilt(fleet):
    a = fleet("edge-a")
    a.service.capture(FAULT)
    a.sync.run_once()
    a.cloud.reset()
    a.sync.run_once()  # notices the gap and queues the memory again
    a.sync.run_once()
    assert cloud_texts(a) == [FAULT]


def test_a_change_that_keeps_failing_is_set_aside_and_the_queue_drains(fleet):
    a = fleet("edge-a")
    a.settings.sync_max_attempts = 3
    bad = a.service.capture("Valve V-17 leaking hydraulic fluid near the flange, gasket looks worn")["memory"]["id"]
    good = a.service.capture(FAULT)["memory"]["id"]
    real_write = a.cloud.write

    def flaky_write(memory_id, *args, **kwargs):
        if memory_id == bad:
            raise RuntimeError("payload rejected")
        return real_write(memory_id, *args, **kwargs)

    a.cloud.write = flaky_write
    assert a.sync.run_once()["pushed"] == 1, "the healthy change behind the failing one still goes"
    assert a.service.get(good)["sync_state"] == "synced"
    assert a.journal.open_op(bad)["attempts"] == 1

    a.sync.run_once()
    result = a.sync.run_once()
    assert result["failed"] == 1 and a.journal.counts() == {**a.journal.counts(), "pending": 0, "failed": 1}
    assert a.service.get(bad)["sync_state"] == "failed"
    assert a.sync.run_once()["pushed"] == 0, "a parked change is not retried on its own"

    a.cloud.write = real_write
    assert a.sync.retry_failed()["sync"]["pushed"] == 1
    assert a.service.get(bad)["sync_state"] == "synced" and a.journal.counts()["failed"] == 0


def test_an_edit_made_during_the_upload_is_not_lost(fleet):
    a, b = fleet("edge-a"), fleet("edge-b")
    memory_id = a.service.capture(FAULT)["memory"]["id"]
    real_write = a.cloud.write
    newer = "Pump P-102 bearing vibration high at 7.9 mm/s, stop the pump"

    def write_while_editing(*args, **kwargs):
        stored = real_write(*args, **kwargs)
        a.cloud.write = real_write
        a.service.edit(memory_id, text=newer)  # the technician types while the upload is in flight
        return stored

    a.cloud.write = write_while_editing
    assert a.sync.run_once()["pushed"] == 1
    mine = a.service.get(memory_id)
    assert mine["text"] == newer and mine["sync_state"] == "pending", "the edit stays queued"
    assert mine["base_rev"] == 1 and mine["rev"] > 1

    assert a.sync.run_once()["pushed"] == 1
    assert a.service.get(memory_id)["sync_state"] == "synced"
    b.sync.run_once()
    assert b.service.get(memory_id)["text"] == newer


def test_cloud_knowledge_is_searchable_on_the_device(fleet, embedder):
    a = fleet("edge-a")
    seed_cloud(a.cloud, embedder)
    assert a.sync.run_once()["pulled"] == 8
    a.sync.set_forced_offline(True)

    top = a.service.search("what vibration level means I should stop the machine")["results"][0]
    assert top["source"] == "replica" and "7.1 mm/s" in top["text"]


def test_other_sites_are_not_downloaded(fleet):
    a, far = fleet("edge-a"), fleet("edge-z", site="plant-9")
    a.service.capture(FAULT)
    a.sync.run_once()
    assert far.sync.run_once()["pulled"] == 0


def test_replica_can_be_rebuilt_without_touching_local_memory(fleet, embedder):
    a = fleet("edge-a")
    seed_cloud(a.cloud, embedder)
    a.service.capture(HEALTH)
    a.sync.run_once()
    result = a.sync.rebuild_replica()
    assert result["cleared"] == 8 and result["sync"]["pulled"] == 8
    assert a.service.stats()["private"] == 1


def test_answer_uses_the_latest_state(fleet):
    a = fleet("edge-a")
    a.service.capture(FAULT)
    a.service.capture("Pump P-102 bearing replaced, vibration now 1.8 mm/s, normal")
    answer = a.service.ask("what is the condition of pump P-102")
    assert "1.8 mm/s" in answer["answer"] and "This resolves an earlier note" in answer["answer"]
    assert answer["network_calls"] == 0


def test_field_notes_never_replace_reference_material(fleet, embedder):
    a = fleet("edge-a")
    seed_cloud(a.cloud, embedder)
    a.sync.run_once()
    a.service.capture("Daily round done, boiler B-2 pressure 6.1 bar, normal")
    manual = a.service.search("boiler normal operating pressure", kind="reference")["results"][0]
    assert manual["status"] == "active" and manual["source"] == "replica"


def test_strong_meaning_beats_a_weak_keyword(fleet, embedder):
    a = fleet("edge-a")
    seed_cloud(a.cloud, embedder)
    a.sync.run_once()
    a.service.capture("Motor M-9 overheating, winding temperature 96C, fan blocked by dust")
    top = a.service.search("which machine is running too hot")["results"][0]
    assert top["asset"] == "M-9"
