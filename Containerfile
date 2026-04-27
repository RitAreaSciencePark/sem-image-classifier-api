# syntax=docker/dockerfile:1.4

# ============================================================================
# Multi-Stage Containerfile for SEM image classifier BentoML service
# ============================================================================
#
# WHY NOT `bentoml containerize`?
# That command is opaque — you can't see/debug the build steps, it requires
# bentoml CLI on the build machine, and creates intermediate Bento artifacts.
# A plain Containerfile is transparent, reproducible, and universal.
#
# BUILD:
#   Prefer: ./k8s/dev.sh build --env dev|prod
#   Manual: podman build \
#     --build-arg MODEL_SOURCE=hugging_face|private \
#     --build-arg MODEL_ID=<huggingface-repo> \
#     --build-arg MODEL_REVISION=<tag-or-commit> \  # required for hugging_face, optional for private
#     --build-context model_cache=<absolute-path-to-hf-cache-root-or-hub-dir> \
#     --build-arg BENTOML_SERVICE=service:SEMInferenceRedisService \
#     -t localhost/sem-image-classifier:latest -f Containerfile .
#
# IMAGE SIZE BREAKDOWN (approximate):
#   python:3.12-slim base  ~150MB
#   PyTorch CPU-only       ~200MB  (vs ~2GB with CUDA stubs)
#   transformers + deps    ~400MB
#   ViT model weights      ~350MB
#   Total                  ~1.1GB
# This image is intentionally CPU-only. GPU support is a separate deployment
# design because it requires node drivers, runtime configuration, image changes,
# PyTorch wheel changes, and Kubernetes resource scheduling.

# ── Stage 1: Builder ────────────────────────────────────────────────────────
# Install dependencies and pre-download the model weights.
# This stage is discarded — only its artifacts are copied to the runtime stage.

# Build-time model pinning (required via --build-arg)
# MODEL_REVISION can be a branch, tag, or commit hash. For production,
# prefer an immutable commit hash for deterministic rollbacks.
ARG MODEL_ID
ARG MODEL_REVISION
ARG MODEL_SOURCE="hugging_face"
ARG HF_BUILD_CACHE_DIR="/tmp/huggingface"
ARG BENTOML_SERVICE="service:SEMInferenceRedisService"

FROM python:3.12-slim AS builder

ARG MODEL_ID
ARG MODEL_REVISION
ARG MODEL_SOURCE
ARG HF_BUILD_CACHE_DIR
ARG BENTOML_SERVICE

RUN [ "$MODEL_SOURCE" = "hugging_face" ] || [ "$MODEL_SOURCE" = "private" ] || (echo "MODEL_SOURCE must be hugging_face or private" >&2; exit 1)
RUN [ -n "$MODEL_ID" ] || (echo "MODEL_ID build arg is required" >&2; exit 1)
RUN [ "$MODEL_SOURCE" = "private" ] || [ -n "$MODEL_REVISION" ] || (echo "MODEL_REVISION build arg is required for hugging_face" >&2; exit 1)

# Builder-local HuggingFace cache path (configurable, default /tmp/huggingface)
# This keeps host-like default cache semantics in runtime while allowing a
# dedicated staging folder during image build.
ENV HF_HOME="${HF_BUILD_CACHE_DIR}"
ENV HF_HUB_CACHE="${HF_BUILD_CACHE_DIR}/hub"

# Install uv for fast, reproducible dependency management
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app

# Copy dependency specification first (Docker layer caching: deps change
# less often than source code, so this layer is cached across builds)
COPY pyproject.toml ./
COPY . .
COPY --from=model_cache . /tmp/model-cache-context

# Install Python dependencies into a virtual environment.
# --index-url restricts torch to CPU-only wheels (~200MB vs ~2GB).
# To get full torch with CUDA stubs: remove the --index-url line.
RUN uv venv /app/.venv && \
    . /app/.venv/bin/activate && \
    uv pip install --index-url https://download.pytorch.org/whl/cpu torch torchvision && \
    uv pip install -r pyproject.toml

# Pre-download the HuggingFace model into the image.
# Without this, every pod startup downloads ~350MB from HuggingFace —
# slow, unreliable, and breaks K8s startup probes.
RUN . /app/.venv/bin/activate && \
    if [ "$MODEL_SOURCE" = "hugging_face" ]; then \
      python -c "\
from transformers import AutoImageProcessor, AutoModelForImageClassification; \
model_id = '${MODEL_ID}'; \
model_revision = '${MODEL_REVISION}'; \
cache_dir = '${HF_HUB_CACHE}'; \
AutoImageProcessor.from_pretrained(model_id, revision=model_revision, cache_dir=cache_dir); \
AutoModelForImageClassification.from_pretrained(model_id, revision=model_revision, cache_dir=cache_dir)"; \
    else \
      mkdir -p "${HF_HUB_CACHE}"; \
      if [ -d "/tmp/model-cache-context/hub" ]; then \
        cp -a /tmp/model-cache-context/hub/. "${HF_HUB_CACHE}/"; \
      else \
        cp -a /tmp/model-cache-context/. "${HF_HUB_CACHE}/"; \
      fi; \
      model_cache_dir="${HF_HUB_CACHE}/models--$(printf '%s' "$MODEL_ID" | sed 's#/#--#g')"; \
      resolved_revision="$MODEL_REVISION"; \
      if [ -z "$resolved_revision" ] && [ -d "$model_cache_dir/snapshots" ]; then \
        snapshot_count="$(find "$model_cache_dir/snapshots" -mindepth 1 -maxdepth 1 -type d | wc -l | tr -d ' ')"; \
        if [ "$snapshot_count" = "1" ]; then \
          mkdir -p "$model_cache_dir/refs"; \
          resolved_revision="$(basename "$(find "$model_cache_dir/snapshots" -mindepth 1 -maxdepth 1 -type d | head -n 1)")"; \
          printf '%s' "$resolved_revision" > "$model_cache_dir/refs/main"; \
        else \
          echo "MODEL_REVISION is required for private cache when snapshot_count=${snapshot_count}" >&2; \
          exit 1; \
        fi; \
      fi; \
      python -c "\
from transformers import AutoImageProcessor, AutoModelForImageClassification; \
model_id = '${MODEL_ID}'; \
model_revision = '${resolved_revision}' or None; \
cache_dir = '${HF_HUB_CACHE}'; \
AutoImageProcessor.from_pretrained(model_id, revision=model_revision, cache_dir=cache_dir, local_files_only=True); \
AutoModelForImageClassification.from_pretrained(model_id, revision=model_revision, cache_dir=cache_dir, local_files_only=True)"; \
    fi


# ── Stage 2: Runtime ────────────────────────────────────────────────────────
# Minimal image with only what's needed to run the service.

FROM python:3.12-slim AS runtime

ARG MODEL_ID
ARG MODEL_REVISION
ARG MODEL_SOURCE
ARG HF_BUILD_CACHE_DIR
ARG BENTOML_SERVICE

ENV MODEL_LOCAL_FILES_ONLY="true"
ENV BENTOML_SERVICE="${BENTOML_SERVICE}"

WORKDIR /app

# Copy virtual environment from builder (all Python packages)
COPY --from=builder /app/.venv /app/.venv

# Copy selected model cache into default runtime HF location
# Builder cache source is configurable via HF_BUILD_CACHE_DIR.
COPY --from=builder ${HF_BUILD_CACHE_DIR} /root/.cache/huggingface

# Copy application source code
COPY src/*.py ./

# Add venv to PATH so `bentoml` command is available
ENV PATH="/app/.venv/bin:$PATH"

# BentoML default port
EXPOSE 3000

# Start the configured BentoML service.
# BENTOML_SERVICE defaults to service:SEMInferenceRedisService, but dev.sh can
# pass another entrypoint when adapting the repo to a different model.
CMD ["sh", "-c", "exec bentoml serve \"$BENTOML_SERVICE\" --host 0.0.0.0 --port 3000"]
