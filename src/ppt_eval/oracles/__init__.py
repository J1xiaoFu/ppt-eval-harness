"""Built-in deterministic Oracle catalog and factories."""

from __future__ import annotations

from typing import Mapping

from ppt_eval.domain.enums import SceneType

from .baseline import (
    AccessibilityOracle,
    BaselinePptQualityOracle,
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
    TypographyOracle,
    VisualHierarchyOracle,
)
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


def build_default_oracles(adapter=None):
    """Return one mandatory baseline and all optional scene composites."""

    return (
        BaselinePptQualityOracle(adapter),
        TextGenerationQualityOracle(adapter),
        ProjectSummaryQualityOracle(adapter),
        MultimodalQualityOracle(adapter),
    )


def build_default_registry(adapter=None):
    """Create the Harness registry without making infrastructure import it."""

    from ppt_eval.application.oracle import OracleRegistry

    return OracleRegistry(build_default_oracles(adapter))


__all__ = [
    "AccessibilityOracle",
    "AssetComplianceOracle",
    "AssetPresentationOracle",
    "AudienceFitOracle",
    "BaselinePptQualityOracle",
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
    "InstructionCoverageOracle",
    "InternalDataConsistencyOracle",
    "KeyPointRecallOracle",
    "LayoutOracle",
    "MediaAvailabilityOracle",
    "MultimediaQualityOracle",
    "MultimodalQualityOracle",
    "NarrativeOracle",
    "NumericAccuracyOracle",
    "ProjectSummaryQualityOracle",
    "RequiredAssetComplianceOracle",
    "SCENE_ORACLE_IDS",
    "SourceFaithfulnessOracle",
    "StyleConsistencyOracle",
    "TextGenerationQualityOracle",
    "TraceabilityOracle",
    "TypographyOracle",
    "VisualHierarchyOracle",
    "build_default_oracles",
    "build_default_registry",
]
