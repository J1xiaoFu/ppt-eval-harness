"""Thread-safe, fail-closed model request budget reservations.

The visual pipeline can outlive a scheduler timeout because Python cannot stop
an in-flight HTTP call.  Post-hoc usage counters are therefore insufficient as
a hard request cap: another DAG node could spend the same capacity while the
timed-out worker is still running.  This ledger reserves each provider call's
worst-case HTTP attempt count before the call starts.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class ModelRequestReservation:
    """Opaque capacity grant for one bounded provider invocation."""

    reservation_id: int
    reserved_attempts: int
    owner: str | None = None


@dataclass(frozen=True, slots=True)
class ModelRequestBudgetSnapshot:
    """Immutable, auditable view of a request ledger."""

    maximum_requests: int
    settled_actual_attempts: int
    in_flight_reserved_attempts: int
    remaining_attempts: int
    active_reservations: int
    settled_reservations: int

    @property
    def accounted_upper_bound(self) -> int:
        """Actual settled spend plus worst-case outstanding spend."""

        return self.settled_actual_attempts + self.in_flight_reserved_attempts

    @property
    def exhausted(self) -> bool:
        return self.remaining_attempts == 0

    def to_mapping(self) -> dict[str, int | bool]:
        return {
            "maximum_requests": self.maximum_requests,
            "settled_actual_attempts": self.settled_actual_attempts,
            "in_flight_reserved_attempts": self.in_flight_reserved_attempts,
            "accounted_upper_bound": self.accounted_upper_bound,
            "remaining_attempts": self.remaining_attempts,
            "active_reservations": self.active_reservations,
            "settled_reservations": self.settled_reservations,
            "exhausted": self.exhausted,
        }


class ModelRequestBudgetLedger:
    """Atomically reserve and settle a per-evaluation HTTP attempt budget.

    An outstanding reservation is deliberately never reclaimed based only on
    scheduler cancellation or elapsed time.  If its worker never returns, its
    full upper bound remains charged, which is the only safe accounting choice.
    Once the provider returns, :meth:`settle` commits the actual attempt count
    and releases only the unused part of that reservation.

    ``EvaluationContext.memo`` is deep-copied for timeout isolation.  Returning
    ``self`` from ``__deepcopy__`` keeps this synchronization primitive shared
    across those isolated views, so a late worker and subsequent DAG nodes
    cannot reserve the same capacity.
    """

    __slots__ = (
        "_active",
        "_lock",
        "_maximum_requests",
        "_next_reservation_id",
        "_settled_actual_attempts",
        "_settled_reservations",
    )

    def __init__(self, maximum_requests: int) -> None:
        self._maximum_requests = _positive_int(
            maximum_requests,
            "maximum_requests",
        )
        self._lock = threading.Lock()
        self._active: dict[int, ModelRequestReservation] = {}
        self._next_reservation_id = 1
        self._settled_actual_attempts = 0
        self._settled_reservations = 0

    @property
    def maximum_requests(self) -> int:
        return self._maximum_requests

    def __copy__(self) -> ModelRequestBudgetLedger:
        return self

    def __deepcopy__(self, memo: dict[int, Any]) -> ModelRequestBudgetLedger:
        memo[id(self)] = self
        return self

    def reserve(
        self,
        maximum_attempts: int,
        *,
        owner: str | None = None,
    ) -> ModelRequestReservation | None:
        """Reserve a provider's maximum attempts, or return ``None``.

        Returning ``None`` performs no mutation.  Callers must not start the
        provider invocation unless a reservation was returned.
        """

        requested = _positive_int(maximum_attempts, "maximum_attempts")
        normalized_owner = _optional_owner(owner)
        with self._lock:
            in_flight = sum(
                reservation.reserved_attempts
                for reservation in self._active.values()
            )
            remaining = (
                self._maximum_requests
                - self._settled_actual_attempts
                - in_flight
            )
            if requested > remaining:
                return None
            reservation = ModelRequestReservation(
                reservation_id=self._next_reservation_id,
                reserved_attempts=requested,
                owner=normalized_owner,
            )
            self._next_reservation_id += 1
            self._active[reservation.reservation_id] = reservation
            return reservation

    def settle(
        self,
        reservation: ModelRequestReservation,
        *,
        actual_attempts: int,
    ) -> ModelRequestBudgetSnapshot:
        """Commit actual attempts and release unused reserved capacity."""

        actual = _nonnegative_int(actual_attempts, "actual_attempts")
        if actual > reservation.reserved_attempts:
            raise ValueError(
                "actual_attempts cannot exceed the reserved provider bound"
            )
        with self._lock:
            active = self._active.get(reservation.reservation_id)
            if active is None:
                raise ValueError("reservation is unknown or already settled")
            if active != reservation:
                raise ValueError("reservation does not match the active grant")
            del self._active[reservation.reservation_id]
            self._settled_actual_attempts += actual
            self._settled_reservations += 1
            return self._snapshot_locked()

    def snapshot(self) -> ModelRequestBudgetSnapshot:
        with self._lock:
            return self._snapshot_locked()

    def _snapshot_locked(self) -> ModelRequestBudgetSnapshot:
        in_flight = sum(
            reservation.reserved_attempts for reservation in self._active.values()
        )
        accounted = self._settled_actual_attempts + in_flight
        if accounted > self._maximum_requests:  # pragma: no cover - invariant guard
            raise RuntimeError("model request budget invariant was violated")
        return ModelRequestBudgetSnapshot(
            maximum_requests=self._maximum_requests,
            settled_actual_attempts=self._settled_actual_attempts,
            in_flight_reserved_attempts=in_flight,
            remaining_attempts=self._maximum_requests - accounted,
            active_reservations=len(self._active),
            settled_reservations=self._settled_reservations,
        )


def _positive_int(value: int, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _nonnegative_int(value: int, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")
    return value


def _optional_owner(value: str | None) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError("owner must be a non-blank string when provided")
    return value.strip()


__all__ = [
    "ModelRequestBudgetLedger",
    "ModelRequestBudgetSnapshot",
    "ModelRequestReservation",
]
