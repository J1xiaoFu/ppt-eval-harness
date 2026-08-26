from __future__ import annotations

import hashlib
import json
import os
import threading
import uuid
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True, slots=True)
class EditOperation:
    operation: str
    slide_number: int | None = None
    object_id: str | None = None
    before_hash: str | None = None
    after_hash: str | None = None
    payload: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class FeedbackRecord:
    feedback_id: str
    run_id: str
    case_id: str
    accepted: bool | None
    abandoned: bool = False
    edit_operations: tuple[EditOperation, ...] = ()
    modification_seconds: float | None = None
    human_label: str | None = None
    uncertainty: float = 0.0
    oracle_disagreement: float = 0.0
    severity: float = 0.0
    business_value: float = 0.0
    diversity_key: str = "unknown"
    created_at: str = field(default_factory=_now)

    def __post_init__(self) -> None:
        if self.accepted not in (True, False, None):
            raise ValueError("accepted must be true, false, or null")
        for name in ("uncertainty", "oracle_disagreement", "severity", "business_value"):
            if not 0 <= float(getattr(self, name)) <= 1:
                raise ValueError(f"{name} must be between zero and one")
        if self.accepted is True and self.abandoned:
            raise ValueError("accepted and abandoned cannot both be true")
        if self.modification_seconds is not None and self.modification_seconds < 0:
            raise ValueError("modification_seconds cannot be negative")


class ProposalStatus(str, Enum):
    DRAFT = "DRAFT"
    VALIDATED = "VALIDATED"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    RELEASE_CANDIDATE = "RELEASE_CANDIDATE"


@dataclass(frozen=True, slots=True)
class ParameterProposal:
    proposal_id: str
    profile_id: str
    base_version: str
    proposed_changes: Mapping[str, Any]
    rationale: str
    evidence_run_ids: tuple[str, ...]
    status: ProposalStatus = ProposalStatus.DRAFT
    validation_report: Mapping[str, Any] = field(default_factory=dict)
    approvals: tuple[str, ...] = ()
    created_at: str = field(default_factory=_now)
    updated_at: str = field(default_factory=_now)


class JsonlRecordStore:
    """Append-only record stream with idempotent IDs and atomic line writes."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    def append(self, record: Any) -> None:
        payload = asdict(record)
        payload = _enum_values(payload)
        with self._lock, self.path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())

    def read(self) -> list[dict[str, Any]]:
        if not self.path.is_file():
            return []
        return [json.loads(line) for line in self.path.read_text(encoding="utf-8").splitlines() if line]


class ActiveSampler:
    """Rank feedback for human labeling while retaining representative coverage."""

    def __init__(self, *, random_fraction: float = 0.10) -> None:
        if not 0 <= random_fraction <= 1:
            raise ValueError("random_fraction must be between zero and one")
        self.random_fraction = random_fraction

    def rank(self, records: Sequence[FeedbackRecord]) -> list[tuple[FeedbackRecord, float]]:
        ranked = []
        seen: dict[str, int] = {}
        for record in records:
            diversity_bonus = 1 / (1 + seen.get(record.diversity_key, 0))
            seen[record.diversity_key] = seen.get(record.diversity_key, 0) + 1
            deterministic_random = int(
                hashlib.sha256(record.feedback_id.encode("utf-8")).hexdigest()[:8], 16
            ) / 0xFFFFFFFF
            score = (
                0.28 * record.uncertainty
                + 0.24 * record.oracle_disagreement
                + 0.22 * record.severity
                + 0.16 * record.business_value
                + 0.10 * diversity_bonus
                + self.random_fraction * 0.05 * deterministic_random
            )
            ranked.append((record, round(score, 6)))
        return sorted(ranked, key=lambda item: (-item[1], item[0].feedback_id))


class ParameterProposalService:
    """Governed proposal workflow; v1 deliberately has no production apply method."""

    def __init__(
        self,
        store: JsonlRecordStore,
        event_sink: Callable[[ParameterProposal], None] | None = None,
    ) -> None:
        self.store = store
        self.event_sink = event_sink
        self._latest: dict[str, ParameterProposal] = {}
        for payload in store.read():
            proposal = ParameterProposal(
                proposal_id=str(payload["proposal_id"]),
                profile_id=str(payload["profile_id"]),
                base_version=str(payload["base_version"]),
                proposed_changes=dict(payload["proposed_changes"]),
                rationale=str(payload["rationale"]),
                evidence_run_ids=tuple(payload["evidence_run_ids"]),
                status=ProposalStatus(payload["status"]),
                validation_report=dict(payload.get("validation_report", {})),
                approvals=tuple(payload.get("approvals", ())),
                created_at=str(payload["created_at"]),
                updated_at=str(payload["updated_at"]),
            )
            self._latest[proposal.proposal_id] = proposal

    def create(
        self,
        *,
        profile_id: str,
        base_version: str,
        proposed_changes: Mapping[str, Any],
        rationale: str,
        evidence_run_ids: Sequence[str],
    ) -> ParameterProposal:
        if not proposed_changes or not rationale.strip() or not evidence_run_ids:
            raise ValueError("proposal requires changes, rationale, and evidence runs")
        proposal = ParameterProposal(
            proposal_id=f"proposal-{uuid.uuid4().hex}",
            profile_id=profile_id,
            base_version=base_version,
            proposed_changes=dict(proposed_changes),
            rationale=rationale,
            evidence_run_ids=tuple(evidence_run_ids),
        )
        self._record(proposal)
        return proposal

    def validate(self, proposal_id: str, report: Mapping[str, Any]) -> ParameterProposal:
        proposal = self.get(proposal_id)
        if proposal.status != ProposalStatus.DRAFT:
            raise ValueError("only DRAFT proposals can be validated")
        required = {"frozen_set_passed", "challenge_set_passed", "shadow_recommended"}
        if not required <= set(report):
            raise ValueError(f"validation report requires {sorted(required)}")
        status = ProposalStatus.VALIDATED if all(bool(report[key]) for key in required) else ProposalStatus.REJECTED
        updated = replace(proposal, status=status, validation_report=dict(report), updated_at=_now())
        self._record(updated)
        return updated

    def approve(self, proposal_id: str, approver: str) -> ParameterProposal:
        proposal = self.get(proposal_id)
        if not approver.strip():
            raise ValueError("approver must not be blank")
        if proposal.status not in {ProposalStatus.VALIDATED, ProposalStatus.APPROVED}:
            raise ValueError("only validated proposals can be approved")
        approvals = tuple(dict.fromkeys((*proposal.approvals, approver)))
        status = ProposalStatus.APPROVED if len(approvals) < 2 else ProposalStatus.RELEASE_CANDIDATE
        updated = replace(proposal, status=status, approvals=approvals, updated_at=_now())
        self._record(updated)
        return updated

    def get(self, proposal_id: str) -> ParameterProposal:
        try:
            return self._latest[proposal_id]
        except KeyError as exc:
            raise KeyError(proposal_id) from exc

    def _record(self, proposal: ParameterProposal) -> None:
        self._latest[proposal.proposal_id] = proposal
        self.store.append(proposal)
        if self.event_sink is not None:
            self.event_sink(proposal)


def feedback_from_mapping(payload: Mapping[str, Any]) -> FeedbackRecord:
    return FeedbackRecord(
        feedback_id=str(payload.get("feedback_id") or f"feedback-{uuid.uuid4().hex}"),
        run_id=str(payload["run_id"]),
        case_id=str(payload["case_id"]),
        accepted=payload.get("accepted"),
        abandoned=bool(payload.get("abandoned", False)),
        edit_operations=tuple(EditOperation(**item) for item in payload.get("edit_operations", ())),
        modification_seconds=payload.get("modification_seconds"),
        human_label=payload.get("human_label"),
        uncertainty=float(payload.get("uncertainty", 0.0)),
        oracle_disagreement=float(payload.get("oracle_disagreement", 0.0)),
        severity=float(payload.get("severity", 0.0)),
        business_value=float(payload.get("business_value", 0.0)),
        diversity_key=str(payload.get("diversity_key", "unknown")),
    )


def _enum_values(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {key: _enum_values(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_enum_values(item) for item in value]
    return value
