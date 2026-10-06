"""Optional on-device language model, served by a local Ollama process.

Only used to phrase an answer from notes that search has already found. A small
model can still misattribute facts, so every generated answer is checked against
the notes it cites before it is shown. When the model is not running, or its
answer fails that check, the device composes the answer from the notes directly.
"""

from __future__ import annotations

import re
import threading
import time

import httpx

SYSTEM = (
    "You are the assistant on a field technician's device. Answer the question using only the numbered notes.\n"
    "Each note is labelled. OBSERVED notes say what a technician saw on a specific machine. "
    "MANUAL notes give general limits and procedures; they are not readings from any machine.\n"
    "Rules:\n"
    "- Answer in one or two short sentences.\n"
    "- State a measurement for a machine only if an OBSERVED note gives that measurement. "
    "Never take a number from a MANUAL note and report it as a reading.\n"
    "- Copy numbers, limits and units exactly. To judge a value against a limit, compare the two numbers before answering.\n"
    "- After each fact, cite its note like [1].\n"
    "- When a note replaces an earlier one, the newer note is the current state.\n"
    "- If the notes do not answer the question, say the device has no record of it."
)
CHECK_EVERY = 15.0

CITATION = re.compile(r"\[(\d+)\]")
NUMBER = re.compile(r"\d+(?:\.\d+)?")
ASSET = re.compile(r"\b[A-Z]{1,4}-\d{1,4}[A-Z]?\b")
SENTENCE_END = re.compile(r"(?<=[.!?])\s+")
WORD = re.compile(r"[a-z]+")

# Words that say whether something is in order or not. A cited sentence may not
# claim one when its sources only say the other.
POSITIVE = {"normal", "acceptable", "fine", "ok", "okay", "healthy", "safe", "resolved", "repaired", "fixed"}
NEGATIVE = {"abnormal", "unacceptable", "unsafe", "overheating", "leaking", "fault", "faulty", "failing", "failed",
            "danger", "dangerous", "stop", "stopped", "high", "exceeds", "exceeded", "worn", "stuck", "tripped"}
NEGATIONS = {"not", "no", "never", "isn", "aren", "wasn", "longer"}


def _numbers(text: str) -> set[str]:
    return set(NUMBER.findall(text))


def _assets(text: str) -> set[str]:
    return set(ASSET.findall(text))


def _polarity(text: str) -> set[str]:
    """``{"positive"}``, ``{"negative"}``, both, or neither. A negation just before a
    word flips it, so "not acceptable" counts as negative."""
    words = WORD.findall(text.lower())
    out: set[str] = set()
    for index, word in enumerate(words):
        if word in POSITIVE or word in NEGATIVE:
            negated = any(w in NEGATIONS for w in words[max(0, index - 3):index])
            positive = (word in POSITIVE) != negated
            out.add("positive" if positive else "negative")
    return out


def grounded(answer: str, notes: list[str], question: str = "") -> bool:
    """Whether what the answer says can be found where the answer says it came from.

    Three checks run on every sentence. Numbers and asset tags in a sentence that
    cites notes must appear in those notes (or in the question); a sentence with
    no citation may draw on any retrieved note. And a cited sentence may not call
    something fine when its sources only call it faulty, or the other way round.
    Together they catch the typical small-model slips: a limit from a manual
    reported as a reading, a fault attributed to the wrong machine, and a stale
    or inverted verdict.
    """
    all_numbers = set().union(*(_numbers(note) for note in notes)) if notes else set()
    all_assets = set().union(*(_assets(note) for note in notes)) if notes else set()
    for sentence in SENTENCE_END.split(answer):
        cited = [int(n) for n in CITATION.findall(sentence)]
        if any(n < 1 or n > len(notes) for n in cited):
            return False
        plain = CITATION.sub(" ", sentence)
        sources = [notes[n - 1] for n in cited]

        numbers = _numbers(plain)
        allowed = set().union(*(_numbers(s) for s in sources)) if cited else all_numbers
        if numbers and not numbers <= allowed | _numbers(question):
            return False

        assets = _assets(plain)
        allowed = set().union(*(_assets(s) for s in sources)) if cited else all_assets
        if assets and not assets <= allowed | _assets(question):
            return False

        if cited:
            claimed = _polarity(plain)
            supported = set().union(*(_polarity(s) for s in sources))
            if claimed and supported and not claimed & supported:
                return False
    return True


class LocalModel:
    def __init__(self, url: str, model: str):
        self.url = url.rstrip("/")
        self.model = model
        self.rejected = 0
        self._available = False
        self._checked_at = 0.0
        self._lock = threading.Lock()

    def available(self) -> bool:
        if not self.model:
            return False
        with self._lock:
            if time.time() - self._checked_at < CHECK_EVERY:
                return self._available
            self._checked_at = time.time()
            try:
                tags = httpx.get(f"{self.url}/api/tags", timeout=0.8).json().get("models", [])
                self._available = any(m.get("name") == self.model for m in tags)
            except (httpx.HTTPError, ValueError):
                self._available = False
            return self._available

    def warm(self) -> None:
        """Load the model into memory in the background so the first answer is not slow."""
        def load() -> None:
            if self.available():
                try:
                    httpx.post(f"{self.url}/api/generate", timeout=120,
                               json={"model": self.model, "keep_alive": "30m"})
                except httpx.HTTPError:
                    pass

        threading.Thread(target=load, name="fieldmind-llm-warm", daemon=True).start()

    def answer(self, question: str, points: list[dict]) -> tuple[str | None, str | None]:
        """Returns ``(answer, problem)``. ``answer`` is None when the model is off,
        failed, or produced something the source check rejected; ``problem`` says which."""
        if not self.available():
            return None, None
        texts, prompt_notes = [], []
        for index, point in enumerate(points, start=1):
            memory = point["memory"]
            label = "MANUAL" if memory.get("kind") == "reference" else "OBSERVED"
            note = f"[{index}] {label}: {memory['text']}"
            if point["earlier"]:
                note += f" (this replaces an earlier note: \"{point['earlier'][0]['text']}\")"
            prompt_notes.append(note)
            texts.append(memory["text"])
        body = {
            "model": self.model,
            "stream": False,
            "keep_alive": "30m",
            "options": {"temperature": 0.0, "num_predict": 160},
            "messages": [
                {"role": "system", "content": SYSTEM},
                {"role": "user", "content": "Notes:\n" + "\n".join(prompt_notes) + f"\n\nQuestion: {question}"},
            ],
        }
        try:
            response = httpx.post(f"{self.url}/api/chat", json=body, timeout=90)
            response.raise_for_status()
            text = (response.json().get("message", {}).get("content") or "").strip()
        except (httpx.HTTPError, ValueError):
            self._checked_at = 0.0  # re-check availability on the next question
            return None, "unreachable"
        if not text:
            return None, "empty"
        if not grounded(text, texts, question):
            self.rejected += 1
            return None, "ungrounded"
        return text, None
