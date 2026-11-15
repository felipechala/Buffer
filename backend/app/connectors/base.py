"""Connector contracts shared by the Nessie, fixture and (mock) sandbox implementations.

Read side: `FinanceConnector` returns raw provider records (`RawRecord`) and never touches the store.
Write side: `SandboxActionConnector` is permission-gated and only used by actions/gateway.py.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any, Literal, Protocol

from pydantic import BaseModel


class ConnectorError(Exception):
    """Base class for provider failures. Messages never contain credentials or URLs with keys."""


class ConnectorAuthError(ConnectorError):
    """401/403 from the provider."""


class ConnectorUnavailable(ConnectorError):
    """Timeouts, 5xx after retries, unknown customer, or malformed responses."""


class ConnectorTimeout(ConnectorUnavailable):
    """A WRITE whose outcome is unknown (timeout, dropped connection, 5xx). Never retried: the provider may
    or may not have applied it, so callers must mark the execution 'uncertain' and re-sync."""


class NonWholeDollarAmount(ConnectorError, ValueError):
    """The provider stores whole dollars; refusing beats silently truncating money."""


class PartialTransferError(ConnectorError):
    """First leg of a transfer was written, the second failed or is unknown. `refs` are the written objects."""

    def __init__(self, message: str, refs: list["ProviderRef"], uncertain: bool = True):
        super().__init__(message)
        self.refs = refs
        self.uncertain = uncertain


RecordKind = Literal["account", "bill", "deposit", "withdrawal", "transfer"]


@dataclass(frozen=True)
class RawRecord:
    """One provider object, untouched, plus the identity needed to upsert it."""
    kind: RecordKind
    provider: str
    provider_id: str
    observed_at: datetime
    data: dict[str, Any]
    account_provider_id: str | None = None   # owning account (list items do not carry it)


@dataclass
class RawSnapshot:
    provider: str
    observed_at: datetime
    accounts: list[RawRecord] = field(default_factory=list)
    bills: list[RawRecord] = field(default_factory=list)
    deposits: list[RawRecord] = field(default_factory=list)
    withdrawals: list[RawRecord] = field(default_factory=list)
    transfers: list[RawRecord] = field(default_factory=list)
    counterparties: list[str] = field(default_factory=list)


class ConnectorHealth(BaseModel):
    provider: str
    ok: bool
    status: Literal["healthy", "unavailable", "auth_error", "not_configured"]
    detail: str = ""
    scope: str = ""
    last_sync_at: datetime | None = None


class FinanceConnector(Protocol):
    provider: str

    def fetch_accounts(self) -> list[RawRecord]: ...
    def fetch_transactions(self, accounts: list[RawRecord]) -> list[RawRecord]: ...
    def fetch_obligations(self, accounts: list[RawRecord]) -> list[RawRecord]: ...
    def fetch_counterparties(self, obligations: list[RawRecord]) -> list[str]: ...
    def health(self) -> ConnectorHealth: ...


def fetch_snapshot(connector: FinanceConnector) -> RawSnapshot:
    """Pull everything the normalizer needs through the read protocol."""
    now = datetime.now(timezone.utc)
    accounts = connector.fetch_accounts()
    txns = connector.fetch_transactions(accounts)
    bills = connector.fetch_obligations(accounts)
    return RawSnapshot(
        provider=connector.provider, observed_at=now, accounts=accounts, bills=bills,
        deposits=[r for r in txns if r.kind == "deposit"],
        withdrawals=[r for r in txns if r.kind == "withdrawal"],
        transfers=[r for r in txns if r.kind == "transfer"],
        counterparties=connector.fetch_counterparties(bills))


class ProviderRef(BaseModel):
    provider: str
    object_type: str
    object_id: str
    description: str = ""


class SandboxActionConnector(Protocol):
    """Permission-gated write side. Read connectors never implement this. Writes are never retried.

    `create_transfer` writes a withdrawal on the source and a deposit on the destination (the provider's
    transfer object cannot name a destination and moves no money). The withdrawal is dated `date`, the
    deposit `arrival_date` (default: `date`)."""
    provider: str

    def create_transfer(self, source_provider_id: str, dest_provider_id: str, amount_cents: int, date: date,
                        description: str, *, arrival_date: date | None = None) -> list[ProviderRef]: ...
    def schedule_bill(self, bill_provider_id: str, payment_date: date,
                      nickname: str | None = None) -> list[ProviderRef]: ...
