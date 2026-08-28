from __future__ import annotations

from typing import Any, Callable

import pytest


def make_test_client(app_factory: Callable[[], Any]) -> Any:
    """Create FastAPI's optional test client or report an explicit test skip."""

    pytest.importorskip("fastapi")
    pytest.importorskip("python_multipart")
    pytest.importorskip("httpx")
    from fastapi.testclient import TestClient

    return TestClient(app_factory())


__all__ = ["make_test_client"]
