"""On-device recognition of people's names, so they can be masked before upload.

A quantised BERT model runs on the CPU through ONNX Runtime. The English model
(about 105 MB) is the default; the multilingual one (about 180 MB) also reads
names in other scripts, such as Devanagari from dictation. Like the embedding
model it is fetched once and then loaded from disk.
"""

from __future__ import annotations

import json
import re
import threading
import time
from pathlib import Path

import numpy as np

MODELS = {
    "english": "Xenova/bert-base-NER",
    "multilingual": "Xenova/bert-base-multilingual-cased-ner-hrl",
}
REPO = MODELS["english"]
FILES = ("onnx/model_quantized.onnx", "tokenizer.json", "config.json")
MIN_CONFIDENCE = 0.5


class NameFinder:
    """Finds character spans that are people's names. ``available`` is False when
    the model could not be loaded; callers then rely on pattern rules alone."""

    def __init__(self, models_dir: Path, model: str = "english"):
        self.available = False
        self.error: str | None = None
        self.model = model if model in MODELS else "english"
        self.repo = MODELS.get(model, model)
        self._lock = threading.Lock()
        # The multilingual model reads other scripts but misses some English names
        # the English model catches, so both run and their spans are joined.
        self._english = NameFinder(models_dir) if self.repo != REPO else None
        started = time.perf_counter()
        try:
            folder = "ner" if self.repo == REPO else "ner-" + re.sub(r"[^a-z0-9]+", "-", self.repo.lower()).strip("-")
            paths = self._files(models_dir / folder, self.repo)
            import onnxruntime as ort
            from tokenizers import Tokenizer

            self._labels = json.loads(paths["config.json"].read_text())["id2label"]
            self._tokenizer = Tokenizer.from_file(str(paths["tokenizer.json"]))
            self._tokenizer.enable_truncation(max_length=512)
            self._session = ort.InferenceSession(str(paths["onnx/model_quantized.onnx"]),
                                                 providers=["CPUExecutionProvider"])
            self._inputs = {i.name for i in self._session.get_inputs()}
            self.available = True
        except Exception as error:  # no model and no network: degrade, do not fail the device
            self.error = str(error)
        self.load_seconds = round(time.perf_counter() - started, 2)

    @property
    def label(self) -> str:
        return "multilingual BERT NER (ONNX)" if self.repo != REPO else "BERT NER (ONNX)"

    @staticmethod
    def _files(target: Path, repo: str = REPO) -> dict[str, Path]:
        paths = {name: target / name for name in FILES}
        if all(p.exists() for p in paths.values()):
            return paths
        from huggingface_hub import hf_hub_download

        return {name: Path(hf_hub_download(repo, name, local_dir=str(target))) for name in FILES}

    def find(self, text: str) -> list[tuple[int, int]]:
        spans = self._find(text) if self.available else []
        if self._english and self._english.available:
            spans = _merge(spans, self._english.find(text))
        return spans

    def _find(self, text: str) -> list[tuple[int, int]]:
        if not self.available or not text.strip():
            return []
        encoding = self._tokenizer.encode(text)
        feeds = {"input_ids": np.array([encoding.ids], dtype=np.int64),
                 "attention_mask": np.array([encoding.attention_mask], dtype=np.int64)}
        if "token_type_ids" in self._inputs:
            feeds["token_type_ids"] = np.array([encoding.type_ids], dtype=np.int64)
        with self._lock:
            logits = self._session.run(None, feeds)[0][0]
        exp = np.exp(logits - logits.max(-1, keepdims=True))
        probabilities = exp / exp.sum(-1, keepdims=True)

        spans: list[list[int]] = []
        for index, (start, end) in enumerate(encoding.offsets):
            label = self._labels[str(int(probabilities[index].argmax()))]
            if start == end or not label.endswith("PER") or probabilities[index].max() < MIN_CONFIDENCE:
                continue
            if spans and start - spans[-1][1] <= 1:
                spans[-1][1] = end
            else:
                spans.append([start, end])
        return _whole_words(text, spans)


def _merge(a: list[tuple[int, int]], b: list[tuple[int, int]]) -> list[tuple[int, int]]:
    out: list[tuple[int, int]] = []
    for start, end in sorted(a + b):
        if out and start <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], end))
        else:
            out.append((start, end))
    return out


def _whole_words(text: str, spans: list[list[int]]) -> list[tuple[int, int]]:
    """The model labels word pieces, so "Suresh" can come back as "Sure". Widen every
    span to the edges of the words it touches and join spans that then overlap."""
    out: list[tuple[int, int]] = []
    for start, end in spans:
        while start > 0 and text[start - 1].isalpha():
            start -= 1
        while end < len(text) and text[end].isalpha():
            end += 1
        if out and start <= out[-1][1] + 1:
            out[-1] = (out[-1][0], end)
        else:
            out.append((start, end))
    return out
