from __future__ import annotations

from ppt_eval.flywheel import (
    ActiveSampler,
    FeedbackRecord,
    JsonlRecordStore,
    ParameterProposalService,
    ProposalStatus,
)


def test_active_sampler_prioritizes_uncertainty_disagreement_and_severity() -> None:
    low = FeedbackRecord("low", "r1", "c1", None, diversity_key="a")
    high = FeedbackRecord(
        "high", "r2", "c2", False, uncertainty=1, oracle_disagreement=1, severity=1, business_value=1, diversity_key="b"
    )
    ranked = ActiveSampler().rank((low, high))

    assert ranked[0][0].feedback_id == "high"
    assert ranked[0][1] > ranked[1][1]


def test_parameter_proposal_requires_validation_and_two_human_approvals(tmp_path) -> None:
    service = ParameterProposalService(JsonlRecordStore(tmp_path / "proposals.jsonl"))
    proposal = service.create(
        profile_id="text-v1",
        base_version="1.0",
        proposed_changes={"lambda_base": 0.58},
        rationale="calibration evidence",
        evidence_run_ids=("run-1",),
    )
    assert proposal.status == ProposalStatus.DRAFT

    try:
        service.approve(proposal.proposal_id, "reviewer-a")
    except ValueError as exc:
        assert "validated" in str(exc)
    else:
        raise AssertionError("unvalidated proposal must not be approved")

    validated = service.validate(
        proposal.proposal_id,
        {"frozen_set_passed": True, "challenge_set_passed": True, "shadow_recommended": True},
    )
    first = service.approve(validated.proposal_id, "evaluation-owner")
    second = service.approve(validated.proposal_id, "business-owner")

    assert first.status == ProposalStatus.APPROVED
    assert second.status == ProposalStatus.RELEASE_CANDIDATE
    assert not hasattr(service, "release")

    restored = ParameterProposalService(JsonlRecordStore(tmp_path / "proposals.jsonl"))
    assert restored.get(proposal.proposal_id).status == ProposalStatus.RELEASE_CANDIDATE


def test_feedback_rejects_ambiguous_acceptance_signal() -> None:
    try:
        FeedbackRecord("f", "r", "c", True, abandoned=True)
    except ValueError as exc:
        assert "accepted and abandoned" in str(exc)
    else:
        raise AssertionError("ambiguous signal should be rejected")
