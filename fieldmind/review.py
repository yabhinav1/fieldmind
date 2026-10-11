"""Who may share what without asking.

Every device has a role. A trainee's shared notes travel to the cloud marked as
awaiting review and are shown only to supervisors until one approves them; a
rejection sends the note back to its author as private, with the reason. A
technician's or supervisor's notes need no review. Safety hazards never wait:
they reach everyone at once and are reviewed afterwards.

Trust is earned. After TRUST_AFTER approvals in a row a trainee's device is
trusted and its notes skip the queue; one rejection resets that.
"""

from __future__ import annotations

ROLES = ("trainee", "technician", "supervisor")
DEFAULT_ROLE = "technician"
TRUST_AFTER = 10

PENDING, APPROVED, REJECTED = "pending", "approved", "rejected"
# Fields the review leaves on a memory. They travel with it and are copied
# from the cloud on pull, but never conflict: the latest decision wins.
REVIEW_FIELDS = ("review", "reviewed_by", "reviewed_at", "review_reason", "role")
URGENT = 2


def may_see(payload: dict, role: str) -> bool:
    """Whether a device with this role may hold a cloud memory in its replica."""
    if payload.get("review") != PENDING:
        return True
    return role == "supervisor" or payload.get("priority", 1) >= URGENT


def copy_review(target: dict, source: dict) -> None:
    for field in REVIEW_FIELDS:
        if field in source:
            target[field] = source[field]
        else:
            target.pop(field, None)
