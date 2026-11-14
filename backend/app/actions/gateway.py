"""Execution gateway: the only code path that turns an approved fix into state changes.

Two modes:
  * simulated (default, SANDBOX_WRITES_ENABLED=false): the fix is posted into the store as
    app-simulated cash events / a rescheduled bill. Nothing leaves the process.
  * sandbox (SANDBOX_WRITES_ENABLED=true and a SandboxActionConnector is configured): the
    same actions are also written to the provider sandbox (Nessie). Added in the connector step.

Preconditions are enforced here, not in the API layer: the fix must exist, be approved,
and have been computed against the current snapshot. Repeated Idempotency-Keys return the
stored result and never execute twice.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Literal

from pydantic import BaseModel

from ..config import SANDBOX_WRITES_ENABLED
from ..connectors.base import ProviderRef, SandboxActionConnector  # noqa: F401  (re-exported)
from ..services.fixes import CreditDrawFix, Fix, ScheduleBillFix, TransferFix
from ..workflows.forecast import build_twin_from_store
from ..workflows.models import APBill, Approval, CashEvent, utcnow
from ..workflows.state import WorkflowError
from ..workflows.store import Store

ExecutionState = Literal["simulated", "confirmed", "uncertain"]


class Execution(BaseModel):
    id: str                      # the idempotency key
    fix_id: str
    run_id: str
    approved_by: str
    state: ExecutionState
    mode: Literal["simulated", "sandbox"]
    executed_at: datetime
    cash_event_ids: list[str]
    provider_refs: list[ProviderRef]
    snapshot_before: str
    snapshot_after: str
    notes: list[str]


def _require_approved(store: Store, fix: Fix) -> Approval:
    approvals = [a for a in store.list(Approval)
                 if a.subject == "fix" and a.subject_id == fix.id and a.decision == "approved"]
    if not approvals:
        raise WorkflowError("not_approved", f"fix {fix.id} has no approval", 403)
    return approvals[-1]


def execute_fix(store: Store, fix_id: str, *, idempotency_key: str,
                connector: SandboxActionConnector | None = None) -> Execution:
    if not idempotency_key:
        raise WorkflowError("idempotency_key_required", "Idempotency-Key header is required", 400)
    existing = store.get(Execution, idempotency_key)
    if existing is not None:
        return existing
    fix = store.get(Fix, fix_id)
    if fix is None:
        raise WorkflowError("not_found", f"fix {fix_id} not found", 404)
    approval = _require_approved(store, fix)
    current = build_twin_from_store(store)
    if fix.snapshot_id != current.snapshot_id:
        raise WorkflowError("stale_fix", "financial state changed since this fix was computed; re-run the check", 409)
    cfg = store.get_config()
    sandbox = SANDBOX_WRITES_ENABLED and connector is not None
    notes: list[str] = []
    refs: list[ProviderRef] = []
    event_ids: list[str] = []

    with_bill = store.get(APBill, fix.bill_id)
    for n, action in enumerate(fix.actions):
        tag = f"{fix.id}:{n}"
        if isinstance(action, ScheduleBillFix):
            if with_bill is None:
                notes.append(f"{action.bill_id} is an ad-hoc payment; reschedule recorded, no stored bill to move")
            else:
                before = with_bill
                with_bill = with_bill.model_copy(update={"status": "scheduled",
                                                         "scheduled_date": action.pay_date})
                store.save(with_bill, actor=approval.approver_id, action="bill.scheduled_by_fix",
                           before=before, correlation_id=fix.id)
            if sandbox:
                refs += connector.reschedule_bill(fix, action, f"{idempotency_key}:{n}")
        elif isinstance(action, TransferFix):
            out_ev = CashEvent(id=f"cash-{tag}-out", account_id=action.from_account,
                               signed_cents=-action.amount_cents, effective_date=action.dispatch_date,
                               kind="transfer_out", source="app_simulated", reference=fix.id)
            in_ev = CashEvent(id=f"cash-{tag}-in", account_id=action.to_account,
                              signed_cents=action.amount_cents,
                              effective_date=cfg.as_of + timedelta(days=action.arrival_day),
                              kind="transfer_in", source="app_simulated", reference=fix.id)
            for ev in (out_ev, in_ev):
                store.save(ev, actor=approval.approver_id, action="cashevent.post", correlation_id=fix.id)
                event_ids.append(ev.id)
            if sandbox:
                refs += connector.post_transfer(fix, action, f"{idempotency_key}:{n}")
        elif isinstance(action, CreditDrawFix):
            ev = CashEvent(id=f"cash-{tag}-draw", account_id=action.account, signed_cents=action.amount_cents,
                           effective_date=cfg.as_of + timedelta(days=action.funds_day),
                           kind="credit_draw", source="app_simulated", reference=fix.id)
            store.save(ev, actor=approval.approver_id, action="cashevent.post", correlation_id=fix.id)
            event_ids.append(ev.id)
            notes.append("credit draw is simulated; the provider sandbox has no credit facility object")

    after = build_twin_from_store(store)
    execution = Execution(
        id=idempotency_key, fix_id=fix.id, run_id=fix.run_id, approved_by=approval.approver_id,
        state="confirmed" if sandbox else "simulated", mode="sandbox" if sandbox else "simulated",
        executed_at=utcnow(), cash_event_ids=event_ids, provider_refs=refs,
        snapshot_before=current.snapshot_id, snapshot_after=after.snapshot_id, notes=notes)
    store.save(execution, actor=approval.approver_id, action="fix.executed", correlation_id=fix.id)
    return execution
