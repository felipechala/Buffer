"""Public Buffer API (v3 naming). Thin wrappers: decisions live in services/, execution in actions/.

Every route requires `Authorization: Bearer <CASHGUARD_API_KEY>` (one demo tenant). Every
response carries enough provenance (snapshot id, seeds, engine version) to reproduce it.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from datetime import date, datetime, timedelta
from typing import Any, Literal

import numpy as np
from fastapi import APIRouter, BackgroundTasks, Depends, Header, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from ..actions.gateway import Execution, execute_fix
from ..config import CASHGUARD_API_KEY, CASHGUARD_DEMO_FIXTURE, SANDBOX_WRITES_ENABLED
from ..agent.runs import DEFAULT_BUDGET, StressRun, latest_run, queue_run, run_and_warm, warm_decisions
from ..connectors.base import ConnectorError
from ..connectors.fixture import FixtureConnector
from ..connectors.nessie import NessieConnector
from ..ingest.normalize import NormalizeError
from ..ingest.sync import save_error_sample, sync_connector
from ..engine.models import Scenario, SimMetrics, TwinState, fmt_usd
from ..engine.optimizer import MODEL_VERSION, optimize_plan
from ..engine.scenarios import HELDOUT_SEED_BASE, sample_scenarios
from ..engine.simulator import simulate
from ..services.decision import Decision, SimulationRequest, decide
from ..services.fixes import Fix
from ..services.explain import explain_record
from ..services.purchases import commit_purchase, demo_purchases
from ..services.stress import StressSet, get_stress_set, set_stress_set
from ..workflows.forecast import build_twin_from_store
from ..workflows.models import AnalysisRun, Approval, AuditEvent, Vendor, utcnow
from ..workflows.seed import seed_demo
from ..workflows.state import WorkflowError
from ..workflows.store import Store

router = APIRouter(prefix="/v1", tags=["cashguard"])


# ------------------------------------------------------------------ auth / deps
def require_token(authorization: str | None = Header(default=None)) -> None:
    if not authorization or not authorization.startswith("Bearer "):
        raise WorkflowError("unauthenticated", "Authorization: Bearer <api key> required", 401)
    if authorization.removeprefix("Bearer ").strip() != CASHGUARD_API_KEY:
        raise WorkflowError("unauthenticated", "invalid API key", 401)


def get_store(request: Request) -> Store:
    return request.app.state.store


# ------------------------------------------------------------------ models
class ConnectionHealth(BaseModel):
    provider: Literal["fixture", "nessie"]
    status: Literal["healthy", "configured", "unavailable", "error", "not_configured"]
    scope: str
    last_sync_at: datetime | None
    detail: str = ""


class ConnectionsResponse(BaseModel):
    mode: Literal["fixture", "nessie"]
    connections: list[ConnectionHealth]
    snapshot_id: str | None
    writes_enabled: bool


class SyncResponse(BaseModel):
    sync_id: str
    provider: str
    snapshot_id: str
    source_count: dict[str, int]
    synced_at: datetime
    stress_run_id: str | None
    warnings: list[str]
    agent_run_id: str | None = None      # set when the watcher woke the treasury agent
    agent_alerts: list[str] = []


class AccountState(BaseModel):
    id: str
    name: str
    role: str
    observed_balance_cents: int
    buffer_cents: int
    provider: str
    provider_id: str | None
    as_of: datetime
    min_headroom_cents: int | None = None
    min_headroom_date: date | None = None


class ObligationState(BaseModel):
    id: str
    account_id: str
    amount_cents: int
    due_day: int
    due_date: date
    counterparty: str
    category: str
    nondeferrable: bool
    payee_name: str = ""


class ReceiptState(BaseModel):
    id: str
    account_id: str
    amount_cents: int
    expected_day: int
    expected_date: date
    counterparty: str
    delay_distribution: dict[int, float]
    posted: bool = False


class TwinStateResponse(BaseModel):
    snapshot_id: str
    company: str
    as_of: date
    horizon_days: int
    accounts: list[AccountState]
    obligations: list[ObligationState]
    receipts: list[ReceiptState]
    routes: list[dict[str, Any]]
    credit_lines: list[dict[str, Any]]
    provenance: dict[str, str]
    assumptions: list[str]


class ForecastRequest(BaseModel):
    scenario: Scenario | None = None
    scenario_count: int = Field(default=200, ge=10, le=2000)
    seed: int = 42


class AccountForecast(BaseModel):
    account_id: str
    buffer_cents: int
    dates: list[date]
    baseline: list[int]
    p10: list[int]
    p50: list[int]
    p90: list[int]
    worst_stress: list[int]
    worst_stress_id: str


class ForecastResponse(BaseModel):
    snapshot_id: str
    horizon_days: int
    accounts: list[AccountForecast]
    baseline_metrics: SimMetrics
    stress_set_id: str
    provenance: dict[str, Any]


class CounterfactualResponse(BaseModel):
    """`POST /v1/twin/simulations` with a scenario and no proposed action."""
    snapshot_id: str
    scenario: Scenario
    before: dict[str, list[int]]
    after: dict[str, list[int]]
    before_metrics: SimMetrics
    after_metrics: SimMetrics
    constraint_report: list[str]
    provenance: dict[str, Any]


class StressRunResponse(BaseModel):
    run_id: str
    snapshot_id: str
    status: Literal["queued", "running", "completed", "failed"]
    source: str
    scenarios: list[Scenario]
    findings: list[dict[str, Any]]
    trace: list[dict[str, Any]]
    provenance: dict[str, Any]
    # additive fields from the adaptive search (older clients ignore them)
    elites: list[dict[str, Any]] = []
    adaptive: dict[str, Any] = {}
    random_baseline: dict[str, Any] = {}
    seed: int | None = None
    budget: int | None = None
    runtime_ms: int | None = None
    error: str | None = None
    mode: str = "deterministic"
    agent: str = "none"
    agent_reason: str = ""
    narrative: list[dict[str, Any]] = []
    agent_meta: dict[str, Any] = {}


class StressRequest(BaseModel):
    budget: int = Field(default=DEFAULT_BUDGET, ge=10, le=1000)
    seed: int = Field(default=0, ge=0)
    force: bool = False
    mode: Literal["deterministic", "deepagent"] = "deterministic"


class PlanRequest(BaseModel):
    scenario: Scenario = Scenario()
    stress_ids: list[str] = []
    bill_pay_days: dict[str, int] = {}


class ApproveRequest(BaseModel):
    approved_by: str
    memo: str = ""


# ------------------------------------------------------------------ helpers
def _curve(result) -> dict[str, list[int]]:
    out: dict[str, list[int]] = {}
    for row in result.ledger:
        out.setdefault(row.account_id, []).append(row.closing_cents)
    return out


def _sync_id(provider: str, snapshot_id: str) -> str:
    return "sync_" + hashlib.sha256(f"{provider}:{snapshot_id}:{utcnow().isoformat()}".encode()).hexdigest()[:12]


def _stress_run_from_set(stress: StressSet, twin: TwinState) -> StressRunResponse:
    findings = []
    for s in stress.scenarios:
        r = simulate(twin, s)
        findings.append({"scenario_id": s.id, "label": s.description or s.id, "breach": r.metrics.breach,
                         "worst_shortfall_cents": r.metrics.worst_shortfall_cents,
                         "first_breach": r.metrics.first_breach.model_dump() if r.metrics.first_breach else None})
    return StressRunResponse(run_id=stress.id, snapshot_id=stress.snapshot_id, status="completed",
                             source=stress.source, scenarios=stress.scenarios, findings=findings, trace=[],
                             provenance={"engine_version": MODEL_VERSION, "snapshot_id": stress.snapshot_id})


# ------------------------------------------------------------------ connections
def _nessie(request: Request) -> NessieConnector:
    """The app-wide Nessie connector (tests inject `app.state.nessie_connector`)."""
    conn = getattr(request.app.state, "nessie_connector", None)
    if conn is None:
        conn = request.app.state.nessie_connector = NessieConnector()
    return conn


def _cached_health(request: Request, ttl_s: float = 30.0):
    """Health checks hit the network; cache them briefly so the UI badge can poll cheaply."""
    cached = getattr(request.app.state, "nessie_health", None)
    if cached is None or time.monotonic() - cached[0] > ttl_s:
        cached = request.app.state.nessie_health = (time.monotonic(), _nessie(request).health())
    return cached[1]


def _current_mode(s: Store) -> str:
    if not s.has_config():
        return "fixture"
    return "nessie" if any(a.provider == "nessie" for a in s.get_config().accounts) else "fixture"


@router.get("/connections", response_model=ConnectionsResponse, dependencies=[Depends(require_token)])
def connections(request: Request, s: Store = Depends(get_store)) -> ConnectionsResponse:
    twin = build_twin_from_store(s) if s.has_config() else None
    cfg = s.get_config() if s.has_config() else None
    mode = _current_mode(s)
    last = max((a.as_of for a in cfg.accounts), default=None) if cfg else None
    fixture_health = FixtureConnector().health()
    conns = [ConnectionHealth(provider="fixture", status="healthy", scope="fixtures/cashguard_demo.json",
                              last_sync_at=last if mode == "fixture" else None,
                              detail="synthetic demo company" if fixture_health.ok else "fixture files missing")]
    h = _cached_health(request)
    conns.append(ConnectionHealth(
        provider="nessie",
        status=("healthy" if h.ok else "not_configured" if h.status == "not_configured" else "unavailable"
                if h.status == "unavailable" else "error"),
        scope=h.scope, last_sync_at=last if mode == "nessie" else None,
        detail=(h.detail + "; " if h.detail else "") + "writes " + ("enabled" if SANDBOX_WRITES_ENABLED else "disabled")))
    return ConnectionsResponse(mode=mode, connections=conns, snapshot_id=twin.snapshot_id if twin else None,
                               writes_enabled=SANDBOX_WRITES_ENABLED)


@router.post("/connections/fixture/sync", response_model=SyncResponse, dependencies=[Depends(require_token)])
def sync_fixture(background: BackgroundTasks, reset: bool = False, s: Store = Depends(get_store)) -> SyncResponse:
    """reset=true reloads the demo company and wipes approvals/executions/audit. reset=false (default) keeps
    them: it only seeds an empty store (or one currently showing another provider) and otherwise re-reads
    the current store, including the gateway's simulated cash events, into a fresh snapshot."""
    demo_name = json.loads(CASHGUARD_DEMO_FIXTURE.read_text(encoding="utf-8"))["config"]["name"]
    if reset or not s.has_config() or s.get_config().name != demo_name             or any(a.provider != "fixture" for a in s.get_config().accounts):
        seed_demo(s, CASHGUARD_DEMO_FIXTURE, reset=True)
    twin = build_twin_from_store(s)
    cfg = s.get_config()
    run = _kick_stress(s, background, twin.snapshot_id)
    stress = get_stress_set(twin, cfg.risk_tolerance)
    agent_id, alerts = (None, []) if reset else _agent_watch(s, background)
    return SyncResponse(sync_id=_sync_id("fixture", twin.snapshot_id), provider="fixture",
                        agent_run_id=agent_id, agent_alerts=alerts,
                        snapshot_id=twin.snapshot_id,
                        source_count={"accounts": len(twin.accounts), "obligations": len(twin.obligations),
                                      "receipts": len(twin.receipts)},
                        synced_at=utcnow(), stress_run_id=run.id if run else stress.id,
                        warnings=["Fixture mode: balances and flows come from the synthetic demo company."])


@router.post("/connections/nessie/sync", response_model=SyncResponse, dependencies=[Depends(require_token)])
def sync_nessie(request: Request, background: BackgroundTasks, s: Store = Depends(get_store)):
    """Pull the company customer from Nessie into the store. Any failure leaves the last snapshot untouched."""
    connector = _nessie(request)

    def failed(kind: str, message: str, status: int = 503) -> JSONResponse:
        snap = build_twin_from_store(s).snapshot_id if s.has_config() else None
        return JSONResponse(status_code=status, content={
            "error": {"type": kind, "code": kind, "message": message},
            "mode": _current_mode(s), "snapshot_id": snap,
            "detail": "Nessie sync failed; the last snapshot is unchanged."})

    health = connector.health()
    request.app.state.nessie_health = (time.monotonic(), health)
    if not health.ok:
        return failed("connector_unavailable", f"nessie {health.status}: {health.detail}")
    try:
        outcome = sync_connector(s, connector)
    except ConnectorError as exc:
        return failed("connector_unavailable", str(exc))
    except NormalizeError as exc:
        return failed("normalize_error", f"{exc} (redacted sample: {save_error_sample(exc)})", 502)
    twin = build_twin_from_store(s)
    run = _kick_stress(s, background, twin.snapshot_id)
    stress = get_stress_set(twin, s.get_config().risk_tolerance)
    agent_id, alerts = _agent_watch(s, background)
    return SyncResponse(sync_id=_sync_id("nessie", twin.snapshot_id), provider="nessie", snapshot_id=twin.snapshot_id,
                        agent_run_id=agent_id, agent_alerts=alerts,
                        source_count={**outcome.batch.counts(), "obligations": len(twin.obligations),
                                      "receipts": len(twin.receipts)},
                        synced_at=utcnow(), stress_run_id=run.id if run else stress.id, warnings=outcome.batch.warnings)


# ------------------------------------------------------------------ twin
@router.get("/twin/state", response_model=TwinStateResponse, dependencies=[Depends(require_token)])
def twin_state(s: Store = Depends(get_store)) -> TwinStateResponse:
    twin = build_twin_from_store(s)
    cfg = s.get_config()
    acct_meta = {a.id: a for a in cfg.accounts}
    vendor_names = {v.id: v.name for v in s.list(Vendor)}
    base = simulate(twin, Scenario())
    low: dict[str, tuple[int, date]] = {}
    buf = {a.id: a.minimum_buffer_cents for a in twin.accounts}
    for r in base.ledger:
        h = r.closing_cents - buf[r.account_id]
        d = twin.as_of + timedelta(days=r.day)
        if r.account_id not in low or h < low[r.account_id][0]:
            low[r.account_id] = (h, d)
    return TwinStateResponse(
        snapshot_id=twin.snapshot_id, company=twin.company_name, as_of=twin.as_of, horizon_days=twin.horizon_days,
        accounts=[AccountState(id=a.id, name=a.name, role=a.role, observed_balance_cents=a.observed_balance_cents,
                               buffer_cents=a.minimum_buffer_cents, provider=acct_meta[a.id].provider,
                               provider_id=acct_meta[a.id].provider_id, as_of=acct_meta[a.id].as_of,
                               min_headroom_cents=low.get(a.id, (None, None))[0],
                               min_headroom_date=low.get(a.id, (None, None))[1])
                  for a in twin.accounts],
        obligations=[ObligationState(id=o.id, account_id=o.account_id, amount_cents=o.amount_cents, due_day=o.due_day,
                                     due_date=twin.as_of + timedelta(days=o.due_day), counterparty=o.counterparty,
                                     category=o.category, nondeferrable=not o.deferrable,
                                     payee_name=vendor_names.get(o.counterparty, o.counterparty))
                     for o in sorted(twin.obligations, key=lambda o: (o.due_day, o.id))],
        receipts=[ReceiptState(id=r.id, account_id=r.account_id, amount_cents=r.amount_cents,
                               expected_day=r.expected_day, expected_date=twin.as_of + timedelta(days=r.expected_day),
                               counterparty=r.counterparty, delay_distribution=r.delay_distribution,
                               posted=r.id.startswith("posted:"))
                  for r in sorted(twin.receipts, key=lambda r: (r.expected_day, r.id))],
        routes=[r.model_dump(mode="json") for r in twin.routes],
        credit_lines=[c.model_dump(mode="json") for c in twin.credit_lines],
        provenance=twin.provenance, assumptions=twin.assumptions)


@router.post("/twin/forecasts", response_model=ForecastResponse, dependencies=[Depends(require_token)])
def forecasts(req: ForecastRequest, s: Store = Depends(get_store)) -> ForecastResponse:
    twin = build_twin_from_store(s)
    cfg = s.get_config()
    scenario = req.scenario or Scenario()
    baseline = simulate(twin, scenario)
    samples = sample_scenarios(twin, req.scenario_count, seed=HELDOUT_SEED_BASE + req.seed)
    runs = [simulate(twin, sc) for sc in samples]
    stress = get_stress_set(twin, cfg.risk_tolerance)
    stress_runs = [(sc, simulate(twin, sc)) for sc in stress.scenarios]
    worst_sc, worst = min(stress_runs, key=lambda sr: sr[1].metrics.min_headroom_cents) if stress_runs \
        else (Scenario(), baseline)
    H = twin.horizon_days
    dates = [twin.as_of + timedelta(days=d) for d in range(H)]
    accounts = []
    for a in twin.accounts:
        base = [r.closing_cents for r in baseline.ledger if r.account_id == a.id]
        matrix = np.array([[r.closing_cents for r in run.ledger if r.account_id == a.id] for run in runs])
        accounts.append(AccountForecast(
            account_id=a.id, buffer_cents=a.minimum_buffer_cents, dates=dates, baseline=base,
            p10=[int(x) for x in np.percentile(matrix, 10, axis=0)],
            p50=[int(x) for x in np.percentile(matrix, 50, axis=0)],
            p90=[int(x) for x in np.percentile(matrix, 90, axis=0)],
            worst_stress=[r.closing_cents for r in worst.ledger if r.account_id == a.id],
            worst_stress_id=worst_sc.id))
    return ForecastResponse(snapshot_id=twin.snapshot_id, horizon_days=H, accounts=accounts,
                            baseline_metrics=baseline.metrics, stress_set_id=stress.id,
                            provenance={"engine_version": MODEL_VERSION, "scenario_seed": HELDOUT_SEED_BASE + req.seed,
                                        "paths": req.scenario_count, "scenario": scenario.id,
                                        "note": "Percentiles are frequencies under synthetic delay distributions."})


class ExplanationResponse(BaseModel):
    run_id: str
    text: str
    source: Literal["llm", "template"]
    model: str | None = None
    latency_ms: int


@router.get("/runs/{run_id}/explanation", response_model=ExplanationResponse, dependencies=[Depends(require_token)])
def run_explanation(run_id: str, s: Store = Depends(get_store)) -> ExplanationResponse:
    """Grounded LLM explanation of a decision run, computed on first request and cached by input digest."""
    run = s.get(AnalysisRun, run_id)
    if run is None or not isinstance(run.result, dict) or "provenance" not in run.result:
        raise WorkflowError("not_found", f"run {run_id} not found", 404)
    exp = explain_record(run.result)
    return ExplanationResponse(run_id=run_id, text=exp.text, source=exp.source, model=exp.model,
                               latency_ms=exp.latency_ms)


@router.post("/twin/simulations", response_model=Decision | CounterfactualResponse,
             dependencies=[Depends(require_token)])
def simulations(req: SimulationRequest, s: Store = Depends(get_store)):
    if req.proposed_action is not None:
        return decide(s, req)
    if req.scenario is None:
        raise WorkflowError("invalid_request", "provide proposed_action and/or scenario", 422)
    twin = build_twin_from_store(s)
    if req.snapshot_id and req.snapshot_id != twin.snapshot_id:
        raise WorkflowError("stale_snapshot", f"current snapshot is {twin.snapshot_id}", 409)
    before = simulate(twin)
    after = simulate(twin, req.scenario)
    report = []
    if after.metrics.first_breach:
        fb = after.metrics.first_breach
        report.append(f"{fb.account_id} breaches its buffer on day {fb.day} "
                      f"({(twin.as_of + timedelta(days=fb.day)).isoformat()}) by {fmt_usd(fb.shortfall_cents)}.")
        plan = optimize_plan(twin, req.scenario)
        report.append(f"Funding plan under this scenario: {plan.status}.")
        for v in plan.violations:
            report.extend(v.explanations)
    return CounterfactualResponse(snapshot_id=twin.snapshot_id, scenario=req.scenario, before=_curve(before),
                                  after=_curve(after), before_metrics=before.metrics, after_metrics=after.metrics,
                                  constraint_report=report,
                                  provenance={"engine_version": MODEL_VERSION, "snapshot_id": twin.snapshot_id})


# ------------------------------------------------------------------ stress tests
def _run_response(run: StressRun) -> StressRunResponse:
    return StressRunResponse(
        run_id=run.id, snapshot_id=run.snapshot_id, status=run.status,  # type: ignore[arg-type]
        source=run.source, scenarios=run.scenarios, findings=run.findings, trace=run.trace,
        provenance={"engine_version": MODEL_VERSION, "snapshot_id": run.snapshot_id, "seed": run.seed,
                    "budget": run.budget, "stress_set_id": run.stress_set_id, "probes": run.probes},
        elites=[e.model_dump(mode="json") for e in run.elites], adaptive=run.adaptive,
        random_baseline=run.random_baseline, seed=run.seed, budget=run.budget, runtime_ms=run.runtime_ms,
        error=run.error, mode=run.mode, agent=run.agent, agent_reason=run.agent_reason,
        narrative=run.narrative, agent_meta=run.agent_meta)


def _kick_stress(s: Store, background: BackgroundTasks, snapshot_id: str, **kw: Any) -> StressRun | None:
    """Queue (once per snapshot) and schedule the search + warm-up. Disabled with CASHGUARD_AUTO_STRESS=0."""
    if os.getenv("CASHGUARD_AUTO_STRESS", "1") == "0":
        return None
    kw.setdefault("budget", int(os.getenv("CASHGUARD_STRESS_BUDGET", DEFAULT_BUDGET)))
    run, needs = queue_run(s, snapshot_id, **kw)
    if needs:
        background.add_task(run_and_warm, s, run.id)
    elif run.status == "completed" and run.stress_set_id:      # e.g. after a restart: re-register the set
        set_stress_set(StressSet(id=run.stress_set_id, snapshot_id=run.snapshot_id, source="risk_search",
                                 scenarios=run.scenarios))
    return run


@router.post("/twin/stress-tests", status_code=202, dependencies=[Depends(require_token)])
def start_stress_test(background: BackgroundTasks, req: StressRequest | None = None,
                      s: Store = Depends(get_store)) -> dict[str, str]:
    """Runs the adaptive risk search in the background; poll GET /v1/twin/stress-tests/{run_id}."""
    req = req or StressRequest()
    twin = build_twin_from_store(s)
    run, needs = queue_run(s, twin.snapshot_id, seed=req.seed, budget=req.budget, force=req.force, mode=req.mode)
    if needs:
        if req.mode == "deepagent":
            from ..agent.deep import run_deep_and_warm
            background.add_task(run_deep_and_warm, s, run.id)
        else:
            background.add_task(run_and_warm, s, run.id)
    return {"run_id": run.id, "status": run.status, "snapshot_id": twin.snapshot_id, "mode": run.mode}


@router.get("/twin/stress-tests/{run_id}", response_model=StressRunResponse, dependencies=[Depends(require_token)])
def get_stress_test(run_id: str, wait_s: float = 25.0, s: Store = Depends(get_store)) -> StressRunResponse:
    """`wait_s` lets simple clients call POST then GET: a queued/running run is awaited up to that long."""
    twin = build_twin_from_store(s)
    cfg = s.get_config()
    if run_id.startswith("stress-default-"):       # the pre-search default set, kept for old clients
        stress = get_stress_set(twin, cfg.risk_tolerance)
        if run_id != stress.id:
            raise WorkflowError("not_found", f"stress run {run_id} not found for the current snapshot", 404)
        return _stress_run_from_set(stress, twin)
    run = latest_run(s, twin.snapshot_id) if run_id == "latest" else s.get(StressRun, run_id)
    if run is None:
        raise WorkflowError("not_found", f"stress run {run_id} not found", 404)
    deadline = time.monotonic() + max(0.0, min(wait_s, 120.0))
    while run.status in ("queued", "running") and time.monotonic() < deadline:
        time.sleep(0.2)
        run = s.get(StressRun, run.id) or run
    return _run_response(run)


# ------------------------------------------------------------------ plans / fixes
@router.post("/twin/plans", dependencies=[Depends(require_token)])
def create_plan(req: PlanRequest, s: Store = Depends(get_store)):
    twin = build_twin_from_store(s, bill_pay_days=req.bill_pay_days)
    cfg = s.get_config()
    stress_set = get_stress_set(build_twin_from_store(s), cfg.risk_tolerance)
    stress = [sc for sc in stress_set.scenarios if not req.stress_ids or sc.id in req.stress_ids]
    plan = optimize_plan(twin, req.scenario, stress=stress)
    s.save(plan, actor="engine", action="plan.create")
    return plan


@router.get("/plans/{fix_id}", response_model=Fix, dependencies=[Depends(require_token)])
def get_fix(fix_id: str, s: Store = Depends(get_store)) -> Fix:
    fix = s.get(Fix, fix_id)
    if fix is None:
        raise WorkflowError("not_found", f"fix {fix_id} not found", 404)
    return fix


@router.post("/plans/{fix_id}/approve", response_model=Approval, dependencies=[Depends(require_token)])
def approve_fix(fix_id: str, req: ApproveRequest, s: Store = Depends(get_store),
                idempotency_key: str | None = Header(default=None, alias="Idempotency-Key")) -> Approval:
    fix = get_fix(fix_id, s)
    cfg = s.get_config()
    user = cfg.user(req.approved_by)
    if user is None or user.role != "finance_lead":
        raise WorkflowError("forbidden", "approved_by must be a finance lead of this company", 403)
    if not fix.verified:
        raise WorkflowError("not_actionable", "only verified fixes can be approved", 409)
    if fix.snapshot_id != build_twin_from_store(s).snapshot_id:
        raise WorkflowError("stale_fix", "financial state changed since this fix was computed; re-run the check", 409)
    approval_id = f"appr-{idempotency_key}" if idempotency_key else f"appr-{fix.id}"
    existing = s.get(Approval, approval_id)
    if existing is not None:
        return existing
    approval = Approval(id=approval_id, subject="fix", subject_id=fix.id, approver_id=user.id, role=user.role,
                        decision="approved", timestamp=utcnow(), memo=req.memo)
    s.save(approval, actor=user.id, action="approval.fix", correlation_id=fix.id)
    return approval


@router.post("/plans/{fix_id}/execute-sandbox", response_model=Execution, dependencies=[Depends(require_token)])
def execute_sandbox(fix_id: str, request: Request, s: Store = Depends(get_store),
                    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key")) -> Execution:
    """Simulated unless SANDBOX_WRITES_ENABLED=true and a write connector exists (`app.state.sandbox_connector`,
    default: the Nessie connector). Sandbox mode re-syncs Nessie after the writes and reconciles."""
    connector = getattr(request.app.state, "sandbox_connector", None)
    if connector is None and SANDBOX_WRITES_ENABLED:
        connector = _nessie(request)
    return execute_fix(s, fix_id, idempotency_key=idempotency_key or "", connector=connector,
                       resync=lambda: sync_connector(s, _nessie(request)))


# ------------------------------------------------------------------ events
@router.get("/events", response_model=list[AuditEvent], dependencies=[Depends(require_token)])
def events(entity_id: str | None = None, limit: int = 200, s: Store = Depends(get_store)) -> list[AuditEvent]:
    return s.audit(limit=limit, entity_id=entity_id)


@router.get("/runs/{run_id}", response_model=AnalysisRun, dependencies=[Depends(require_token)])
def get_run(run_id: str, s: Store = Depends(get_store)) -> AnalysisRun:
    run = s.get(AnalysisRun, run_id)
    if run is None:
        raise WorkflowError("not_found", f"run {run_id} not found", 404)
    return run


# ------------------------------------------------------------------ purchase checks (demo 2)
class CommitRequest(BaseModel):
    run_id: str
    fix_id: str | None = None
    pay_date: date | None = None


@router.get("/purchases/demo", dependencies=[Depends(require_token)])
def purchases_demo() -> dict[str, list[dict[str, Any]]]:
    return {"requests": demo_purchases()}


@router.post("/purchases/commit", dependencies=[Depends(require_token)])
def purchases_commit(req: CommitRequest, request: Request, background: BackgroundTasks,
                     s: Store = Depends(get_store),
                     idempotency_key: str | None = Header(default=None, alias="Idempotency-Key")) -> dict[str, Any]:
    """Commit a checked new purchase as a scheduled bill (optionally executing a funding fix in the same call)."""
    connector = getattr(request.app.state, "sandbox_connector", None)
    if connector is None and SANDBOX_WRITES_ENABLED:
        connector = _nessie(request)
    out = commit_purchase(s, run_id=req.run_id, fix_id=req.fix_id, pay_date=req.pay_date,
                          idempotency_key=idempotency_key or "", connector=connector,
                          resync=lambda: sync_connector(s, _nessie(request)))
    if os.getenv("CASHGUARD_AUTO_STRESS", "1") != "0":       # re-warm the engine caches for the new snapshot
        background.add_task(warm_decisions, s)
    return out


# ------------------------------------------------------------------ treasury agent (deepagents)
class AgentStartRequest(BaseModel):
    goal: str = "weekly_payment_run"
    note: str | None = None          # optional instruction from the user, passed to the agent as a trigger


class AgentApproveRequest(BaseModel):
    approved_by: str


class AgentRejectRequest(BaseModel):
    rejected_by: str
    reason: str = ""


def _agent_watch(s: Store, background: BackgroundTasks, force: bool = False) -> tuple[str | None, list[str]]:
    """Run the watcher after a sync; wake the agent on new alerts (CASHGUARD_AGENT_AUTO=0 disables)."""
    from ..agent import ops
    if not (force or ops.auto_enabled()):
        return None, []
    try:
        fresh, run, needs = ops.watch(s)
    except Exception:  # noqa: BLE001 - the watcher must never break a sync
        return None, []
    if needs and run is not None:
        background.add_task(ops.execute_run, s, run.id)
    return (run.id if run else None), [a.message for a in fresh]


def _agent_connector(request: Request):
    connector = getattr(request.app.state, "sandbox_connector", None)
    if connector is None and SANDBOX_WRITES_ENABLED:
        connector = _nessie(request)
    return connector


@router.post("/agent/runs", status_code=202, dependencies=[Depends(require_token)])
def agent_start(background: BackgroundTasks, req: AgentStartRequest | None = None,
                s: Store = Depends(get_store)) -> dict[str, Any]:
    """Start the treasury deepagent in the background; poll GET /v1/agent/runs/{run_id}."""
    from ..agent import ops
    req = req or AgentStartRequest()
    trigger = [f"Requested by the user: {req.note}"] if req.note else ["Requested by the user"]
    run, needs = ops.start_run(s, trigger=trigger, goal=req.goal)
    if needs:
        background.add_task(ops.execute_run, s, run.id)
    return {"run_id": run.id, "status": run.status}


@router.get("/agent/runs", dependencies=[Depends(require_token)])
def agent_runs(limit: int = 20, s: Store = Depends(get_store)) -> list[dict[str, Any]]:
    from ..agent import ops
    return [{"id": r.id, "status": r.status, "parent_id": r.parent_id, "started_at": r.started_at,
             "trigger": r.trigger, "net_benefit_cents": r.proposal.net_benefit_cents if r.proposal else None}
            for r in ops.list_runs(s)[:limit]]


@router.get("/agent/runs/{run_id}", dependencies=[Depends(require_token)])
def agent_run(run_id: str, s: Store = Depends(get_store)):
    from ..agent import ops
    run = ops.get_run(s, run_id)
    if run is None:
        raise WorkflowError("not_found", f"agent run {run_id} not found", 404)
    return run


@router.post("/agent/runs/{run_id}/approve", dependencies=[Depends(require_token)])
def agent_approve(run_id: str, req: AgentApproveRequest, request: Request, background: BackgroundTasks,
                  s: Store = Depends(get_store)):
    """Finance-lead approval: schedules the bills and executes the funding through the gateway (Nessie sandbox
    writes when enabled), then re-syncs and reconciles. Idempotent per run."""
    from ..agent import ops
    run = ops.approve_run(s, run_id, approved_by=req.approved_by, connector=_agent_connector(request),
                          resync=lambda: sync_connector(s, _nessie(request)))
    if os.getenv("CASHGUARD_AUTO_STRESS", "1") != "0":
        background.add_task(warm_decisions, s)
    return run


@router.post("/agent/runs/{run_id}/reject", dependencies=[Depends(require_token)])
def agent_reject(run_id: str, req: AgentRejectRequest, background: BackgroundTasks,
                 s: Store = Depends(get_store)) -> dict[str, Any]:
    """Reject with a reason: the reason becomes a learned preference and a revision run starts."""
    from ..agent import ops
    run, rev, needs = ops.reject_run(s, run_id, rejected_by=req.rejected_by, reason=req.reason)
    if needs and rev is not None:
        background.add_task(ops.execute_run, s, rev.id)
    return {"run_id": run.id, "status": run.status, "revision_run_id": rev.id if rev else None,
            "preference": ops.preferences(s)[-1] if ops.preferences(s) else None}


@router.get("/agent/preferences", dependencies=[Depends(require_token)])
def agent_preferences(s: Store = Depends(get_store)):
    from ..agent import ops
    return ops.preferences(s)


@router.delete("/agent/preferences", dependencies=[Depends(require_token)])
def agent_preferences_clear(s: Store = Depends(get_store)) -> dict[str, int]:
    from ..agent import ops
    prefs = ops.preferences(s)
    for p in prefs:
        s.delete(ops.AgentPreference, p.id, actor="u_maya", action="agent.preference.forgotten")
    return {"deleted": len(prefs)}


class AgentDemoEvent(BaseModel):
    kind: Literal["late_payment", "new_bill"]


@router.post("/agent/watch", dependencies=[Depends(require_token)])
def agent_watch(background: BackgroundTasks, s: Store = Depends(get_store)) -> dict[str, Any]:
    """Scan for changes the payment run should react to; wakes the agent if any are new."""
    from ..agent import ops
    run_id, alerts = _agent_watch(s, background, force=True)
    return {"run_id": run_id, "new_alerts": alerts, "all_alerts": [a.model_dump() for a in ops.scan(s)]}


@router.post("/agent/demo-events", dependencies=[Depends(require_token)])
def agent_demo_event(req: AgentDemoEvent, background: BackgroundTasks, s: Store = Depends(get_store)) -> dict[str, Any]:
    """Demo: change the world (a customer pays late / a new bill lands), then let the watcher react."""
    from ..agent import ops
    what = ops.inject_demo_event(s, req.kind)
    run_id, alerts = _agent_watch(s, background, force=True)
    return {"event": what, "run_id": run_id, "new_alerts": alerts}


@router.get("/ml/payment-timing", dependencies=[Depends(require_token)])
def ml_payment_timing(s: Store = Depends(get_store)) -> dict[str, Any]:
    """Learned payment-timing model: model card (holdout log-loss vs the per-customer assumption) and
    per-invoice lateness predictions. Advisory; trained mostly on synthetic history (see the card)."""
    from ..ml.payment_timing import summary
    return summary(s)
