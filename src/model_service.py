"""Reusable async BentoML service foundation for non-LLM model serving."""

import importlib
import logging
import os
import signal
import threading
import time
from abc import ABC, abstractmethod
from typing import Any, Dict, Optional, TYPE_CHECKING

import bentoml
from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from redis_queue import RedisJobQueue, JobStatus
else:
    try:
        from redis_queue import RedisJobQueue, JobStatus
    except ModuleNotFoundError:
        redis_queue_module = importlib.import_module("src.redis_queue")
        RedisJobQueue = redis_queue_module.RedisJobQueue
        JobStatus = redis_queue_module.JobStatus

logger = logging.getLogger("bentoml.service")


class JobSubmitResponse(BaseModel):
    """Response when submitting a new inference job."""

    job_id: str = Field(description="Unique job identifier for tracking")
    status: str = Field(description="Initial job status")
    message: str = Field(description="Human-readable message")


class JobStatusResponse(BaseModel):
    """Response for job status queries."""

    job_id: str
    status: str = Field(description="Current job status")
    submitted_at: str = Field(description="ISO timestamp when job was submitted")
    started_at: Optional[str] = Field(
        description="ISO timestamp when processing started"
    )
    completed_at: Optional[str] = Field(description="ISO timestamp when job completed")
    metadata: Dict[str, Any] = Field(description="Job metadata")


class JobResultResponse(BaseModel):
    """Response for job result queries."""

    job_id: str
    status: str
    result: Optional[Dict[str, Any]] = Field(description="Results if completed")
    error: Optional[str] = Field(description="Error message if failed")
    completed_at: Optional[str]


class QueueStatsResponse(BaseModel):
    """Queue statistics for monitoring dashboards."""

    total_jobs: int
    pending: int
    processing: int
    completed: int
    failed: int
    queue_size: int


REDIS_HOST = os.getenv("REDIS_HOST", "localhost")
REDIS_PORT = int(os.getenv("REDIS_PORT", "6379"))
REDIS_PASSWORD = os.getenv("REDIS_PASSWORD", None)
JOB_TTL = int(os.getenv("JOB_TTL", "3600"))


class BaseAsyncModelService(ABC):
    """Generic async queue orchestration for non-LLM BentoML model services."""

    def __init__(self):
        self.model: Optional[Any] = None
        logger.info("Connecting to Redis at %s:%s", REDIS_HOST, REDIS_PORT)
        self.job_queue = RedisJobQueue(
            host=REDIS_HOST,
            port=REDIS_PORT,
            password=REDIS_PASSWORD,
            job_ttl=JOB_TTL,
        )

        self._load_model_components()
        self._shutting_down = False
        signal.signal(signal.SIGTERM, self._handle_shutdown)
        self._start_background_worker()

    def _handle_shutdown(self, signum, frame):
        logger.info("SIGTERM received; finishing current job, then shutting down")
        self._shutting_down = True

    @abstractmethod
    def _load_model_components(self) -> None:
        """Load model-specific runtime state."""

    @abstractmethod
    def _serialize_inference_input(self, inference_input: Any) -> str:
        """Encode a resolved inference input into a Redis-safe string."""

    @abstractmethod
    def _deserialize_inference_input(self, payload: str) -> Any:
        """Decode a Redis payload into the model-specific inference input."""

    @abstractmethod
    def _run_model_inference(self, inference_input: Any) -> BaseModel | Dict[str, Any]:
        """Run model inference and return a JSON-serializable result."""

    def _serialize_result(self, result: BaseModel | Dict[str, Any]) -> Dict[str, Any]:
        if isinstance(result, BaseModel):
            return result.model_dump()
        if isinstance(result, dict):
            return result
        raise TypeError(
            f"Inference result must be a Pydantic model or dict, got {type(result).__name__}"
        )

    def _submit_inference_input(
        self, inference_input: Any, metadata: Optional[Dict[str, Any]] = None
    ) -> JobSubmitResponse:
        payload = self._serialize_inference_input(inference_input)
        job_id = self.job_queue.submit_job(payload, metadata=metadata)
        return JobSubmitResponse(
            job_id=job_id,
            status=JobStatus.PENDING,
            message=(
                f'Job queued. Poll status: POST /status with body {{"job_id": "{job_id}"}}. '
                f"Get results: POST /results with same body."
            ),
        )

    def _start_background_worker(self) -> None:
        def background_worker():
            logger.info("Background worker started")
            idle_cycles = 0
            while not self._shutting_down:
                next_job = self.job_queue.get_next_pending_job()

                if next_job is None:
                    time.sleep(0.1)
                    idle_cycles += 1
                    if idle_cycles >= 60:
                        idle_cycles = 0
                        self.job_queue.timeout_stale_jobs()
                    continue

                idle_cycles = 0
                job_id, payload, metadata = next_job
                logger.info("Processing job %s", job_id)

                try:
                    inference_input = self._deserialize_inference_input(payload)
                    result = self._run_model_inference(inference_input)
                    self.job_queue.mark_job_completed(
                        job_id, self._serialize_result(result)
                    )
                except Exception as exc:
                    error_msg = f"{type(exc).__name__}: {str(exc)}"
                    logger.error("Job %s failed: %s", job_id, error_msg)
                    self.job_queue.mark_job_failed(job_id, error_msg)

            logger.info("Worker thread exiting cleanly")

        worker = threading.Thread(target=background_worker, daemon=True)
        worker.start()

    @bentoml.api
    def status(self, job_id: str) -> JobStatusResponse:
        """Return current status for a job id."""
        job_status = self.job_queue.get_job_status(job_id)
        if job_status is None:
            return JobStatusResponse(
                job_id=job_id,
                status="NOT_FOUND",
                submitted_at="",
                started_at=None,
                completed_at=None,
                metadata={"error": "Job not found or TTL expired"},
            )
        return JobStatusResponse(**job_status)

    @bentoml.api
    def results(self, job_id: str) -> JobResultResponse:
        """Return job result if available, otherwise status/error metadata."""
        result = self.job_queue.get_job_result(job_id)
        if result is None:
            current_status = self.job_queue.get_job_status(job_id)
            if current_status is None:
                return JobResultResponse(
                    job_id=job_id,
                    status="NOT_FOUND",
                    result=None,
                    error="Job not found or TTL expired",
                    completed_at=None,
                )
            return JobResultResponse(
                job_id=job_id,
                status=current_status["status"],
                result=None,
                error="Job not completed yet",
                completed_at=None,
            )

        return JobResultResponse(
            job_id=result["job_id"],
            status=result["status"],
            result=result["result"],
            error=result["error"],
            completed_at=result["completed_at"],
        )

    @bentoml.api
    def queue_stats(self) -> QueueStatsResponse:
        stats = self.job_queue.get_queue_stats()
        return QueueStatsResponse(**stats)

    @bentoml.api
    def health(self) -> Dict[str, Any]:
        redis_healthy = False
        stats = {"pending": 0, "processing": 0}
        try:
            self.job_queue.redis_client.ping()
            redis_healthy = True
            stats_full = self.job_queue.get_queue_stats()
            stats = {
                "pending": stats_full["pending"],
                "processing": stats_full["processing"],
            }
        except Exception as exc:
            logger.error("Redis health check failed: %s", exc)

        return {
            "status": "healthy" if redis_healthy else "degraded",
            "model_loaded": self.model is not None,
            "device": getattr(self, "device", None),
            "redis_connected": redis_healthy,
            "queue": stats,
        }
