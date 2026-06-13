"""Redis-backed queue used by BentoML workers for asynchronous inference jobs."""

import uuid
import json
import logging
from datetime import datetime
from enum import Enum
from typing import Dict, Optional, Any, Tuple

import redis

logger = logging.getLogger(__name__)


class JobStatus(str, Enum):
    """Job lifecycle states."""

    PENDING = "PENDING"
    PROCESSING = "PROCESSING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class RedisJobQueue:
    """Distributed Redis queue with persisted jobs and lightweight stats tracking."""

    def __init__(
        self,
        host: str = "localhost",
        port: int = 6379,
        db: int = 0,
        password: str | None = None,
        job_ttl: int = 3600,
    ):
        """
        Initialize Redis connection.

        Args:
            host: Redis server hostname (K8s: "redis-0.redis..." for master)
            port: Redis server port
            db: Redis database number (0-15)
            password: Redis password (--requirepass). None for unauthenticated.
            job_ttl: Seconds before completed/failed jobs auto-delete
        """
        # Return decoded strings instead of bytes to simplify downstream handling.
        self.redis_client = redis.Redis(
            host=host,
            port=port,
            db=db,
            password=password,
            decode_responses=True,
        )

        # Validate connectivity during startup.
        try:
            self.redis_client.ping()
            logger.info("Connected to Redis at %s:%s", host, port)
        except redis.ConnectionError as e:
            logger.error("Failed to connect to Redis at %s:%s: %s", host, port, e)
            raise

        self.job_ttl = job_ttl

        # Key namespaced to avoid collisions.
        self.JOB_PREFIX = "job:"
        self.PENDING_QUEUE = "queue:pending"
        self.STATS_KEY = "queue:stats"

        # Repair stats drift from prior crashes/restarts.
        self.reconcile_stats()

    def submit_job(self, payload: str, metadata: Optional[Dict] = None) -> str:
        """Store job payload and enqueue id for worker pickup."""
        job_id = str(uuid.uuid4())

        job_data = {
            "job_id": job_id,
            "status": JobStatus.PENDING,
            "payload": payload,
            "metadata": json.dumps(metadata or {}),
            "submitted_at": datetime.utcnow().isoformat(),
            "started_at": "",
            "completed_at": "",
            "result": "",
            "error": "",
        }

        job_key = f"{self.JOB_PREFIX}{job_id}"
        self.redis_client.hset(job_key, mapping=job_data)
        self.redis_client.rpush(self.PENDING_QUEUE, job_id)

        # Update stats counters
        self.redis_client.hincrby(self.STATS_KEY, "total_jobs", 1)
        self.redis_client.hincrby(self.STATS_KEY, "pending", 1)

        logger.info("Job %s submitted to queue", job_id)
        return job_id

    def get_job_status(self, job_id: str) -> Optional[Dict[str, Any]]:
        """Get current status of a job. Returns None if job not found (or TTL expired)."""
        job_key = f"{self.JOB_PREFIX}{job_id}"
        job = self.redis_client.hgetall(job_key)

        if not job:
            return None

        return {
            "job_id": job["job_id"],
            "status": job["status"],
            "submitted_at": job["submitted_at"],
            "started_at": job["started_at"] or None,
            "completed_at": job["completed_at"] or None,
            "metadata": json.loads(job.get("metadata", "{}")),
            "has_result": bool(job.get("result")),
            "has_error": bool(job.get("error")),
        }

    def get_job_result(self, job_id: str) -> Optional[Dict[str, Any]]:
        """Get result of a completed/failed job. Returns None if not ready."""
        job_key = f"{self.JOB_PREFIX}{job_id}"
        job = self.redis_client.hgetall(job_key)

        if not job:
            return None

        if job["status"] not in [JobStatus.COMPLETED, JobStatus.FAILED]:
            return None

        result = json.loads(job["result"]) if job.get("result") else None

        return {
            "job_id": job["job_id"],
            "status": job["status"],
            "result": result,
            "error": job.get("error"),
            "completed_at": job.get("completed_at"),
        }

    def get_next_pending_job(self) -> Optional[Tuple[str, str, Dict]]:
        """Pop next pending job and mark it PROCESSING before returning payload."""
        job_id = self.redis_client.lpop(self.PENDING_QUEUE)

        if not job_id:
            return None

        job_key = f"{self.JOB_PREFIX}{job_id}"
        job = self.redis_client.hgetall(job_key)

        if not job:
            logger.warning("Job %s in queue but not in Redis (orphaned ID)", job_id)
            return None

        self.redis_client.hset(
            job_key,
            mapping={
                "status": JobStatus.PROCESSING,
                "started_at": datetime.utcnow().isoformat(),
            },
        )

        # Counters are eventually corrected by reconcile_stats() on startup.
        self.redis_client.hincrby(self.STATS_KEY, "pending", -1)
        self.redis_client.hincrby(self.STATS_KEY, "processing", 1)

        payload = job.get("payload", job.get("image", ""))
        metadata = json.loads(job.get("metadata", "{}"))

        return (job_id, payload, metadata)

    def mark_job_completed(self, job_id: str, result: Dict[str, Any]):
        """Mark job as completed and set TTL for auto-cleanup."""
        job_key = f"{self.JOB_PREFIX}{job_id}"

        self.redis_client.hset(
            job_key,
            mapping={
                "status": JobStatus.COMPLETED,
                "result": json.dumps(result),
                "completed_at": datetime.utcnow().isoformat(),
            },
        )

        # Set retention window for completed jobs.
        self.redis_client.expire(job_key, self.job_ttl)

        self.redis_client.hincrby(self.STATS_KEY, "processing", -1)
        self.redis_client.hincrby(self.STATS_KEY, "completed", 1)

        logger.info("Job %s completed (TTL: %ds)", job_id, self.job_ttl)

    def mark_job_failed(self, job_id: str, error: str):
        """Mark job as failed and set TTL for auto-cleanup."""
        job_key = f"{self.JOB_PREFIX}{job_id}"

        self.redis_client.hset(
            job_key,
            mapping={
                "status": JobStatus.FAILED,
                "error": error,
                "completed_at": datetime.utcnow().isoformat(),
            },
        )

        self.redis_client.expire(job_key, self.job_ttl)

        self.redis_client.hincrby(self.STATS_KEY, "processing", -1)
        self.redis_client.hincrby(self.STATS_KEY, "failed", 1)

        logger.warning("Job %s failed: %s", job_id, error)

    def get_queue_stats(self) -> Dict[str, Any]:
        """Return queue statistics snapshot."""
        stats = self.redis_client.hgetall(self.STATS_KEY)

        return {
            "total_jobs": int(stats.get("total_jobs", 0)),
            "pending": int(stats.get("pending", 0)),
            "processing": int(stats.get("processing", 0)),
            "completed": int(stats.get("completed", 0)),
            "failed": int(stats.get("failed", 0)),
            "queue_size": self.redis_client.llen(self.PENDING_QUEUE),
        }

    def timeout_stale_jobs(self, max_age_seconds: int = 300):
        """Fail PROCESSING jobs older than max_age_seconds."""
        now = datetime.utcnow()
        timed_out = 0
        cursor = 0
        while True:
            # SCAN avoids blocking Redis for large keyspaces.
            cursor, keys = self.redis_client.scan(
                cursor=cursor, match=f"{self.JOB_PREFIX}*", count=100
            )
            for key in keys:
                job = self.redis_client.hgetall(key)
                if job.get("status") != JobStatus.PROCESSING:
                    continue
                started_at = job.get("started_at", "")
                if not started_at:
                    continue
                try:
                    started = datetime.fromisoformat(started_at)
                    age = (now - started).total_seconds()
                    if age > max_age_seconds:
                        job_id = job.get("job_id", key.replace(self.JOB_PREFIX, ""))
                        self.mark_job_failed(
                            job_id,
                            f"Timeout: processing exceeded {max_age_seconds}s "
                            f"(likely pod crash during inference)",
                        )
                        timed_out += 1
                except (ValueError, TypeError):
                    continue
            if cursor == 0:
                break
        if timed_out > 0:
            logger.warning("Timed out %d stale PROCESSING jobs", timed_out)

    def reconcile_stats(self):
        """Recompute stats from persisted keys to repair counter drift."""
        counts = {
            "pending": 0,
            "processing": 0,
            "completed": 0,
            "failed": 0,
        }
        total = 0
        cursor = 0
        while True:
            cursor, keys = self.redis_client.scan(
                cursor=cursor, match=f"{self.JOB_PREFIX}*", count=100
            )
            for key in keys:
                status = self.redis_client.hget(key, "status")
                total += 1
                if status in counts:
                    counts[status] += 1
            if cursor == 0:
                break

        # Pending queue length is authoritative for pending count.
        queue_len = self.redis_client.llen(self.PENDING_QUEUE)
        counts["pending"] = queue_len

        old_stats = self.redis_client.hgetall(self.STATS_KEY)
        corrections = {}
        for field, actual in counts.items():
            old_val = int(old_stats.get(field, 0))
            if old_val != actual:
                corrections[field] = f"{old_val} → {actual}"

        if corrections:
            # Full overwrite keeps counters self-consistent after crashes/restarts.
            self.redis_client.hset(
                self.STATS_KEY,
                mapping={
                    "total_jobs": str(total),
                    "pending": str(counts["pending"]),
                    "processing": str(counts["processing"]),
                    "completed": str(counts["completed"]),
                    "failed": str(counts["failed"]),
                },
            )
            logger.warning("Stats reconciled: %s", corrections)
        else:
            logger.info("Stats consistent (no corrections needed)")

    def clear_queue(self):
        """Clear all jobs (for testing/maintenance). WARNING: deletes everything."""
        job_keys = self.redis_client.keys(f"{self.JOB_PREFIX}*")
        if job_keys:
            self.redis_client.delete(*job_keys)
        self.redis_client.delete(self.PENDING_QUEUE)
        self.redis_client.delete(self.STATS_KEY)
        logger.info("Queue cleared (%d jobs deleted)", len(job_keys))
