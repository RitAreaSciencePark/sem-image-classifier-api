#!/usr/bin/env python3
"""Generate authenticated API traffic for stress and usage-report validation."""

from __future__ import annotations

import argparse
import concurrent.futures
import random
import statistics
import time
import uuid
from dataclasses import dataclass
from typing import Any

import requests

DEFAULT_IMAGE_URL = (
    "https://enif.unl.edu/sites/unl.edu.research.nebraska-center-for-materials-"
    "and-nanoscience.electron-nanoscopy/files/styles/no_crop_720/public/media/"
    "image/NanoSEM_15KV.jpg?itok=McoSOeAv"
)


@dataclass
class Result:
    user: str
    endpoint: str
    status_code: int
    ok: bool
    latency_s: float
    detail: str


def positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError(f"value must be > 0: {value}")
    return parsed


def parse_users(value: str) -> list[str]:
    users = [item.strip() for item in value.split(",") if item.strip()]
    if not users:
        raise argparse.ArgumentTypeError("at least one user is required")
    return users


def get_mock_token(mock_token_url: str, user: str, timeout: float) -> str:
    response = requests.post(
        mock_token_url,
        headers={"Host": "mock-oidc:8080"},
        data={
            "grant_type": "client_credentials",
            "scope": "openid",
            "client_id": user,
            "client_secret": "test-secret",
        },
        timeout=timeout,
    )
    response.raise_for_status()
    token = response.json().get("access_token")
    if not token:
        raise RuntimeError(f"token response for user={user} had no access_token")
    return token


def auth_headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def post_json(
    url: str, token: str, body: dict[str, Any], timeout: float
) -> requests.Response:
    return requests.post(url, json=body, headers=auth_headers(token), timeout=timeout)


def poll_job(base_url: str, token: str, job_id: str, timeout: float) -> str:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        response = post_json(
            f"{base_url}/api/v1/jobs/status",
            token,
            {"job_id": job_id},
            min(5.0, timeout),
        )
        if response.status_code != 200:
            return f"poll_http_{response.status_code}"
        status = response.json().get("status", "UNKNOWN")
        if status in {"COMPLETED", "FAILED", "NOT_FOUND"}:
            return status
        time.sleep(0.5)
    return "POLL_TIMEOUT"


def run_one(
    *,
    index: int,
    base_url: str,
    user: str,
    token: str,
    mode: str,
    poll: bool,
    timeout: float,
    image_url: str,
) -> Result:
    selected_mode = (
        random.choice(["inference", "status", "results"]) if mode == "mixed" else mode
    )
    started = time.perf_counter()
    try:
        if selected_mode == "inference":
            response = post_json(
                f"{base_url}/api/v1/inference",
                token,
                {"image_url": image_url},
                timeout,
            )
            detail = response.text[:160]
            if response.status_code == 200:
                job_id = response.json().get("job_id", "")
                detail = f"job_id={job_id}"
                if poll and job_id:
                    detail += f" poll={poll_job(base_url, token, job_id, timeout)}"
            return Result(
                user,
                selected_mode,
                response.status_code,
                response.ok,
                time.perf_counter() - started,
                detail,
            )

        fake_job_id = str(uuid.uuid4())
        endpoint = "status" if selected_mode == "status" else "results"
        response = post_json(
            f"{base_url}/api/v1/jobs/{endpoint}",
            token,
            {"job_id": fake_job_id},
            timeout,
        )
        detail = response.text[:160]
        return Result(
            user,
            selected_mode,
            response.status_code,
            response.ok,
            time.perf_counter() - started,
            detail,
        )
    except Exception as exc:
        return Result(
            user,
            selected_mode,
            0,
            False,
            time.perf_counter() - started,
            f"{type(exc).__name__}: {exc}",
        )


def summarize(results: list[Result]) -> str:
    total = len(results)
    ok = sum(1 for result in results if result.ok)
    failed = total - ok
    latencies = [result.latency_s for result in results]
    by_endpoint: dict[str, int] = {}
    by_user: dict[str, int] = {}
    by_status: dict[str, int] = {}
    for result in results:
        by_endpoint[result.endpoint] = by_endpoint.get(result.endpoint, 0) + 1
        by_user[result.user] = by_user.get(result.user, 0) + 1
        by_status[str(result.status_code)] = (
            by_status.get(str(result.status_code), 0) + 1
        )

    lines = [
        "Stress test complete",
        f"Total requests : {total}",
        f"Success        : {ok}",
        f"Failed         : {failed}",
    ]
    if latencies:
        lines.extend(
            [
                f"Latency min/s : {min(latencies):.3f}",
                f"Latency p50/s : {statistics.median(latencies):.3f}",
                f"Latency max/s : {max(latencies):.3f}",
            ]
        )
    lines.append("")
    lines.append("By endpoint:")
    for key, value in sorted(by_endpoint.items()):
        lines.append(f"  {key:10} {value}")
    lines.append("By status:")
    for key, value in sorted(by_status.items()):
        lines.append(f"  {key:10} {value}")
    lines.append("By user:")
    for key, value in sorted(by_user.items()):
        lines.append(f"  {key:18} {value}")
    failures = [result for result in results if not result.ok]
    if failures:
        lines.append("")
        lines.append("Sample failures:")
        for result in failures[:8]:
            lines.append(
                f"  {result.user} {result.endpoint} status={result.status_code} {result.detail}"
            )
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Generate authenticated traffic against the SEM API."
    )
    parser.add_argument("--base-url", default="http://localhost:8080")
    parser.add_argument(
        "--mock-token-url", default="http://localhost:18080/default/token"
    )
    parser.add_argument(
        "--users", type=parse_users, default=parse_users("alice,bob,charlie")
    )
    parser.add_argument("--requests", type=positive_int, default=30)
    parser.add_argument("--concurrency", type=positive_int, default=5)
    parser.add_argument(
        "--mode", choices=("inference", "status", "results", "mixed"), default="mixed"
    )
    parser.add_argument("--poll", action="store_true")
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--image-url", default=DEFAULT_IMAGE_URL)
    args = parser.parse_args()

    print(f"Acquiring tokens for {len(args.users)} users...")
    tokens = {
        user: get_mock_token(args.mock_token_url, user, args.timeout)
        for user in args.users
    }

    print(
        f"Sending {args.requests} requests with concurrency={args.concurrency} mode={args.mode}..."
    )
    results: list[Result] = []
    with concurrent.futures.ThreadPoolExecutor(
        max_workers=args.concurrency
    ) as executor:
        futures = []
        for index in range(args.requests):
            user = args.users[index % len(args.users)]
            futures.append(
                executor.submit(
                    run_one,
                    index=index,
                    base_url=args.base_url.rstrip("/"),
                    user=user,
                    token=tokens[user],
                    mode=args.mode,
                    poll=args.poll,
                    timeout=args.timeout,
                    image_url=args.image_url,
                )
            )
        for future in concurrent.futures.as_completed(futures):
            results.append(future.result())

    print(summarize(results))
    return 1 if any(not result.ok for result in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
