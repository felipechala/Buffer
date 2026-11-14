"""FastAPI routes. Business logic lives in twin/simulator/planner; this layer only wires it."""
from __future__ import annotations

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from .config import DEFAULT_FIXTURE
from .planner import optimize_plan
from .schemas import Plan, PlanAction, Scenario, SimulationResult, TwinOverrides, TwinState
from .simulator import simulate
from .twin import apply_overrides, load_fixture

app = FastAPI(title="TreasuryTwin", version="0.1.0",
              description="Sandbox-only treasury digital twin. Synthetic data; not financial advice.")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# In-memory stores (immutable snapshots keyed by content hash).
SNAPSHOTS: dict[str, TwinState] = {}
PLANS: dict[str, Plan] = {}


def _get_snapshot(snapshot_id: str) -> TwinState:
    twin = SNAPSHOTS.get(snapshot_id)
    if twin is None:
        raise HTTPException(404, f"unknown snapshot {snapshot_id}")
    return twin


class SyncRequest(BaseModel):
    fixture: str = DEFAULT_FIXTURE
    overrides: TwinOverrides | None = None


class SyncResponse(BaseModel):
    snapshot_id: str
    counts: dict[str, int]
    warnings: list[str]


class ScenarioRunRequest(BaseModel):
    snapshot_id: str
    scenario: Scenario = Scenario()
    actions: list[PlanAction] = []
    horizon_days: int | None = Field(default=None, ge=1, le=30)


class OptimizeRequest(BaseModel):
    snapshot_id: str
    scenario: Scenario = Scenario()
    horizon_days: int | None = Field(default=None, ge=1, le=30)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/v1/twin/sync", response_model=SyncResponse)
def sync_twin(req: SyncRequest) -> SyncResponse:
    if "/" in req.fixture or "\\" in req.fixture or ".." in req.fixture:
        raise HTTPException(400, "fixture must be a bare name")
    try:
        twin = load_fixture(req.fixture)
    except FileNotFoundError:
        raise HTTPException(404, f"unknown fixture {req.fixture}")
    if req.overrides:
        twin = apply_overrides(twin, req.overrides)
    SNAPSHOTS[twin.snapshot_id] = twin
    return SyncResponse(
        snapshot_id=twin.snapshot_id,
        counts={"accounts": len(twin.accounts), "obligations": len(twin.obligations),
                "receipts": len(twin.receipts), "routes": len(twin.routes),
                "credit_lines": len(twin.credit_lines)},
        warnings=["Nessie not connected yet: all values come from the synthetic fixture."],
    )


@app.post("/v1/twin/{snapshot_id}/overrides", response_model=SyncResponse)
def override_twin(snapshot_id: str, overrides: TwinOverrides) -> SyncResponse:
    twin = apply_overrides(_get_snapshot(snapshot_id), overrides)
    SNAPSHOTS[twin.snapshot_id] = twin
    return SyncResponse(snapshot_id=twin.snapshot_id, counts={}, warnings=[])


@app.get("/v1/twin/{snapshot_id}", response_model=TwinState)
def get_twin(snapshot_id: str) -> TwinState:
    return _get_snapshot(snapshot_id)


@app.post("/v1/scenarios/run", response_model=SimulationResult)
def run_scenario(req: ScenarioRunRequest) -> SimulationResult:
    return simulate(_get_snapshot(req.snapshot_id), req.scenario, req.actions, req.horizon_days)


@app.post("/v1/plans/optimize", response_model=Plan)
def optimize(req: OptimizeRequest) -> Plan:
    plan = optimize_plan(_get_snapshot(req.snapshot_id), req.scenario, req.horizon_days)
    PLANS[plan.id] = plan
    return plan


@app.get("/v1/plans/{plan_id}", response_model=Plan)
def get_plan(plan_id: str) -> Plan:
    plan = PLANS.get(plan_id)
    if plan is None:
        raise HTTPException(404, f"unknown plan {plan_id}")
    return plan
