#!/usr/bin/env python3
"""Reproducible benchmark suite for paper evaluation (eScience 2026)."""

from __future__ import annotations

import sys
from pathlib import Path

_repo_root = Path(__file__).resolve().parents[2]
if str(_repo_root) not in sys.path:
    sys.path.insert(0, str(_repo_root))

import argparse
import concurrent.futures
import json
import math
import random
import subprocess
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import requests

from ml_platform.devtools.api_client import DEFAULT_IMAGE_URL, InferenceClient, get_token
from ml_platform.devtools.cli import add_service_arguments, resolve_service_context
from ml_platform.devtools.paths import repo_root

BOOTSTRAP_SAMPLES = 2000

SCENARIOS: dict[str, dict[str, int]] = {
    "baseline": {"jobs": 30, "concurrency": 1, "warmup": 2},
    "load-5": {"jobs": 40, "concurrency": 5, "warmup": 2},
    "load-10": {"jobs": 50, "concurrency": 10, "warmup": 2},
    "scaleout-3": {"jobs": 50, "concurrency": 10, "warmup": 2},
}


@dataclass
class JobMetrics:
    job_id: str = ""
    trial: int = 0
    final_status: str = ""
    submit_latency_s: float | None = None
    e2e_latency_s: float | None = None
    processing_s: float | None = None
    residual_wait_s: float | None = None
    error: str = ""


@dataclass
class MetricStats:
    n: int = 0
    mean: float | None = None
    stdev: float | None = None
    p50: float | None = None
    p90: float | None = None
    p95: float | None = None
    p99: float | None = None
    ci95_low: float | None = None
    ci95_high: float | None = None


@dataclass
class TrialSummary:
    trial: int
    jobs: int
    concurrency: int
    warmup: int
    wall_clock_s: float
    completed: int
    failed: int
    success_rate_pct: float
    throughput_jobs_per_min: float
    job_records: list[JobMetrics] = field(default_factory=list)


@dataclass
class AggregateSummary:
    scenario: str
    trials: int
    jobs_per_trial: int
    concurrency: int
    warmup: int
    total_jobs: int
    completed: int
    failed: int
    success_rate_pct: float
    throughput_jobs_per_min: float
    submit: MetricStats = field(default_factory=MetricStats)
    e2e: MetricStats = field(default_factory=MetricStats)
    processing: MetricStats = field(default_factory=MetricStats)
    residual_wait: MetricStats = field(default_factory=MetricStats)


def positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError(f"value must be > 0: {value}")
    return parsed


def non_negative_int(value: str) -> int:
    parsed = int(value)
    if parsed < 0:
        raise argparse.ArgumentTypeError(f"value must be >= 0: {value}")
    return parsed


def percentile(values: list[float], pct: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    rank = (len(ordered) - 1) * (pct / 100.0)
    lower = int(rank)
    upper = min(lower + 1, len(ordered) - 1)
    weight = rank - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * weight


def bootstrap_ci(
    values: list[float], stat_fn, alpha: float = 0.05, samples: int = BOOTSTRAP_SAMPLES
) -> tuple[float | None, float | None]:
    if len(values) < 2:
        return None, None
    rng = random.Random(42)
    n = len(values)
    estimates: list[float] = []
    for _ in range(samples):
        sample = [values[rng.randrange(n)] for _ in range(n)]
        estimate = stat_fn(sample)
        if estimate is not None:
            estimates.append(estimate)
    if not estimates:
        return None, None
    estimates.sort()
    low_idx = int((alpha / 2) * len(estimates))
    high_idx = int((1 - alpha / 2) * len(estimates)) - 1
    return estimates[low_idx], estimates[high_idx]


def compute_metric_stats(
    values: list[float], ci_stat_fn: Callable[[list[float]], float | None] | None = None
) -> MetricStats:
    if not values:
        return MetricStats()
    mean = sum(values) / len(values)
    stdev = statistics_stdev(values, mean)
    p50 = percentile(values, 50)
    ci_low, ci_high = bootstrap_ci(values, ci_stat_fn or (lambda v: percentile(v, 50)))
    return MetricStats(
        n=len(values),
        mean=mean,
        stdev=stdev,
        p50=p50,
        p90=percentile(values, 90),
        p95=percentile(values, 95),
        p99=percentile(values, 99),
        ci95_low=ci_low,
        ci95_high=ci_high,
    )


def statistics_stdev(values: list[float], mean: float) -> float | None:
    if len(values) < 2:
        return None
    variance = sum((v - mean) ** 2 for v in values) / (len(values) - 1)
    return math.sqrt(variance)


def parse_iso(ts: str | None) -> datetime | None:
    if not ts:
        return None
    normalized = ts.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def delta_s(start: datetime | None, end: datetime | None) -> float | None:
    if start is None or end is None:
        return None
    return max(0.0, (end - start).total_seconds())


def fetch_health(base_url: str, timeout: float) -> dict[str, Any]:
    response = requests.get(f"{base_url.rstrip('/')}/health", timeout=timeout)
    response.raise_for_status()
    return response.json()


def kubectl_json(args: list[str]) -> dict[str, Any] | None:
    try:
        result = subprocess.run(
            ["kubectl", *args],
            check=True,
            capture_output=True,
            text=True,
        )
        return json.loads(result.stdout)
    except (subprocess.CalledProcessError, json.JSONDecodeError, FileNotFoundError):
        return None


def git_commit() -> str | None:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
            cwd=repo_root(),
        )
        return result.stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None


def capture_environment(base_url: str, namespace: str, timeout: float, poll_interval: float) -> dict[str, Any]:
    health = fetch_health(base_url, timeout)
    deploy = kubectl_json(
        ["get", "deploy", "bentoml", "-n", namespace, "-o", "json"]
    )
    configmap = kubectl_json(
        ["get", "configmap", "bentoml-config", "-n", namespace, "-o", "json"]
    )

    bentoml_env: dict[str, str] = {}
    if configmap:
        bentoml_env = configmap.get("data", {})

    replicas = None
    ready_replicas = None
    image = None
    cpu_limit = None
    memory_limit = None
    if deploy:
        spec = deploy.get("spec", {})
        status = deploy.get("status", {})
        replicas = spec.get("replicas")
        ready_replicas = status.get("readyReplicas")
        containers = (
            spec.get("template", {}).get("spec", {}).get("containers", [])
        )
        if containers:
            image = containers[0].get("image")
            limits = containers[0].get("resources", {}).get("limits", {})
            cpu_limit = limits.get("cpu")
            memory_limit = limits.get("memory")

    return {
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "git_commit": git_commit(),
        "poll_interval_s": poll_interval,
        "namespace": namespace,
        "health": health,
        "bentoml": {
            "replicas": replicas,
            "ready_replicas": ready_replicas,
            "image": image,
            "cpu_limit": cpu_limit,
            "memory_limit": memory_limit,
            "model_id": bentoml_env.get("MODEL_ID"),
            "model_revision": bentoml_env.get("MODEL_REVISION"),
            "model_local_files_only": bentoml_env.get("MODEL_LOCAL_FILES_ONLY"),
        },
    }


def wait_for_idle_queue(base_url: str, timeout: float, max_wait: float = 120.0) -> None:
    deadline = time.monotonic() + max_wait
    while time.monotonic() < deadline:
        health = fetch_health(base_url, timeout)
        queue = health.get("queue") or {}
        if int(queue.get("pending", 0)) == 0 and int(queue.get("processing", 0)) <= 0:
            return
        time.sleep(1.0)
    raise TimeoutError("Queue did not drain within cooldown window")


def run_one_job(
    *,
    base_url: str,
    token: str,
    image_url: str,
    timeout: float,
    poll_interval: float,
    trial: int,
) -> JobMetrics:
    metrics = JobMetrics(trial=trial)
    client = InferenceClient(
        base_url, token, default_timeout=timeout, poll_interval=poll_interval
    )
    run_started = time.perf_counter()

    try:
        submit = client.submit(image_url, timeout=timeout)
        metrics.submit_latency_s = submit.latency_s
        if submit.status_code != 200:
            metrics.final_status = f"SUBMIT_HTTP_{submit.status_code}"
            metrics.error = submit.error
            return metrics

        metrics.job_id = submit.job_id
        if not submit.job_id:
            metrics.final_status = "SUBMIT_NO_JOB_ID"
            metrics.error = submit.error or "missing job_id"
            return metrics

        poll = client.poll_until_done(
            submit.job_id, timeout=timeout, interval=poll_interval
        )
        metrics.final_status = poll.status
        if poll.status == "COMPLETED":
            metrics.e2e_latency_s = time.perf_counter() - run_started
            started = parse_iso(poll.payload.get("started_at"))
            completed = parse_iso(poll.payload.get("completed_at"))
            metrics.processing_s = delta_s(started, completed)
            if (
                metrics.e2e_latency_s is not None
                and metrics.submit_latency_s is not None
                and metrics.processing_s is not None
            ):
                metrics.residual_wait_s = max(
                    0.0,
                    metrics.e2e_latency_s
                    - metrics.submit_latency_s
                    - metrics.processing_s,
                )
            return metrics

        metrics.error = poll.error
        if poll.status == "POLL_TIMEOUT":
            metrics.error = poll.error or f"timeout after {timeout:.1f}s"
        return metrics
    except Exception as exc:
        metrics.final_status = "ERROR"
        metrics.error = f"{type(exc).__name__}: {exc}"
        return metrics


def execute_trial(
    *,
    trial: int,
    base_url: str,
    token: str,
    image_url: str,
    jobs: int,
    concurrency: int,
    warmup: int,
    timeout: float,
    poll_interval: float,
) -> TrialSummary:
    for _ in range(warmup):
        run_one_job(
            base_url=base_url,
            token=token,
            image_url=image_url,
            timeout=timeout,
            poll_interval=poll_interval,
            trial=trial,
        )

    all_records: list[JobMetrics] = []
    wall_started = time.perf_counter()
    with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as executor:
        futures = [
            executor.submit(
                run_one_job,
                base_url=base_url,
                token=token,
                image_url=image_url,
                timeout=timeout,
                poll_interval=poll_interval,
                trial=trial,
            )
            for _ in range(jobs)
        ]
        for future in concurrent.futures.as_completed(futures):
            all_records.append(future.result())
    wall_clock_s = time.perf_counter() - wall_started

    completed = [r for r in all_records if r.final_status == "COMPLETED"]
    failed = len(all_records) - len(completed)
    success_rate = (len(completed) / len(all_records) * 100.0) if all_records else 0.0
    throughput = (len(completed) / wall_clock_s) * 60.0 if wall_clock_s > 0 else 0.0

    return TrialSummary(
        trial=trial,
        jobs=jobs,
        concurrency=concurrency,
        warmup=warmup,
        wall_clock_s=wall_clock_s,
        completed=len(completed),
        failed=failed,
        success_rate_pct=success_rate,
        throughput_jobs_per_min=throughput,
        job_records=all_records,
    )


def aggregate_trials(
    scenario: str,
    trials: list[TrialSummary],
    jobs_per_trial: int,
    concurrency: int,
    warmup: int,
) -> AggregateSummary:
    all_jobs = [job for trial in trials for job in trial.job_records]
    completed_jobs = [job for job in all_jobs if job.final_status == "COMPLETED"]
    failed = len(all_jobs) - len(completed_jobs)
    total_wall = sum(t.wall_clock_s for t in trials)
    total_completed = len(completed_jobs)
    throughput = (total_completed / total_wall) * 60.0 if total_wall > 0 else 0.0

    submit_vals = [j.submit_latency_s for j in completed_jobs if j.submit_latency_s is not None]
    e2e_vals = [j.e2e_latency_s for j in completed_jobs if j.e2e_latency_s is not None]
    processing_vals = [j.processing_s for j in completed_jobs if j.processing_s is not None]
    residual_vals = [j.residual_wait_s for j in completed_jobs if j.residual_wait_s is not None]

    return AggregateSummary(
        scenario=scenario,
        trials=len(trials),
        jobs_per_trial=jobs_per_trial,
        concurrency=concurrency,
        warmup=warmup,
        total_jobs=len(all_jobs),
        completed=total_completed,
        failed=failed,
        success_rate_pct=(total_completed / len(all_jobs) * 100.0) if all_jobs else 0.0,
        throughput_jobs_per_min=throughput,
        submit=compute_metric_stats(submit_vals),
        e2e=compute_metric_stats(e2e_vals, lambda v: percentile(v, 50)),
        processing=compute_metric_stats(processing_vals),
        residual_wait=compute_metric_stats(residual_vals),
    )


def metric_stats_to_dict(stats: MetricStats) -> dict[str, Any]:
    return asdict(stats)


def aggregate_to_dict(aggregate: AggregateSummary) -> dict[str, Any]:
    return {
        "scenario": aggregate.scenario,
        "trials": aggregate.trials,
        "jobs_per_trial": aggregate.jobs_per_trial,
        "concurrency": aggregate.concurrency,
        "warmup": aggregate.warmup,
        "total_jobs": aggregate.total_jobs,
        "completed": aggregate.completed,
        "failed": aggregate.failed,
        "success_rate_pct": aggregate.success_rate_pct,
        "throughput_jobs_per_min": aggregate.throughput_jobs_per_min,
        "submit": metric_stats_to_dict(aggregate.submit),
        "e2e": metric_stats_to_dict(aggregate.e2e),
        "processing": metric_stats_to_dict(aggregate.processing),
        "residual_wait": metric_stats_to_dict(aggregate.residual_wait),
    }


def format_ci(stats: MetricStats) -> str:
    if stats.p50 is None:
        return "n/a"
    if stats.ci95_low is not None and stats.ci95_high is not None:
        return f"{stats.p50:.2f} [{stats.ci95_low:.2f}, {stats.ci95_high:.2f}]"
    return f"{stats.p50:.2f}"


def format_aggregate_text(aggregate: AggregateSummary) -> str:
    lines = [
        f"=== {aggregate.scenario} (aggregate over {aggregate.trials} trials) ===",
        f"Jobs total         : {aggregate.total_jobs} ({aggregate.jobs_per_trial}/trial, c={aggregate.concurrency})",
        f"Completed / failed : {aggregate.completed} / {aggregate.failed}",
        f"Success rate       : {aggregate.success_rate_pct:.1f}%",
        f"Throughput         : {aggregate.throughput_jobs_per_min:.2f} jobs/min",
        f"Submit  p50/p95    : {aggregate.submit.p50:.3f}s / {aggregate.submit.p95:.3f}s"
        if aggregate.submit.p50 is not None
        else "Submit  p50/p95    : n/a",
        f"E2E     p50/p95    : {aggregate.e2e.p50:.3f}s / {aggregate.e2e.p95:.3f}s"
        if aggregate.e2e.p50 is not None
        else "E2E     p50/p95    : n/a",
        f"E2E     p50 CI95   : {format_ci(aggregate.e2e)}",
        f"Processing p50/p95 : {aggregate.processing.p50:.3f}s / {aggregate.processing.p95:.3f}s"
        if aggregate.processing.p50 is not None
        else "Processing p50/p95 : n/a",
        f"Residual p50/p95    : {aggregate.residual_wait.p50:.3f}s / {aggregate.residual_wait.p95:.3f}s"
        if aggregate.residual_wait.p50 is not None
        else "Residual p50/p95    : n/a",
    ]
    return "\n".join(lines)


def latex_table_row(label: str, aggregate: AggregateSummary) -> str:
    e2e = format_ci(aggregate.e2e)
    proc = f"{aggregate.processing.p50:.2f}" if aggregate.processing.p50 is not None else "n/a"
    tp = f"{aggregate.throughput_jobs_per_min:.1f}"
    success = f"{aggregate.success_rate_pct:.0f}\\%"
    return f"{label} & {tp} & {e2e} & {proc} & {success} \\\\"


def write_latex_summary(path: Path, aggregates: list[AggregateSummary]) -> None:
    lines = [
        "% Auto-generated by scripts/benchmarks/benchmark_suite.py",
        "\\begin{tabular}{lrrrr}",
        "\\toprule",
        "Config & Throughput (jobs/min) & E2E p50 [95\\% CI] (s) & Processing p50 (s) & Success \\\\",
        "\\midrule",
    ]
    labels = {
        "baseline": "1 rep, $c{=}1$",
        "load-5": "1 rep, $c{=}5$",
        "load-10": "1 rep, $c{=}10$",
        "scaleout-3": "3 rep, $c{=}10$",
    }
    for aggregate in aggregates:
        label = labels.get(aggregate.scenario, aggregate.scenario)
        lines.append(latex_table_row(label, aggregate))
    lines.extend(["\\bottomrule", "\\end{tabular}", ""])
    path.write_text("\n".join(lines), encoding="utf-8")


def resolve_scenario_params(args: argparse.Namespace) -> tuple[int, int, int]:
    if args.scenario:
        preset = SCENARIOS[args.scenario]
        jobs = args.jobs if args.jobs is not None else preset["jobs"]
        concurrency = (
            args.concurrency if args.concurrency is not None else preset["concurrency"]
        )
        warmup = args.warmup if args.warmup is not None else preset["warmup"]
        return jobs, concurrency, warmup
    jobs = args.jobs if args.jobs is not None else 20
    concurrency = args.concurrency if args.concurrency is not None else 1
    warmup = args.warmup if args.warmup is not None else 2
    return jobs, concurrency, warmup


def main() -> int:
    parser = argparse.ArgumentParser(description="Paper benchmark suite for SEM classifier API.")
    add_service_arguments(parser)
    parser.add_argument(
        "--scenario",
        choices=sorted(SCENARIOS.keys()),
        default="",
        help="Preset scenario (overrides jobs/concurrency/warmup unless explicitly set)",
    )
    parser.add_argument("--jobs", type=positive_int, default=None)
    parser.add_argument("--concurrency", type=positive_int, default=None)
    parser.add_argument("--warmup", type=non_negative_int, default=None)
    parser.add_argument("--trials", type=positive_int, default=3)
    parser.add_argument("--cooldown-s", type=float, default=10.0)
    parser.add_argument("--timeout", type=float, default=180.0)
    parser.add_argument("--poll-interval", type=float, default=0.1)
    parser.add_argument("--image-url", default=DEFAULT_IMAGE_URL)
    parser.add_argument("--output-dir", default="")
    parser.add_argument("--output", choices=("text", "json"), default="text")
    parser.add_argument("--write-latex", default="")
    args = parser.parse_args()

    ctx = resolve_service_context(args)

    if not ctx.auth_client_secret:
        raise SystemExit(
            "AUTH_CLIENT_SECRET or --auth-client-secret is required for Authentik tokens"
        )

    scenario = args.scenario or "custom"
    jobs, concurrency, warmup = resolve_scenario_params(args)

    token = get_token(
        ctx.auth_token_url,
        ctx.auth_client_id,
        ctx.auth_client_secret,
        ctx.auth_host_header,
        timeout=args.timeout,
    )
    environment = capture_environment(
        ctx.base_url, ctx.namespace, args.timeout, args.poll_interval
    )

    trial_summaries: list[TrialSummary] = []
    for trial_idx in range(1, args.trials + 1):
        if trial_idx > 1 and args.cooldown_s > 0:
            time.sleep(args.cooldown_s)
            wait_for_idle_queue(ctx.base_url, args.timeout)
        summary = execute_trial(
            trial=trial_idx,
            base_url=ctx.base_url,
            token=token,
            image_url=args.image_url,
            jobs=jobs,
            concurrency=concurrency,
            warmup=warmup if trial_idx == 1 else 0,
            timeout=args.timeout,
            poll_interval=args.poll_interval,
        )
        trial_summaries.append(summary)

        if args.output_dir:
            trial_dir = Path(args.output_dir)
            trial_dir.mkdir(parents=True, exist_ok=True)
            trial_path = trial_dir / f"trial-{trial_idx}.json"
            trial_payload = {
                "scenario": scenario,
                "environment": environment,
                "trial": asdict(summary),
                "jobs": [asdict(job) for job in summary.job_records],
            }
            trial_path.write_text(json.dumps(trial_payload, indent=2) + "\n", encoding="utf-8")

    aggregate = aggregate_trials(
        scenario=scenario,
        trials=trial_summaries,
        jobs_per_trial=jobs,
        concurrency=concurrency,
        warmup=warmup,
    )

    payload = {
        "scenario": scenario,
        "image_url": args.image_url,
        "metric_notes": {
            "e2e_latency_s": "Client submit through first COMPLETED poll",
            "submit_latency_s": "HTTP /inference including URL fetch before enqueue",
            "processing_s": "Server started_at to completed_at (ViT inference)",
            "residual_wait_s": "E2E - submit - processing (queue + poll granularity)",
        },
        "environment": environment,
        "aggregate": aggregate_to_dict(aggregate),
        "trials": [
            {
                key: value
                for key, value in asdict(t).items()
                if key != "job_records"
            }
            for t in trial_summaries
        ],
        "jobs": [
            asdict(job)
            for trial in trial_summaries
            for job in trial.job_records
        ],
    }

    if args.output_dir:
        aggregate_path = Path(args.output_dir) / "aggregate.json"
        aggregate_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    if args.write_latex:
        write_latex_summary(Path(args.write_latex), [aggregate])

    if args.output == "json":
        print(json.dumps(payload, indent=2))
    else:
        print(format_aggregate_text(aggregate))

    return 0 if aggregate.failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
