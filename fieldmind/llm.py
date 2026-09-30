"""Optional on-device language model, served by a local Ollama process.

Only used to phrase an answer from notes that search has already found. When the
model is not running, the device composes the answer from the notes directly.
"""

from __future__ import annotations

import threading
import time

import httpx

SYSTEM = (
    "You are the assistant on a field technician's device. Answer the question using only the numbered notes. "
    "Be direct and practical, at most three sentences. After each fact, cite the note it came from like [1]. "
    "When a note replaces an earlier one, treat the newer note as the current state. "
    "If the notes do not answer the question, say that the device has no record of it."
)
CHECK_EVERY = 15.0


class LocalModel:
    def __init__(self, url: str, model: str):
        self.url = url.rstrip("/")
        self.model = model
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

    def answer(self, question: str, points: list[dict]) -> str | None:
        if not self.available():
            return None
        notes = []
        for index, point in enumerate(points, start=1):
            memory = point["memory"]
            note = f"[{index}] {memory['text']}"
            if point["earlier"]:
                note += f" (this replaces an earlier note: \"{point['earlier'][0]['text']}\")"
            notes.append(note)
        body = {
            "model": self.model,
            "stream": False,
            "keep_alive": "30m",
            "options": {"temperature": 0.1, "num_predict": 200},
            "messages": [
                {"role": "system", "content": SYSTEM},
                {"role": "user", "content": "Notes:\n" + "\n".join(notes) + f"\n\nQuestion: {question}"},
            ],
        }
        try:
            response = httpx.post(f"{self.url}/api/chat", json=body, timeout=90)
            response.raise_for_status()
            return (response.json().get("message", {}).get("content") or "").strip() or None
        except (httpx.HTTPError, ValueError):
            self._checked_at = 0.0  # re-check availability on the next question
            return None
