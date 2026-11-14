"""SQLite persistence: JSON documents per entity kind + an append-only audit table.

Deliberately small for the hackathon. Every mutation goes through `save`, which
writes the entity and its audit record in one transaction.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from typing import Iterator, TypeVar

from pydantic import BaseModel

from .models import AuditEvent, CompanyConfig, utcnow

T = TypeVar("T", bound=BaseModel)

SCHEMA = """
CREATE TABLE IF NOT EXISTS entities (
    kind TEXT NOT NULL, id TEXT NOT NULL, body TEXT NOT NULL,
    PRIMARY KEY (kind, id));
CREATE TABLE IF NOT EXISTS audit (
    seq INTEGER PRIMARY KEY AUTOINCREMENT, body TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS config (
    key TEXT PRIMARY KEY, body TEXT NOT NULL);
CREATE TRIGGER IF NOT EXISTS audit_no_update BEFORE UPDATE ON audit
    BEGIN SELECT RAISE(ABORT, 'audit log is append-only'); END;
CREATE TRIGGER IF NOT EXISTS audit_no_delete BEFORE DELETE ON audit
    WHEN (SELECT user_version FROM pragma_user_version) = 0
    BEGIN SELECT RAISE(ABORT, 'audit log is append-only'); END;
"""


class Store:
    def __init__(self, path: str = ":memory:"):
        self._conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._lock = threading.RLock()
        self._conn.executescript(SCHEMA)

    @contextmanager
    def transaction(self) -> Iterator[None]:
        with self._lock:
            self._conn.execute("BEGIN")
            try:
                yield
                self._conn.execute("COMMIT")
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise

    # ---- entities
    def get(self, cls: type[T], entity_id: str) -> T | None:
        with self._lock:
            row = self._conn.execute("SELECT body FROM entities WHERE kind=? AND id=?",
                                     (cls.__name__, entity_id)).fetchone()
        return cls.model_validate_json(row[0]) if row else None

    def list(self, cls: type[T]) -> list[T]:
        with self._lock:
            rows = self._conn.execute("SELECT body FROM entities WHERE kind=? ORDER BY id",
                                      (cls.__name__,)).fetchall()
        return [cls.model_validate_json(r[0]) for r in rows]

    def save(self, obj: BaseModel, *, actor: str, action: str,
             before: BaseModel | None = None, correlation_id: str = "") -> None:
        """Upsert an entity and append its audit event atomically."""
        entity_id = getattr(obj, "id")
        event = AuditEvent(
            timestamp=utcnow(), actor=actor, action=action, entity=type(obj).__name__,
            entity_id=entity_id,
            before=before.model_dump(mode="json") if before is not None else None,
            after=obj.model_dump(mode="json"), correlation_id=correlation_id)
        with self.transaction():
            self._conn.execute(
                "INSERT INTO entities(kind, id, body) VALUES (?,?,?) "
                "ON CONFLICT(kind, id) DO UPDATE SET body=excluded.body",
                (type(obj).__name__, entity_id, obj.model_dump_json()))
            self._conn.execute("INSERT INTO audit(body) VALUES (?)",
                               (event.model_dump_json(exclude={"seq"}),))

    def delete(self, cls: type[BaseModel], entity_id: str, *, actor: str, action: str,
               correlation_id: str = "") -> bool:
        """Remove an entity and append an audit event (used by provider re-sync)."""
        before = self.get(cls, entity_id)
        if before is None:
            return False
        event = AuditEvent(timestamp=utcnow(), actor=actor, action=action, entity=cls.__name__,
                           entity_id=entity_id, before=before.model_dump(mode="json"),
                           correlation_id=correlation_id)
        with self.transaction():
            self._conn.execute("DELETE FROM entities WHERE kind=? AND id=?", (cls.__name__, entity_id))
            self._conn.execute("INSERT INTO audit(body) VALUES (?)", (event.model_dump_json(exclude={"seq"}),))
        return True

    def append_audit(self, event: AuditEvent) -> None:
        with self._lock:
            self._conn.execute("INSERT INTO audit(body) VALUES (?)",
                               (event.model_dump_json(exclude={"seq"}),))

    def audit(self, limit: int = 200, entity_id: str | None = None) -> list[AuditEvent]:
        with self._lock:
            rows = self._conn.execute("SELECT seq, body FROM audit ORDER BY seq DESC").fetchall()
        events = []
        for seq, body in rows:
            ev = AuditEvent.model_validate_json(body)
            if entity_id and ev.entity_id != entity_id and ev.correlation_id != entity_id:
                continue
            events.append(ev.model_copy(update={"seq": seq}))
            if len(events) >= limit:
                break
        return events

    # ---- config
    def get_config(self) -> CompanyConfig:
        with self._lock:
            row = self._conn.execute("SELECT body FROM config WHERE key='company'").fetchone()
        if row is None:
            raise LookupError("company config not loaded")
        return CompanyConfig.model_validate_json(row[0])

    def set_config(self, cfg: CompanyConfig, *, actor: str) -> None:
        with self.transaction():
            self._conn.execute(
                "INSERT INTO config(key, body) VALUES ('company', ?) "
                "ON CONFLICT(key) DO UPDATE SET body=excluded.body", (cfg.model_dump_json(),))
            self._conn.execute("INSERT INTO audit(body) VALUES (?)", (AuditEvent(
                timestamp=utcnow(), actor=actor, action="config.set", entity="CompanyConfig",
                entity_id="company").model_dump_json(exclude={"seq"}),))

    def has_config(self) -> bool:
        with self._lock:
            return self._conn.execute("SELECT 1 FROM config WHERE key='company'").fetchone() is not None

    def reset(self) -> None:
        """Demo-only full reset. Audit history is the one thing we allow to be wiped,
        and only through this explicit path."""
        with self._lock:
            self._conn.execute("PRAGMA user_version = 1")
            self._conn.executescript("DELETE FROM entities; DELETE FROM config; DELETE FROM audit;")
            self._conn.execute("PRAGMA user_version = 0")

    def next_id(self, prefix: str, cls: type[BaseModel]) -> str:
        with self._lock:
            n = self._conn.execute("SELECT COUNT(*) FROM entities WHERE kind=?",
                                   (cls.__name__,)).fetchone()[0]
        candidate = n + 1
        while self.get(cls, f"{prefix}-{candidate}") is not None:  # type: ignore[arg-type]
            candidate += 1
        return f"{prefix}-{candidate}"


def dumps(obj: BaseModel) -> str:  # pragma: no cover - debugging helper
    return json.dumps(obj.model_dump(mode="json"), indent=2)
