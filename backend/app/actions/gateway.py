"""Execution gateway: the only code path that turns an approved fix into state changes.

Two modes:
  * simulated (default, SANDBOX_WRITES_ENABLED=false): the fix is posted into the store as
    app-simulated cash events / a rescheduled bill. Nothing leaves the process; the response says so.
  * sandbox (SANDBOX_WRITES_ENABLED=true and a SandboxActionConnector is injected): actions are written to
    the provider sandbox (Nessie): TransferFix -> withdrawal + deposit, ScheduleBillFix -> PUT bill,
    CreditDrawFix -> internal simulated CashEvent (the provider has no credit facility). The store is NOT
    edited for provider-backed actions; a provider sync brings them back as cash events, and the new snapshot
    is reconciled against what the fix implies (actions/reconcile.py).

Preconditions are enforced here, not in the API layer: the fix must exist, be approved, and have been
computed against the current snapshot. An Idempotency-Key is mandatory; a repeated key returns the stored
result and never writes twice. Provider writes are never retried: a timeout (or a failed second transfer
leg) persists the execution as "uncertain" and tells the client to re-sync; a fix with an uncertain
execution cannot be executed again under a new key.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta
from typing import Any, Callable, Literal

from pydantic import BaseModel

from ..config import SANDBOX_WRITES_ENABLED
from ..connectors.base import (  # noqa: F401  (re-exported)
    ConnectorError,
    ConnectorTimeout,
    PartialTransferError,
    ProviderRef,
    SandboxActionConnector,
)
from ..services.fixes import CreditDrawFix, Fix, ScheduleBillFix, TransferFix
from ..workflows.forecast import build_twin_from_store
from ..workflows.models import APBill, Approval, AuditEvent, CashEvent, utcnow
from ..workflows.state import WorkflowError
from ..workflows.store import Store
from .reconcile import Reconciliation, reconcile

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
    reconciliation: Reconciliation | None = None
    missing_provider_refs: list[str] = []
    next_step: str = ""
    label: str = ""              # human-readable mode label for UIs


def _require_approved(store: Store, fix: Fix) -> Approval:
    approvals = [a for a in store.list(Approval)
                 if a.subject == "fix" and a.subject_id == fix.id and a.decision == "approved"]
    if not approvals:
        raise WorkflowError("not_approved", f"fix {fix.id} has no approval", 403)
    return approvals[-1]


def _post_simulated(store: Store, fix: Fix, approval: Approval, notes: list[str], event_ids: list[str]) -> None:
    """Simulated mode: post every action into the store."""
    cfg = store.get_config()
    bill = store.get(APBill, fix.bill_id)
    for n, action in enumerate(fix.actions):
        tag = f"{fix.id}:{n}"
        if isinstance(action, ScheduleBillFix):
            if bill is None:
                notes.append(f"{action.bill_id} is an ad-hoc payment; reschedule recorded, no stored bill to move")
            else:
                before = bill
                bill = bill.model_copy(update={"status": "scheduled", "scheduled_date": action.pay_date})
                store.save(bill, actor=approval.approver_id, action="bill.scheduled_by_fix",
                           before=before, correlation_id=fix.id)
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
        elif isinstance(action, CreditDrawFix):
            _post_draw(store, fix, approval, n, action, notes, event_ids)


def _post_draw(store: Store, fix: Fix, approval: Approval, n: int, draw: CreditDrawFix, notes: list[str],
               event_ids: list[str]) -> CashEvent:
    cfg = store.get_config()
    ev = CashEvent(id=f"cash-{fix.id}:{n}-draw", account_id=draw.account, signed_cents=draw.amount_cents,
                   effective_date=cfg.as_of + timedelta(days=draw.funds_day),
                   kind="credit_draw", source="app_simulated", reference=fix.id)
    store.save(ev, actor=approval.approver_id, action="cashevent.post", correlation_id=fix.id)
    event_ids.append(ev.id)
    notes.append("credit draw is SIMULATED (app-internal cash event); the provider sandbox has no credit facility")
    return ev


def _audit(store: Store, actor: str, action: str, ex: Execution) -> None:
    rec = ex.reconciliation
    store.append_audit(AuditEvent(
        timestamp=utcnow(), actor=actor, action=action, entity="Execution", entity_id=ex.id,
        after={"fix_id": ex.fix_id, "state": ex.state, "mode": ex.mode,
               "provider_refs": [r.model_dump(mode="json") for r in ex.provider_refs],
               "reconciliation": None if rec is None else {
                   "unexplained_cents": rec.unexplained_cents, "in_flight_cents": rec.in_flight_cents,
                   "balanced": rec.balanced,
                   "accounts": {r.account_id: [r.expected_cents, r.observed_cents, r.unexplained_cents]
                                for r in rec.accounts if r.unexplained_cents or r.in_flight_cents}},
               "missing_provider_refs": ex.missing_provider_refs},
        correlation_id=ex.fix_id))


def execute_fix(store: Store, fix_id: str, *, idempotency_key: str,
                connector: SandboxActionConnector | None = None,
                resync: Callable[[], Any] | None = None,
                writes_enabled: bool | None = None) -> Execution:
    """`resync` re-reads the provider into the store (sandbox mode); `writes_enabled` overrides the env flag."""
    if not idempotency_key:
        raise WorkflowError("idempotency_key_required", "Idempotency-Key header is required", 400)
    existing = store.get(Execution, idempotency_key)
    if existing is not None:
        return existing
    fix = store.get(Fix, fix_id)
    if fix is None:
        raise WorkflowError("not_found", f"fix {fix_id} not found", 404)
    stuck = [e for e in store.list(Execution) if e.fix_id == fix.id and e.state == "uncertain"]
    if stuck:
        raise WorkflowError("execution_uncertain",
                            f"execution {stuck[0].id} of this fix is uncertain; re-sync the provider and inspect "
                            "it before doing anything else (writes are never retried)", 409)
    approval = _require_approved(store, fix)
    current = build_twin_from_store(store)
    if fix.snapshot_id != current.snapshot_id:
        raise WorkflowError("stale_fix", "financial state changed since this fix was computed; re-run the check", 409)
    flag = SANDBOX_WRITES_ENABLED if writes_enabled is None else writes_enabled
    notes: list[str] = []
    event_ids: list[str] = []

    if not (flag and connector is not None):
        notes.append("SIMULATED: nothing was sent to the provider sandbox"
                     + (" (writes are enabled but no sandbox connector is configured)" if flag else ""))
        _post_simulated(store, fix, approval, notes, event_ids)
        after = build_twin_from_store(store)
        execution = Execution(
            id=idempotency_key, fix_id=fix.id, run_id=fix.run_id, approved_by=approval.approver_id,
            state="simulated", mode="simulated", executed_at=utcnow(), cash_event_ids=event_ids,
            provider_refs=[], snapshot_before=current.snapshot_id, snapshot_after=after.snapshot_id,
            notes=notes, label="simulated (app-internal; no provider write)")
        store.save(execution, actor=approval.approver_id, action="fix.executed", correlation_id=fix.id)
        return execution
    return _execute_sandbox(store, fix, approval, current, idempotency_key, connector, resync, notes, event_ids)


def _execute_sandbox(store: Store, fix: Fix, approval: Approval, current, key: str,
                     connector: SandboxActionConnector, resync: Callable[[], Any] | None,
                     notes: list[str], event_ids: list[str]) -> Execution:
    cfg = store.get_config()
    acct_pid = {a.id: a.provider_id for a in cfg.accounts}
    expected = {a.id: a.observed_balance_cents for a in current.accounts}
    in_flight: dict[str, int] = defaultdict(int)

    def effect(account: str, cents: int, when: date) -> None:
        expected[account] += cents
        if when > cfg.as_of:
            in_flight[account] += cents

    # ---- preflight: refuse before the first write if any action cannot be written faithfully
    for action in fix.actions:
        if isinstance(action, TransferFix):
            if action.amount_cents <= 0 or action.amount_cents % 100:
                raise WorkflowError("amount_not_whole_dollars",
                                    f"transfer of {action.amount_cents} cents is not a whole-dollar amount; "
                                    "the provider truncates cents, so nothing was written", 422)
            if not (acct_pid.get(action.from_account) and acct_pid.get(action.to_account)):
                raise WorkflowError("not_connected", "accounts are not linked to the provider; "
                                    "sync the provider connection before executing in sandbox mode", 409)

    refs: list[ProviderRef] = []
    bill_checks: list[tuple[str, date]] = []
    draws: list[tuple[int, CreditDrawFix]] = []
    uncertain_reason: str | None = None
    for n, action in enumerate(fix.actions):
        try:
            if isinstance(action, ScheduleBillFix):
                bill = store.get(APBill, action.bill_id)
                if bill is None or not bill.provider_id:
                    notes.append(f"{action.bill_id} has no provider bill; reschedule not written")
                else:
                    refs += connector.schedule_bill(bill.provider_id, action.pay_date)
                    bill_checks.append((bill.provider_id, action.pay_date))
            elif isinstance(action, TransferFix):
                arrival = cfg.as_of + timedelta(days=action.arrival_day)
                desc = (f"TreasuryTwin plan_{fix.id.removeprefix('plan_')} transfer "
                        f"${action.amount_cents // 100} {action.from_account}->{action.to_account} "
                        f"via {action.route_id} ({n})")
                refs += connector.create_transfer(acct_pid[action.from_account], acct_pid[action.to_account],
                                                  action.amount_cents, action.dispatch_date, desc,
                                                  arrival_date=arrival)
                effect(action.from_account, -action.amount_cents, action.dispatch_date)
                effect(action.to_account, action.amount_cents, arrival)
                if action.fee_cents:
                    notes.append(f"route fee ${action.fee_cents / 100:,.2f} is modeled, not written to the provider")
            elif isinstance(action, CreditDrawFix):
                draws.append((n, action))
        except PartialTransferError as exc:
            refs += exc.refs
            uncertain_reason = str(exc)
            break
        except ConnectorTimeout as exc:
            uncertain_reason = f"write outcome unknown: {exc}"
            break
        except ConnectorError as exc:
            if refs:        # earlier writes already landed: the provider no longer matches the store
                uncertain_reason = f"action {n} failed after earlier writes landed: {exc}"
                break
            raise WorkflowError("provider_rejected", f"the provider rejected the write; nothing was written: {exc}",
                                502) from None

    if uncertain_reason is not None:
        notes.append(uncertain_reason)
        notes.append("Not retried. Re-sync the provider and inspect the recorded refs before acting again.")
        after = build_twin_from_store(store)
        ex = Execution(
            id=key, fix_id=fix.id, run_id=fix.run_id, approved_by=approval.approver_id, state="uncertain",
            mode="sandbox", executed_at=utcnow(), cash_event_ids=event_ids, provider_refs=refs,
            snapshot_before=current.snapshot_id, snapshot_after=after.snapshot_id, notes=notes,
            next_step="POST /v1/connections/nessie/sync, then compare the provider's records with provider_refs; "
                      "this fix cannot be executed again until then",
            label="uncertain (provider write outcome unknown)")
        store.save(ex, actor=approval.approver_id, action="fix.uncertain", correlation_id=fix.id)
        _audit(store, approval.approver_id, "fix.execution_uncertain", ex)
        return ex

    # internal simulated events (credit draws) only after every provider write succeeded
    for n, draw in draws:
        ev = _post_draw(store, fix, approval, n, draw, notes, event_ids)
        effect(draw.account, draw.amount_cents, ev.effective_date)

    state: ExecutionState = "confirmed"
    recon: Reconciliation | None = None
    missing: list[str] = []
    next_step = ""
    try:
        if resync is None:
            raise RuntimeError("no re-sync configured")
        resync()
    except Exception as exc:  # noqa: BLE001 - the writes landed; only the verification failed
        state = "uncertain"
        notes.append(f"writes were accepted but the post-write re-sync failed ({type(exc).__name__}); not verified")
        next_step = "POST /v1/connections/nessie/sync, then re-simulate"
    after = build_twin_from_store(store)
    if state == "confirmed":
        recon = reconcile(expected, {a.id: a.observed_balance_cents for a in after.accounts}, dict(in_flight))
        seen = {e.provider_ref for e in store.list(CashEvent) if e.provider_ref}
        missing = [r.object_id for r in refs if r.object_type in ("withdrawal", "deposit") and r.object_id not in seen]
        by_pid = {b.provider_id: b for b in store.list(APBill) if b.provider_id}
        missing += [pid for pid, want in bill_checks if pid not in by_pid or by_pid[pid].due_date != want]
        if not recon.balanced or missing:
            state = "uncertain"
            notes.append(f"reconciliation: {recon.unexplained_cents} cents unexplained, "
                         f"{len(missing)} provider object(s) not visible after sync")
            next_step = "re-sync and inspect the account differences in `reconciliation`"
    ex = Execution(
        id=key, fix_id=fix.id, run_id=fix.run_id, approved_by=approval.approver_id, state=state, mode="sandbox",
        executed_at=utcnow(), cash_event_ids=event_ids, provider_refs=refs, snapshot_before=current.snapshot_id,
        snapshot_after=after.snapshot_id, notes=notes, reconciliation=recon, missing_provider_refs=missing,
        next_step=next_step,
        label="sandbox write confirmed by re-sync" if state == "confirmed" else "uncertain (see notes)")
    store.save(ex, actor=approval.approver_id, action="fix.executed" if state == "confirmed" else "fix.uncertain",
               correlation_id=fix.id)
    _audit(store, approval.approver_id, "fix.reconciled", ex)
    return ex
