"""Notes in other languages, with the optional multilingual embedding and name models."""

import pytest

from fieldmind.config import EMBED_MODELS, Settings
from fieldmind.embedder import Embedder
from fieldmind.ner import NameFinder
from fieldmind.policy import PolicyEngine
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


@pytest.fixture(scope="module")
def multilingual_embedder():
    return Embedder(Settings().models_dir, model=EMBED_MODELS["multilingual"])


@pytest.fixture(scope="module")
def multilingual_names():
    finder = NameFinder(Settings().models_dir, "multilingual")
    assert finder.available, finder.error
    return finder


@pytest.mark.parametrize("text, hidden", [
    ("सुरेश और प्रिया ने पंप P-102 की कपलिंग बदली", ["सुरेश", "प्रिया"]),
    ("Juan García cambió el acoplamiento de la bomba P-102", ["Juan", "García"]),
    ("Motor M-9 tripped twice, Anil restarted it both times", ["Anil"]),
    ("Spoke with Deepak Yadav about the K-4 compressor drain valve", ["Deepak", "Yadav"]),
])
def test_names_in_other_scripts_are_masked_with_the_multilingual_model(multilingual_embedder, multilingual_names, text, hidden):
    policy = PolicyEngine(multilingual_embedder, multilingual_names)
    decision = policy.decide(text)
    assert decision.scope == "redacted", decision
    for name in hidden:
        assert name not in decision.shared_text, decision.shared_text
    assert "[person removed]" in decision.shared_text


def test_the_multilingual_name_model_keeps_english_recall(names, multilingual_names):
    for text in ("Suresh fixed pump P-102 seal yesterday", "Asked Priya to check valve V-17 before the shift ends",
                 "Mohammed and Lakshmi replaced the gasket on HX-5"):
        english = {text[a:b] for a, b in names.find(text)}
        both = {text[a:b] for a, b in multilingual_names.find(text)}
        assert english <= both, (text, english, both)
