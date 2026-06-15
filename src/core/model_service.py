"""Reusable async BentoML service foundation for non-LLM model serving."""

import importlib
import logging
import os
import signal
import threading
import time
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional, Tuple, TYPE_CHECKING

import bentoml
from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from redis_queue import RedisJobQueue, JobStatus
else:
    try:
        from core.redis_queue import RedisJobQueue, JobStatus
    except ModuleNotFoundError:
        try:
            from redis_queue import RedisJobQueue, JobStatus
        except ModuleNotFoundError:
            redis_queue_module = importlib.import_module("src.core.redis_queue")
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


# Worker tuning from BentoML ConfigMap (ml_platform/config.yaml → render).
REDIS_HOST = os.getenv("REDIS_HOST", "localhost")
REDIS_PORT = int(os.getenv("REDIS_PORT", "6379"))
REDIS_PASSWORD = os.getenv("REDIS_PASSWORD", None)
JOB_TTL = int(os.getenv("JOB_TTL", "3600"))
INFERENCE_BATCH_SIZE = max(1, int(os.getenv("INFERENCE_BATCH_SIZE", "16")))
INFERENCE_BATCH_MAX_WAIT_MS = max(
    0, int(os.getenv("INFERENCE_BATCH_MAX_WAIT_MS", "100"))
)
WORKER_POLL_INTERVAL_MS = max(1, int(os.getenv("WORKER_POLL_INTERVAL_MS", "50")))


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

    def _run_model_inference_batch(
        self, inference_inputs: List[Any]
    ) -> List[BaseModel | Dict[str, Any]]:
        """Run inference for a batch; default falls back to sequential singles."""
        return [self._run_model_inference(item) for item in inference_inputs]

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
        return self._submit_raw_payload(payload, metadata=metadata)

    def _submit_raw_payload(
        self, payload: str, metadata: Optional[Dict[str, Any]] = None
    ) -> JobSubmitResponse:
        job_id = self.job_queue.submit_job(payload, metadata=metadata)
        return JobSubmitResponse(
            job_id=job_id,
            status=JobStatus.PENDING,
            message=(
                f'Job queued. Poll status: POST /status with body {{"job_id": "{job_id}"}}. '
                f"Get results: POST /results with same body."
            ),
        )

    def _collect_job_batch(self) -> List[Tuple[str, str, Dict]]:
        """Collect up to INFERENCE_BATCH_SIZE jobs within max wait window."""
        batch: List[Tuple[str, str, Dict]] = []
        first = self._wait_for_next_job()
        if first is None:
            return batch
        batch.append(first)
        if INFERENCE_BATCH_SIZE <= 1:
            return batch

        deadline = time.monotonic() + (INFERENCE_BATCH_MAX_WAIT_MS / 1000.0)
        while len(batch) < INFERENCE_BATCH_SIZE and time.monotonic() < deadline:
            next_job = self.job_queue.get_next_pending_job()
            if next_job is None:
                time.sleep(WORKER_POLL_INTERVAL_MS / 1000.0)
                continue
            batch.append(next_job)
        return batch

    def _wait_for_next_job(self) -> Optional[Tuple[str, str, Dict]]:
        while not self._shutting_down:
            next_job = self.job_queue.get_next_pending_job()
            if next_job is not None:
                return next_job
            time.sleep(WORKER_POLL_INTERVAL_MS / 1000.0)
        return None

    def _process_job_batch(self, batch: List[Tuple[str, str, Dict]]) -> None:
        job_ids = [item[0] for item in batch]
        payloads = [item[1] for item in batch]
        logger.info("Processing batch of %d jobs: %s", len(batch), job_ids[:5])

        resolved_inputs: List[Any] = []
        per_job_error: Dict[int, str] = {}

        for index, payload in enumerate(payloads):
            try:
                resolved_inputs.append(self._deserialize_inference_input(payload))
            except Exception as exc:
                per_job_error[index] = f"{type(exc).__name__}: {exc}"

        valid_indices = [i for i in range(len(batch)) if i not in per_job_error]
        valid_inputs = [resolved_inputs[i] for i in valid_indices]

        batch_results: Dict[int, BaseModel | Dict[str, Any]] = {}
        if valid_inputs:
            try:
                outputs = self._run_model_inference_batch(valid_inputs)
                if len(outputs) != len(valid_inputs):
                    raise RuntimeError(
                        f"Batch inference returned {len(outputs)} results for "
                        f"{len(valid_inputs)} inputs"
                    )
                for offset, index in enumerate(valid_indices):
                    batch_results[index] = outputs[offset]
            except Exception as exc:
                error_msg = f"{type(exc).__name__}: {exc}"
                logger.error("Batch inference failed: %s", error_msg)
                for index in valid_indices:
                    per_job_error[index] = error_msg

        for index, (job_id, _, _) in enumerate(batch):
            if index in per_job_error:
                self.job_queue.mark_job_failed(job_id, per_job_error[index])
                continue
            result = batch_results.get(index)
            if result is None:
                self.job_queue.mark_job_failed(job_id, "Missing batch result")
                continue
            self.job_queue.mark_job_completed(job_id, self._serialize_result(result))

        self.job_queue.maybe_reconcile_stats()

    def _start_background_worker(self) -> None:
        def background_worker():
            logger.info(
                "Background worker started (batch_size=%d max_wait_ms=%d poll_ms=%d)",
                INFERENCE_BATCH_SIZE,
                INFERENCE_BATCH_MAX_WAIT_MS,
                WORKER_POLL_INTERVAL_MS,
            )
            idle_cycles = 0
            while not self._shutting_down:
                batch = self._collect_job_batch()
                if not batch:
                    idle_cycles += 1
                    if idle_cycles >= 60:
                        idle_cycles = 0
                        self.job_queue.timeout_stale_jobs()
                    continue

                idle_cycles = 0
                self._process_job_batch(batch)

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
            "worker": {
                "batch_size": INFERENCE_BATCH_SIZE,
                "batch_max_wait_ms": INFERENCE_BATCH_MAX_WAIT_MS,
            },
        }
