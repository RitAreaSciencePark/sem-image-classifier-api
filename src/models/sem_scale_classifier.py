"""SEM scale bar classifier (ViT / timm wrapper)."""

import bentoml

try:
    from core.image_classification_service import ImageClassificationModelService
except ModuleNotFoundError:
    from src.core.image_classification_service import ImageClassificationModelService


@bentoml.service(name="sem-scale-classifier", traffic={"timeout": 300})
class SemScaleClassifierService(ImageClassificationModelService):
    """SEM scale classification over the async image pipeline."""
