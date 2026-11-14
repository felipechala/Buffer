"""Typed domain models for TreasuryTwin.

Money rule: every persisted amount is an integer number of cents (`*_cents`).
Dollar values only appear at the fixture boundary (converted with `to_cents`)
and for display (`fmt_usd`).

Time rule: time is a discrete daily bucket index. Day 0 is the snapshot date
("today"); day 1 is tomorrow, and so on up to `horizon_days - 1`.
"""
from __future__ import annotations

from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, model_validator


# ---------------------------------------------------------------- money utils
def to_cents(value: int | float | str | Decimal) -> int:
    """Convert a dollar amount to integer cents; reject sub-cent precision."""
    scaled = Decimal(str(value)) * 100
    cents = scaled.quantize(Decimal(1), rounding=ROUND_HALF_UP)
    if cents != scaled:
        raise ValueError(f"amount {value!r} has sub-cent precision")
    return int(cents)


def fee_cents(amount_cents: int, rate: float) -> int:
    """Fee on an amount, rounded half-up to the cent."""
    fee = Decimal(amount_cents) * Decimal(str(rate))
    return int(fee.quantize(Decimal(1), rounding=ROUND_HALF_UP))


def fmt_usd(cents: int) -> str:
    sign = "-" if cents < 0 else ""
    return f"{sign}${abs(cents) / 100:,.2f}"


class Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


DataSource = Literal["fixture", "nessie", "synthetic"]


# ------------------------------------------------------------- twin entities
class Account(Frozen):
    id: str
    name: str
    role: str = ""
    currency: Literal["USD"] = "USD"
    observed_balance_cents: int
    minimum_buffer_cents: int = Field(ge=0)
    source: DataSource = "fixture"
    nessie_id: str | None = None


class Obligation(Frozen):
    """A scheduled outgoing payment (assumption, not a posted bank event)."""
    id: str
    account_id: str
    amount_cents: int = Field(gt=0)
    due_day: int = Field(ge=0)
    priority: int = 1  # lower number is paid first within a day
    deferrable: bool = False
    counterparty: str = ""
    category: str = ""
    source: DataSource = "synthetic"


class ExpectedReceipt(Frozen):
    """An anticipated incoming payment. Never treated as posted cash."""
    id: str
    account_id: str
    amount_cents: int = Field(gt=0)
    expected_day: int = Field(ge=0)
    # extra days of delay -> probability. Must sum to 1.
    delay_distribution: dict[int, float] = Field(default_factory=lambda: {0: 1.0})
    correlation_key: str | None = None
    counterparty: str = ""
    source: DataSource = "synthetic"

    @model_validator(mode="after")
    def _check_distribution(self) -> "ExpectedReceipt":
        if not self.delay_distribution:
            raise ValueError(f"receipt {self.id}: empty delay distribution")
        if any(k < 0 for k in self.delay_distribution):
            raise ValueError(f"receipt {self.id}: negative delay")
        if any(p < 0 for p in self.delay_distribution.values()):
            raise ValueError(f"receipt {self.id}: negative probability")
        if abs(sum(self.delay_distribution.values()) - 1.0) > 1e-6:
            raise ValueError(f"receipt {self.id}: delay probabilities must sum to 1")
        return self


class TransferRoute(Frozen):
    id: str
    source_id: str
    destination_id: str
    max_amount_cents: int = Field(gt=0)  # cap per dispatch day
    settlement_days: int = Field(default=0, ge=0)
    fee_rate: float = Field(default=0.0, ge=0)
    allowed: bool = True
    note: str = ""

    @model_validator(mode="after")
    def _distinct_endpoints(self) -> "TransferRoute":
        if self.source_id == self.destination_id:
            raise ValueError(f"route {self.id}: source and destination are identical")
        return self


class CreditLine(Frozen):
    id: str
    account_id: str
    limit_cents: int = Field(ge=0)
    drawn_cents: int = Field(default=0, ge=0)
    draw_delay_days: int = Field(default=0, ge=0)
    fee_rate: float = Field(default=0.0, ge=0)  # one-time draw fee
    enabled: bool = True

    @property
    def available_cents(self) -> int:
        return max(0, self.limit_cents - self.drawn_cents)


class TwinState(Frozen):
    """Immutable, hashed snapshot of the company's financial state."""
    snapshot_id: str
    company_name: str
    as_of: date
    horizon_days: int = Field(ge=1, le=30)
    accounts: list[Account]
    obligations: list[Obligation] = []
    receipts: list[ExpectedReceipt] = []
    routes: list[TransferRoute] = []
    credit_lines: list[CreditLine] = []
    provenance: dict[str, str] = {}
    assumptions: list[str] = []

    @model_validator(mode="after")
    def _check_references(self) -> "TwinState":
        for label, items in (("account", self.accounts), ("obligation", self.obligations),
                             ("receipt", self.receipts), ("route", self.routes),
                             ("credit line", self.credit_lines)):
            ids = [i.id for i in items]
            dupes = {i for i in ids if ids.count(i) > 1}
            if dupes:
                raise ValueError(f"duplicate {label} ids: {sorted(dupes)}")
        known = {a.id for a in self.accounts}
        refs = ([(o.id, o.account_id) for o in self.obligations]
                + [(r.id, r.account_id) for r in self.receipts]
                + [(c.id, c.account_id) for c in self.credit_lines]
                + [(r.id, r.source_id) for r in self.routes]
                + [(r.id, r.destination_id) for r in self.routes])
        missing = [(owner, acct) for owner, acct in refs if acct not in known]
        if missing:
            raise ValueError(f"unknown account references: {missing}")
        return self

    def account(self, account_id: str) -> Account:
        return next(a for a in self.accounts if a.id == account_id)

    def route(self, route_id: str) -> TransferRoute | None:
        return next((r for r in self.routes if r.id == route_id), None)

    def credit_line(self, line_id: str) -> CreditLine | None:
        return next((c for c in self.credit_lines if c.id == line_id), None)


class TwinOverrides(Frozen):
    """User-adjustable constraints (UI sliders/toggles). Produces a new snapshot."""
    account_buffers_cents: dict[str, int] = {}
    route_allowed: dict[str, bool] = {}
    route_max_cents: dict[str, int] = {}
    route_settlement_days: dict[str, int] = {}
    credit_limit_cents: dict[str, int] = {}
    credit_enabled: dict[str, bool] = {}


# ------------------------------------------------------------------ scenarios
class UnexpectedOutflow(Frozen):
    account_id: str
    day: int = Field(ge=0)
    amount_cents: int = Field(gt=0)
    description: str = ""


class Scenario(Frozen):
    """A perturbation of the expected future. The empty scenario is the baseline."""
    id: str = "baseline"
    description: str = ""
    receipt_delays: dict[str, int] = {}       # receipt id -> extra delay days
    receipt_haircuts: dict[str, float] = {}   # receipt id -> fraction NOT received
    unexpected_outflows: list[UnexpectedOutflow] = []
    disabled_routes: list[str] = []
    disabled_credit_lines: list[str] = []
    seed: int | None = None

    @model_validator(mode="after")
    def _check_bounds(self) -> "Scenario":
        if any(d < 0 for d in self.receipt_delays.values()):
            raise ValueError("receipt delays must be >= 0")
        if any(not 0.0 <= h <= 1.0 for h in self.receipt_haircuts.values()):
            raise ValueError("receipt haircuts must be within [0, 1]")
        return self


# -------------------------------------------------------------------- actions
class TransferAction(Frozen):
    kind: Literal["transfer"] = "transfer"
    route_id: str
    day: int = Field(ge=0)
    amount_cents: int = Field(gt=0)


class CreditDrawAction(Frozen):
    kind: Literal["credit_draw"] = "credit_draw"
    credit_line_id: str
    day: int = Field(ge=0)
    amount_cents: int = Field(gt=0)


PlanAction = Annotated[Union[TransferAction, CreditDrawAction], Field(discriminator="kind")]


# ---------------------------------------------------------- simulation output
class AccountDay(BaseModel):
    account_id: str
    day: int
    opening_cents: int
    receipts_cents: int = 0
    transfer_arrivals_cents: int = 0
    financing_cents: int = 0
    transfer_out_cents: int = 0
    unexpected_out_cents: int = 0
    payments_cents: int = 0
    closing_cents: int = 0
    buffer_cents: int = 0
    shortfall_cents: int = 0  # max(0, buffer - closing)


class ObligationOutcome(BaseModel):
    obligation_id: str
    account_id: str
    due_day: int
    amount_cents: int
    deferrable: bool
    balance_before_cents: int
    balance_after_cents: int
    covered: bool          # balance before payment was >= amount
    buffer_breached: bool  # balance after payment fell below the account buffer


class RejectedAction(BaseModel):
    action: PlanAction
    reason: str


class BreachPoint(BaseModel):
    account_id: str
    day: int
    shortfall_cents: int


class SimMetrics(BaseModel):
    breach: bool
    first_breach: BreachPoint | None
    worst_shortfall_cents: int
    shortfall_cent_days: int            # sum over accounts/days of buffer shortfall
    uncovered_obligations: list[str]
    uncovered_mandatory_cents: int      # non-deferrable payments not covered
    fees_cents: int
    min_headroom_cents: int             # min over accounts/days of closing - buffer


class SimulationResult(BaseModel):
    scenario_id: str
    snapshot_id: str
    horizon_days: int
    ledger: list[AccountDay]
    obligations: list[ObligationOutcome]
    rejected_actions: list[RejectedAction]
    receipts_beyond_horizon: list[str]
    total_cash_by_day: list[int]     # account balances + in-transit, at close
    in_transit_by_day: list[int]
    metrics: SimMetrics


# ------------------------------------------------------------------- planning
class Violation(BaseModel):
    scenario_id: str = "baseline"
    account_id: str
    day: int
    shortfall_cents: int
    explanations: list[str]


class StressCheck(BaseModel):
    """Integer-cent replay of a plan under one scenario."""
    scenario_id: str
    breach: bool
    worst_shortfall_cents: int
    min_headroom_cents: int
    rejected_actions: int


class SolverInfo(BaseModel):
    method: str
    model_version: str
    status: int
    message: str
    runtime_ms: float
    n_variables: int
    n_constraints: int
    objective: float | None


class Plan(BaseModel):
    id: str
    snapshot_id: str
    scenario_id: str                 # primary scenario
    scenario_ids: list[str]          # primary + stress scenarios the plan must survive
    generated_at: datetime
    status: Literal["feasible", "infeasible", "verification_failed", "solver_error"]
    actionable: bool                 # only feasible plans may be approved / executed
    actions: list[PlanAction]
    transfer_total_cents: int
    credit_draw_total_cents: int
    modeled_cost_cents: int
    violations: list[Violation]
    binding_constraints: list[str]
    assumptions: list[str]
    solver: SolverInfo
    verification: SimMetrics         # primary scenario replay
    stress_checks: list[StressCheck]
