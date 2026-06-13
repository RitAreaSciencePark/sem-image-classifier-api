"""Environment helpers for model loading."""

import os
from pathlib import Path


def env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def normalize_model_source(raw_source: str) -> str:
    source = (raw_source or "hugging_face").strip() or "hugging_face"
    if source == "hf_public":
        return "hugging_face"
    if source in {"local_dir", "private_cache"}:
        return "private"
    return source


MODEL_SOURCE = normalize_model_source(os.getenv("MODEL_SOURCE", "hugging_face"))
MODEL_ID = os.getenv("MODEL_ID", "").strip()
MODEL_REVISION = os.getenv("MODEL_REVISION", "").strip()
MODEL_LOCAL_FILES_ONLY = env_bool("MODEL_LOCAL_FILES_ONLY", True)

if MODEL_SOURCE not in {"hugging_face", "private"}:
    raise RuntimeError("MODEL_SOURCE must be hugging_face or private")
if not MODEL_ID:
    raise RuntimeError("MODEL_ID must be set")
if MODEL_SOURCE == "hugging_face" and not MODEL_REVISION:
    raise RuntimeError("MODEL_REVISION must be set when MODEL_SOURCE=hugging_face")


def cached_model_snapshots_dir() -> Path:
    hub_cache = os.getenv("HF_HUB_CACHE")
    if not hub_cache:
        hf_home = os.getenv("HF_HOME", os.path.expanduser("~/.cache/huggingface"))
        hub_cache = os.path.join(hf_home, "hub")
    return Path(hub_cache) / f"models--{MODEL_ID.replace('/', '--')}" / "snapshots"


def revision_arg() -> str | None:
    if MODEL_REVISION:
        return MODEL_REVISION
    if MODEL_SOURCE != "private":
        return None

    snapshots_dir = cached_model_snapshots_dir()
    snapshots = (
        sorted(path.name for path in snapshots_dir.iterdir() if path.is_dir())
        if snapshots_dir.is_dir()
        else []
    )
    if len(snapshots) == 1:
        return snapshots[0]
    raise RuntimeError(
        "MODEL_REVISION is required for private mode unless the baked cache has exactly one snapshot "
        f"for MODEL_ID={MODEL_ID} (found {len(snapshots)})"
    )
