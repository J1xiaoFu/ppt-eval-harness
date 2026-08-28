"""Append-only audit port and a small in-memory reference implementation."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from datetime import datetime, timezone
from threading import Lock
from typing import Any, Callable, Mapping, Protocol
from uuid import uuid4

from ppt_eval.domain import AuditEvent


class AuditLog(Protocol):
    def append(
        self,
        *,
        run_id: str,
        event_type: str,
        actor: str,
        payload: Mapping[str, Any],
        supersedes: str | None = None,
        occurred_at: str | None = None,
    ) -> AuditEvent:
        ...


class InMemoryAuditLog:
    """Hash-chained reference log; production adapters persist the same events."""

    def __init__(
        self,
        *,
        id_factory: Callable[[str], str] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._events: list[AuditEvent] = []
        self._id_factory = id_factory or (lambda prefix: f"{prefix}-{uuid4()}")
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._lock = Lock()

    @property
    def events(self) -> tuple[AuditEvent, ...]:
        with self._lock:
            return tuple(self._events)

    def append(
        self,
        *,
        run_id: str,
        event_type: str,
        actor: str,
        payload: Mapping[str, Any],
        supersedes: str | None = None,
        occurred_at: str | None = None,
    ) -> AuditEvent:
        with self._lock:
            if supersedes is not None and supersedes not in {
                item.event_id for item in self._events
            }:
                raise ValueError("supersedes must reference an existing audit event")
            previous_hash = self._events[-1].event_hash if self._events else None
            core = {
                "event_id": self._id_factory("event"),
                "run_id": run_id,
                "event_type": event_type,
                "occurred_at": occurred_at or self._clock().isoformat(),
                "actor": actor,
                "payload": dict(payload),
                "previous_hash": previous_hash,
                "supersedes": supersedes,
            }
            digest = hashlib.sha256(
                json.dumps(core, ensure_ascii=True, sort_keys=True, default=str).encode(
                    "utf-8"
                )
            ).hexdigest()
            event = AuditEvent(
                event_id=str(core["event_id"]),
                run_id=str(core["run_id"]),
                event_type=str(core["event_type"]),
                occurred_at=str(core["occurred_at"]),
                actor=str(core["actor"]),
                payload=dict(payload),
                previous_hash=previous_hash,
                event_hash=digest,
                supersedes=supersedes,
            )
            self._events.append(event)
            return event

    def verify_chain(self) -> bool:
        with self._lock:
            previous: str | None = None
            for event in self._events:
                data = asdict(event)
                event_hash = data.pop("event_hash")
                data.pop("schema_version")
                if data["previous_hash"] != previous:
                    return False
                digest = hashlib.sha256(
                    json.dumps(
                        data, ensure_ascii=True, sort_keys=True, default=str
                    ).encode("utf-8")
                ).hexdigest()
                if event_hash != digest:
                    return False
                previous = event_hash
            return True
