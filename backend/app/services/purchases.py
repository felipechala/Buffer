"""Purchase checks: demo requests + committing a checked ad-hoc purchase as a real scheduled bill.

`decide()` stores the ad-hoc ProposedPayment in the run result. `commit_purchase` turns that run into an
`APBill` (status "scheduled") so every later decision sees it as an obligation. With a funding fix it first
approves + executes that fix through the gateway (simulated unless sandbox writes are configured), then
persists the bill, because the write changes the snapshot the fix was computed against.
"""
from __future__ import annotations

import hashlib
import json
from datetime import date, timedelta
from typing import Any

from pydantic import BaseModel

from ..actions.gateway import execute_fix
from ..config import CASHGUARD_DEMO_FIXTURE
from ..workflows.forecast import build_twin_from_store
from ..workflows.models import AnalysisRun, APBill, Approval, AuditEvent, utcnow
from ..workflows.state import WorkflowError
from ..workflows.store import Store
from .decision import ProposedPayment, SimulationRequest, _resolve_bill
from .fixes import Fix
from .stress import get_stress_set, set_stress_set


class PurchaseCommit(BaseModel):
    """Stored response of a commit, keyed by Idempotency-Key (a replay returns it without writing)."""
    id: str
    response: dict[str, Any]


def demo_purchases() -> list[dict[str, Any]]:
    raw = json.loads(CASHGUARD_DEMO_FIXTURE.read_text(encoding="utf-8"))
    return sorted(raw.get("demo_purchases", []), key=lambda p: p["step"])


def demo_request(p: dict[str, Any]) -> SimulationRequest:
    return SimulationRequest(proposed_action=ProposedPayment(
        amount_cents=p["amount_cents"], from_account=p["from_account"], payee=p["payee"],
        pay_date=date.fromisoformat(p["pay_date"]), channel=p["channel"], description=p["description"],
        requested_by=p["requested_by"]))


def commit_purchase(store: Store, *, run_id: str, fix_id: str | None, pay_date: date | None,
                    idempotency_key: str, connector=None, resync=None) -> dict[str, Any]:
    if not idempotency_key:
        raise WorkflowError("idempotency_key_required", "Idempotency-Key header is required", 400)
    prior = store.get(PurchaseCommit, idempotency_key)
    if prior is not None:
        return prior.response
    run = store.get(AnalysisRun, run_id)
    if run is None or not isinstance(run.result, dict) or "provenance" not in run.result:
        raise WorkflowError("not_found", f"run {run_id} not found", 404)
    res = run.result
    if not res.get("ephemeral") or not res.get("proposed_action"):
        raise WorkflowError("not_committable", "only a checked new purchase can be committed "
                            "(this run was for a stored bill)", 422)
    if res["decision"] == "block":
        raise WorkflowError("not_committable", "this purchase was blocked; no funding option keeps the "
                            "company safe", 422)
    twin = build_twin_from_store(store)
    if res["provenance"]["snapshot_id"] != twin.snapshot_id:
        raise WorkflowError("stale_snapshot", f"financial state changed since this check (current snapshot "
                            f"{twin.snapshot_id}); re-run the check", 409)

    cfg = store.get_config()
    action = ProposedPayment.model_validate(res["proposed_action"])
    bill, _payee, _ = _resolve_bill(store, action, cfg.as_of)
    requested = date.fromisoformat(res["requested_date"])

    fix: Fix | None = None
    if fix_id is None:
        if res["decision"] != "allow":
            raise WorkflowError("fix_required", "this purchase needs a fix (reschedule or funding) before it "
                                "can be committed", 422)
        scheduled = pay_date or requested
    else:
        fix = store.get(Fix, fix_id)
        if fix is None or fix.run_id != run_id:
            raise WorkflowError("not_found", f"fix {fix_id} not found for run {run_id}", 404)
        if not fix.verified:
            raise WorkflowError("not_actionable", "only verified fixes can be committed", 409)
        scheduled = fix.pay_date
    if scheduled < cfg.as_of:
        raise WorkflowError("invalid_request", "pay_date is in the past", 422)

    executed = None
    if fix is not None and fix.kind != "reschedule":
        approver = next((u for u in cfg.users if u.role == "finance_lead"), None)
        if approver is None:
            raise WorkflowError("forbidden", "no finance lead configured to approve the funding fix", 403)
        store.save(Approval(id=f"appr-{idempotency_key}", subject="fix", subject_id=fix.id,
                            approver_id=approver.id, role=approver.role, decision="approved",
                            timestamp=utcnow(), memo=f"approved with purchase commit for {run_id}"),
                   actor=approver.id, action="approval.fix", correlation_id=fix.id)
        executed = execute_fix(store, fix.id, idempotency_key=f"{idempotency_key}:fix",
                               connector=connector, resync=resync)

    purchase_id = "PUR-" + hashlib.sha256(f"{run_id}:{idempotency_key}".encode()).hexdigest()[:8].upper()
    saved = APBill(
        id=purchase_id, vendor_id=bill.vendor_id, amount_cents=bill.amount_cents, category=bill.category,
        due_date=max(scheduled, bill.due_date), earliest_pay_date=scheduled,
        source_account_id=bill.source_account_id, status="scheduled", scheduled_date=scheduled,
        nondeferrable=False, memo=action.description or bill.memo, created_by=action.requested_by or "system")
    old_stress = get_stress_set(twin, cfg.risk_tolerance)
    store.save(saved, actor=action.requested_by or "system", action="purchase.committed",
               correlation_id=run_id)
    store.append_audit(AuditEvent(
        timestamp=utcnow(), actor=action.requested_by or "system", action="purchase.committed.detail",
        entity="APBill", entity_id=purchase_id, correlation_id=run_id,
        after={"channel": action.channel, "description": action.description,
               "requested_by": action.requested_by, "run_id": run_id, "fix_id": fix_id,
               "scheduled_date": scheduled.isoformat(), "amount_cents": bill.amount_cents}))
    after = build_twin_from_store(store)
    # the risk-search stress set describes shocks, not the book: carry it over to the new snapshot so the
    # next check uses the same scenarios instead of silently falling back to the default set
    set_stress_set(old_stress.model_copy(update={"snapshot_id": after.snapshot_id}))
    response = {"purchase_id": purchase_id, "bill_id": purchase_id, "scheduled_date": scheduled.isoformat(),
                "snapshot_id": after.snapshot_id,
                "executed_fix": executed.model_dump(mode="json") if executed else None,
                "decision": res["decision"]}
    store.save(PurchaseCommit(id=idempotency_key, response=response), actor="engine",
               action="purchase.commit_recorded", correlation_id=run_id)
    return response
