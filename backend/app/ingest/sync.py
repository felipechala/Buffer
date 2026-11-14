"""Provider sync orchestration: fetch -> normalize -> upsert into the Store.

`apply_batch` is idempotent: running it twice on the same batch changes nothing (no duplicate rows, no audit
noise, same snapshot id). Entities are matched on (provider, provider_id), not on display ids.
"""
from __future__ import annotations

import json
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TypeVar

from pydantic import BaseModel

from ..config import CASHGUARD_DEMO_FIXTURE
from ..connectors.base import FinanceConnector, RawSnapshot, fetch_snapshot
from ..workflows.models import APBill, ARInvoice, CashEvent, Customer, Vendor
from ..workflows.store import Store
from .normalize import NormalizedBatch, NormalizeError, normalize

T = TypeVar("T", bound=BaseModel)
ACTOR = "connector-sync"


@dataclass
class ApplyResult:
    inserted: dict[str, int] = field(default_factory=dict)
    updated: dict[str, int] = field(default_factory=dict)
    removed: dict[str, int] = field(default_factory=dict)


def load_overlay(path: Path | str = CASHGUARD_DEMO_FIXTURE) -> dict[str, Any]:
    """Treasury overlay = routes, credit, policy, buffers, shock bounds, nicknames, customers, vendors."""
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _key(obj: Any) -> tuple[str, str] | None:
    provider = getattr(obj, "provider", None) or ("nessie" if getattr(obj, "source", None) == "nessie" else None)
    pid = getattr(obj, "provider_id", None) or getattr(obj, "provider_ref", None)
    return (provider, pid) if provider and pid else None


def _upsert(store: Store, cls: type[T], incoming: list[T], *, stale_ok: Any, res: ApplyResult,
            keep: tuple[str, ...] = ()) -> None:
    name = cls.__name__
    existing = {e.id: e for e in store.list(cls)}  # type: ignore[attr-defined]
    by_key = {k: e for e in existing.values() if (k := _key(e))}
    seen_ids: set[str] = set()
    for obj in incoming:
        prior = existing.get(obj.id) or by_key.get(_key(obj))  # type: ignore[arg-type]
        if prior is not None and prior.id != obj.id:          # same provider object, new display id
            store.delete(cls, prior.id, actor=ACTOR, action=f"{name.lower()}.replaced")
            res.removed[name] = res.removed.get(name, 0) + 1
            prior = None
        if prior is not None and keep:
            obj = obj.model_copy(update={f: getattr(prior, f) for f in keep if getattr(prior, f, None)})
        seen_ids.add(obj.id)
        if prior is None:
            store.save(obj, actor=ACTOR, action=f"{name.lower()}.ingest")
            res.inserted[name] = res.inserted.get(name, 0) + 1
        elif prior.model_dump() != obj.model_dump():
            store.save(obj, actor=ACTOR, action=f"{name.lower()}.update", before=prior)
            res.updated[name] = res.updated.get(name, 0) + 1
    for eid, e in existing.items():
        if eid not in seen_ids and stale_ok(e):
            store.delete(cls, eid, actor=ACTOR, action=f"{name.lower()}.removed")
            res.removed[name] = res.removed.get(name, 0) + 1


def apply_batch(store: Store, batch: NormalizedBatch) -> ApplyResult:
    res = ApplyResult()
    switching = not store.has_config() or any(a.provider != batch.provider for a in store.get_config().accounts)
    config = batch.config
    if store.has_config():
        old = {a.id: a for a in store.get_config().accounts}
        # Keep the original observation time when nothing changed, so a no-op re-sync keeps the snapshot id.
        config = config.model_copy(update={"accounts": [
            a.model_copy(update={"as_of": old[a.id].as_of})
            if a.id in old and old[a.id].provider == a.provider and old[a.id].provider_id == a.provider_id
            and old[a.id].observed_balance_cents == a.observed_balance_cents else a
            for a in config.accounts]})
        if config.model_dump() != store.get_config().model_dump():
            store.set_config(config, actor=ACTOR)
    else:
        store.set_config(config, actor=ACTOR)

    def from_provider(e: Any) -> bool:
        k = _key(e)
        return (k is not None and k[0] == batch.provider) or (
            switching and getattr(e, "provider", None) in (None, "fixture") and getattr(e, "source", "") != "app_simulated")

    _upsert(store, Customer, batch.customers, stale_ok=lambda e: False, res=res)
    _upsert(store, Vendor, batch.vendors, stale_ok=lambda e: False, res=res)
    _upsert(store, APBill, batch.bills, stale_ok=from_provider, res=res, keep=("required_role",))
    _upsert(store, ARInvoice, batch.invoices, stale_ok=from_provider, res=res)
    _upsert(store, CashEvent, batch.cash_events,
            stale_ok=lambda e: e.source != "app_simulated" and (e.source == batch.provider or switching), res=res)
    return res


def save_error_sample(err: NormalizeError) -> str | None:
    """Write the redacted offending record to scratch so it can be inspected without leaking ids."""
    if not err.sample:
        return None
    path = Path(tempfile.gettempdir()) / "cashguard_normalize_error.json"
    path.write_text(json.dumps({"tag": err.tag, "message": str(err), "sample": err.sample}, indent=2, default=str),
                    encoding="utf-8")
    return str(path)


@dataclass
class SyncOutcome:
    batch: NormalizedBatch
    result: ApplyResult
    raw: RawSnapshot


def sync_connector(store: Store, connector: FinanceConnector, overlay: dict[str, Any] | None = None,
                   *, use_history: bool = True) -> SyncOutcome:
    """Fetch everything first; the store is only touched if fetch and normalize both succeed."""
    raw = fetch_snapshot(connector)
    batch = normalize(raw, overlay or load_overlay(), use_history=use_history)
    result = apply_batch(store, batch)
    if hasattr(connector, "mark_synced"):
        connector.mark_synced()
    return SyncOutcome(batch=batch, result=result, raw=raw)

