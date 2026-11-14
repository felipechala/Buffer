"""Load the synthetic demo company into a Store."""
from __future__ import annotations

import json
from pathlib import Path

from ..config import DEMO_COMPANY_FIXTURE
from .models import APBill, ARInvoice, CashEvent, CompanyConfig, Customer, Vendor
from .store import Store


def seed_demo(store: Store, path: Path | str = DEMO_COMPANY_FIXTURE, *, reset: bool = True) -> None:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if reset:
        store.reset()
    store.set_config(CompanyConfig.model_validate(raw["config"]), actor="seed")
    for cls, key in ((Customer, "customers"), (Vendor, "vendors"), (ARInvoice, "invoices"),
                     (APBill, "bills"), (CashEvent, "cash_events")):
        for item in raw.get(key, []):
            store.save(cls.model_validate(item), actor="seed", action=f"{cls.__name__.lower()}.seed")
