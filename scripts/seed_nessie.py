"""Replicate the CashGuard demo company (fixtures/cashguard_demo.json) in the Nessie sandbox.

Idempotent: existing accounts / bills / deposits are found by nickname / description and reused; only what
is missing or different is created. Everything it creates is recorded in fixtures/nessie_ids.json (gitignored).

    backend/.venv/Scripts/python scripts/seed_nessie.py            # create / reconcile
    backend/.venv/Scripts/python scripts/seed_nessie.py --reset    # delete what a previous run created

Conventions (must match backend/app/ingest/normalize.py):
  * account nickname = overlay account name ("Operating", ...), mapped back via config.nessie_nicknames;
  * bill nickname    = "<bill id>|<earliest pay date>|<D|A|S>", payee = vendor name, payment_date = due date;
  * deposit description = "INV-<n> <customer id> due YYYY-MM-DD"; issued invoices -> future-dated *pending*
    deposits (expected receipts, not cash), paid invoices -> *completed* deposits dated on the paid date.
Nessie truncates amounts to whole dollars and ignores PUT balance, so the fixture must be in whole dollars.
Reset order is child-first (bills, deposits, then accounts) because Nessie does not cascade. Withdrawals,
purchases and transfers cannot be deleted; this script never creates them.
The API key is read from the environment / .env and is never printed.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.config import CASHGUARD_DEMO_FIXTURE, NESSIE_API_KEY, NESSIE_BASE_URL, NESSIE_CUSTOMER_ID  # noqa: E402
from app.connectors.base import ConnectorError  # noqa: E402
from app.connectors.nessie import NessieClient  # noqa: E402

IDS_PATH = ROOT / "fixtures" / "nessie_ids.json"
SAVINGS = {"tax", "reserve"}
STATE = {"draft": "D", "scheduled": "S"}


def dollars(cents: int, what: str, problems: list[str]) -> int:
    if cents % 100:
        problems.append(f"{what}: {cents} cents is not whole dollars (Nessie truncates)")
    return cents // 100


def bill_nickname(b: dict) -> str:
    return f"{b['id']}|{b['earliest_pay_date']}|{STATE.get(b['status'], 'A')}"


def load_ids() -> dict:
    if IDS_PATH.exists():
        return json.loads(IDS_PATH.read_text(encoding="utf-8"))
    return {"customer_id": NESSIE_CUSTOMER_ID, "accounts": {}, "bills": {}, "deposits": {}}


def save_ids(ids: dict) -> None:
    IDS_PATH.write_text(json.dumps(ids, indent=2, sort_keys=True), encoding="utf-8")


def reset(c: NessieClient, ids: dict) -> None:
    for bill_id, nid in list(ids["bills"].items()):
        c.request("DELETE", f"/bills/{nid}", ok_404=True)
        del ids["bills"][bill_id]
        print(f"deleted bill {bill_id}")
    for key, nid in list(ids["deposits"].items()):
        c.request("DELETE", f"/deposits/{nid}", ok_404=True)
        del ids["deposits"][key]
        print(f"deleted deposit {key}")
    for nick, nid in list(ids["accounts"].items()):
        c.request("DELETE", f"/accounts/{nid}", ok_404=True)
        del ids["accounts"][nick]
        print(f"deleted account {nick}")
    save_ids(ids)


def seed(c: NessieClient, ids: dict) -> int:
    raw = json.loads(Path(CASHGUARD_DEMO_FIXTURE).read_text(encoding="utf-8"))
    cfg = raw["config"]
    problems: list[str] = []
    vendors = {v["id"]: v["name"] for v in raw["vendors"]}
    name_of = {a["id"]: a["name"] for a in cfg["accounts"]}
    created = reused = 0

    # ---- accounts
    existing = {a["nickname"]: a for a in c.get(f"/customers/{NESSIE_CUSTOMER_ID}/accounts") or []}
    nessie_acct: dict[str, str] = {}
    for a in cfg["accounts"]:
        nick = a["name"]
        want = dollars(a["observed_balance_cents"], f"account {nick}", problems)
        if nick in existing:
            got = existing[nick]
            if int(got.get("balance", -1)) != want:
                problems.append(f"account {nick}: Nessie balance {got.get('balance')} != fixture {want}; "
                                "balance cannot be changed via PUT - run --reset and seed again")
            nessie_acct[a["id"]] = got["_id"]
            reused += 1
        else:
            body = {"type": "Savings" if a["id"] in SAVINGS else "Checking", "nickname": nick, "rewards": 0,
                    "balance": want}
            nessie_acct[a["id"]] = c.request("POST", f"/customers/{NESSIE_CUSTOMER_ID}/accounts",
                                             json=body)["objectCreated"]["_id"]
            created += 1
            print(f"created account {nick}")
        ids["accounts"][nick] = nessie_acct[a["id"]]
    save_ids(ids)

    # ---- bills
    for b in raw["bills"]:
        aid = nessie_acct[b["source_account_id"]]
        body = {"status": "pending", "payee": vendors[b["vendor_id"]], "nickname": bill_nickname(b),
                "payment_date": b["due_date"], "recurring_date": int(b["due_date"][8:10]),
                "payment_amount": dollars(b["amount_cents"], f"bill {b['id']}", problems)}
        have = {(x.get("nickname") or "").split("|")[0]: x for x in c.get(f"/accounts/{aid}/bills", ok_404=True) or []}
        cur = have.get(b["id"])
        if cur and (cur.get("nickname"), cur.get("payee"), cur.get("payment_date"), float(cur.get("payment_amount", -1))) \
                == (body["nickname"], body["payee"], body["payment_date"], float(body["payment_amount"])):
            ids["bills"][b["id"]] = cur["_id"]
            reused += 1
            continue
        if cur:
            c.request("DELETE", f"/bills/{cur['_id']}", ok_404=True)
        ids["bills"][b["id"]] = c.request("POST", f"/accounts/{aid}/bills", json=body)["objectCreated"]["_id"]
        created += 1
        print(f"created bill {b['id']}")
    save_ids(ids)

    # ---- deposits (expected receipts + collection history)
    for inv in raw["invoices"]:
        if inv["status"] not in ("issued", "paid"):
            continue
        aid = nessie_acct[inv["deposit_account_id"]]
        paid = inv["status"] == "paid"
        body = {"medium": "balance", "status": "completed" if paid else "pending",
                "transaction_date": inv["paid_date"] if paid else inv["expected_receipt_date"],
                "amount": dollars(inv["amount_cents"], f"invoice {inv['id']}", problems),
                "description": f"{inv['id']} {inv['customer_id']} due {inv['due_date']}"}
        have = {x.get("description"): x for x in c.get(f"/accounts/{aid}/deposits", ok_404=True) or []}
        cur = have.get(body["description"])
        if cur and (cur.get("status"), cur.get("transaction_date"), int(cur.get("amount", -1))) == \
                (body["status"], body["transaction_date"], body["amount"]):
            ids["deposits"][inv["id"]] = cur["_id"]
            reused += 1
            continue
        if cur:
            c.request("DELETE", f"/deposits/{cur['_id']}", ok_404=True)
        ids["deposits"][inv["id"]] = c.request("POST", f"/accounts/{aid}/deposits", json=body)["objectCreated"]["_id"]
        created += 1
        print(f"created deposit {inv['id']} ({body['status']})")
    save_ids(ids)

    print(f"done: created {created}, reused {reused}; ids -> {IDS_PATH.relative_to(ROOT)}")
    for p in problems:
        print("PROBLEM:", p)
    return 1 if problems else 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--reset", action="store_true", help="delete everything a previous run created (child-first)")
    args = ap.parse_args()
    if not (NESSIE_API_KEY and NESSIE_CUSTOMER_ID):
        print("NESSIE_API_KEY and NESSIE_CUSTOMER_ID must be set (see .env.example)")
        return 2
    client = NessieClient(NESSIE_BASE_URL, NESSIE_API_KEY)
    try:
        cust = client.get(f"/customers/{NESSIE_CUSTOMER_ID}", ok_404=True)
        if not isinstance(cust, dict) or cust.get("_id") != NESSIE_CUSTOMER_ID:
            print("company customer does not resolve in Nessie; check NESSIE_CUSTOMER_ID")
            return 2
        ids = load_ids()
        if args.reset:
            reset(client, ids)
            return 0
        return seed(client, ids)
    except ConnectorError as exc:
        print(f"Nessie error: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
