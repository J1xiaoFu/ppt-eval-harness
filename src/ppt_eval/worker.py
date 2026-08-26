from __future__ import annotations

from ppt_eval.config import case_from_mapping, profile_from_mapping
from ppt_eval.infrastructure.production import create_celery_app
from ppt_eval.runtime import get_runtime

celery_app = create_celery_app()


@celery_app.task(name="ppt_eval.evaluate", autoretry_for=(OSError,), retry_backoff=True, max_retries=2)
def evaluate_task(payload: dict, profile_payload: dict | None = None):
    case = case_from_mapping(payload)
    profile = profile_from_mapping(profile_payload) if profile_payload else None
    return get_runtime().evaluate(case, profile)

