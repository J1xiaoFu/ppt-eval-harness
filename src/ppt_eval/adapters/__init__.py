"""Infrastructure adapters used by the PPT evaluation domain."""

from .facts import (
    FactSourceSnapshot,
    FactVerdict,
    FactVerification,
    FactVerificationBundle,
    NetworkFactVerifier,
    SearchHit,
    SearchProvider,
)
from .pptx import (
    BoundingBox,
    DependencyUnavailableError,
    ParsedPresentation,
    ParsedSlide,
    PptxAdapter,
    PptxAdapterError,
    SecurityFinding,
    SecurityLimits,
    SlideObject,
    UnsafePptxError,
    ZipPreflightReport,
)
from .renderers import (
    LibreOfficeRenderer,
    PowerPointRenderer,
    RenderingError,
    RenderResult,
)

__all__ = [
    "BoundingBox",
    "DependencyUnavailableError",
    "ParsedPresentation",
    "ParsedSlide",
    "PptxAdapter",
    "PptxAdapterError",
    "SecurityFinding",
    "SecurityLimits",
    "SlideObject",
    "UnsafePptxError",
    "ZipPreflightReport",
    "FactSourceSnapshot",
    "FactVerification",
    "FactVerificationBundle",
    "FactVerdict",
    "NetworkFactVerifier",
    "SearchHit",
    "SearchProvider",
    "LibreOfficeRenderer",
    "PowerPointRenderer",
    "RenderResult",
    "RenderingError",
]
