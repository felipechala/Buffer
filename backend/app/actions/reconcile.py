"""Reconciliation of an executed fix against the re-synced provider snapshot.

Nessie's `balance` never moves, so the comparison is on *derived* balances (snapshot balance + posted cash
events dated on/before day 0, i.e. `TwinState.accounts[].observed_balance_cents`).

  expected   derived balance after the fix, assuming every movement it creates has landed
  in_flight  signed part of the fix not yet effective on the as-of date (route `settlement_days`,
             credit draw delay); the observed balance must NOT include it yet
  observed   derived balance in the new snapshot

  unexplained = observed - (expected - in_flight): zero when the provider holds exactly what the fix implies.
"""
from __future__ import annotations

from typing import Mapping

from pydantic import BaseModel


class AccountReconciliation(BaseModel):
    account_id: str
    expected_cents: int
    in_flight_cents: int
    observed_cents: int
    diff_cents: int              # observed - expected (raw)
    unexplained_cents: int       # signed residual after crediting in-flight amounts


class Reconciliation(BaseModel):
    basis: str = "derived balance = snapshot balance + posted cash events dated on/before the as-of date"
    accounts: list[AccountReconciliation]
    unexplained_cents: int       # sum of absolute residuals
    in_flight_cents: int         # sum of absolute in-flight amounts (explained by settlement days)
    balanced: bool


def reconcile(expected: Mapping[str, int], observed: Mapping[str, int],
              in_flight: Mapping[str, int] | None = None) -> Reconciliation:
    in_flight = in_flight or {}
    rows: list[AccountReconciliation] = []
    for acct in sorted(set(expected) | set(observed)):
        e, o, f = expected.get(acct, 0), observed.get(acct, 0), in_flight.get(acct, 0)
        rows.append(AccountReconciliation(account_id=acct, expected_cents=e, in_flight_cents=f, observed_cents=o,
                                          diff_cents=o - e, unexplained_cents=o - (e - f)))
    total = sum(abs(r.unexplained_cents) for r in rows)
    return Reconciliation(accounts=rows, unexplained_cents=total,
                          in_flight_cents=sum(abs(r.in_flight_cents) for r in rows), balanced=total == 0)
