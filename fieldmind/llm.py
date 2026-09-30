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
SENTENCE_END = re.compile(r"(?<=[.!?])\s+")


def _numbers(text: str) -> set[str]:
    return set(NUMBER.findall(text))


def grounded(answer: str, notes: list[str], question: str = "") -> bool:
    """Whether every number in the answer can be found where the answer says it came from.

    A sentence that cites notes may only use numbers from those notes (or from the
    question). A sentence with no citation may use numbers from any retrieved note.
    This catches the typical small-model slip: taking a limit from a manual and
    reporting it as a reading on a machine.
    """
    allowed_anywhere = _numbers(question)
    all_notes = set().union(*(_numbers(note) for note in notes)) if notes else set()
    for sentence in SENTENCE_END.split(answer):
        cited = [int(n) for n in CITATION.findall(sentence)]
        if any(n < 1 or n > len(notes) for n in cited):
            return False
        used = _numbers(CITATION.sub(" ", sentence))
        if not used:
            continue
        sources = set().union(*(_numbers(notes[n - 1]) for n in cited)) if cited else all_notes
        if not used <= sources | allowed_anywhere:
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
