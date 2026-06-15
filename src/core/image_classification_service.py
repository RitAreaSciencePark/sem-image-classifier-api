"""Default ViT-style image classification service base."""

import logging

import bentoml
import torch
from PIL import Image
from transformers import AutoImageProcessor, AutoModelForImageClassification

try:
    from core.classification_result import ClassificationResult
    from core.image_service import ImageAsyncModelService
    from core.model_env import (
        MODEL_ID,
        MODEL_LOCAL_FILES_ONLY,
        MODEL_SOURCE,
        revision_arg,
    )
except ModuleNotFoundError:
    from src.core.classification_result import ClassificationResult
    from src.core.image_service import ImageAsyncModelService
    from src.core.model_env import (
        MODEL_ID,
        MODEL_LOCAL_FILES_ONLY,
        MODEL_SOURCE,
        revision_arg,
    )

logger = logging.getLogger("bentoml.service")


class ImageClassificationModelService(ImageAsyncModelService):
    """Loads HuggingFace image classifiers and runs softmax inference."""

    def __init__(self):
        self.device = "cuda" if torch.cuda.is_available() else "cpu"
        self.image_processor = None
        super().__init__()

    def _load_model_components(self) -> None:
        local_only = True if MODEL_SOURCE == "private" else MODEL_LOCAL_FILES_ONLY
        revision = revision_arg()

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

    def _to_rgb(self, image: Image.Image) -> Image.Image:
        if image.mode != "RGB":
            return image.convert("RGB")
        return image

    def _classify_logits(self, logits: torch.Tensor) -> ClassificationResult:
        probabilities = torch.nn.functional.softmax(logits, dim=-1).squeeze()
        if probabilities.dim() == 0:
            probabilities = probabilities.unsqueeze(0)
        predicted_idx = int(logits.argmax(-1).item())
        predicted_label = self.model.config.id2label[predicted_idx]
        confidence = float(probabilities[predicted_idx].item())
        all_scores = {
            self.model.config.id2label[index]: float(probabilities[index].item())
            for index in range(len(probabilities))
        }
        return ClassificationResult(
            label=predicted_label,
            confidence=confidence,
            all_scores=all_scores,
            device_used=self.device,
        )

    def _run_model_inference(self, inference_input: Image.Image) -> ClassificationResult:
        if self.model is None or self.image_processor is None:
            raise RuntimeError("Model is not loaded")
        return self._run_model_inference_batch([inference_input])[0]

    def _run_model_inference_batch(
        self, inference_inputs: list[Image.Image]
    ) -> list[ClassificationResult]:
        if self.model is None or self.image_processor is None:
            raise RuntimeError("Model is not loaded")
        if not inference_inputs:
            return []

        images = [self._to_rgb(image) for image in inference_inputs]
        inputs = self.image_processor(images=images, return_tensors="pt")
        inputs = {key: value.to(self.device) for key, value in inputs.items()}

        with torch.no_grad():
            outputs = self.model(**inputs)
            logits = outputs.logits
            if logits.dim() == 1:
                logits = logits.unsqueeze(0)
            return [self._classify_logits(logits[i]) for i in range(logits.size(0))]
