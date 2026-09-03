"""Current v8 Oracle catalog and runtime registry."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ppt_eval.adapters import ModelAuditProvider, PptxAdapter
    from ppt_eval.application.oracle import Oracle, OracleRegistry

from .baseline import (
    BaselinePptQualityOracle,
    CriticalContentVisibilityOracle,
    FileDeliverabilityOracle,
    InternalDataConsistencyOracle,
)
from .model_audits import (
    GROUNDED_VLM_DEFECT_CODES,
    GROUNDED_VLM_POSITIVE_SIGNALS,
    V8_GROUNDED_VISUAL_CRITERION_IDS,
    V8_GROUNDED_VLM_CRITERION_PROMPTS,
    V8_RASTER_TEXT_CRITERION_IDS,
    V83_GROUNDED_VLM_CRITERION_PROMPTS,
    V83_GROUNDED_VLM_DEFECT_CODES,
    GroundedSingleCriterionVlmOracle,
)
from .v8_composites import (
    V8_VISUAL_CRITERION_IDS,
    V8AtomicObservationComposite,
    V8InitialVisualCriterionOracle,
    V8QualityReducerOracle,
    V8RasterTextObservationOracle,
    V8TieredVisualCriterionOracle,
)
from .visual_routing import (
    AtlasScoutOracle,
    VisualCoverageOracle,
    VisualPageIndexOracle,
    VisualSelectionOracle,
)


def build_default_oracles(
    adapter: PptxAdapter | None = None,
    *,
    vlm_provider: ModelAuditProvider | None = None,
    advanced_vlm_provider: ModelAuditProvider | None = None,
) -> tuple[Oracle, ...]:
    """Return only the current v8 execution graph and mandatory hard baseline."""

    visual_oracles = tuple(
        V8TieredVisualCriterionOracle(
            criterion_id,
            vlm_provider,
            advanced_vlm_provider,
            adapter,
        )
        for criterion_id in V8_VISUAL_CRITERION_IDS
    )
    return (
        BaselinePptQualityOracle(adapter),
        V8AtomicObservationComposite(adapter),
        VisualPageIndexOracle(adapter),
        AtlasScoutOracle(vlm_provider, advanced_vlm_provider),
        VisualSelectionOracle(),
        *(V8InitialVisualCriterionOracle(oracle) for oracle in visual_oracles),
        *visual_oracles,
        *(
            V8RasterTextObservationOracle(
                criterion_id,
                vlm_provider,
                advanced_vlm_provider,
                adapter,
            )
            for criterion_id in V8_RASTER_TEXT_CRITERION_IDS
        ),
        VisualCoverageOracle(),
        V8QualityReducerOracle(),
    )


def build_default_registry(
    adapter: PptxAdapter | None = None,
    *,
    vlm_provider: ModelAuditProvider | None = None,
    advanced_vlm_provider: ModelAuditProvider | None = None,
) -> OracleRegistry:
    """Create the current v8 Harness registry."""

    from ppt_eval.application.oracle import OracleRegistry

    return OracleRegistry(
        build_default_oracles(
            adapter,
            vlm_provider=vlm_provider,
            advanced_vlm_provider=advanced_vlm_provider,
        )
    )


__all__ = [
    "BaselinePptQualityOracle",
    "CriticalContentVisibilityOracle",
    "FileDeliverabilityOracle",
    "GROUNDED_VLM_DEFECT_CODES",
    "GROUNDED_VLM_POSITIVE_SIGNALS",
    "GroundedSingleCriterionVlmOracle",
    "InternalDataConsistencyOracle",
    "V8AtomicObservationComposite",
    "V8InitialVisualCriterionOracle",
    "V8QualityReducerOracle",
    "V8RasterTextObservationOracle",
    "V8TieredVisualCriterionOracle",
    "V8_GROUNDED_VISUAL_CRITERION_IDS",
    "V8_GROUNDED_VLM_CRITERION_PROMPTS",
    "V8_RASTER_TEXT_CRITERION_IDS",
    "V83_GROUNDED_VLM_CRITERION_PROMPTS",
    "V83_GROUNDED_VLM_DEFECT_CODES",
    "V8_VISUAL_CRITERION_IDS",
    "AtlasScoutOracle",
    "VisualCoverageOracle",
    "VisualPageIndexOracle",
    "VisualSelectionOracle",
    "build_default_oracles",
    "build_default_registry",
]
