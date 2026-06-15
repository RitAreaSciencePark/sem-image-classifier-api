"""Shared CLI flags for service-aware scripts."""

from __future__ import annotations

import argparse
import os

from ml_platform.devtools.models import ServiceContext
from ml_platform.devtools.service_context import load_service_context


def add_service_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--service", default=os.environ.get("SERVICE", "sem-classifier")
    )
    parser.add_argument("--base-url", default="")
    parser.add_argument("--auth-token-url", default="")
    parser.add_argument("--auth-client-id", default="")
    parser.add_argument(
        "--auth-client-secret", default=os.environ.get("AUTH_CLIENT_SECRET", "")
    )
    parser.add_argument("--auth-host-header", default="")


def resolve_service_context(args: argparse.Namespace) -> ServiceContext:
    ctx = load_service_context(args.service)
    return ctx.with_overrides(
        base_url=args.base_url,
        auth_token_url=args.auth_token_url,
        auth_client_id=args.auth_client_id,
        auth_host_header=args.auth_host_header,
        auth_client_secret=args.auth_client_secret,
    )
