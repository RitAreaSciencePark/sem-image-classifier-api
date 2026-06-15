"""HTTP helpers for Authentik tokens and inference API."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

import requests

DEFAULT_IMAGE_URL = (
    "https://enif.unl.edu/sites/unl.edu.research.nebraska-center-for-materials-"
    "and-nanoscience.electron-nanoscopy/files/styles/no_crop_720/public/media/"
    "image/NanoSEM_15KV.jpg?itok=McoSOeAv"
)

DEFAULT_AUTH_SCOPE = "openid profile"


def get_token(
    token_url: str,
    client_id: str,
    client_secret: str,
    host_header: str,
    *,
    scope: str = DEFAULT_AUTH_SCOPE,
    timeout: float = 10.0,
) -> str:
    headers: dict[str, str] = {"Content-Type": "application/x-www-form-urlencoded"}
    if host_header:
        headers["Host"] = host_header
    response = requests.post(
        token_url,
        headers=headers,
        data={
            "grant_type": "client_credentials",
            "scope": scope,
            "client_id": client_id,
            "client_secret": client_secret,
        },
        timeout=timeout,
    )
    response.raise_for_status()
    token = response.json().get("access_token")
    if not token:
        raise RuntimeError("token response had no access_token")
    return token


def auth_headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def post_json(
    url: str, token: str, body: dict[str, Any], timeout: float
) -> requests.Response:
    return requests.post(url, json=body, headers=auth_headers(token), timeout=timeout)


def poll_job(
    base_url: str,
    token: str,
    job_id: str,
    timeout: float,
    *,
    interval: float = 0.5,
) -> str:
    deadline = time.monotonic() + timeout
    base = base_url.rstrip("/")
    while time.monotonic() < deadline:
        response = post_json(
            f"{base}/api/v1/jobs/status",
            token,
            {"job_id": job_id},
            min(5.0, timeout),
        )
        if response.status_code != 200:
            return f"poll_http_{response.status_code}"
        status = response.json().get("status", "UNKNOWN")
        if status in {"COMPLETED", "FAILED", "NOT_FOUND"}:
            return status
        time.sleep(interval)
    return "POLL_TIMEOUT"


@dataclass(frozen=True)
class SubmitResult:
    job_id: str
    status_code: int
    latency_s: float
    error: str = ""


@dataclass(frozen=True)
class PollResult:
    status: str
    payload: dict[str, Any]
    error: str = ""


class InferenceClient:
    """Gateway inference API: submit, poll, results."""

    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        default_timeout: float = 60.0,
        poll_interval: float = 0.5,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.default_timeout = default_timeout
        self.poll_interval = poll_interval

    def submit(self, image_url: str, *, timeout: float | None = None) -> SubmitResult:
        request_timeout = timeout if timeout is not None else self.default_timeout
        started = time.perf_counter()
        response = post_json(
            f"{self.base_url}/api/v1/inference",
            self.token,
            {"image_url": image_url},
            request_timeout,
        )
        latency_s = time.perf_counter() - started
        if response.status_code != 200:
            return SubmitResult(
                job_id="",
                status_code=response.status_code,
                latency_s=latency_s,
                error=response.text[:200],
            )
        job_id = response.json().get("job_id", "")
        if not job_id:
            return SubmitResult(
                job_id="",
                status_code=response.status_code,
                latency_s=latency_s,
                error="missing job_id",
            )
        return SubmitResult(
            job_id=job_id,
            status_code=response.status_code,
            latency_s=latency_s,
        )

    def poll_until_done(
        self,
        job_id: str,
        *,
        timeout: float | None = None,
        interval: float | None = None,
    ) -> PollResult:
        request_timeout = timeout if timeout is not None else self.default_timeout
        poll_every = interval if interval is not None else self.poll_interval
        deadline = time.monotonic() + request_timeout
        while time.monotonic() < deadline:
            response = post_json(
                f"{self.base_url}/api/v1/jobs/status",
                self.token,
                {"job_id": job_id},
                min(5.0, request_timeout),
            )
            if response.status_code != 200:
                return PollResult(
                    status=f"POLL_HTTP_{response.status_code}",
                    payload={},
                    error=response.text[:200],
                )
            payload = response.json()
            status = payload.get("status", "UNKNOWN")
            if status in {"COMPLETED", "FAILED", "NOT_FOUND"}:
                return PollResult(status=status, payload=payload)
            time.sleep(poll_every)
        return PollResult(
            status="POLL_TIMEOUT",
            payload={},
            error=f"timeout after {request_timeout:.1f}s",
        )

    def get_results(self, job_id: str, *, timeout: float | None = None) -> dict[str, Any]:
        request_timeout = timeout if timeout is not None else self.default_timeout
        response = post_json(
            f"{self.base_url}/api/v1/jobs/results",
            self.token,
            {"job_id": job_id},
            request_timeout,
        )
        response.raise_for_status()
        return response.json()

    def run_inference(
        self, image_url: str, *, timeout: float | None = None
    ) -> dict[str, Any]:
        request_timeout = timeout if timeout is not None else self.default_timeout
        submit = self.submit(image_url, timeout=request_timeout)
        if submit.status_code != 200 or not submit.job_id:
            raise RuntimeError(
                f"Submit failed: HTTP {submit.status_code} {submit.error}".strip()
            )
        poll = self.poll_until_done(submit.job_id, timeout=request_timeout)
        if poll.status != "COMPLETED":
            raise RuntimeError(
                f"Job {submit.job_id} ended with {poll.status}: {poll.error}".strip()
            )
        result_data = self.get_results(submit.job_id, timeout=request_timeout)
        if result_data.get("status") != "COMPLETED" or result_data.get("result") is None:
            raise RuntimeError(f"Unexpected results payload: {result_data}")
        return result_data["result"]
