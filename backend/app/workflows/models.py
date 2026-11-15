"""Operational (what happened / what is scheduled) domain models.

Three kinds of state are kept separate (v2 Section 05):
  * observed  - bank balances at a snapshot + posted CashEvents
  * planned   - invoices, bills, approvals, payment intents
  * hypothetical - scenarios and plans (engine models, never stored as facts)
Money is integer cents; dates are calendar dates, timestamps are UTC.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

from ..engine.models import CreditLine, TransferRoute

Role = Literal["finance_lead", "ap_specialist", "ar_specialist", "viewer"]
InvoiceStatus = Literal["draft", "issued", "paid", "overdue", "void"]
BillStatus = Literal["draft", "pending", "approved", "scheduled", "paid", "rejected", "failed"]
PaymentStatus = Literal["proposed", "approved", "submitted", "confirmed", "canceled", "uncertain", "failed"]


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class User(BaseModel):
    id: str
    name: str
    role: Role


class BankAccount(BaseModel):
    """Observed account balance at a snapshot. Never inferred credit."""
    id: str
    name: str
    role: str = ""
    observed_balance_cents: int
    buffer_cents: int = Field(ge=0)
    provider: Literal["fixture", "nessie"] = "fixture"
    provider_id: str | None = None
    as_of: datetime


class Policy(BaseModel):
    approval_threshold_cents: int = 1_000_000          # bills above $10k need a finance lead
    budgets_cents: dict[str, int] = {}                 # category -> monthly budget
    nondeferrable_categories: list[str] = ["payroll", "tax"]


class CompanyConfig(BaseModel):
    name: str
    as_of: date                                        # day 0 of the forecast
    horizon_days: int = Field(default=7, ge=1, le=30)
    accounts: list[BankAccount]
    routes: list[TransferRoute] = []
    credit_lines: list[CreditLine] = []
    policy: Policy = Policy()
    users: list[User] = []
    assumptions: list[str] = []
    # CashGuard treasury overlay (customer-specified assumptions, never provider facts).
    risk_tolerance: float = Field(default=0.05, gt=0, lt=0.5)
    shock_bounds: dict[str, Any] = {}          # bounds for the risk-search shock library
    nessie_nicknames: dict[str, str] = {}      # Nessie account nickname -> internal account id

    def user(self, user_id: str) -> User | None:
        return next((u for u in self.users if u.id == user_id), None)

    def account(self, account_id: str) -> BankAccount | None:
        return next((a for a in self.accounts if a.id == account_id), None)


class Customer(BaseModel):
    id: str
    name: str
    payment_terms_days: int = 30
    # Synthetic collection-timing assumption: extra days late -> probability.
    delay_distribution: dict[int, float] = {0: 1.0}
    correlation_key: str | None = None


class Vendor(BaseModel):
    id: str
    name: str
    category: str
    payment_terms_days: int = 30
    # Early-payment terms (e.g. "2/2 net 30"): pct off when paid within `early_pay_days` of the bill's
    # earliest pay date. Synthetic treasury overlay, not a provider fact.
    early_pay_discount_pct: float = Field(default=0.0, ge=0, lt=0.2)
    early_pay_days: int = Field(default=0, ge=0)


class ARInvoice(BaseModel):
    id: str
    customer_id: str
    amount_cents: int = Field(gt=0)
    issue_date: date
    due_date: date
    expected_receipt_date: date
    deposit_account_id: str
    status: InvoiceStatus = "draft"
    paid_date: date | None = None
    memo: str = ""
    created_by: str = "system"
    provider: Literal["fixture", "nessie"] | None = None   # None = app-local
    provider_id: str | None = None                          # upsert key with `provider`

    @model_validator(mode="after")
    def _dates(self) -> "ARInvoice":
        if self.due_date < self.issue_date:
            raise ValueError("due_date must be on or after issue_date")
        if self.expected_receipt_date < self.issue_date:
            raise ValueError("expected_receipt_date must be on or after issue_date")
        return self


class APBill(BaseModel):
    id: str
    vendor_id: str
    amount_cents: int = Field(gt=0)
    category: str
    due_date: date
    earliest_pay_date: date
    source_account_id: str
    status: BillStatus = "draft"
    scheduled_date: date | None = None
    nondeferrable: bool = False
    required_role: Role | None = None    # set by policy on submission
    memo: str = ""
    created_by: str = "system"
    provider: Literal["fixture", "nessie"] | None = None   # None = app-local
    provider_id: str | None = None                          # upsert key with `provider`

    @model_validator(mode="after")
    def _window(self) -> "APBill":
        if self.earliest_pay_date > self.due_date:
            raise ValueError("earliest_pay_date must be on or before due_date")
        return self


class Approval(BaseModel):
    id: str
    subject: Literal["bill", "plan", "fix"]
    subject_id: str
    approver_id: str
    role: Role
    decision: Literal["approved", "rejected"]
    timestamp: datetime
    memo: str = ""


class PaymentIntent(BaseModel):
    """A schedule, not a posted payment."""
    id: str
    bill_id: str
    source_account_id: str
    amount_cents: int = Field(gt=0)
    scheduled_date: date
    status: PaymentStatus = "proposed"
    idempotency_key: str
    plan_id: str | None = None
    provider_ref: str | None = None


class CashEvent(BaseModel):
    """A posted (observed or simulated-posted) bank movement."""
    id: str
    account_id: str
    signed_cents: int
    effective_date: date
    kind: Literal["receipt", "bill_payment", "transfer_out", "transfer_in", "credit_draw", "fee"]
    source: Literal["app_simulated", "nessie", "fixture"]
    reference: str = ""                     # invoice / bill / payment id
    provider_ref: str | None = None         # dedupe key against provider events
    included_in_snapshot: bool = False      # already reflected in observed balance?


class AnalysisRun(BaseModel):
    """A persisted engine run (impact analysis, simulation decision, stress search)."""
    id: str
    kind: str
    created_at: datetime
    result: dict[str, Any]


class AuditEvent(BaseModel):
    seq: int | None = None
    timestamp: datetime
    actor: str
    action: str
    entity: str
    entity_id: str
    before: dict[str, Any] | None = None
    after: dict[str, Any] | None = None
    correlation_id: str = ""
