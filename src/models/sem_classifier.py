"""SEM material classifier (ViT)."""

import bentoml

try:
    from core.image_classification_service import ImageClassificationModelService
except ModuleNotFoundError:
    from src.core.image_classification_service import ImageClassificationModelService


@bentoml.service(name="sem-classifier", traffic={"timeout": 300})
class SemClassifierService(ImageClassificationModelService):
    """SEM image classification over the async image pipeline."""
