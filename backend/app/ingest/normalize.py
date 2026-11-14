"""Pure normalization: raw provider records + treasury overlay -> operational entities.

Conventions (shared with scripts/seed_nessie.py; see fixtures/nessie/schema_notes.md):
  * account nickname -> internal account id through `overlay.config.nessie_nicknames` (explicit, no guessing);
  * bill `nickname` = "<bill id>|<earliest pay date>|<state>" with state D(raft)/A(pproved)/S(cheduled);
    `payee` = vendor name (resolved to an overlay vendor), `payment_date` = due date;
  * deposit `description` = "INV-<n> <customer id> due YYYY-MM-DD";
  * pending deposits are expected receipts -> issued ARInvoice, NEVER cash;
  * completed deposits/withdrawals are posted CashEvents. They are `included_in_snapshot=True`
    (Nessie balances are static observed snapshots) except movements TreasuryTwin itself wrote
    ("TreasuryTwin plan_<id> ..."), which the static balance cannot reflect -> `included_in_snapshot=False`;
  * Nessie amounts/balances are whole dollars -> cents = dollars * 100.

Identity: every produced APBill/ARInvoice/CashEvent carries (provider, provider_id), the upsert key used by
`apply_batch`. Ids are deterministic so a double ingest cannot duplicate anything.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from ..connectors.base import RawRecord, RawSnapshot
from ..workflows.models import (
    APBill,
    ARInvoice,
    BankAccount,
    CashEvent,
    CompanyConfig,
    Customer,
    Vendor,
)
from .history import PaymentObservation, apply_history, build_history

PLAN_TAG = "TreasuryTwin plan_"
DESC_RE = re.compile(r"^\s*(INV-[A-Za-z0-9-]+)\s+(\S+)\s+due\s+(\d{4}-\d{2}-\d{2})\s*$")
STATE_STATUS = {"D": "draft", "A": "approved", "S": "scheduled"}
REDACT_KEYS = {"account_number", "customer_id", "_id", "id", "payer_id", "payee_id"}


class NormalizeError(ValueError):
    """Unknown or inconsistent provider schema. `sample` is a redacted copy of the offending record."""

    def __init__(self, tag: str, message: str, sample: dict[str, Any] | None = None):
        super().__init__(f"[{tag}] {message}")
        self.tag = tag
        self.sample = {k: ("<redacted>" if k in REDACT_KEYS else v) for k, v in (sample or {}).items()}


@dataclass
class NormalizedBatch:
    provider: str
    config: CompanyConfig
    customers: list[Customer]
    vendors: list[Vendor]
    bills: list[APBill] = field(default_factory=list)
    invoices: list[ARInvoice] = field(default_factory=list)
    cash_events: list[CashEvent] = field(default_factory=list)
    observations: list[PaymentObservation] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def counts(self) -> dict[str, int]:
        return {"accounts": len(self.config.accounts), "bills": len(self.bills),
                "invoices": len(self.invoices), "cash_events": len(self.cash_events)}


def _cents(value: Any, tag: str, rec: RawRecord) -> int:
    try:
        return int(round(float(value) * 100))
    except (TypeError, ValueError):
        raise NormalizeError(f"bad_{tag}", f"{rec.kind} {rec.provider_id}: amount {value!r} is not numeric",
                             rec.data) from None


def _date(value: Any, rec: RawRecord) -> date:
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        raise NormalizeError("bad_date", f"{rec.kind} {rec.provider_id}: date {value!r} is not YYYY-MM-DD",
                             rec.data) from None


def normalize(raw: RawSnapshot, overlay: dict[str, Any], *, use_history: bool = True,
              min_history_samples: int = 3) -> NormalizedBatch:
    cfg0 = CompanyConfig.model_validate(overlay["config"])
    provider = raw.provider if raw.provider in ("fixture", "nessie") else "nessie"
    warnings: list[str] = []
    customers = [Customer.model_validate(c) for c in overlay.get("customers", [])]
    vendors = [Vendor.model_validate(v) for v in overlay.get("vendors", [])]
    cust_by_id = {c.id: c for c in customers}
    vendor_by_name = {v.name.strip().lower(): v for v in vendors}

    # ---- accounts
    internal_by_nick = cfg0.nessie_nicknames
    overlay_acct = {a.id: a for a in cfg0.accounts}
    acct_internal: dict[str, str] = {}
    accounts: dict[str, BankAccount] = {}
    for rec in raw.accounts:
        nick = str(rec.data.get("nickname", ""))
        internal = internal_by_nick.get(nick)
        if internal is None or internal not in overlay_acct:
            warnings.append(f"account nickname {nick!r} is not in nessie_nicknames; ignored")
            continue
        if "balance" not in rec.data:
            raise NormalizeError("account_schema", f"account {rec.provider_id} has no balance", rec.data)
        base = overlay_acct[internal]
        acct_internal[rec.provider_id] = internal
        accounts[internal] = BankAccount(
            id=internal, name=base.name, role=base.role,
            observed_balance_cents=_cents(rec.data["balance"], "balance", rec), buffer_cents=base.buffer_cents,
            provider=provider, provider_id=rec.provider_id, as_of=rec.observed_at)  # type: ignore[arg-type]
    missing = [a for a in overlay_acct if a not in accounts]
    if missing:
        raise NormalizeError("missing_accounts",
                             f"provider has no account for internal ids {missing}; check nessie_nicknames")
    config = cfg0.model_copy(update={"accounts": [accounts[a.id] for a in cfg0.accounts]})
    terms = {c.id: c.payment_terms_days for c in customers}
    batch = NormalizedBatch(provider=provider, config=config, customers=customers, vendors=vendors,
                            warnings=warnings)
    nondeferrable = set(cfg0.policy.nondeferrable_categories)

    def internal_of(rec: RawRecord) -> str | None:
        return acct_internal.get(rec.account_provider_id or "")

    # ---- bills
    for rec in raw.bills:
        src = internal_of(rec)
        if src is None:
            continue
        d = rec.data
        if d.get("status") in ("cancelled", "canceled"):
            continue
        parts = str(d.get("nickname", "")).split("|")
        bill_id = parts[0].strip() or rec.provider_id
        due = _date(d.get("payment_date"), rec)
        earliest = due
        state = "A"
        if len(parts) >= 3:
            try:
                earliest = min(date.fromisoformat(parts[1]), due)
            except ValueError:
                warnings.append(f"bill {bill_id}: unparseable earliest date {parts[1]!r}; using due date")
            state = parts[2].strip().upper()
        else:
            warnings.append(f"bill {bill_id}: nickname has no metadata; treated as approved, earliest=due")
        status = "paid" if d.get("status") == "completed" else STATE_STATUS.get(state, "approved")
        vendor = vendor_by_name.get(str(d.get("payee", "")).strip().lower())
        if vendor is None:
            warnings.append(f"bill {bill_id}: payee {d.get('payee')!r} is not an overlay vendor; category 'other'")
        category = vendor.category if vendor else "other"
        batch.bills.append(APBill(
            id=bill_id, vendor_id=vendor.id if vendor else re.sub(r"\W+", "-", str(d.get("payee", "unknown")).lower()),
            amount_cents=_cents(d.get("payment_amount"), "amount", rec), category=category, due_date=due,
            earliest_pay_date=earliest, source_account_id=src, status=status,
            scheduled_date=due if status == "scheduled" else None, nondeferrable=category in nondeferrable,
            memo=f"Nessie bill {rec.provider_id}", created_by="nessie", provider=provider,  # type: ignore[arg-type]
            provider_id=rec.provider_id))

    # ---- deposits / withdrawals
    for rec in raw.deposits:
        dst = internal_of(rec)
        if dst is None:
            continue
        d = rec.data
        desc = str(d.get("description", ""))
        status = d.get("status")
        when = _date(d.get("transaction_date"), rec)
        cents = _cents(d.get("amount"), "amount", rec)
        own = desc.startswith(PLAN_TAG)
        m = DESC_RE.match(desc)
        if status == "completed":
            if cents <= 0:
                raise NormalizeError("bad_amount", f"deposit {rec.provider_id}: non-positive amount", d)
            batch.cash_events.append(CashEvent(
                id=f"nessie-dep-{rec.provider_id}", account_id=dst, signed_cents=cents, effective_date=when,
                kind="transfer_in" if own else "receipt", source="nessie", reference=m.group(1) if m else desc[:80],
                provider_ref=rec.provider_id, included_in_snapshot=not own))
            if m:
                inv_id, cust_id, due_s = m.groups()
                due = date.fromisoformat(due_s)
                batch.observations.append(PaymentObservation(cust_id, inv_id, due, when))
                batch.invoices.append(_invoice(inv_id, cust_id, cents, due, when, dst, "paid", rec, provider, terms))
        elif status == "pending":
            if not m:
                warnings.append(f"pending deposit {rec.provider_id} has no 'INV-... <customer> due DATE' "
                                "description; not forecast (never counted as cash)")
                continue
            inv_id, cust_id, due_s = m.groups()
            due = date.fromisoformat(due_s)
            if cust_id not in cust_by_id:
                warnings.append(f"invoice {inv_id}: customer {cust_id!r} is not an overlay customer")
            batch.invoices.append(_invoice(inv_id, cust_id, cents, due, None, dst, "issued", rec, provider, terms,
                                           expected=when, as_of=config.as_of))
        else:
            warnings.append(f"deposit {rec.provider_id} has status {status!r}; ignored")

    for rec in raw.withdrawals:
        src = internal_of(rec)
        if src is None:
            continue
        d = rec.data
        if d.get("status") != "completed":
            warnings.append(f"withdrawal {rec.provider_id} has status {d.get('status')!r}; ignored")
            continue
        desc = str(d.get("description", ""))
        own = desc.startswith(PLAN_TAG)
        batch.cash_events.append(CashEvent(
            id=f"nessie-wd-{rec.provider_id}", account_id=src, signed_cents=-_cents(d.get("amount"), "amount", rec),
            effective_date=_date(d.get("transaction_date"), rec), kind="transfer_out" if own else "bill_payment",
            source="nessie", reference=desc[:80], provider_ref=rec.provider_id, included_in_snapshot=not own))

    if raw.transfers:
        warnings.append(f"{len(raw.transfers)} Nessie transfer record(s) ignored: transfers move no money "
                        "(executed fixes are withdrawal+deposit pairs)")

    if use_history:
        batch.customers = apply_history(customers, build_history(batch.observations), min_history_samples)
    return batch


def _invoice(inv_id: str, cust_id: str, cents: int, due: date, paid: date | None, account: str, status: str,
             rec: RawRecord, provider: str, terms: dict[str, int], *, expected: date | None = None,
             as_of: date | None = None) -> ARInvoice:
    issue = due - timedelta(days=terms.get(cust_id, 30))
    expected = expected or due
    issue = min(issue, expected, due)
    if status == "issued" and as_of and expected < as_of:
        status = "overdue"
    return ARInvoice(id=inv_id, customer_id=cust_id, amount_cents=cents, issue_date=issue, due_date=due,
                     expected_receipt_date=expected, deposit_account_id=account, status=status,  # type: ignore[arg-type]
                     paid_date=paid, memo=f"Nessie deposit {rec.provider_id}", created_by="nessie",
                     provider=provider, provider_id=rec.provider_id)  # type: ignore[arg-type]

