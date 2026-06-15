#!/usr/bin/env python3
"""Validate BentoML HPA and throughput under burst load (queue + replica + HPA telemetry)."""

from __future__ import annotations

import sys
from pathlib import Path

_repo_root = Path(__file__).resolve().parents[2]
if str(_repo_root) not in sys.path:
    sys.path.insert(0, str(_repo_root))

import argparse
import concurrent.futures
import json
import re
import statistics
import subprocess
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any

from ml_platform.devtools.api_client import DEFAULT_IMAGE_URL, InferenceClient, get_token
from ml_platform.devtools.cli import add_service_arguments, resolve_service_context
from scripts.benchmarks.benchmark_suite import fetch_health, kubectl_json, wait_for_idle_queue

PROFILES: dict[str, dict[str, int | float]] = {
    "light": {"jobs": 50, "concurrency": 12, "users": 1, "observe_duration": 300},
    "heavy": {"jobs": 1000, "concurrency": 80, "users": 50, "observe_duration": 900},
    "sweep": {"jobs": 500, "concurrency": 80, "users": 50, "observe_duration": 600},
}


@dataclass
class TelemetrySample:
    timestamp_utc: str
    elapsed_s: float
    queue_pending: int
    queue_processing: int
    replicas: int | None
    ready_replicas: int | None
    hpa_cpu_current: str | None
    hpa_cpu_target: str | None
    hpa_replicas: int | None
    bentoml_top_cpu: str | None = None
    krakend_top_cpu: str | None = None


@dataclass
class SubmitResult:
    ok: bool
    job_id: str
    latency_s: float
    error: str
    user: str = ""


@dataclass
class ValidationReport:
    profile: str
    phase: str
    service_id: str
    namespace: str
    started_at_utc: str
    jobs: int
    concurrency: int
    users: int
    poll_interval_s: float
    observe_duration_s: float
    baseline_replicas: int | None
    max_replicas_observed: int
    max_queue_pending: int
    scaled_up: bool
    time_to_scale_s: float | None
    submits_ok: int
    submits_failed: int
    submit_wall_s: float
    submit_latency_p50_s: float | None
    submit_latency_p95_s: float | None
    drain_s: float | None
    throughput_jobs_per_min: float | None
    worker_drain_jobs_per_min: float | None
    peak_bentoml_memory_mib: float | None
    telemetry: list[TelemetrySample] = field(default_factory=list)
    verdict: str = ""
    notes: list[str] = field(default_factory=list)


def positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError(f"value must be > 0: {value}")
    return parsed


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def parse_hpa_cpu(hpa: dict[str, Any] | None) -> tuple[str | None, str | None, int | None]:
    if not hpa:
        return None, None, None
    status = hpa.get("status") or {}
    current = None
    target = None
    for metric in status.get("currentMetrics") or []:
        resource = metric.get("resource") or {}
        if resource.get("name") == "cpu":
            util = resource.get("current", {}).get("averageUtilization")
            if util is not None:
                current = f"{util}%"
    spec = hpa.get("spec") or {}
    for metric in spec.get("metrics") or []:
        resource = metric.get("resource") or {}
        if resource.get("name") == "cpu":
            target_util = resource.get("target", {}).get("averageUtilization")
            if target_util is not None:
                target = f"{target_util}%"
    replicas = status.get("currentReplicas")
    return current, target, replicas


def kubectl_top_pods(namespace: str, label: str) -> str | None:
    try:
        result = subprocess.run(
            [
                "kubectl",
                "top",
                "pods",
                "-n",
                namespace,
                "-l",
                label,
                "--no-headers",
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
        return "; ".join(lines[:5]) if lines else None
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None


def get_deploy_replicas(namespace: str) -> tuple[int | None, int | None]:
    deploy = kubectl_json(["get", "deploy", "bentoml", "-n", namespace, "-o", "json"])
    if not deploy:
        return None, None
    spec = deploy.get("spec") or {}
    status = deploy.get("status") or {}
    return spec.get("replicas"), status.get("readyReplicas")


def get_hpa(namespace: str) -> dict[str, Any] | None:
    return kubectl_json(["get", "hpa", "bentoml-hpa", "-n", namespace, "-o", "json"])


def sample_telemetry(
    *,
    base_url: str,
    namespace: str,
    elapsed_s: float,
    timeout: float,
    include_top: bool,
) -> TelemetrySample:
    health = fetch_health(base_url, timeout)
    queue = health.get("queue") or {}
    replicas, ready = get_deploy_replicas(namespace)
    hpa = get_hpa(namespace)
    cpu_current, cpu_target, hpa_replicas = parse_hpa_cpu(hpa)
    bentoml_top = krakend_top = None
    if include_top:
        bentoml_top = kubectl_top_pods(namespace, "app.kubernetes.io/name=bentoml")
        krakend_top = kubectl_top_pods(namespace, "app.kubernetes.io/name=krakend")
    return TelemetrySample(
        timestamp_utc=utc_now(),
        elapsed_s=elapsed_s,
        queue_pending=int(queue.get("pending", 0)),
        queue_processing=int(queue.get("processing", 0)),
        replicas=replicas,
        ready_replicas=ready,
        hpa_cpu_current=cpu_current,
        hpa_cpu_target=cpu_target,
        hpa_replicas=hpa_replicas,
        bentoml_top_cpu=bentoml_top,
        krakend_top_cpu=krakend_top,
    )


def submit_one(
    *,
    base_url: str,
    token: str,
    image_url: str,
    timeout: float,
    user: str,
) -> SubmitResult:
    started = time.perf_counter()
    try:
        client = InferenceClient(base_url, token, default_timeout=timeout)
        submit = client.submit(image_url, timeout=timeout)
        return SubmitResult(
            ok=submit.status_code == 200 and bool(submit.job_id),
            job_id=submit.job_id or "",
            latency_s=time.perf_counter() - started,
            error=submit.error or "",
            user=user,
        )
    except Exception as exc:
        return SubmitResult(
            ok=False,
            job_id="",
            latency_s=time.perf_counter() - started,
            error=f"{type(exc).__name__}: {exc}",
            user=user,
        )


def wait_for_replicas(
    namespace: str,
    target: int,
    *,
    timeout_s: float,
    poll_s: float = 5.0,
) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        replicas, ready = get_deploy_replicas(namespace)
        if ready is not None and ready == target and replicas == target:
            return True
        time.sleep(poll_s)
    return False


def run_burst_submits(
    *,
    base_url: str,
    tokens: dict[str, str],
    user_names: list[str],
    image_url: str,
    jobs: int,
    concurrency: int,
    timeout: float,
) -> tuple[int, int, list[SubmitResult]]:
    results: list[SubmitResult] = []
    ok_count = 0
    fail_count = 0

    def pick_user(index: int) -> str:
        return user_names[index % len(user_names)]

    with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as executor:
        futures = [
            executor.submit(
                submit_one,
                base_url=base_url,
                token=tokens[pick_user(i)],
                image_url=image_url,
                timeout=timeout,
                user=pick_user(i),
            )
            for i in range(jobs)
        ]
        for future in concurrent.futures.as_completed(futures):
            result = future.result()
            results.append(result)
            if result.ok:
                ok_count += 1
            else:
                fail_count += 1
    return ok_count, fail_count, results


def telemetry_loop(
    *,
    base_url: str,
    namespace: str,
    timeout: float,
    poll_interval_s: float,
    duration_s: float,
    include_top: bool,
    samples: list[TelemetrySample],
    stop_event: threading.Event,
    started_monotonic: float,
) -> None:
    while not stop_event.is_set():
        elapsed = time.monotonic() - started_monotonic
        if elapsed > duration_s:
            break
        try:
            samples.append(
                sample_telemetry(
                    base_url=base_url,
                    namespace=namespace,
                    elapsed_s=elapsed,
                    timeout=timeout,
                    include_top=include_top,
                )
            )
        except Exception:
            pass
        stop_event.wait(poll_interval_s)


_MEMORY_MIB_RE = re.compile(r"(\d+(?:\.\d+)?)\s*Mi\b", re.IGNORECASE)


def parse_bentoml_memory_mib(top_line: str | None) -> float | None:
    """Extract max Mi value from kubectl top pod line(s)."""
    if not top_line:
        return None
    values = [float(match.group(1)) for match in _MEMORY_MIB_RE.finditer(top_line)]
    return max(values) if values else None


def peak_bentoml_memory_from_samples(samples: list[TelemetrySample]) -> float | None:
    peaks = [
        parsed
        for sample in samples
        if (parsed := parse_bentoml_memory_mib(sample.bentoml_top_cpu)) is not None
    ]
    return max(peaks) if peaks else None


def compute_worker_drain_jobs_per_min(
    submits_ok: int,
    submit_wall_s: float,
    drain_s: float | None,
) -> float | None:
    if submits_ok <= 0 or drain_s is None:
        return None
    worker_window_s = submit_wall_s + drain_s
    if worker_window_s <= 0:
        return None
    return (submits_ok / worker_window_s) * 60.0


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


def build_verdict(report: ValidationReport) -> str:
    success_rate = report.submits_ok / report.jobs if report.jobs else 0.0
    if success_rate < 0.95:
        return f"FAIL: Submit success rate {success_rate:.1%} below 95%"
    if report.profile == "sweep":
        tp = report.throughput_jobs_per_min
        tp_note = f", {tp:.1f} jobs/min" if tp is not None else ""
        return f"PASS: Sweep completed ({success_rate:.1%} submit success{tp_note})"
    if report.profile == "heavy":
        if report.scaled_up or report.max_replicas_observed > (report.baseline_replicas or 1):
            return "PASS: Heavy burst handled with scale-out"
        if report.max_queue_pending >= 10:
            return "WARN: Heavy backlog without scale-out (check HPA signal or maxReplicas)"
        return "PASS: Heavy burst completed (load may not have required scale-out)"
    if report.profile == "light":
        return f"PASS: Light burst completed ({success_rate:.1%} submit success)"
    if report.scaled_up:
        return "PASS: HPA scaled out under burst load"
    if report.max_queue_pending >= 5 and report.submits_ok >= report.jobs * 0.9:
        return "FAIL: Queue backlog grew but replicas did not increase"
    return "INCONCLUSIVE: Insufficient backlog or load to trigger scale-out"


def analyze_samples(
    samples: list[TelemetrySample],
    baseline_replicas: int | None,
) -> tuple[int, int, bool, float | None]:
    max_replicas = baseline_replicas or 1
    max_pending = 0
    scaled_up = False
    time_to_scale: float | None = None
    baseline = baseline_replicas or 1

    for sample in samples:
        max_pending = max(max_pending, sample.queue_pending)
        ready = sample.ready_replicas or sample.replicas or baseline
        if ready is not None:
            max_replicas = max(max_replicas, ready)
            if ready > baseline and not scaled_up:
                scaled_up = True
                time_to_scale = sample.elapsed_s

    return max_replicas, max_pending, scaled_up, time_to_scale


def resolve_profile_params(args: argparse.Namespace) -> tuple[str, int, int, int, float]:
    profile = args.profile or "custom"
    if args.profile:
        preset = PROFILES[args.profile]
        jobs = args.jobs if args.jobs is not None else int(preset["jobs"])
        concurrency = (
            args.concurrency if args.concurrency is not None else int(preset["concurrency"])
        )
        users = args.users if args.users is not None else int(preset["users"])
        observe = (
            args.observe_duration
            if args.observe_duration is not None
            else float(preset["observe_duration"])
        )
        return profile, jobs, concurrency, users, observe
    jobs = args.jobs if args.jobs is not None else 50
    concurrency = args.concurrency if args.concurrency is not None else 12
    users = args.users if args.users is not None else 1
    observe = args.observe_duration if args.observe_duration is not None else 300.0
    return profile, jobs, concurrency, users, observe


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Burst-load validation for BentoML CPU HPA autoscaling."
    )
    add_service_arguments(parser)
    parser.add_argument(
        "--profile",
        choices=sorted(PROFILES.keys()),
        default="",
        help="Preset: light (50 jobs), heavy (1000 jobs), or sweep (500 jobs, throughput-only pass)",
    )
    parser.add_argument("--phase", default="p0", help="Experiment phase label for artifacts")
    parser.add_argument("--jobs", type=positive_int, default=None)
    parser.add_argument("--concurrency", type=positive_int, default=None)
    parser.add_argument("--users", type=positive_int, default=None)
    parser.add_argument("--timeout", type=float, default=180.0)
    parser.add_argument("--poll-interval", type=float, default=5.0)
    parser.add_argument("--observe-duration", type=float, default=None)
    parser.add_argument("--image-url", default=DEFAULT_IMAGE_URL)
    parser.add_argument("--output-dir", default="")
    parser.add_argument("--skip-reset", action="store_true")
    parser.add_argument("--skip-drain", action="store_true")
    args = parser.parse_args()

    profile, jobs, concurrency, users, observe_duration = resolve_profile_params(args)

    ctx = resolve_service_context(args)
    if not ctx.auth_client_secret:
        raise SystemExit(
            "AUTH_CLIENT_SECRET or --auth-client-secret is required for Authentik tokens"
        )

    user_names = [f"user-{i:03d}" for i in range(1, users + 1)]
    print("Acquiring API token (client_credentials shared across simulated users)...")
    shared_token = get_token(
        ctx.auth_token_url,
        ctx.auth_client_id,
        ctx.auth_client_secret,
        ctx.auth_host_header,
        timeout=args.timeout,
    )
    tokens = {name: shared_token for name in user_names}

    baseline_replicas, _ = get_deploy_replicas(ctx.namespace)
    notes: list[str] = []
    if not args.skip_reset and baseline_replicas and baseline_replicas > 1:
        notes.append(f"Waiting for scale-down to 1 replica (currently {baseline_replicas})")
        if not wait_for_replicas(ctx.namespace, 1, timeout_s=360.0):
            notes.append("Scale-down to 1 replica did not complete within 6 minutes")
        baseline_replicas, _ = get_deploy_replicas(ctx.namespace)

    samples: list[TelemetrySample] = []
    stop_event = threading.Event()
    started = time.monotonic()
    include_top = profile in ("heavy", "sweep")
    collector = threading.Thread(
        target=telemetry_loop,
        kwargs={
            "base_url": ctx.base_url,
            "namespace": ctx.namespace,
            "timeout": args.timeout,
            "poll_interval_s": args.poll_interval,
            "duration_s": observe_duration,
            "include_top": include_top,
            "samples": samples,
            "stop_event": stop_event,
            "started_monotonic": started,
        },
        daemon=True,
    )
    collector.start()

    submit_started = time.monotonic()
    submits_ok, submits_failed, submit_results = run_burst_submits(
        base_url=ctx.base_url,
        tokens=tokens,
        user_names=user_names,
        image_url=args.image_url,
        jobs=jobs,
        concurrency=concurrency,
        timeout=args.timeout,
    )
    submit_wall_s = time.monotonic() - submit_started
    latencies = [r.latency_s for r in submit_results if r.ok]
    notes.append(
        f"Burst submit completed in {submit_wall_s:.1f}s ({submits_ok} ok, {submits_failed} failed)"
    )

    stop_event.wait(max(0.0, observe_duration - (time.monotonic() - started)))
    stop_event.set()
    collector.join(timeout=5.0)

    if samples:
        samples.append(
            sample_telemetry(
                base_url=ctx.base_url,
                namespace=ctx.namespace,
                elapsed_s=time.monotonic() - started,
                timeout=args.timeout,
                include_top=include_top,
            )
        )

    max_replicas, max_pending, scaled_up, time_to_scale = analyze_samples(
        samples, baseline_replicas
    )

    drain_s: float | None = None
    throughput: float | None = None
    total_wall_s = time.monotonic() - started
    if not args.skip_drain:
        drain_started = time.monotonic()
        try:
            max_drain = 3600.0 if profile in ("heavy", "sweep") else 600.0
            wait_for_idle_queue(ctx.base_url, args.timeout, max_wait=max_drain)
            drain_s = time.monotonic() - drain_started
            notes.append(f"Queue drained in {drain_s:.1f}s")
            if total_wall_s > 0:
                throughput = (submits_ok / total_wall_s) * 60.0
        except TimeoutError:
            drain_s = time.monotonic() - drain_started
            notes.append(f"Queue did not fully drain within {max_drain:.0f}s")
            if total_wall_s > 0:
                throughput = (submits_ok / total_wall_s) * 60.0
    elif submits_ok and submit_wall_s > 0:
        throughput = (submits_ok / submit_wall_s) * 60.0
        notes.append(
            f"Throughput from submit wall ({throughput:.1f} jobs/min, skip-drain)"
        )

    worker_drain = compute_worker_drain_jobs_per_min(submits_ok, submit_wall_s, drain_s)
    peak_memory_mib = peak_bentoml_memory_from_samples(samples)
    if worker_drain is not None:
        notes.append(f"Worker drain rate {worker_drain:.1f} jobs/min (submit+drain window)")
    if peak_memory_mib is not None:
        notes.append(f"Peak BentoML memory {peak_memory_mib:.0f} Mi")

    report = ValidationReport(
        profile=profile,
        phase=args.phase,
        service_id=ctx.service_id,
        namespace=ctx.namespace,
        started_at_utc=utc_now(),
        jobs=jobs,
        concurrency=concurrency,
        users=users,
        poll_interval_s=args.poll_interval,
        observe_duration_s=observe_duration,
        baseline_replicas=baseline_replicas,
        max_replicas_observed=max_replicas,
        max_queue_pending=max_pending,
        scaled_up=scaled_up,
        time_to_scale_s=time_to_scale,
        submits_ok=submits_ok,
        submits_failed=submits_failed,
        submit_wall_s=submit_wall_s,
        submit_latency_p50_s=percentile(latencies, 50),
        submit_latency_p95_s=percentile(latencies, 95),
        drain_s=drain_s,
        throughput_jobs_per_min=throughput,
        worker_drain_jobs_per_min=worker_drain,
        peak_bentoml_memory_mib=peak_memory_mib,
        telemetry=samples,
        notes=notes,
    )
    report.verdict = build_verdict(report)

    payload = asdict(report)
    text_lines = [
        f"=== Autoscale validation ({ctx.service_id}) profile={profile} phase={args.phase} ===",
        f"Verdict           : {report.verdict}",
        f"Baseline replicas : {report.baseline_replicas}",
        f"Max replicas      : {report.max_replicas_observed}",
        f"Max queue pending : {report.max_queue_pending}",
        f"Submit wall       : {report.submit_wall_s:.1f}s",
        f"Submit p50/p95    : {report.submit_latency_p50_s:.3f}s / {report.submit_latency_p95_s:.3f}s"
        if report.submit_latency_p50_s is not None
        else "Submit p50/p95    : n/a",
        f"Drain time        : {report.drain_s:.1f}s"
        if report.drain_s is not None
        else "Drain time        : n/a",
        f"Throughput        : {report.throughput_jobs_per_min:.1f} jobs/min"
        if report.throughput_jobs_per_min is not None
        else "Throughput        : n/a",
        f"Worker drain      : {report.worker_drain_jobs_per_min:.1f} jobs/min"
        if report.worker_drain_jobs_per_min is not None
        else "Worker drain      : n/a",
        f"Peak BentoML mem  : {report.peak_bentoml_memory_mib:.0f} Mi"
        if report.peak_bentoml_memory_mib is not None
        else "Peak BentoML mem  : n/a",
        f"Time to scale     : {report.time_to_scale_s:.1f}s"
        if report.time_to_scale_s is not None
        else "Time to scale     : n/a",
        f"Submits ok/fail   : {report.submits_ok} / {report.submits_failed}",
    ]
    for note in report.notes:
        text_lines.append(f"Note              : {note}")
    print("\n".join(text_lines))

    if args.output_dir:
        out_dir = Path(args.output_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / f"autoscale-{args.phase}.json"
        out_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        print(f"Wrote {out_path}")

    if report.submits_ok < report.jobs * 0.95:
        return 1
    if profile == "sweep":
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
