"""TwinBuilder: turn fixture data (and later Nessie snapshots) into an immutable TwinState."""
from __future__ import annotations

import hashlib
import json
from datetime import date
from pathlib import Path
from typing import Any

import yaml

from ..config import ENGINE_FIXTURES_DIR
from .models import (
    Account,
    CreditLine,
    ExpectedReceipt,
    Obligation,
    TransferRoute,
    TwinOverrides,
    TwinState,
    to_cents,
)


def snapshot_hash(twin: TwinState) -> str:
    payload = twin.model_dump(mode="json", exclude={"snapshot_id"})
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return "snap_" + hashlib.sha256(canonical.encode()).hexdigest()[:16]


def build_twin(**fields: Any) -> TwinState:
    """Validate fields into a TwinState whose id is the hash of its content."""
    draft = TwinState(snapshot_id="pending", **fields)
    return draft.model_copy(update={"snapshot_id": snapshot_hash(draft)})


def twin_from_fixture_dict(raw: dict[str, Any], *, label: str = "inline") -> TwinState:
    """Fixture format uses dollars for readability; everything is converted to cents."""
    company = raw.get("company", {})
    accounts = [
        Account(
            id=a["id"],
            name=a.get("name", a["id"].replace("_", " ").title()),
            role=a.get("role", ""),
            observed_balance_cents=to_cents(a["observed_balance"]),
            minimum_buffer_cents=to_cents(a.get("min_buffer", 0)),
            source="fixture",
        )
        for a in raw["accounts"]
    ]
    obligations = [
        Obligation(
            id=o["id"],
            account_id=o["account"],
            amount_cents=to_cents(o["amount"]),
            due_day=o["due_day"],
            priority=o.get("priority", 1),
            deferrable=o.get("deferrable", False),
            counterparty=o.get("counterparty", ""),
            category=o.get("category", ""),
        )
        for o in raw.get("obligations", [])
    ]
    receipts = [
        ExpectedReceipt(
            id=r["id"],
            account_id=r["account"],
            amount_cents=to_cents(r["amount"]),
            expected_day=r["expected_day"],
            delay_distribution={int(k): float(v) for k, v in
                                r.get("delay_distribution", {0: 1.0}).items()},
            correlation_key=r.get("correlation_key"),
            counterparty=r.get("counterparty", ""),
        )
        for r in raw.get("expected_receipts", [])
    ]
    routes = [
        TransferRoute(
            id=r.get("id", f"{r['from']}->{r['to']}"),
            source_id=r["from"],
            destination_id=r["to"],
            max_amount_cents=to_cents(r["max_amount"]),
            settlement_days=r.get("settlement_days", 0),
            fee_rate=r.get("fee_rate", 0.0),
            allowed=r.get("allowed", True),
            note=r.get("note", ""),
        )
        for r in raw.get("transfer_routes", [])
    ]
    credit_lines = [
        CreditLine(
            id=c.get("id", f"credit-{c['account']}"),
            account_id=c["account"],
            limit_cents=to_cents(c["available_limit"]),
            drawn_cents=to_cents(c.get("drawn", 0)),
            draw_delay_days=c.get("draw_delay_days", 0),
            fee_rate=c.get("fee_rate", 0.0),
            enabled=c.get("enabled", True),
        )
        for c in raw.get("credit_lines", [])
    ]
    return build_twin(
        company_name=company.get("name", "Synthetic Co"),
        as_of=date.fromisoformat(str(company.get("as_of", "2026-11-16"))),
        horizon_days=company.get("horizon_days", 7),
        accounts=accounts,
        obligations=obligations,
        receipts=receipts,
        routes=routes,
        credit_lines=credit_lines,
        provenance={"source": "fixture", "fixture": label},
        assumptions=list(raw.get("assumptions", [])),
    )


def load_fixture(name_or_path: str | Path) -> TwinState:
    path = Path(name_or_path)
    if not path.suffix:
        path = ENGINE_FIXTURES_DIR / f"{name_or_path}.yaml"
    with path.open(encoding="utf-8") as fh:
        raw = yaml.safe_load(fh)
    return twin_from_fixture_dict(raw, label=path.stem)


def apply_overrides(twin: TwinState, ov: TwinOverrides) -> TwinState:
    """Return a new snapshot with user-adjusted constraints. The input is not mutated."""
    accounts = [
        a.model_copy(update={"minimum_buffer_cents": ov.account_buffers_cents[a.id]})
        if a.id in ov.account_buffers_cents else a
        for a in twin.accounts
    ]
    routes = []
    for r in twin.routes:
        upd: dict[str, Any] = {}
        if r.id in ov.route_allowed:
            upd["allowed"] = ov.route_allowed[r.id]
        if r.id in ov.route_max_cents:
            upd["max_amount_cents"] = ov.route_max_cents[r.id]
        if r.id in ov.route_settlement_days:
            upd["settlement_days"] = ov.route_settlement_days[r.id]
        routes.append(r.model_copy(update=upd) if upd else r)
    lines = []
    for c in twin.credit_lines:
        upd = {}
        if c.id in ov.credit_limit_cents:
            upd["limit_cents"] = ov.credit_limit_cents[c.id]
        if c.id in ov.credit_enabled:
            upd["enabled"] = ov.credit_enabled[c.id]
        lines.append(c.model_copy(update=upd) if upd else c)
    fields = twin.model_dump(exclude={"snapshot_id", "accounts", "routes", "credit_lines"})
    # Re-validate everything (model_copy skips validation) and re-hash.
    return build_twin(
        **fields,
        accounts=[Account.model_validate(a.model_dump()) for a in accounts],
        routes=[TransferRoute.model_validate(r.model_dump()) for r in routes],
        credit_lines=[CreditLine.model_validate(c.model_dump()) for c in lines],
    )
