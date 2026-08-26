from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .local import sha256_file, to_primitive


class MissingProductionDependency(RuntimeError):
    pass


class S3ArtifactStore:
    """S3/MinIO implementation of the content-addressed artifact port."""

    def __init__(self, bucket: str, *, prefix: str = "ppt-eval", client: Any | None = None, **client_options: Any) -> None:
        if client is None:
            try:
                import boto3
            except ImportError as exc:  # pragma: no cover - optional integration
                raise MissingProductionDependency("install ppt-eval-harness[storage]") from exc
            client = boto3.client("s3", **client_options)
        self.client = client
        self.bucket = bucket
        self.prefix = prefix.strip("/")

    def put(self, source: str | Path, *, media_type: str = "application/octet-stream") -> dict[str, Any]:
        source_path = Path(source)
        digest = sha256_file(source_path)
        key = f"{self.prefix}/{digest[:2]}/{digest}"
        self.client.upload_file(
            str(source_path), self.bucket, key, ExtraArgs={"ContentType": media_type, "Metadata": {"sha256": digest}}
        )
        return {
            "sha256": digest,
            "uri": f"s3://{self.bucket}/{key}",
            "size_bytes": source_path.stat().st_size,
            "media_type": media_type,
            "original_name": source_path.name,
        }


class PostgresRunRepository:
    """Small JSONB repository; domain schemas remain independent of psycopg."""

    def __init__(self, dsn: str) -> None:
        try:
            import psycopg
        except ImportError as exc:  # pragma: no cover - optional integration
            raise MissingProductionDependency("install ppt-eval-harness[storage]") from exc
        self._psycopg = psycopg
        self.dsn = dsn
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        with self._psycopg.connect(self.dsn) as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS evaluation_runs (
                    run_id TEXT PRIMARY KEY,
                    payload JSONB NOT NULL,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                );
                CREATE TABLE IF NOT EXISTS review_events (
                    review_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    payload JSONB NOT NULL,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                );
                """
            )

    def save(self, report: Any) -> str:
        payload = to_primitive(report)
        run_id = str(payload["run_id"])
        with self._psycopg.connect(self.dsn) as connection, connection.cursor() as cursor:
            cursor.execute(
                """INSERT INTO evaluation_runs(run_id, payload) VALUES (%s, %s::jsonb)
                ON CONFLICT(run_id) DO UPDATE SET payload=EXCLUDED.payload, updated_at=NOW()""",
                (run_id, json.dumps(payload, ensure_ascii=False)),
            )
        return run_id

    def get(self, run_id: str) -> dict[str, Any]:
        with self._psycopg.connect(self.dsn) as connection, connection.cursor() as cursor:
            cursor.execute("SELECT payload FROM evaluation_runs WHERE run_id=%s", (run_id,))
            row = cursor.fetchone()
        if row is None:
            raise KeyError(run_id)
        return row[0]

    def list(self, limit: int = 200) -> list[dict[str, Any]]:
        with self._psycopg.connect(self.dsn) as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT payload FROM evaluation_runs ORDER BY updated_at DESC LIMIT %s", (limit,)
            )
            return [row[0] for row in cursor.fetchall()]


def create_celery_app() -> Any:
    try:
        from celery import Celery
    except ImportError as exc:  # pragma: no cover - optional integration
        raise MissingProductionDependency("install ppt-eval-harness[worker]") from exc
    import os

    broker = os.getenv("PPT_EVAL_BROKER_URL", "redis://127.0.0.1:6379/0")
    backend = os.getenv("PPT_EVAL_RESULT_BACKEND", "redis://127.0.0.1:6379/1")
    app = Celery("ppt_eval", broker=broker, backend=backend, include=["ppt_eval.worker"])
    app.conf.update(
        task_serializer="json",
        result_serializer="json",
        accept_content=["json"],
        task_acks_late=True,
        worker_prefetch_multiplier=1,
        task_time_limit=600,
        task_soft_time_limit=540,
    )
    return app

