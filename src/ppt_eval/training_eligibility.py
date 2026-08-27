"""Isolated v8 contracts for deciding whether a deck may enter training.

The evaluation decision and the training decision answer different questions.
A high PPT quality score is not sufficient when critical defects exist, and
missing evidence must route review instead of being interpreted as poor
quality.  This module deliberately has no dependency on the current runtime so
the v8 policy can be reviewed before it is wired into production composition.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from math import isfinite
from typing import Mapping, Sequence

TRAIN_THRESHOLD = 80.0
REVIEW_THRESHOLD = 60.0
TRAINING_ELIGIBILITY_SCHEMA_VERSION = "1.0"


class TrainingTrack(str, Enum):
    """The independently governed datasets a deck may contribute to."""

    VISUAL = "visual"
    LAYOUT = "layout"
    CONTENT = "content"
    FULL_DECK = "full_deck"


TrackScores = Mapping[TrainingTrack, float | None] | Mapping[str, float | None]


class TrainingTrackStatus(str, Enum):
    """A training disposition, intentionally separate from EvalDecision."""

    TRAIN = "TRAIN"
    REVIEW = "REVIEW"
    REJECT = "REJECT"


TRACK_ORDER = (
    TrainingTrack.VISUAL,
    TrainingTrack.LAYOUT,
    TrainingTrack.CONTENT,
    TrainingTrack.FULL_DECK,
)


@dataclass(frozen=True, slots=True)
class TrainingTrackDecision:
    """One auditable decision for one training track."""

    track: TrainingTrack
    status: TrainingTrackStatus
    score: float | None
    reason_codes: tuple[str, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.track, TrainingTrack):
            object.__setattr__(self, "track", TrainingTrack(self.track))
        if not isinstance(self.status, TrainingTrackStatus):
            object.__setattr__(self, "status", TrainingTrackStatus(self.status))
        object.__setattr__(self, "score", _score(self.score))
        normalized_reasons = tuple(
            dict.fromkeys(str(reason).strip() for reason in self.reason_codes if str(reason).strip())
        )
        if not normalized_reasons:
            raise ValueError("training decisions require at least one reason code")
        object.__setattr__(self, "reason_codes", normalized_reasons)


@dataclass(frozen=True, slots=True)
class TrainingEligibility:
    """Immutable result for all four v8 training tracks."""

    decisions: tuple[TrainingTrackDecision, ...]
    train_threshold: float = TRAIN_THRESHOLD
    review_threshold: float = REVIEW_THRESHOLD
    raster_only: bool = False
    content_evidence_available: bool = True
    critical_issue_codes: tuple[str, ...] = ()
    schema_version: str = TRAINING_ELIGIBILITY_SCHEMA_VERSION

    def __post_init__(self) -> None:
        train_threshold, review_threshold = _thresholds(
            self.train_threshold, self.review_threshold
        )
        object.__setattr__(self, "train_threshold", train_threshold)
        object.__setattr__(self, "review_threshold", review_threshold)
        if not isinstance(self.raster_only, bool):
            raise TypeError("raster_only must be a bool")
        if not isinstance(self.content_evidence_available, bool):
            raise TypeError("content_evidence_available must be a bool")
        critical_codes = _reason_codes(self.critical_issue_codes)
        object.__setattr__(self, "critical_issue_codes", critical_codes)
        if tuple(decision.track for decision in self.decisions) != TRACK_ORDER:
            raise ValueError(
                "training eligibility must contain visual, layout, content, and full_deck "
                "decisions in contract order"
            )
        if not str(self.schema_version).strip():
            raise ValueError("schema_version must not be blank")

    @classmethod
    def assess(
        cls,
        track_scores: TrackScores,
        *,
        critical_issue_codes: Sequence[str] = (),
        content_evidence_available: bool = True,
        raster_only: bool = False,
        train_threshold: float = TRAIN_THRESHOLD,
        review_threshold: float = REVIEW_THRESHOLD,
    ) -> "TrainingEligibility":
        """Apply the deterministic v8 precedence rules.

        Precedence is deliberate: key CRITICAL issues reject every track;
        raster-only decks may enter only the visual track; an observed score
        below the review threshold rejects the affected track even when other
        evidence is missing.  Evidence debt is considered only after that
        explicit failure, so it cannot conceal a known-bad score.  A missing
        score remains REVIEW, never an inferred zero.
        """

        train_threshold, review_threshold = _thresholds(
            train_threshold, review_threshold
        )
        scores = _track_scores(track_scores)
        critical_codes = _reason_codes(critical_issue_codes)
        decisions = tuple(
            _decide_track(
                track,
                scores[track],
                critical_codes=critical_codes,
                content_evidence_available=content_evidence_available,
                raster_only=raster_only,
                train_threshold=train_threshold,
                review_threshold=review_threshold,
            )
            for track in TRACK_ORDER
        )
        return cls(
            decisions=decisions,
            train_threshold=train_threshold,
            review_threshold=review_threshold,
            raster_only=raster_only,
            content_evidence_available=content_evidence_available,
            critical_issue_codes=critical_codes,
        )

    def for_track(self, track: TrainingTrack | str) -> TrainingTrackDecision:
        requested = TrainingTrack(track)
        return next(decision for decision in self.decisions if decision.track == requested)

    @property
    def trainable_tracks(self) -> tuple[TrainingTrack, ...]:
        return tuple(
            decision.track
            for decision in self.decisions
            if decision.status == TrainingTrackStatus.TRAIN
        )

    def to_mapping(self) -> dict[str, object]:
        """Return a stable JSON-compatible representation for audit storage."""

        return {
            "schema_version": self.schema_version,
            "train_threshold": self.train_threshold,
            "review_threshold": self.review_threshold,
            "raster_only": self.raster_only,
            "content_evidence_available": self.content_evidence_available,
            "critical_issue_codes": list(self.critical_issue_codes),
            "decisions": [
                {
                    "track": decision.track.value,
                    "status": decision.status.value,
                    "score": decision.score,
                    "reason_codes": list(decision.reason_codes),
                }
                for decision in self.decisions
            ],
        }


def assess_training_eligibility(
    track_scores: TrackScores,
    *,
    critical_issue_codes: Sequence[str] = (),
    content_evidence_available: bool = True,
    raster_only: bool = False,
    train_threshold: float = TRAIN_THRESHOLD,
    review_threshold: float = REVIEW_THRESHOLD,
) -> TrainingEligibility:
    """Functional entry point for callers that do not need the class factory."""

    return TrainingEligibility.assess(
        track_scores,
        critical_issue_codes=critical_issue_codes,
        content_evidence_available=content_evidence_available,
        raster_only=raster_only,
        train_threshold=train_threshold,
        review_threshold=review_threshold,
    )


def _decide_track(
    track: TrainingTrack,
    score: float | None,
    *,
    critical_codes: tuple[str, ...],
    content_evidence_available: bool,
    raster_only: bool,
    train_threshold: float,
    review_threshold: float,
) -> TrainingTrackDecision:
    if critical_codes:
        return TrainingTrackDecision(
            track,
            TrainingTrackStatus.REJECT,
            score,
            tuple(f"critical:{code}" for code in critical_codes),
        )
    if raster_only and track != TrainingTrack.VISUAL:
        return TrainingTrackDecision(
            track,
            TrainingTrackStatus.REJECT,
            score,
            ("raster_only:visual_track_only",),
        )
    if score is not None and score < review_threshold:
        return TrainingTrackDecision(
            track,
            TrainingTrackStatus.REJECT,
            score,
            ("score:below_review_threshold",),
        )
    if not content_evidence_available and track in {
        TrainingTrack.CONTENT,
        TrainingTrack.FULL_DECK,
    }:
        return TrainingTrackDecision(
            track,
            TrainingTrackStatus.REVIEW,
            score,
            ("content_evidence:missing",),
        )
    if score is None:
        return TrainingTrackDecision(
            track,
            TrainingTrackStatus.REVIEW,
            None,
            ("score:missing",),
        )
    if score >= train_threshold:
        return TrainingTrackDecision(
            track,
            TrainingTrackStatus.TRAIN,
            score,
            ("score:at_or_above_train_threshold",),
        )
    if score >= review_threshold:
        return TrainingTrackDecision(
            track,
            TrainingTrackStatus.REVIEW,
            score,
            ("score:review_band",),
        )
    raise AssertionError("score threshold branches must be exhaustive")


def _track_scores(
    values: TrackScores,
) -> dict[TrainingTrack, float | None]:
    scores: dict[TrainingTrack, float | None] = {track: None for track in TRACK_ORDER}
    seen: set[TrainingTrack] = set()
    for raw_track, raw_score in values.items():
        track = TrainingTrack(raw_track)
        if track in seen:
            raise ValueError(f"duplicate training track score: {track.value}")
        seen.add(track)
        scores[track] = _score(raw_score)
    return scores


def _score(value: float | None) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError("training track scores must be numbers or null")
    score = float(value)
    if not isfinite(score) or not 0.0 <= score <= 100.0:
        raise ValueError("training track scores must be finite and between 0 and 100")
    return score


def _thresholds(train_threshold: float, review_threshold: float) -> tuple[float, float]:
    train = _score(train_threshold)
    review = _score(review_threshold)
    if train is None or review is None:  # values are statically non-optional
        raise TypeError("training thresholds must be numbers")
    if review > train:
        raise ValueError("thresholds must satisfy 0 <= review <= train <= 100")
    return train, review


def _reason_codes(values: Sequence[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(str(value).strip() for value in values if str(value).strip()))


__all__ = [
    "REVIEW_THRESHOLD",
    "TRACK_ORDER",
    "TRAIN_THRESHOLD",
    "TRAINING_ELIGIBILITY_SCHEMA_VERSION",
    "TrainingEligibility",
    "TrainingTrack",
    "TrainingTrackDecision",
    "TrainingTrackStatus",
    "assess_training_eligibility",
]
