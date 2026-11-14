"""Validated state machines (v2 Section 06) and the workflow error type."""
from __future__ import annotations


class WorkflowError(Exception):
    """Business-rule failure. `status` maps to the HTTP status code."""

    def __init__(self, code: str, message: str, status: int = 409):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


INVOICE_TRANSITIONS: dict[str, set[str]] = {
    "draft": {"issued", "void"},
    "issued": {"paid", "overdue", "void"},
    "overdue": {"paid", "void"},
}

BILL_TRANSITIONS: dict[str, set[str]] = {
    "draft": {"pending"},
    "pending": {"approved", "rejected"},
    "approved": {"scheduled"},
    "scheduled": {"paid", "failed", "approved"},  # approved = unschedule
    "failed": {"scheduled"},
}

PAYMENT_TRANSITIONS: dict[str, set[str]] = {
    "proposed": {"approved", "canceled"},
    "approved": {"submitted", "canceled"},
    "submitted": {"confirmed", "uncertain"},
    "uncertain": {"confirmed", "failed"},
}


def check_transition(machine: dict[str, set[str]], entity: str, current: str, target: str) -> None:
    if target not in machine.get(current, set()):
        raise WorkflowError("invalid_transition",
                            f"{entity} cannot move from '{current}' to '{target}'")
