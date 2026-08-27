from __future__ import annotations

import json
from pathlib import Path

import pytest

from ppt_eval.config import load_profile
from ppt_eval.training_eligibility import (
    TRACK_ORDER,
    TrainingEligibility,
    TrainingTrack,
    TrainingTrackStatus,
    assess_training_eligibility,
)

BASE_WEIGHTS = {
    "content_structure": 0.25,
    "composition_craft": 0.20,
    "typography_craft": 0.10,
    "palette_craft": 0.08,
    "visual_communication": 0.15,
    "visual_system_sequence": 0.10,
    "authorship_specificity": 0.12,
}

PROFILE_CONTRACTS = {
    "text_generation_v8.json": (
        0.55,
        {"instruction": 0.45, "audience": 0.25, "fact_claim": 0.30},
    ),
    "project_summary_v8.json": (
        0.40,
        {
            "source_claim": 0.30,
            "key_point": 0.25,
            "numeric": 0.20,
            "compression_richness": 0.15,
            "traceability": 0.10,
        },
    ),
    "multimodal_generation_v8.json": (
        0.45,
        {
            "asset_coverage": 0.30,
            "asset_presentation": 0.25,
            "crop_image_integrity": 0.15,
            "chart_fidelity": 0.20,
            "media_integrity": 0.10,
        },
    ),
    "finished_deck_v8.json": (1.0, {}),
}


def _scores(value: float | None) -> dict[TrainingTrack, float | None]:
    return {track: value for track in TRACK_ORDER}


def test_score_boundaries_produce_train_review_and_reject() -> None:
    eligibility = assess_training_eligibility(
        {
            TrainingTrack.VISUAL: 80,
            TrainingTrack.LAYOUT: 60,
            TrainingTrack.CONTENT: 59.999,
            TrainingTrack.FULL_DECK: None,
        }
    )

    assert eligibility.for_track("visual").status == TrainingTrackStatus.TRAIN
    assert eligibility.for_track("layout").status == TrainingTrackStatus.REVIEW
    assert eligibility.for_track("content").status == TrainingTrackStatus.REJECT
    assert eligibility.for_track("full_deck").status == TrainingTrackStatus.REVIEW
    assert eligibility.trainable_tracks == (TrainingTrack.VISUAL,)


def test_key_critical_issues_reject_every_training_track() -> None:
    eligibility = TrainingEligibility.assess(
        _scores(100), critical_issue_codes=("privacy_leak", "malware")
    )

    assert all(
        decision.status == TrainingTrackStatus.REJECT
        for decision in eligibility.decisions
    )
    assert eligibility.for_track("visual").reason_codes == (
        "critical:privacy_leak",
        "critical:malware",
    )


def test_missing_content_evidence_routes_content_tracks_to_review() -> None:
    eligibility = TrainingEligibility.assess(
        _scores(95), content_evidence_available=False
    )

    assert eligibility.for_track("visual").status == TrainingTrackStatus.TRAIN
    assert eligibility.for_track("layout").status == TrainingTrackStatus.TRAIN
    assert eligibility.for_track("content").status == TrainingTrackStatus.REVIEW
    assert eligibility.for_track("full_deck").status == TrainingTrackStatus.REVIEW
    assert eligibility.for_track("content").reason_codes == (
        "content_evidence:missing",
    )


def test_raster_only_decks_are_eligible_only_for_visual_training() -> None:
    eligibility = TrainingEligibility.assess(_scores(95), raster_only=True)

    assert eligibility.trainable_tracks == (TrainingTrack.VISUAL,)
    for track in (
        TrainingTrack.LAYOUT,
        TrainingTrack.CONTENT,
        TrainingTrack.FULL_DECK,
    ):
        decision = eligibility.for_track(track)
        assert decision.status == TrainingTrackStatus.REJECT
        assert decision.reason_codes == ("raster_only:visual_track_only",)


def test_contract_validates_scores_thresholds_and_complete_track_order() -> None:
    with pytest.raises(ValueError, match="between 0 and 100"):
        TrainingEligibility.assess({"visual": 101})
    with pytest.raises(ValueError, match="review <= train"):
        TrainingEligibility.assess({}, train_threshold=60, review_threshold=80)
    with pytest.raises(ValueError, match="contract order"):
        TrainingEligibility(decisions=())


def test_mapping_is_json_compatible_and_preserves_contract_order() -> None:
    payload = TrainingEligibility.assess(_scores(85)).to_mapping()
    decisions = payload["decisions"]

    assert isinstance(decisions, list)
    assert [item["track"] for item in decisions] == [
        "visual",
        "layout",
        "content",
        "full_deck",
    ]
    assert json.loads(json.dumps(payload))["train_threshold"] == 80.0


def test_v8_profile_drafts_freeze_construct_and_scene_contracts() -> None:
    for filename, expected in PROFILE_CONTRACTS.items():
        profile = json.loads(
            (Path("configs/profiles") / filename).read_text(encoding="utf-8")
        )
        expected_lambda, expected_scene_weights = expected

        assert profile["version"] == "8.1"
        assert profile["lambda_base"] == expected_lambda
        assert profile["base_weights"] == BASE_WEIGHTS
        assert profile["scene_weights"] == expected_scene_weights
        assert sum(profile["base_weights"].values()) == pytest.approx(1.0)
        if expected_scene_weights:
            assert sum(profile["scene_weights"].values()) == pytest.approx(1.0)
        assert profile["pass_threshold"] == 80
        assert profile["review_threshold"] == 60
        assert profile["metadata"]["lifecycle"] == "PRE_RESEARCH"
        assert profile["metadata"]["production_approved"] is True
        assert profile["metadata"]["runtime_wired"] is True

        loaded = load_profile(Path("configs/profiles") / filename)
        assert loaded.profile_id == profile["profile_id"]
        assert sum(loaded.base_weights.values()) == pytest.approx(1.0)

        eligibility = profile["metadata"]["training_eligibility"]
        assert eligibility == {
            "schema_version": "1.0",
            "tracks": ["visual", "layout", "content", "full_deck"],
            "statuses": ["TRAIN", "REVIEW", "REJECT"],
            "train_threshold": 80,
            "review_threshold": 60,
            "critical_status": "REJECT",
            "missing_content_evidence_status": "REVIEW",
            "raster_only_tracks": ["visual"],
        }
