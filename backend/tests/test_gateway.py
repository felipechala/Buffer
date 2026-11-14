"""Sandbox execution gateway + reconciliation, fully offline (mock connector / stateful mock Nessie)."""
import json
import re
import uuid
from datetime import date

import httpx
import pytest
from fastapi.testclient import TestClient

import app.actions.gateway as gateway
import app.api.v1 as v1
from app.actions.gateway import Execution, execute_fix
from app.actions.reconcile import reconcile
from app.config import CASHGUARD_API_KEY, CASHGUARD_DEMO_FIXTURE
from app.connectors.base import (
    ConnectorTimeout,
    ConnectorUnavailable,
    NonWholeDollarAmount,
    PartialTransferError,
    ProviderRef,
)
from app.connectors.nessie import NessieClient, NessieConnector
from app.main import create_app
from app.services.decision import SimulationRequest, decide
from app.services.fixes import Fix, TransferFix
from app.workflows.models import APBill, Approval, CashEvent, utcnow
from app.workflows.seed import seed_demo
from app.workflows.state import WorkflowError
from app.workflows.store import Store

AUTH = {"Authorization": f"Bearer {CASHGUARD_API_KEY}"}
NW = {"proposed_action": {"bill_id": "BILL-NW-4471", "pay_date": "2026-11-20"}}
CUST = "cust-1"


# ---------------------------------------------------------------- mock write connector (no HTTP)
class MockConnector:
    provider = "mock"

    def __init__(self, transfer_error: Exception | None = None):
        self.transfers: list[tuple] = []
        self.reschedules: list[tuple] = []
        self.transfer_error = transfer_error

    def create_transfer(self, src, dst, amount_cents, date, description, *, arrival_date=None):
        self.transfers.append((src, dst, amount_cents, date, description, arrival_date))
        if self.transfer_error is not None:
            raise self.transfer_error
        n = len(self.transfers)
        return [ProviderRef(provider="mock", object_type="withdrawal", object_id=f"wd-{n}"),
                ProviderRef(provider="mock", object_type="deposit", object_id=f"dep-{n}")]

    def schedule_bill(self, bill_provider_id, payment_date):
        self.reschedules.append((bill_provider_id, payment_date))
        return [ProviderRef(provider="mock", object_type="bill", object_id=bill_provider_id)]


def linked_store() -> Store:
    """Fixture company whose accounts/bills carry provider ids, so sandbox preflight passes."""
    s = Store()
    seed_demo(s, CASHGUARD_DEMO_FIXTURE)
    cfg = s.get_config()
    s.set_config(cfg.model_copy(update={"accounts": [a.model_copy(update={"provider_id": f"pid-{a.id}"})
                                                     for a in cfg.accounts]}), actor="test")
    return s


def approved_fix(s: Store, kind: str = "funding") -> Fix:
    d = decide(s, SimulationRequest(**NW))
    fix = next(f for f in d.fixes if f.kind == kind)
    s.save(Approval(id=f"appr-{fix.id}", subject="fix", subject_id=fix.id, approver_id="u_maya",
                    role="finance_lead", decision="approved", timestamp=utcnow()), actor="u_maya", action="approval.fix")
    return fix


def sandbox_run(s, fix, key, connector, **kw):
    return execute_fix(s, fix.id, idempotency_key=key, connector=connector, writes_enabled=True,
                       resync=kw.pop("resync", lambda: None), **kw)


# ---------------------------------------------------------------- gateway rules
def test_idempotent_replay_writes_once_and_disabled_mode_is_simulated():
    s = linked_store()
    fix = approved_fix(s)
    conn = MockConnector()
    first = sandbox_run(s, fix, "k1", conn)
    n = len(conn.transfers)
    assert n >= 1 and first.mode == "sandbox"
    replay = sandbox_run(s, fix, "k1", conn)
    assert replay == first and len(conn.transfers) == n
    with pytest.raises(WorkflowError) as e:
        execute_fix(s, fix.id, idempotency_key="", connector=conn, writes_enabled=True)
    assert e.value.status == 400


def test_writes_disabled_is_simulated_and_never_touches_the_connector():
    s = linked_store()
    fix = approved_fix(s)
    conn = MockConnector()
    ex = execute_fix(s, fix.id, idempotency_key="k-sim", connector=conn, writes_enabled=False)
    assert ex.state == "simulated" and ex.mode == "simulated" and ex.provider_refs == []
    assert "SIMULATED" in ex.notes[0] and "simulated" in ex.label and conn.transfers == []
    assert len(ex.cash_event_ids) == 2          # unchanged simulated behaviour: out + in events


def test_stale_snapshot_is_409_before_any_write():
    s = linked_store()
    fix = approved_fix(s)
    s.save(CashEvent(id="drift", account_id="operating", signed_cents=100_00, effective_date=date(2026, 11, 20),
                     kind="receipt", source="app_simulated"), actor="t", action="cashevent.post")
    conn = MockConnector()
    with pytest.raises(WorkflowError) as e:
        sandbox_run(s, fix, "k-stale", conn)
    assert e.value.status == 409 and e.value.code == "stale_fix" and conn.transfers == []


def test_not_approved_is_403():
    s = linked_store()
    d = decide(s, SimulationRequest(**NW))
    with pytest.raises(WorkflowError) as e:
        sandbox_run(s, d.fixes[0], "k-na", MockConnector())
    assert e.value.status == 403


def test_timeout_is_uncertain_persisted_and_never_retried():
    s = linked_store()
    fix = approved_fix(s)
    conn = MockConnector(transfer_error=ConnectorTimeout("read timed out"))
    ex = sandbox_run(s, fix, "k-to", conn)
    assert ex.state == "uncertain" and "sync" in ex.next_step and len(conn.transfers) == 1
    assert s.get(Execution, "k-to").state == "uncertain"
    assert sandbox_run(s, fix, "k-to", conn) == ex and len(conn.transfers) == 1      # replay: stored, no write
    with pytest.raises(WorkflowError) as e:                                            # new key: blocked
        sandbox_run(s, fix, "k-to-2", conn)
    assert e.value.status == 409 and e.value.code == "execution_uncertain" and len(conn.transfers) == 1
    assert "fix.uncertain" in {a.action for a in s.audit(limit=500)}


def test_partial_transfer_is_uncertain_with_partial_ref():
    s = linked_store()
    fix = approved_fix(s)
    leg1 = ProviderRef(provider="mock", object_type="withdrawal", object_id="wd-orphan")
    conn = MockConnector(transfer_error=PartialTransferError("deposit leg failed", [leg1]))
    ex = sandbox_run(s, fix, "k-part", conn)
    assert ex.state == "uncertain" and [r.object_id for r in ex.provider_refs] == ["wd-orphan"]
    assert any("deposit leg failed" in n for n in ex.notes)


def test_clean_rejection_writes_nothing_and_stores_nothing():
    s = linked_store()
    fix = approved_fix(s)
    conn = MockConnector(transfer_error=ConnectorUnavailable("HTTP 400"))
    with pytest.raises(WorkflowError) as e:
        sandbox_run(s, fix, "k-rej", conn)
    assert e.value.status == 502 and s.get(Execution, "k-rej") is None


def test_non_whole_dollar_transfer_is_refused_before_any_write():
    s = linked_store()
    fix = approved_fix(s)
    bad = fix.model_copy(update={"actions": [a.model_copy(update={"amount_cents": a.amount_cents + 50})
                                             if isinstance(a, TransferFix) else a for a in fix.actions]})
    s.save(bad, actor="t", action="fix.create")
    conn = MockConnector()
    with pytest.raises(WorkflowError) as e:
        sandbox_run(s, bad, "k-cents", conn)
    assert e.value.code == "amount_not_whole_dollars" and conn.transfers == []


# ---------------------------------------------------------------- reconcile()
def test_reconcile_credits_in_flight_and_flags_the_rest():
    ok = reconcile({"a": 90, "b": 150}, {"a": 90, "b": 100}, {"b": 50})     # +50 still settling into b
    assert ok.balanced and ok.unexplained_cents == 0 and ok.in_flight_cents == 50
    bad = reconcile({"a": 90, "b": 150}, {"a": 80, "b": 100}, {"b": 50})
    assert not bad.balanced and bad.unexplained_cents == 10
    assert next(r for r in bad.accounts if r.account_id == "a").unexplained_cents == -10


# ---------------------------------------------------------------- NessieConnector writes over MockTransport
def nessie_over(handler) -> NessieConnector:
    client = NessieClient("https://nessie.test", "secret-key", client=httpx.Client(transport=httpx.MockTransport(handler)),
                          sleep=lambda s: None, max_retries=3)
    return NessieConnector(client, customer_id=CUST)


def test_nessie_transfer_is_withdrawal_plus_deposit_with_whole_dollars():
    seen = []

    def handler(req):
        seen.append((req.method, req.url.path, json.loads(req.content)))
        return httpx.Response(201, json={"code": 201, "objectCreated": {"_id": f"obj-{len(seen)}"}})
    refs = nessie_over(handler).create_transfer("A1", "B2", 40_000_00, date(2026, 11, 20),
                                                "TreasuryTwin plan_x moved", arrival_date=date(2026, 11, 21))
    assert [(m, p) for m, p, _ in seen] == [("POST", "/accounts/A1/withdrawals"), ("POST", "/accounts/B2/deposits")]
    assert seen[0][2] == {"medium": "balance", "transaction_date": "2026-11-20", "status": "completed",
                          "amount": 40000, "description": "TreasuryTwin plan_x moved"}
    assert seen[1][2]["transaction_date"] == "2026-11-21" and seen[1][2]["amount"] == 40000
    assert [(r.object_type, r.object_id) for r in refs] == [("withdrawal", "obj-1"), ("deposit", "obj-2")]


def test_nessie_refuses_cents_without_any_http_call():
    calls = []
    c = nessie_over(lambda r: calls.append(r) or httpx.Response(201))
    with pytest.raises(NonWholeDollarAmount):
        c.create_transfer("A", "B", 1050, date(2026, 11, 20), "d")
    assert calls == []


def test_nessie_write_timeout_is_not_retried():
    calls = []

    def handler(req):
        calls.append(req)
        raise httpx.ReadTimeout("slow", request=req)
    with pytest.raises(ConnectorTimeout) as e:
        nessie_over(handler).create_transfer("A", "B", 100_00, date(2026, 11, 20), "d")
    assert len(calls) == 1 and "secret-key" not in str(e.value)
    calls.clear()
    with pytest.raises(ConnectorTimeout):
        nessie_over(handler).schedule_bill("bill-1", date(2026, 11, 25))
    assert len(calls) == 1


def test_nessie_second_leg_failure_is_partial_and_not_retried():
    calls = []

    def handler(req):
        calls.append(req.url.path)
        if req.url.path.endswith("/withdrawals"):
            return httpx.Response(201, json={"objectCreated": {"_id": "wd-1"}})
        return httpx.Response(503, text="down")
    with pytest.raises(PartialTransferError) as e:
        nessie_over(handler).create_transfer("A", "B", 100_00, date(2026, 11, 20), "d")
    assert [r.object_id for r in e.value.refs] == ["wd-1"] and len(calls) == 2


def test_nessie_schedule_bill_puts_payment_date():
    seen = []

    def handler(req):
        seen.append((req.method, req.url.path, json.loads(req.content)))
        return httpx.Response(202, json={"code": 202, "message": "Accepted bill modification"})
    refs = nessie_over(handler).schedule_bill("bill-9", date(2026, 11, 25))
    assert seen == [("PUT", "/bills/bill-9", {"payment_date": "2026-11-25"})] and refs[0].object_id == "bill-9"


# ---------------------------------------------------------------- full round trip over a stateful mock Nessie
class MockNessie:
    """Just enough of the Nessie sandbox: balance never moves; deposits/withdrawals are plain records."""

    def __init__(self):
        raw = json.loads(CASHGUARD_DEMO_FIXTURE.read_text(encoding="utf-8"))
        cfg = raw["config"]
        vendors = {v["id"]: v["name"] for v in raw["vendors"]}
        self.accounts, self.bills, self.deposits, self.withdrawals = {}, {}, {}, {}
        by_internal = {}
        for a in cfg["accounts"]:
            oid = str(uuid.uuid4())
            by_internal[a["id"]] = oid
            self.accounts[oid] = {"_id": oid, "nickname": a["name"], "balance": a["observed_balance_cents"] // 100,
                                  "type": "Checking", "customer_id": CUST}
        for b in raw["bills"]:
            oid = str(uuid.uuid4())
            state = {"draft": "D", "scheduled": "S"}.get(b["status"], "A")
            self.bills[oid] = {"_id": oid, "account_id": by_internal[b["source_account_id"]], "status": "pending",
                               "payee": vendors[b["vendor_id"]], "nickname": f"{b['id']}|{b['earliest_pay_date']}|{state}",
                               "payment_date": b["due_date"], "payment_amount": b["amount_cents"] // 100}
        for inv in raw["invoices"]:
            if inv["status"] not in ("issued", "paid"):
                continue
            oid = str(uuid.uuid4())
            paid = inv["status"] == "paid"
            self.deposits[oid] = {"_id": oid, "account_id": by_internal[inv["deposit_account_id"]], "medium": "balance",
                                  "status": "completed" if paid else "pending",
                                  "transaction_date": inv["paid_date"] if paid else inv["expected_receipt_date"],
                                  "amount": inv["amount_cents"] // 100,
                                  "description": f"{inv['id']} {inv['customer_id']} due {inv['due_date']}"}
        self.log: list[tuple[str, str]] = []

    def handler(self, req: httpx.Request) -> httpx.Response:
        p, m = req.url.path, req.method
        body = json.loads(req.content) if req.content else None
        if m != "GET":
            self.log.append((m, p))
        if p == f"/customers/{CUST}":
            return httpx.Response(200, json={"_id": CUST})
        if p == f"/customers/{CUST}/accounts":
            return httpx.Response(200, json=list(self.accounts.values()))
        mt = re.fullmatch(r"/accounts/([^/]+)/(bills|deposits|withdrawals|transfers)", p)
        if mt and m == "GET":
            aid, kind = mt.groups()
            table = {"bills": self.bills, "deposits": self.deposits, "withdrawals": self.withdrawals,
                     "transfers": {}}[kind]
            rows = [r for r in table.values() if r["account_id"] == aid]
            return httpx.Response(200, json=rows) if rows else httpx.Response(404, json="none")
        if mt and m == "POST" and mt.group(2) in ("deposits", "withdrawals"):
            aid, kind = mt.groups()
            oid = str(uuid.uuid4())
            rec = {"_id": oid, "account_id": aid, **body}
            (self.deposits if kind == "deposits" else self.withdrawals)[oid] = rec
            return httpx.Response(201, json={"code": 201, "message": "created", "objectCreated": rec})
        mb = re.fullmatch(r"/bills/([^/]+)", p)
        if mb and m == "PUT":
            self.bills[mb.group(1)].update(body)
            return httpx.Response(202, json={"code": 202, "message": "Accepted bill modification"})
        return httpx.Response(404, json="unexpected route")


@pytest.fixture
def nessie_app(monkeypatch):
    monkeypatch.setattr(gateway, "SANDBOX_WRITES_ENABLED", True)
    monkeypatch.setattr(v1, "SANDBOX_WRITES_ENABLED", True)
    mock = MockNessie()
    connector = nessie_over(mock.handler)
    connector.customer_id = CUST
    app = create_app(Store())
    app.state.nessie_connector = connector
    app.state.sandbox_connector = connector
    with TestClient(app) as client:
        r = client.post("/v1/connections/nessie/sync", headers=AUTH)
        assert r.status_code == 200, r.text
        yield client, mock


def test_clean_round_trip_reconciles_to_zero_and_the_payment_becomes_safe(nessie_app):
    client, mock = nessie_app
    warn = client.post("/v1/twin/simulations", headers=AUTH, json=NW).json()
    assert warn["decision"] == "warn"
    fix = next(f for f in warn["fixes"] if f["kind"] == "funding")
    assert client.post(f"/v1/plans/{fix['id']}/approve", headers=AUTH, json={"approved_by": "u_maya"}).status_code == 200
    r = client.post(f"/v1/plans/{fix['id']}/execute-sandbox", headers={**AUTH, "Idempotency-Key": "rt-1"})
    assert r.status_code == 200, r.text
    ex = r.json()
    assert ex["state"] == "confirmed" and ex["mode"] == "sandbox" and ex["missing_provider_refs"] == []
    assert ex["reconciliation"]["unexplained_cents"] == 0 and ex["reconciliation"]["balanced"] is True
    assert {x["object_type"] for x in ex["provider_refs"]} == {"withdrawal", "deposit"}
    # exactly one withdrawal + one deposit per transfer, tagged as ours, whole dollars
    posts = [x for x in mock.log if x[0] == "POST"]
    assert len(posts) == 2 * len([a for a in fix["actions"] if a["kind"] == "transfer"])
    assert all(w["description"].startswith("TreasuryTwin plan_") and isinstance(w["amount"], int)
               for w in [*mock.withdrawals.values(), *(d for d in mock.deposits.values()
                                                       if d["description"].startswith("TreasuryTwin"))])
    # replay: no second write
    again = client.post(f"/v1/plans/{fix['id']}/execute-sandbox", headers={**AUTH, "Idempotency-Key": "rt-1"}).json()
    assert again == ex and [x for x in mock.log if x[0] == "POST"] == posts
    # derived balances now include our movements exactly once, and the twin sees the payment as safe
    assert ex["snapshot_after"] != ex["snapshot_before"]
    assert client.post("/v1/twin/simulations", headers=AUTH, json=NW).json()["decision"] == "allow"
    # a second sync is a no-op (no double counting of the plan_ movements)
    snap = client.post("/v1/connections/nessie/sync", headers=AUTH).json()["snapshot_id"]
    assert snap == ex["snapshot_after"]
    events = client.get("/v1/events", headers=AUTH).json()
    rec = next(e for e in events if e["action"] == "fix.reconciled")
    assert rec["after"]["reconciliation"]["unexplained_cents"] == 0 and rec["after"]["provider_refs"]


def test_unexpected_provider_state_is_reported_as_unexplained(nessie_app, monkeypatch):
    client, mock = nessie_app
    warn = client.post("/v1/twin/simulations", headers=AUTH, json=NW).json()
    fix = next(f for f in warn["fixes"] if f["kind"] == "funding")
    client.post(f"/v1/plans/{fix['id']}/approve", headers=AUTH, json={"approved_by": "u_maya"})
    real = mock.handler

    def tamper(req):                       # the provider silently records an extra $100 withdrawal on each write
        resp = real(req)
        if req.method == "POST" and req.url.path.endswith("/withdrawals"):
            rec = json.loads(resp.content)["objectCreated"]
            mock.withdrawals[rec["_id"]]["amount"] += 100
        return resp
    client.app.state.nessie_connector.client._client = httpx.Client(transport=httpx.MockTransport(tamper))
    ex = client.post(f"/v1/plans/{fix['id']}/execute-sandbox", headers={**AUTH, "Idempotency-Key": "rt-bad"}).json()
    assert ex["state"] == "uncertain" and ex["reconciliation"]["unexplained_cents"] > 0
