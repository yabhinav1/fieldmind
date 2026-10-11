"""Pull sync asks the cloud for what changed, not for the whole site, on most cycles."""

from fieldmind.config import Settings

FAULT = "Pump P-102 bearing vibration high at 7.2 mm/s, recommend replacement"
GAS = "Smell of gas near compressor K-4, isolated the line and cleared the bay"


def counting(device):
    calls = {"manifest": 0, "changes": 0}
    manifest, changes = device.cloud.manifest, device.cloud.changes

    def count_manifest(*args, **kwargs):
        calls["manifest"] += 1
        return manifest(*args, **kwargs)

    def count_changes(*args, **kwargs):
        calls["changes"] += 1
        return changes(*args, **kwargs)

    device.cloud.manifest, device.cloud.changes = count_manifest, count_changes
    return calls


def test_after_the_first_listing_only_changes_are_fetched(fleet):
    a, b = fleet("edge-a"), fleet("edge-b")
    calls = counting(b)
    b.sync.run_once()
    assert b.sync.pull_mode == "full" and calls == {"manifest": 1, "changes": 0}

    a.service.capture(FAULT)
    a.sync.run_once()
    b.sync.run_once()
    assert b.sync.pull_mode == "changes" and calls == {"manifest": 1, "changes": 1}
    assert FAULT in [m["text"] for m in b.service.list()]

    memory_id = a.service.capture(GAS)["memory"]["id"]
    a.sync.run_once()
    a.service.delete(memory_id)
    a.sync.run_once()
    b.sync.run_once()
    texts = [m["text"] for m in b.service.list()]
    assert FAULT in texts and GAS not in texts, "a tombstone arrives through the change log too"
    assert calls["manifest"] == 1


def test_the_whole_site_is_listed_again_every_so_often_and_after_a_wipe(fleet):
    a = fleet("edge-a", full_pull_every=3)
    calls = counting(a)
    a.service.capture(FAULT)
    for _ in range(6):
        a.sync.run_once()
    assert calls == {"manifest": 3, "changes": 3}, "cycles 1, 3 and 6 list the whole site; the others ask for changes"

    a.cloud.reset()
    a.sync.run_once()
    assert a.sync.pull_mode == "full", "a collection that had to be created is listed in full"
    a.sync.run_once()
    assert [r["payload"].get("text") for r in a.cloud.browse()] == [FAULT], "and the lost memory is sent again"


def test_a_device_with_a_slow_clock_still_gets_recent_changes(fleet):
    a, b = fleet("edge-a"), fleet("edge-b")
    b.sync.run_once()
    b.journal.set("pull_since", b.journal.get("pull_since") + 200)  # b believes it is 200 s ahead of a
    a.service.capture(FAULT)
    a.sync.run_once()
    b.sync.run_once()
    assert b.sync.pull_mode == "changes" and FAULT in [m["text"] for m in b.service.list()]


def test_the_setting_is_read_from_the_environment(monkeypatch):
    monkeypatch.setenv("FIELDMIND_FULL_PULL_EVERY", "7")
    assert Settings().full_pull_every == 7
