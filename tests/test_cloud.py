"""Compare-and-swap writes against the cloud collection."""

import uuid

from qdrant_client import QdrantClient

from fieldmind.cloud import Cloud


def test_a_write_is_only_confirmed_when_it_is_the_one_stored(embedder):
    cloud = Cloud("http://unused", None, "cas_test", client=QdrantClient(":memory:"))
    cloud.ensure()
    memory_id = str(uuid.uuid4())
    dense, sparse = embedder.document("Pump P-102 bearing vibration high")

    assert cloud.write(memory_id, dense, sparse, {"text": "v1", "rev": 1}, None) is True
    # Another device moved the cloud on to rev 2 ...
    assert cloud.write(memory_id, dense, sparse, {"text": "v2", "rev": 2}, 1) is True
    # ... so a write that still expects rev 1 is refused, even with the same rev and content as the winner.
    assert cloud.write(memory_id, dense, sparse, {"text": "v2", "rev": 2}, 1) is False
    assert cloud.get(memory_id)["payload"]["text"] == "v2"
    assert cloud.write(memory_id, dense, sparse, {"text": "v3", "rev": 3}, 2) is True


def test_going_offline_never_probes_the_cloud_on_the_callers_thread(fleet):
    a = fleet("edge-a")

    def no_probe():
        raise AssertionError("the dashboard thread must not wait on a network probe")

    a.cloud.reachable = no_probe
    a.sync.set_forced_offline(True)
    assert a.sync.link()["online"] is False
