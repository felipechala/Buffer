"""Nessie (Capital One hackathon sandbox) connector.

Scope rule: every read is scoped to the single company customer (`NESSIE_CUSTOMER_ID`); the
sandbox-wide `GET /accounts` is never called. The API key travels as the `key` query param, so
no exception message or log line here may include a request URL.

Observed quirks handled here (fixtures/nessie/schema_notes.md):
  * a bad key still returns 200 [] on list reads -> health() verifies the customer resolves;
  * accounts with no transfers/deposits answer 404 ("No transfers found...") -> treated as empty;
  * transfer list items use `id`, everything else `_id`.
"""
from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any, Callable

import httpx

from ..config import NESSIE_API_KEY, NESSIE_BASE_URL, NESSIE_CUSTOMER_ID
from .base import (
    ConnectorAuthError,
    ConnectorHealth,
    ConnectorUnavailable,
    RawRecord,
)

RETRY_STATUSES = {429, 500, 502, 503, 504}


def _base(url: str) -> str:
    url = url.rstrip("/")
    # The sandbox refuses plain http (schema_notes.md); upgrade rather than fail.
    if url.startswith("http://") and "nessieisreal.com" in url:
        url = "https://" + url.removeprefix("http://")
    return url


class NessieClient:
    """Thin authenticated HTTP wrapper with bounded retries. Shared by the connector and the seeder."""

    def __init__(self, base_url: str = NESSIE_BASE_URL, api_key: str = NESSIE_API_KEY, *,
                 client: httpx.Client | None = None, timeout: float = 10.0, max_retries: int = 3,
                 backoff_s: float = 0.5, sleep: Callable[[float], None] = time.sleep):
        self.base_url = _base(base_url)
        self._api_key = api_key
        self._client = client or httpx.Client(timeout=timeout)
        self.max_retries = max_retries
        self.backoff_s = backoff_s
        self._sleep = sleep

    @property
    def has_key(self) -> bool:
        return bool(self._api_key)

    def request(self, method: str, path: str, *, json: Any = None, ok_404: bool = False) -> Any:
        """Return parsed JSON (None for empty bodies / tolerated 404). Retries 429/5xx/transport errors."""
        url = f"{self.base_url}{path}"
        last = "no attempt made"
        for attempt in range(self.max_retries + 1):
            try:
                resp = self._client.request(method, url, params={"key": self._api_key}, json=json)
            except httpx.HTTPError as exc:       # never chain: exc text contains the URL (and key)
                last = type(exc).__name__
                resp = None
            if resp is not None:
                if resp.status_code in (401, 403):
                    raise ConnectorAuthError(f"Nessie rejected the API key (HTTP {resp.status_code})")
                if resp.status_code == 404 and ok_404:
                    return None
                if resp.status_code not in RETRY_STATUSES:
                    if resp.status_code >= 400:
                        raise ConnectorUnavailable(
                            f"Nessie {method} {path} -> HTTP {resp.status_code}: {resp.text[:200]}")
                    if not resp.content:
                        return None
                    try:
                        return resp.json()
                    except ValueError:
                        raise ConnectorUnavailable(f"Nessie {method} {path}: response was not JSON") from None
                last = f"HTTP {resp.status_code}"
            if attempt < self.max_retries:
                self._sleep(self.backoff_s * (2 ** attempt))
        raise ConnectorUnavailable(f"Nessie {method} {path} failed after {self.max_retries + 1} attempts ({last})")

    def get(self, path: str, **kw: Any) -> Any:
        return self.request("GET", path, **kw)

    def close(self) -> None:
        self._client.close()


class NessieConnector:
    provider = "nessie"

    def __init__(self, client: NessieClient | None = None, customer_id: str = NESSIE_CUSTOMER_ID):
        self.client = client or NessieClient()
        self.customer_id = customer_id
        self.last_sync_at: datetime | None = None

    # ---- reads
    def fetch_accounts(self) -> list[RawRecord]:
        if not self.customer_id:
            raise ConnectorUnavailable("NESSIE_CUSTOMER_ID is not set")
        items = self._list(f"/customers/{self.customer_id}/accounts")
        now = datetime.now(timezone.utc)
        return [RawRecord("account", self.provider, _pid(i, "_id"), now, i) for i in items]

    def fetch_transactions(self, accounts: list[RawRecord]) -> list[RawRecord]:
        now = datetime.now(timezone.utc)
        out: list[RawRecord] = []
        for acct in accounts:
            for kind, path, key in (("deposit", "deposits", "_id"), ("withdrawal", "withdrawals", "_id"),
                                    ("transfer", "transfers", "id")):
                for item in self._list(f"/accounts/{acct.provider_id}/{path}", ok_404=True):
                    out.append(RawRecord(kind, self.provider, _pid(item, key, fallback="_id"), now, item,
                                         account_provider_id=acct.provider_id))
        return out

    def fetch_obligations(self, accounts: list[RawRecord]) -> list[RawRecord]:
        now = datetime.now(timezone.utc)
        return [RawRecord("bill", self.provider, _pid(item, "_id"), now, item, account_provider_id=a.provider_id)
                for a in accounts for item in self._list(f"/accounts/{a.provider_id}/bills", ok_404=True)]

    def fetch_counterparties(self, obligations: list[RawRecord]) -> list[str]:
        """Payee names come straight from bills; /merchants is global (not customer-scoped) so it is not read."""
        return sorted({str(o.data.get("payee", "")).strip() for o in obligations if o.data.get("payee")})

    def health(self) -> ConnectorHealth:
        scope = f"customer {self.customer_id[:8]}" if self.customer_id else ""
        if not (self.client.has_key and self.customer_id):
            return ConnectorHealth(provider=self.provider, ok=False, status="not_configured", scope=scope,
                                   detail="set NESSIE_API_KEY and NESSIE_CUSTOMER_ID")
        try:
            # A bad key still returns 200 [] on lists, so require the customer object itself to resolve.
            cust = self.client.get(f"/customers/{self.customer_id}", ok_404=True)
        except ConnectorAuthError as exc:
            return ConnectorHealth(provider=self.provider, ok=False, status="auth_error", scope=scope,
                                   detail=str(exc), last_sync_at=self.last_sync_at)
        except ConnectorUnavailable as exc:
            return ConnectorHealth(provider=self.provider, ok=False, status="unavailable", scope=scope,
                                   detail=str(exc), last_sync_at=self.last_sync_at)
        if not isinstance(cust, dict) or cust.get("_id") != self.customer_id:
            return ConnectorHealth(provider=self.provider, ok=False, status="unavailable", scope=scope,
                                   detail="company customer did not resolve (404 or unexpected body)",
                                   last_sync_at=self.last_sync_at)
        return ConnectorHealth(provider=self.provider, ok=True, status="healthy", scope=scope,
                               detail="customer resolves", last_sync_at=self.last_sync_at)

    def mark_synced(self) -> None:
        self.last_sync_at = datetime.now(timezone.utc)

    # ---- internals
    def _list(self, path: str, ok_404: bool = False) -> list[dict[str, Any]]:
        body = self.client.get(path, ok_404=ok_404)
        if body is None:
            return []
        if not isinstance(body, list):
            raise ConnectorUnavailable(f"Nessie GET {path}: expected a JSON list")
        return [i for i in body if isinstance(i, dict)]


def _pid(item: dict[str, Any], key: str, fallback: str | None = None) -> str:
    value = item.get(key) or (item.get(fallback) if fallback else None)
    if not value:
        raise ConnectorUnavailable(f"Nessie record missing id field '{key}'")
    return str(value)
