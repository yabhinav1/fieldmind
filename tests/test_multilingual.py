"""Notes in other languages, with the optional multilingual embedding model."""

from fieldmind.config import EMBED_MODELS, Settings
from fieldmind.embedder import Embedder
from fieldmind.runtime import build

HINDI = "पंप P-102 के बेयरिंग में तेज़ कंपन, 7.2 mm/s, बदलने की सिफारिश"
UNRELATED = "Supervisor was rude to the new trainee again today"


def test_a_hindi_note_is_found_by_an_english_question(tmp_path, names, reranker):
    embedder = Embedder(Settings().models_dir, model=EMBED_MODELS["multilingual"])
    assert embedder.dim == 384
    settings = Settings(device_id="edge-m", data_root=tmp_path, ollama_model="")
    from qdrant_client import QdrantClient

    device = build(settings, embedder=embedder, cloud_client=QdrantClient(":memory:"), names=names, reranker=reranker)
    try:
        device.service.capture(HINDI)
        device.service.capture(UNRELATED)
        found = device.service.search("which pump has high bearing vibration")
        assert found["results"][0]["text"] == HINDI, [r["text"] for r in found["results"]]
        assert found["results"][0]["asset"] == "P-102", "the asset tag is read out of the Hindi note"
        assert device.service.preview(HINDI)["decision"]["scope"] == "shared", "the policy understands it too"
    finally:
        device.close()
