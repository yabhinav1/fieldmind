"""The SQLite journal: retention and outbox bookkeeping."""

from fieldmind.journal import PRUNE_EVERY, Journal


def test_history_is_capped_so_a_long_running_device_does_not_fill_its_disk(tmp_path):
    journal = Journal(tmp_path / "journal.sqlite", keep_events=50, keep_runs=5, keep_finished_ops=4)
    for i in range(PRUNE_EVERY * 2 + 10):
        journal.log("test", f"event {i}")
    events = journal.events(limit=10_000)
    assert 50 <= len(events) < PRUNE_EVERY * 2 + 10, "pruning kicked in on its own"
    assert events[-1]["message"] == f"event {PRUNE_EVERY * 2 + 9}", "the newest events are kept"

    for i in range(12):
        run = journal.start_run("test")
        journal.finish_run(run, "ok", pushed=i)
    for i in range(10):
        journal.enqueue(f"m{i}", "upsert", 1, 0, None)
        journal.finish(i + 1)
    journal.enqueue("open", "upsert", 1, 0, None)
    journal.prune()
    assert len(journal.events(limit=10_000)) == 50
    assert [r["pushed"] for r in journal.runs(100)] == [11, 10, 9, 8, 7]
    assert journal.counts()["done"] == 4 and journal.counts()["pending"] == 1, "open entries are never pruned"
    journal.close()


def test_finish_leaves_an_entry_that_changed_underneath(tmp_path):
    journal = Journal(tmp_path / "journal.sqlite")
    journal.enqueue("m1", "upsert", 1, 0, None)
    op = journal.open_op("m1")
    journal.enqueue("m1", "upsert", 2, 0, None)  # an edit collapses into the same entry
    assert journal.finish(op["seq"], if_updated_at=op["updated_at"]) is False
    assert journal.open_op("m1")["state"] == "pending"
    assert journal.finish(op["seq"]) is True
    journal.close()
