"""Built-in deterministic Oracle catalog and factories."""

from __future__ import annotations

from typing import TYPE_CHECKING, Mapping

from ppt_eval.domain.enums import SceneType

if TYPE_CHECKING:
    from ppt_eval.adapters import ModelAuditProvider, PptxAdapter
    from ppt_eval.application.oracle import Oracle, OracleRegistry

    from .model_source_access import ModelSourceAccessPolicy

from .baseline import (
    AccessibilityOracle,
    BaselinePptQualityOracle,
    BodyCompletenessOracle,
    CompatibilityOracle,
    ContentClarityOracle,
    CriticalContentVisibilityOracle,
    EditabilityOracle,
    FileDeliverabilityOracle,
    InternalDataConsistencyOracle,
    LayoutOracle,
    MultimediaQualityOracle,
    NarrativeOracle,
    StyleConsistencyOracle,
    TemplateResidueOracle,
    TypographyOracle,
    VisualHierarchyOracle,
)
from .model_audits import (
    STRUCTURED_DIMENSIONS_MODEL_AUDIT_COMPOSITE_ID,
    STRUCTURED_MODEL_AUDIT_COMPOSITE_ID,
    STRUCTURED_VLM_VISUAL_CRITERIA,
    STRUCTURED_VLM_VISUAL_CRITERION_IDS,
    STRUCTURED_VLM_VISUAL_DIMENSION_METRICS,
    STRUCTURED_VLM_VISUAL_DIMENSIONS_PROMPT,
    AdvancedLlmContentReviewOracle,
    AdvancedLlmScenarioReviewOracle,
    AdvancedModelReviewOracle,
    AdvancedVlmVisualReviewOracle,
    HighCostModelAuditOracle,
    LlmContentQualityAuditOracle,
    LlmScenarioComplianceAuditOracle,
    StructuredDimensionsModelAuditOracle,
    StructuredModelAuditOracle,
    StructuredVlmVisualAuditOracle,
    StructuredVlmVisualDimensionsAuditOracle,
    VlmVisualQualityAuditOracle,
)
from .model_source_access import ModelSourceAccessPolicy
from .scenarios import (
    AssetComplianceOracle,
    AssetPresentationOracle,
    AudienceFitOracle,
    ChartDataAccuracyOracle,
    CompressionQualityOracle,
    CriticalChartDataAccuracyOracle,
    CriticalInstructionComplianceOracle,
    CriticalSourceConsistencyOracle,
    CropClarityOracle,
    FactQualityOracle,
    InstructionCoverageOracle,
    KeyPointRecallOracle,
    MediaAvailabilityOracle,
    MultimodalQualityOracle,
    NumericAccuracyOracle,
    ProjectSummaryQualityOracle,
    RequiredAssetComplianceOracle,
    SourceFaithfulnessOracle,
    TextGenerationQualityOracle,
    TraceabilityOracle,
)

SCENE_ORACLE_IDS: Mapping[SceneType, tuple[str, ...]] = {
    SceneType.TEXT_TO_PPT: (TextGenerationQualityOracle.oracle_id,),
    SceneType.PROJECT_SUMMARY: (ProjectSummaryQualityOracle.oracle_id,),
    SceneType.MULTIMODAL: (MultimodalQualityOracle.oracle_id,),
    SceneType.READY_MADE: (),
}


def build_default_oracles(
    adapter: PptxAdapter | None = None,
    *,
    llm_provider: ModelAuditProvider | None = None,
    vlm_provider: ModelAuditProvider | None = None,
    model_source_access_policy: ModelSourceAccessPolicy | None = None,
) -> tuple[Oracle, ...]:
    """Return the baseline plus deterministic and optional model composites."""

    return (
        BaselinePptQualityOracle(adapter),
        TextGenerationQualityOracle(adapter),
        ProjectSummaryQualityOracle(adapter),
        MultimodalQualityOracle(adapter),
        HighCostModelAuditOracle(
            adapter,
            llm_provider=llm_provider,
            vlm_provider=vlm_provider,
            source_access_policy=model_source_access_policy,
        ),
        StructuredModelAuditOracle(
            adapter,
            llm_provider=llm_provider,
            vlm_provider=vlm_provider,
            source_access_policy=model_source_access_policy,
        ),
        StructuredDimensionsModelAuditOracle(
            adapter,
            llm_provider=llm_provider,
            vlm_provider=vlm_provider,
            source_access_policy=model_source_access_policy,
        ),
    )


def build_default_registry(
    adapter: PptxAdapter | None = None,
    *,
    llm_provider: ModelAuditProvider | None = None,
    vlm_provider: ModelAuditProvider | None = None,
    model_source_access_policy: ModelSourceAccessPolicy | None = None,
) -> OracleRegistry:
    """Create the Harness registry without making infrastructure import it."""

    from ppt_eval.application.oracle import OracleRegistry

    return OracleRegistry(
        build_default_oracles(
            adapter,
            llm_provider=llm_provider,
            vlm_provider=vlm_provider,
            model_source_access_policy=model_source_access_policy,
        )
    )


__all__ = [
    "AdvancedLlmContentReviewOracle",
    "AdvancedLlmScenarioReviewOracle",
    "AdvancedModelReviewOracle",
    "AdvancedVlmVisualReviewOracle",
    "AccessibilityOracle",
    "AssetComplianceOracle",
    "AssetPresentationOracle",
    "AudienceFitOracle",
    "BaselinePptQualityOracle",
    "BodyCompletenessOracle",
    "ChartDataAccuracyOracle",
    "CompatibilityOracle",
    "CompressionQualityOracle",
    "ContentClarityOracle",
    "CriticalChartDataAccuracyOracle",
    "CriticalContentVisibilityOracle",
    "CriticalInstructionComplianceOracle",
    "CriticalSourceConsistencyOracle",
    "CropClarityOracle",
    "EditabilityOracle",
    "FactQualityOracle",
    "FileDeliverabilityOracle",
    "HighCostModelAuditOracle",
    "InstructionCoverageOracle",
    "InternalDataConsistencyOracle",
    "KeyPointRecallOracle",
    "LayoutOracle",
    "LlmContentQualityAuditOracle",
    "LlmScenarioComplianceAuditOracle",
    "MediaAvailabilityOracle",
    "MultimediaQualityOracle",
    "MultimodalQualityOracle",
    "ModelSourceAccessPolicy",
    "NarrativeOracle",
    "NumericAccuracyOracle",
    "ProjectSummaryQualityOracle",
    "RequiredAssetComplianceOracle",
    "SCENE_ORACLE_IDS",
    "SourceFaithfulnessOracle",
    "StyleConsistencyOracle",
    "STRUCTURED_DIMENSIONS_MODEL_AUDIT_COMPOSITE_ID",
    "STRUCTURED_MODEL_AUDIT_COMPOSITE_ID",
    "STRUCTURED_VLM_VISUAL_CRITERIA",
    "STRUCTURED_VLM_VISUAL_CRITERION_IDS",
    "STRUCTURED_VLM_VISUAL_DIMENSIONS_PROMPT",
    "STRUCTURED_VLM_VISUAL_DIMENSION_METRICS",
    "StructuredDimensionsModelAuditOracle",
    "StructuredModelAuditOracle",
    "StructuredVlmVisualAuditOracle",
    "StructuredVlmVisualDimensionsAuditOracle",
    "TemplateResidueOracle",
    "TextGenerationQualityOracle",
    "TraceabilityOracle",
    "TypographyOracle",
    "VisualHierarchyOracle",
    "VlmVisualQualityAuditOracle",
    "build_default_oracles",
    "build_default_registry",
]
