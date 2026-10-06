"""Decides, on the device and without a network, what may leave the device.

Two kinds of evidence are combined:

* detectors for things that must never be uploaded as written: patterns for
  credentials, contact details and identity numbers, and a name recognition
  model for people
* a semantic classifier that compares the note with a handful of example
  sentences per category, using the same local embedding model as search

The outcome is one of three scopes:

* ``private``  - stays on the device
* ``shared``   - synced to the cloud as written
* ``redacted`` - the device keeps the original, the cloud gets a masked copy
"""

from __future__ import annotations

import re
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Protocol

import numpy as np

from .embedder import Embedder
from .ner import NameFinder

PRIVATE, SHARED, REDACTED = "private", "shared", "redacted"
PRIORITY_LABEL = {0: "low", 1: "normal", 2: "urgent"}

# A second category whose score comes within this margin of the best one also
# describes the note, and is reported so a mixed note is not treated as plain.
SECONDARY_MARGIN = 0.03

# When a person overrides a decision, the note's vector and the chosen scope are
# kept (never the text). A later note at least this similar follows that choice.
LEARNED_AT = 0.88
MAX_LEARNED = 200


class ExampleStore(Protocol):
    def get(self, key: str, default: Any = None) -> Any: ...
    def set(self, key: str, value: Any) -> None: ...

# category -> (default scope, sync priority, example sentences)
CATEGORIES: dict[str, tuple[str, int, list[str]]] = {
    "safety_hazard": (SHARED, 2, [
        "gas leak detected, evacuate the area immediately",
        "exposed live wiring creates an electrocution risk",
        "fire hazard, smoke coming from the panel",
        "guard missing on rotating machinery, danger to operators",
        "pressure above safe limit, risk of rupture",
    ]),
    "equipment_fault": (SHARED, 1, [
        "pump bearing vibration is abnormally high",
        "motor overheating, winding temperature too high",
        "valve leaking hydraulic fluid at the flange",
        "conveyor belt misaligned and slipping",
        "sensor giving erratic readings, needs replacement",
    ]),
    "procedure_fix": (SHARED, 1, [
        "replaced the worn gasket and torqued bolts to specification",
        "recalibrated the sensor following the standard procedure",
        "fixed the fault by cleaning the filter and restarting",
        "steps to reset the controller after a trip",
        "lubricated the bearing and realigned the coupling",
    ]),
    "routine_reading": (SHARED, 0, [
        "routine inspection, all parameters within normal range",
        "pressure reading normal, no action required",
        "daily check completed, equipment running fine",
        "temperature logged at the usual level",
    ]),
    "personal_health": (PRIVATE, 0, [
        "a worker felt unwell and was sent to the clinic",
        "employee reported chest pain and dizziness during the shift",
        "colleague has a medical condition and is taking medication",
        "someone was injured and needs treatment, personal medical detail",
    ]),
    "people_hr": (PRIVATE, 0, [
        "complaint about a colleague's behaviour",
        "salary, leave and performance discussion about an employee",
        "argument between two workers on the shift",
        "supervisor is unhappy with a team member",
    ]),
    "scratch_note": (PRIVATE, 0, [
        "reminder to myself to call home after the shift",
        "note to self, buy lunch and pick up my keys",
        "personal to-do list for tomorrow",
        "remember my locker combination",
    ]),
}

SCOPE_LABEL = {PRIVATE: "private", SHARED: "shared", REDACTED: "shared with details masked"}

CATEGORY_LABEL = {
    "safety_hazard": "Safety hazard",
    "equipment_fault": "Equipment fault",
    "procedure_fix": "Procedure or fix",
    "routine_reading": "Routine reading",
    "personal_health": "Personal or health",
    "people_hr": "People matter",
    "scratch_note": "Personal note",
}

DETECTORS: list[tuple[str, str, re.Pattern]] = [
    ("credential", "secret",
     re.compile(r"(?i)\b(?:password|passwd|pwd|passcode|api[ _-]?key|secret|token|pin)\b\s*(?:is|=|:)?\s*\S+")),
    ("email", "contact", re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")),
    # Aadhaar (12 digits in groups of 4) and PAN (AAAAA9999A).
    ("id_number", "identity", re.compile(r"\b\d{4}[ -]?\d{4}[ -]?\d{4}\b|\b[A-Z]{5}\d{4}[A-Z]\b")),
    # Indian vehicle registration: state, district, series, four digits.
    ("vehicle", "identity", re.compile(r"\b[A-Z]{2}[ -]?\d{1,2}[ -]?[A-Z]{1,3}[ -]?\d{4}\b")),
    # Staff, badge and employee numbers, as written on a note: "emp id 48213", "badge #A1234".
    ("badge", "identity",
     re.compile(r"(?i)\b(?:emp(?:loyee)?|badge|staff|worker)\s*(?:id|no|number|#)?\.?\s*[:#-]?\s*[A-Z]{0,3}\d{3,8}\b")),
    # A street address starting at a house, flat or plot number, up to the next clause.
    ("address", "contact", re.compile(r"(?i)\b(?:flat|house|plot|door|h\.?\s?no)\s*(?:no\.?)?\s*#?\s*\d+[A-Za-z]?\b[^.,;\n]{0,50}")),
    ("phone", "contact", re.compile(r"(?<![\w.])(?:\+?\d{1,3}[ -]?)?(?:\d[ -]?){9,11}\d(?![\w.])")),
    ("person", "identity",
     re.compile(r"\b(?:Mr|Mrs|Ms|Dr|Technician|Operator|Engineer|Supervisor|Worker)\.?\s+([A-Z][a-z]+(?:\s[A-Z][a-z]+)?)")),
]

ASSET = re.compile(r"\b[A-Z]{1,4}-\d{1,4}[A-Z]?\b")


def find_asset(text: str) -> str | None:
    match = ASSET.search(text)
    return match.group(0) if match else None


@dataclass
class Decision:
    scope: str
    category: str
    category_label: str
    priority: int
    priority_label: str
    confidence: float
    decided_by: str = "policy"
    reasons: list[str] = field(default_factory=list)
    signals: list[dict] = field(default_factory=list)
    scores: dict[str, float] = field(default_factory=dict)
    shared_text: str | None = None
    # Every category that describes the note, best first. More than one means the
    # note mixes topics and deserves a look before it is shared.
    categories: list[str] = field(default_factory=list)
    review: bool = False

    def to_dict(self) -> dict:
        return asdict(self)

    def summary(self) -> dict:
        """What is safe to store alongside the memory and upload: no matched values."""
        return {
            "scope": self.scope,
            "category": self.category,
            "categories": self.categories,
            "priority": self.priority,
            "confidence": self.confidence,
            "decided_by": self.decided_by,
            "review": self.review,
            "reasons": self.reasons,
            "signals": sorted({s["type"] for s in self.signals}),
        }


class PolicyEngine:
    def __init__(self, embedder: Embedder, names: NameFinder | None = None, store: ExampleStore | None = None):
        self._embedder = embedder
        self._names_model = names
        self._names = list(CATEGORIES)
        self._prototypes = [np.asarray(embedder.dense(CATEGORIES[name][2])) for name in self._names]
        self._store = store
        self._learned: list[dict] = list(store.get("policy_examples", [])) if store is not None else []
        self._learned_matrix = self._matrix()

    # -- learning from the person ------------------------------------------

    @property
    def learned_count(self) -> int:
        return len(self._learned)

    def _matrix(self) -> np.ndarray | None:
        return np.asarray([e["vector"] for e in self._learned], dtype=np.float32) if self._learned else None

    def learn(self, text: str, scope: str) -> None:
        """Remember that a note like this one was set to ``scope`` by hand. Only the
        vector is kept, so the example store never holds the note's text."""
        vector = np.asarray(self._embedder.dense([text])[0], dtype=np.float32)
        if self._learned_matrix is not None:
            sims = self._learned_matrix @ vector
            nearest = int(sims.argmax())
            if sims[nearest] >= 0.99:  # the same note again: just update the choice
                self._learned[nearest].update(scope=scope, at=time.time())
                self._persist()
                return
        self._learned.append({"vector": [round(float(v), 5) for v in vector], "scope": scope, "at": time.time()})
        del self._learned[:-MAX_LEARNED]
        self._persist()

    def forget(self) -> None:
        self._learned = []
        self._persist()

    def _persist(self) -> None:
        self._learned_matrix = self._matrix()
        if self._store is not None:
            self._store.set("policy_examples", self._learned)

    def _recall(self, vector: np.ndarray) -> tuple[str, float] | None:
        if self._learned_matrix is None:
            return None
        sims = self._learned_matrix @ vector
        nearest = int(sims.argmax())
        return (self._learned[nearest]["scope"], float(sims[nearest])) if sims[nearest] >= LEARNED_AT else None

    # -- classification ---------------------------------------------------

    def classify(self, text: str) -> tuple[str, float, dict[str, float]]:
        category, confidence, scores, _ = self._classify(np.asarray(self._embedder.dense([text])[0]))
        return category, confidence, scores

    def _classify(self, vector: np.ndarray) -> tuple[str, float, dict[str, float], list[str]]:
        scores = {}
        for name, protos in zip(self._names, self._prototypes, strict=True):
            sims = protos @ vector
            # Best single example, steadied by the category average.
            scores[name] = float(0.7 * sims.max() + 0.3 * sims.mean())
        ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
        margin = ranked[0][1] - ranked[1][1]
        confidence = round(min(0.99, 0.5 + margin * 4), 2)
        categories = [name for name, score in ranked if score >= ranked[0][1] - SECONDARY_MARGIN]
        return ranked[0][0], confidence, {k: round(v, 3) for k, v in ranked}, categories

    def detect(self, text: str) -> list[dict]:
        found, taken = [], []
        people: list[tuple[int, int]] = []
        for type_, group, pattern in DETECTORS:
            for match in pattern.finditer(text):
                if type_ == "person":
                    people.append(match.span(1))
                    continue
                start, end = match.span()
                if any(start < e and end > s for s, e in taken):
                    continue
                taken.append((start, end))
                found.append({"type": type_, "group": group, "start": start, "end": end})

        if self._names_model is not None:
            assets = [m.span() for m in ASSET.finditer(text)]
            people.extend(s for s in self._names_model.find(text)
                          if not any(s[0] < e and s[1] > a for a, e in assets))
        # A name can be found by both the title rule and the model, sometimes with
        # different edges. Keep the widest span so no part of it is left unmasked.
        merged: list[list[int]] = []
        for start, end in sorted(people):
            if merged and start <= merged[-1][1]:
                merged[-1][1] = max(merged[-1][1], end)
            else:
                merged.append([start, end])
        for start, end in merged:
            if not any(start < e and end > s for s, e in taken):
                found.append({"type": "person", "group": "identity", "start": start, "end": end})
        return sorted(found, key=lambda s: s["start"])

    def redact(self, text: str, signals: list[dict]) -> str:
        out = text
        for signal in sorted(signals, key=lambda s: s["start"], reverse=True):
            out = out[: signal["start"]] + f"[{signal['type']} removed]" + out[signal["end"]:]
        return out

    def decide(self, text: str, override: str | None = None) -> Decision:
        vector = np.asarray(self._embedder.dense([text])[0])
        category, confidence, scores, categories = self._classify(vector)
        default_scope, priority, _ = CATEGORIES[category]
        signals = self.detect(text)
        types = {s["type"] for s in signals}
        label = CATEGORY_LABEL[category]
        reasons: list[str] = []
        scope = default_scope
        shared_text = None
        review = False

        if "credential" in types:
            scope, priority = PRIVATE, 0
            reasons.append("Contains a credential, which is never uploaded.")
        elif default_scope == PRIVATE:
            reasons.append(f"Reads as \"{label}\", which stays on this device.")
            if signals:
                reasons.append("Also contains personal details: " + ", ".join(sorted(types)) + ".")
        elif signals:
            scope = REDACTED
            shared_text = self.redact(text, signals)
            reasons.append(f"Reads as \"{label}\", which is useful to other devices.")
            reasons.append("Personal details (" + ", ".join(sorted(types)) + ") are masked in the cloud copy; the original stays here.")
        else:
            reasons.append(f"Reads as \"{label}\" with no personal details, so it is shared.")

        # A note that also reads as another kind. Sharing a note that is partly
        # personal, or keeping one that is partly a hazard, deserves a look.
        for other in categories[1:]:
            other_scope = CATEGORIES[other][0]
            if other_scope != default_scope and "credential" not in types:
                review = True
                if other_scope == PRIVATE:
                    reasons.append(f"Also reads partly as \"{CATEGORY_LABEL[other]}\". Check it before it is shared.")
                else:
                    reasons.append(f"Also reads partly as \"{CATEGORY_LABEL[other]}\", which the fleet may need. "
                                   "Share it by hand if the personal part can go.")
                break

        if scope != PRIVATE and priority == 2:
            reasons.append("Safety related, so it is sent first when the link returns.")

        decision = Decision(
            scope=scope, category=category, category_label=label, priority=priority,
            priority_label=PRIORITY_LABEL[priority], confidence=confidence, reasons=reasons,
            signals=signals, scores=scores, shared_text=shared_text, categories=categories, review=review,
        )

        # A choice the person made for a note like this one carries over.
        learned = self._recall(vector) if "credential" not in types else None
        if learned and learned[0] != decision.scope:
            self._apply_override(decision, text, learned[0])
            decision.decided_by = "learned"
            decision.reasons = [f"A note like this one ({int(learned[1] * 100)}% similar) was set to "
                                f"{SCOPE_LABEL[decision.scope]} by hand before, so this one follows it."]
            decision.review = False

        return self._apply_override(decision, text, override) if override else decision

    def _apply_override(self, decision: Decision, text: str, override: str) -> Decision:
        if override not in (PRIVATE, SHARED, REDACTED) or override == decision.scope:
            return decision
        types = {s["type"] for s in decision.signals}
        if override != PRIVATE and "credential" in types:
            decision.reasons.append("Sharing was requested, but a credential is present, so it stays private.")
            return decision
        if override == SHARED and decision.signals:
            # A person may widen the scope, but matched personal details are still masked.
            override = REDACTED
        decision.scope = override
        decision.decided_by = "user"
        decision.review = False
        decision.shared_text = self.redact(text, decision.signals) if override == REDACTED else None
        if override == PRIVATE:
            decision.priority = 0
            decision.priority_label = PRIORITY_LABEL[0]
        else:
            decision.priority = CATEGORIES[decision.category][1]
            decision.priority_label = PRIORITY_LABEL[decision.priority]
        decision.reasons = [f"Set to {override} by the user."] + (
            ["Personal details are still masked in the cloud copy."] if override == REDACTED else [])
        return decision
