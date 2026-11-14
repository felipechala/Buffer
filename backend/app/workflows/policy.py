"""Deterministic spend policy (v2 Section 08). Never model-generated."""
from __future__ import annotations

from pydantic import BaseModel

from ..engine.models import fmt_usd
from .models import APBill, CompanyConfig, Role, User
from .store import Store

ROLE_RANK: dict[str, int] = {"viewer": 0, "ar_specialist": 1, "ap_specialist": 1, "finance_lead": 2}


class PolicyResult(BaseModel):
    required_role: Role
    within_budget: bool
    budget_limit_cents: int | None
    budget_used_cents: int
    nondeferrable: bool
    reasons: list[str]


def evaluate_bill(cfg: CompanyConfig, store: Store, bill: APBill) -> PolicyResult:
    reasons: list[str] = []
    required: Role = "ap_specialist"
    if bill.amount_cents > cfg.policy.approval_threshold_cents:
        required = "finance_lead"
        reasons.append(f"Amount above {fmt_usd(cfg.policy.approval_threshold_cents)} "
                       "requires finance-lead approval.")

    limit = cfg.policy.budgets_cents.get(bill.category)
    used = sum(
        b.amount_cents for b in store.list(APBill)
        if b.id != bill.id and b.category == bill.category
        and b.status not in ("draft", "rejected")
        and (b.due_date.year, b.due_date.month) == (bill.due_date.year, bill.due_date.month)
    )
    within = limit is None or used + bill.amount_cents <= limit
    if limit is not None:
        reasons.append(f"{bill.category} budget: {fmt_usd(used + bill.amount_cents)} of "
                       f"{fmt_usd(limit)} this month including this bill.")
    if not within:
        required = "finance_lead"
        reasons.append("Over category budget: requires finance-lead approval.")

    nondeferrable = bill.nondeferrable or bill.category in cfg.policy.nondeferrable_categories
    if nondeferrable:
        reasons.append(f"{bill.category} obligations are nondeferrable.")
    return PolicyResult(required_role=required, within_budget=within, budget_limit_cents=limit,
                        budget_used_cents=used, nondeferrable=nondeferrable, reasons=reasons)


def can_approve(user: User, required: Role | None) -> bool:
    if user.role == "finance_lead":
        return True
    if required is None:
        return False
    return user.role == required
