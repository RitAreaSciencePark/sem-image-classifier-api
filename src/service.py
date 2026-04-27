"""SEM classifier BentoML service implementation."""

import logging
import os
from pathlib import Path
from typing import Dict

import bentoml
import torch
from PIL import Image
from pydantic import BaseModel
from transformers import AutoImageProcessor, AutoModelForImageClassification

try:
    from image_service import ImageAsyncModelService
except ModuleNotFoundError:
    from src.image_service import ImageAsyncModelService

logger = logging.getLogger("bentoml.service")


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _normalize_model_source(raw_source: str) -> str:
    source = (raw_source or "hugging_face").strip() or "hugging_face"
    if source == "hf_public":
        return "hugging_face"
    if source in {"local_dir", "private_cache"}:
        return "private"
    return source


MODEL_SOURCE = _normalize_model_source(os.getenv("MODEL_SOURCE", "hugging_face"))
MODEL_ID = os.getenv("MODEL_ID", "").strip()
MODEL_REVISION = os.getenv("MODEL_REVISION", "").strip()
MODEL_LOCAL_FILES_ONLY = _env_bool("MODEL_LOCAL_FILES_ONLY", True)

if MODEL_SOURCE not in {"hugging_face", "private"}:
    raise RuntimeError("MODEL_SOURCE must be hugging_face or private")
if not MODEL_ID:
    raise RuntimeError("MODEL_ID must be set")
if MODEL_SOURCE == "hugging_face" and not MODEL_REVISION:
    raise RuntimeError("MODEL_REVISION must be set when MODEL_SOURCE=hugging_face")


class SEMInferenceResult(BaseModel):
    """Inference output payload for the SEM classifier."""

    label: str
    confidence: float
    all_scores: Dict[str, float]
    device_used: str


def _cached_model_snapshots_dir() -> Path:
    hub_cache = os.getenv("HF_HUB_CACHE")
    if not hub_cache:
        hf_home = os.getenv("HF_HOME", os.path.expanduser("~/.cache/huggingface"))
        hub_cache = os.path.join(hf_home, "hub")
    return Path(hub_cache) / f"models--{MODEL_ID.replace('/', '--')}" / "snapshots"


def _revision_arg() -> str | None:
    if MODEL_REVISION:
        return MODEL_REVISION
    if MODEL_SOURCE != "private":
        return None

    snapshots_dir = _cached_model_snapshots_dir()
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


@bentoml.service(name="sem-image-classifier", traffic={"timeout": 300})
class SEMInferenceRedisService(ImageAsyncModelService):
    """SEM image-classification implementation over the async image service."""

    def __init__(self):
        self.device = "cuda" if torch.cuda.is_available() else "cpu"
        self.image_processor = None
        super().__init__()

    def _load_model_components(self) -> None:
        local_only = True if MODEL_SOURCE == "private" else MODEL_LOCAL_FILES_ONLY
        revision = _revision_arg()

        logger.info(
            "Loading model_source=%s model_id=%s revision=%s local_files_only=%s device=%s",
            MODEL_SOURCE,
            MODEL_ID,
            revision or "<default>",
            local_only,
            self.device,
        )

        self.image_processor = AutoImageProcessor.from_pretrained(
            MODEL_ID,
            revision=revision,
            local_files_only=local_only,
        )
        self.model = AutoModelForImageClassification.from_pretrained(
            MODEL_ID,
            revision=revision,
            local_files_only=local_only,
        )
        self.model.to(self.device)
        self.model.eval()
        logger.info("Model loaded successfully on %s", self.device)

    def _run_model_inference(self, inference_input: Image.Image) -> SEMInferenceResult:
        if self.model is None or self.image_processor is None:
            raise RuntimeError("Model is not loaded")

        image = inference_input
        if image.mode != "RGB":
            image = image.convert("RGB")

        inputs = self.image_processor(images=image, return_tensors="pt")
        inputs = {key: value.to(self.device) for key, value in inputs.items()}

        with torch.no_grad():
            outputs = self.model(**inputs)
            logits = outputs.logits
            probabilities = torch.nn.functional.softmax(logits, dim=-1).squeeze()
            predicted_idx = logits.argmax(-1).item()
            predicted_label = self.model.config.id2label[predicted_idx]
            confidence = float(probabilities[predicted_idx].item())
            all_scores = {
                self.model.config.id2label[index]: float(probabilities[index].item())
                for index in range(len(probabilities))
            }

        return SEMInferenceResult(
            label=predicted_label,
            confidence=confidence,
            all_scores=all_scores,
            device_used=self.device,
        )
