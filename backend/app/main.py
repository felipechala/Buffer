"""FastAPI composition. Routes are thin: business rules live in workflows/, engine/, services/."""
from __future__ import annotations

from datetime import date, datetime
from typing import Any

from fastapi import Depends, FastAPI, Header, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .api import v1
from .config import DATABASE_PATH
from .engine.models import Plan, PlanAction, Scenario, SimulationResult, TwinState
from .engine.optimizer import optimize_plan
from .engine.scenarios import adverse_scenario
from .engine.simulator import simulate
from .services.impact import ImpactRequest, ImpactResult, analyze_bill_impact
from .workflows import ap, ar
from .workflows.forecast import build_twin_from_store
from .workflows.models import (
    AnalysisRun,
    APBill,
    Approval,
    ARInvoice,
    AuditEvent,
    CashEvent,
    CompanyConfig,
    Customer,
    PaymentIntent,
    User,
    Vendor,
    utcnow,
)
from .workflows.policy import PolicyResult
from .workflows.seed import seed_demo
from .workflows.state import WorkflowError
from .workflows.store import Store


class Decision(BaseModel):
    memo: str = ""


class ScheduleRequest(BaseModel):
    pay_date: date
    plan_id: str | None = None


class MarkPaid(BaseModel):
    paid_date: date
    provider_ref: str | None = None


class ScenarioRunRequest(BaseModel):
    scenario: Scenario = Scenario()
    actions: list[PlanAction] = []
    bill_pay_days: dict[str, int] = {}


class PlanRequest(BaseModel):
    scenario: Scenario = Scenario()
    stress: list[Scenario] = []
    include_adverse: bool = True
    risk_tolerance: float = Field(default=0.05, gt=0, lt=0.5)
    bill_pay_days: dict[str, int] = {}


class BillDetail(BaseModel):
    bill: APBill
    vendor: Vendor | None
    policy: PolicyResult
    approvals: list[Approval]
    payments: list[PaymentIntent]


class Forecast(BaseModel):
    twin: TwinState
    baseline: SimulationResult


def create_app(store: Store | None = None) -> FastAPI:
    store = store or Store(DATABASE_PATH)
    if not store.has_config():
        seed_demo(store)

    app = FastAPI(title="TreasuryTwin", version="0.2.0",
                  description="Financial operations with a predictive digital twin. "
                              "Sandbox prototype; synthetic data; not financial advice.")
    app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
    app.state.store = store

    @app.exception_handler(WorkflowError)
    async def _workflow_error(_: Request, exc: WorkflowError) -> JSONResponse:
        return JSONResponse(status_code=exc.status,
                            content={"error": {"type": exc.code, "code": exc.code, "message": exc.message}})

    app.include_router(v1.router)

    def get_store() -> Store:
        return store

    def current_user(x_user_id: str | None = Header(default=None)) -> User:
        """Simulated authentication: the UI's user switcher sends X-User-Id."""
        if not x_user_id:
            raise WorkflowError("unauthenticated", "X-User-Id header required", 401)
        user = store.get_config().user(x_user_id)
        if user is None:
            raise WorkflowError("unauthenticated", f"unknown user {x_user_id}", 401)
        return user

    # ------------------------------------------------------------- company
    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/api/company", response_model=CompanyConfig)
    def company(s: Store = Depends(get_store)) -> CompanyConfig:
        return s.get_config()

    @app.get("/api/users", response_model=list[User])
    def users(s: Store = Depends(get_store)) -> list[User]:
        return s.get_config().users

    @app.get("/api/accounts")
    def accounts(s: Store = Depends(get_store)) -> list[dict[str, Any]]:
        cfg = s.get_config()
        twin = build_twin_from_store(s)
        opening = {a.id: a.observed_balance_cents for a in twin.accounts}
        return [{**a.model_dump(mode="json"), "available_today_cents": opening[a.id],
                 "label": "observed (Nessie)" if a.provider == "nessie" else "observed (fixture)"}
                for a in cfg.accounts]

    @app.post("/api/demo/reset")
    def demo_reset(s: Store = Depends(get_store)) -> dict[str, str]:
        seed_demo(s)
        return {"status": "reset"}

    # ---------------------------------------------------------- receivables
    @app.get("/api/customers", response_model=list[Customer])
    def list_customers(s: Store = Depends(get_store)) -> list[Customer]:
        return s.list(Customer)

    @app.post("/api/customers", response_model=Customer, status_code=201)
    def new_customer(req: ar.CustomerCreate, user: User = Depends(current_user),
                     s: Store = Depends(get_store)) -> Customer:
        return ar.create_customer(s, user, req)

    @app.get("/api/invoices", response_model=list[ARInvoice])
    def list_invoices(s: Store = Depends(get_store)) -> list[ARInvoice]:
        return s.list(ARInvoice)

    @app.post("/api/invoices", response_model=ARInvoice, status_code=201)
    def new_invoice(req: ar.InvoiceCreate, user: User = Depends(current_user),
                    s: Store = Depends(get_store)) -> ARInvoice:
        return ar.create_invoice(s, user, req)

    @app.patch("/api/invoices/{invoice_id}/dates", response_model=ARInvoice)
    def invoice_dates(invoice_id: str, req: ar.InvoiceDates, user: User = Depends(current_user),
                      s: Store = Depends(get_store)) -> ARInvoice:
        return ar.update_invoice_dates(s, user, invoice_id, req)

    @app.post("/api/invoices/{invoice_id}/issue", response_model=ARInvoice)
    def invoice_issue(invoice_id: str, user: User = Depends(current_user),
                      s: Store = Depends(get_store)) -> ARInvoice:
        return ar.issue_invoice(s, user, invoice_id)

    @app.post("/api/invoices/{invoice_id}/void", response_model=ARInvoice)
    def invoice_void(invoice_id: str, user: User = Depends(current_user),
                     s: Store = Depends(get_store)) -> ARInvoice:
        return ar.void_invoice(s, user, invoice_id)

    @app.post("/api/invoices/{invoice_id}/mark-paid", response_model=ARInvoice)
    def invoice_paid(invoice_id: str, req: MarkPaid, user: User = Depends(current_user),
                     s: Store = Depends(get_store)) -> ARInvoice:
        return ar.mark_invoice_paid(s, user, invoice_id, req.paid_date, req.provider_ref)

    # ------------------------------------------------------------- payables
    @app.get("/api/vendors", response_model=list[Vendor])
    def list_vendors(s: Store = Depends(get_store)) -> list[Vendor]:
        return s.list(Vendor)

    @app.post("/api/vendors", response_model=Vendor, status_code=201)
    def new_vendor(req: ap.VendorCreate, user: User = Depends(current_user),
                   s: Store = Depends(get_store)) -> Vendor:
        return ap.create_vendor(s, user, req)

    @app.get("/api/bills", response_model=list[APBill])
    def list_bills(s: Store = Depends(get_store)) -> list[APBill]:
        return s.list(APBill)

    @app.post("/api/bills", response_model=APBill, status_code=201)
    def new_bill(req: ap.BillCreate, user: User = Depends(current_user),
                 s: Store = Depends(get_store)) -> APBill:
        return ap.create_bill(s, user, req)

    @app.get("/api/bills/{bill_id}", response_model=BillDetail)
    def bill_detail(bill_id: str, s: Store = Depends(get_store)) -> BillDetail:
        bill = ap.get_bill(s, bill_id)
        return BillDetail(
            bill=bill, vendor=s.get(Vendor, bill.vendor_id), policy=ap.bill_policy(s, bill_id),
            approvals=[a for a in s.list(Approval) if a.subject == "bill" and a.subject_id == bill_id],
            payments=[p for p in s.list(PaymentIntent) if p.bill_id == bill_id])

    @app.post("/api/bills/{bill_id}/submit", response_model=APBill)
    def bill_submit(bill_id: str, user: User = Depends(current_user),
                    s: Store = Depends(get_store)) -> APBill:
        return ap.submit_bill(s, user, bill_id)

    @app.post("/api/bills/{bill_id}/approve", response_model=APBill)
    def bill_approve(bill_id: str, req: Decision, user: User = Depends(current_user),
                     s: Store = Depends(get_store)) -> APBill:
        return ap.decide_bill(s, user, bill_id, "approved", req.memo)

    @app.post("/api/bills/{bill_id}/reject", response_model=APBill)
    def bill_reject(bill_id: str, req: Decision, user: User = Depends(current_user),
                    s: Store = Depends(get_store)) -> APBill:
        return ap.decide_bill(s, user, bill_id, "rejected", req.memo)

    @app.post("/api/bills/{bill_id}/schedule", response_model=PaymentIntent)
    def bill_schedule(bill_id: str, req: ScheduleRequest, user: User = Depends(current_user),
                      s: Store = Depends(get_store)) -> PaymentIntent:
        return ap.schedule_bill(s, user, bill_id, req.pay_date, req.plan_id)

    @app.post("/api/bills/{bill_id}/impact", response_model=ImpactResult)
    def bill_impact(bill_id: str, req: ImpactRequest, s: Store = Depends(get_store)) -> ImpactResult:
        result = analyze_bill_impact(s, bill_id, req)
        run = AnalysisRun(id=result.run_id, kind="bill_impact", created_at=utcnow(),
                          result=result.model_dump(mode="json"))
        s.save(run, actor="engine", action="run.bill_impact", correlation_id=bill_id)
        return result

    @app.get("/api/payments", response_model=list[PaymentIntent])
    def list_payments(s: Store = Depends(get_store)) -> list[PaymentIntent]:
        return s.list(PaymentIntent)

    @app.post("/api/payments/{payment_id}/complete-simulated", response_model=PaymentIntent)
    def payment_complete(payment_id: str, user: User = Depends(current_user),
                         s: Store = Depends(get_store)) -> PaymentIntent:
        return ap.complete_payment_simulated(s, user, payment_id)

    @app.get("/api/cash-events", response_model=list[CashEvent])
    def cash_events(s: Store = Depends(get_store)) -> list[CashEvent]:
        return s.list(CashEvent)

    # ------------------------------------------------------- twin / engine
    @app.get("/api/forecast", response_model=Forecast)
    def forecast(s: Store = Depends(get_store)) -> Forecast:
        twin = build_twin_from_store(s)
        return Forecast(twin=twin, baseline=simulate(twin))

    @app.post("/api/scenarios/run", response_model=SimulationResult)
    def run_scenario(req: ScenarioRunRequest, s: Store = Depends(get_store)) -> SimulationResult:
        twin = build_twin_from_store(s, bill_pay_days=req.bill_pay_days)
        return simulate(twin, req.scenario, req.actions)

    @app.post("/api/plans", response_model=Plan)
    def create_plan(req: PlanRequest, s: Store = Depends(get_store)) -> Plan:
        twin = build_twin_from_store(s, bill_pay_days=req.bill_pay_days)
        stress = list(req.stress)
        if req.include_adverse:
            stress.append(adverse_scenario(twin, 1 - req.risk_tolerance))
        plan = optimize_plan(twin, req.scenario, stress=stress)
        s.save(plan, actor="engine", action="plan.create")
        return plan

    @app.get("/api/plans/{plan_id}", response_model=Plan)
    def get_plan(plan_id: str, s: Store = Depends(get_store)) -> Plan:
        plan = s.get(Plan, plan_id)
        if plan is None:
            raise WorkflowError("not_found", f"plan {plan_id} not found", 404)
        return plan

    @app.post("/api/plans/{plan_id}/approve", response_model=Approval)
    def approve_plan(plan_id: str, req: Decision, user: User = Depends(current_user),
                     s: Store = Depends(get_store)) -> Approval:
        plan = get_plan(plan_id, s)
        if user.role != "finance_lead":
            raise WorkflowError("forbidden", "only a finance lead can approve a funding plan", 403)
        if not plan.actionable:
            raise WorkflowError("not_actionable", f"plan status is {plan.status}; it cannot be approved")
        if plan.snapshot_id != build_twin_from_store(s).snapshot_id:
            raise WorkflowError("stale_plan", "financial state changed since this plan was computed; replan")
        approval = Approval(id=f"appr-{plan.id}", subject="plan", subject_id=plan.id,
                            approver_id=user.id, role=user.role, decision="approved",
                            timestamp=utcnow(), memo=req.memo)
        s.save(approval, actor=user.id, action="approval.plan", correlation_id=plan.id)
        return approval

    @app.get("/api/runs/{run_id}", response_model=AnalysisRun)
    def get_run(run_id: str, s: Store = Depends(get_store)) -> AnalysisRun:
        run = s.get(AnalysisRun, run_id)
        if run is None:
            raise WorkflowError("not_found", f"run {run_id} not found", 404)
        return run

    @app.get("/api/audit", response_model=list[AuditEvent])
    def audit(entity_id: str | None = None, limit: int = 200,
              s: Store = Depends(get_store)) -> list[AuditEvent]:
        return s.audit(limit=limit, entity_id=entity_id)

    return app


app = create_app()
