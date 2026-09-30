"""Decides, on the device and without a network, what may leave the device.

Two kinds of evidence are combined:

* pattern detectors for things that must never be uploaded as written
  (credentials, contact details, identity numbers, named people)
* a semantic classifier that compares the note with a handful of example
  sentences per category, using the same local embedding model as search

The outcome is one of three scopes:

* ``private``  - stays on the device
* ``shared``   - synced to the cloud as written
* ``redacted`` - the device keeps the original, the cloud gets a masked copy
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field

import numpy as np

from .embedder import Embedder

PRIVATE, SHARED, REDACTED = "private", "shared", "redacted"
PRIORITY_LABEL = {0: "low", 1: "normal", 2: "urgent"}

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
    ("id_number", "identity", re.compile(r"\b\d{4}[ -]?\d{4}[ -]?\d{4}\b|\b[A-Z]{5}\d{4}[A-Z]\b")),
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

    def to_dict(self) -> dict:
        return asdict(self)

    def summary(self) -> dict:
        """What is safe to store alongside the memory and upload: no matched values."""
        return {
            "scope": self.scope,
            "category": self.category,
            "priority": self.priority,
            "confidence": self.confidence,
            "decided_by": self.decided_by,
            "reasons": self.reasons,
            "signals": sorted({s["type"] for s in self.signals}),
        }


class PolicyEngine:
    def __init__(self, embedder: Embedder):
        self._embedder = embedder
        self._names = list(CATEGORIES)
        self._prototypes = [np.asarray(embedder.dense(CATEGORIES[name][2])) for name in self._names]

    def classify(self, text: str) -> tuple[str, float, dict[str, float]]:
        vector = np.asarray(self._embedder.dense([text])[0])
        scores = {}
        for name, protos in zip(self._names, self._prototypes):
            sims = protos @ vector
            # Best single example, steadied by the category average.
            scores[name] = float(0.7 * sims.max() + 0.3 * sims.mean())
        ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
        margin = ranked[0][1] - ranked[1][1]
        confidence = round(min(0.99, 0.5 + margin * 4), 2)
        return ranked[0][0], confidence, {k: round(v, 3) for k, v in ranked}

    def detect(self, text: str) -> list[dict]:
        found, taken = [], []
        for type_, group, pattern in DETECTORS:
            for match in pattern.finditer(text):
                start, end = match.span(1) if type_ == "person" else match.span()
                if any(start < e and end > s for s, e in taken):
                    continue
                taken.append((start, end))
                found.append({"type": type_, "group": group, "start": start, "end": end})
        return sorted(found, key=lambda s: s["start"])

    def redact(self, text: str, signals: list[dict]) -> str:
        out = text
        for signal in sorted(signals, key=lambda s: s["start"], reverse=True):
            out = out[: signal["start"]] + f"[{signal['type']} removed]" + out[signal["end"]:]
        return out

    def decide(self, text: str, override: str | None = None) -> Decision:
        category, confidence, scores = self.classify(text)
        default_scope, priority, _ = CATEGORIES[category]
        signals = self.detect(text)
        types = {s["type"] for s in signals}
        label = CATEGORY_LABEL[category]
        reasons: list[str] = []
        scope = default_scope
        shared_text = None

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

        if scope != PRIVATE and priority == 2:
            reasons.append("Safety related, so it is sent first when the link returns.")

        decision = Decision(
            scope=scope, category=category, category_label=label, priority=priority,
            priority_label=PRIORITY_LABEL[priority], confidence=confidence, reasons=reasons,
            signals=signals, scores=scores, shared_text=shared_text,
        )
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
        decision.shared_text = self.redact(text, decision.signals) if override == REDACTED else None
        if override == PRIVATE:
            decision.priority = 0
            decision.priority_label = PRIORITY_LABEL[0]
        decision.reasons = [f"Set to {override} by the user."] + (
            ["Personal details are still masked in the cloud copy."] if override == REDACTED else [])
        return decision
