"""The Qdrant Edge shard wrapper under failure."""

from pathlib import Path

import pytest

from fieldmind.store import Shard


def test_a_failed_snapshot_restore_leaves_a_working_empty_shard(tmp_path, embedder):
    shard = Shard(tmp_path / "replica", "replica")
    dense, sparse = embedder.document("Boiler B-2 normal operating pressure is 5.8 to 6.4 bar.")
    shard.upsert("0a9d4d6e-0000-4000-8000-000000000001", dense, sparse, {"text": "manual", "rev": 1})
    assert shard.count() == 1

    bad = tmp_path / "broken.snapshot"
    bad.write_bytes(b"this is not a snapshot")
    with pytest.raises(Exception, match=r".*"):
        shard.restore(bad)

    # The device must never be left without a usable replica shard.
    assert shard.count() == 0
    shard.upsert("0a9d4d6e-0000-4000-8000-000000000002", dense, sparse, {"text": "again", "rev": 1})
    assert shard.count() == 1 and shard.nearest(dense, "dense", 1)[0]["payload"]["text"] == "again"
    with pytest.raises(Exception, match=r".*"):
        shard.restore(Path(tmp_path / "missing.snapshot"))
    assert shard.count() == 0
    shard.close()
