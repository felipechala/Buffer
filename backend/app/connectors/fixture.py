"""Fixture connector: serves recorded Nessie-shaped JSON (fixtures/nessie/*.json) through the read protocol.

Each list file may be a flat list (items carry `account_id`) or a dict keyed by account provider id.
Transfers use `id` instead of `_id`, exactly like the live API.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..config import FIXTURES_DIR
from .base import ConnectorHealth, RawRecord


class FixtureConnector:
    provider = "fixture"

    def __init__(self, directory: Path | str = FIXTURES_DIR / "nessie"):
        self.dir = Path(directory)
        self.last_sync_at: datetime | None = None

    def _load(self, name: str) -> Any:
        path = self.dir / f"{name}.json"
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else []

    def _records(self, kind: str, name: str, key: str, accounts: list[RawRecord]) -> list[RawRecord]:
        raw = self._load(name)
        now = datetime.now(timezone.utc)
        if isinstance(raw, dict):
            pairs = [(acct, item) for acct, items in raw.items() for item in items]
        else:
            pairs = [(item.get("account_id"), item) for item in raw]
        known = {a.provider_id for a in accounts}
        return [RawRecord(kind, self.provider, str(item.get(key) or item["_id"]), now, item,  # type: ignore[arg-type]
                          account_provider_id=acct)
                for acct, item in pairs if acct in known]

    def fetch_accounts(self) -> list[RawRecord]:
        now = datetime.now(timezone.utc)
        return [RawRecord("account", self.provider, str(i["_id"]), now, i) for i in self._load("accounts")]

    def fetch_transactions(self, accounts: list[RawRecord]) -> list[RawRecord]:
        return [*self._records("deposit", "deposits", "_id", accounts),
                *self._records("withdrawal", "withdrawals", "_id", accounts),
                *self._records("transfer", "transfers", "id", accounts)]

    def fetch_obligations(self, accounts: list[RawRecord]) -> list[RawRecord]:
        return self._records("bill", "bills", "_id", accounts)

    def fetch_counterparties(self, obligations: list[RawRecord]) -> list[str]:
        return sorted({str(o.data["payee"]) for o in obligations if o.data.get("payee")})

    def health(self) -> ConnectorHealth:
        ok = self.dir.exists()
        return ConnectorHealth(provider=self.provider, ok=ok, status="healthy" if ok else "unavailable",
                               scope=self.dir.name, detail="" if ok else "fixture directory not found",
                               last_sync_at=self.last_sync_at)
