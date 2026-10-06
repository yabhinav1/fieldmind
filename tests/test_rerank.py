"""The on-device cross-encoder that reorders search candidates."""

from fieldmind.demo import seed_cloud


def test_the_reranker_prefers_the_note_that_answers(reranker):
    scores = reranker.score("which pump has a bearing problem", [
        "Pump P-102 bearing vibration high at 7.2 mm/s, recommend replacement",
        "Supervisor was rude to the new trainee again today",
        "Lockout and tagout is mandatory before opening any rotating equipment.",
    ])
    assert scores[0] > max(scores[1:]) and 0.0 <= min(scores) <= max(scores) <= 1.0


def test_search_can_rerank_and_reports_it(fleet, embedder):
    a = fleet("edge-a")
    seed_cloud(a.cloud, embedder)
    a.sync.run_once()
    a.service.capture("Motor M-9 overheating, winding temperature 96C, fan blocked by dust")
    a.service.capture("Daily round done, boiler B-2 pressure 6.1 bar, normal")

    plain = a.service.search("why is the motor running hot")
    assert plain["reranked"] is False and "rerank" not in plain["results"][0]["matched"]

    found = a.service.search("why is the motor running hot", rerank=True)
    assert found["reranked"] is True and found["timing_ms"]["rerank"] > 0
    top = found["results"][0]
    assert top["asset"] == "M-9" and "rerank" in top["matched"]
    assert found["network_calls"] == 0


def test_search_degrades_without_the_model(fleet):
    a = fleet("edge-a", rerank=False)
    a.service.reranker = None
    a.service.capture("Pump P-102 bearing vibration high at 7.2 mm/s, recommend replacement")
    found = a.service.search("pump vibration", rerank=True)
    assert found["reranked"] is False and found["results"][0]["asset"] == "P-102"
