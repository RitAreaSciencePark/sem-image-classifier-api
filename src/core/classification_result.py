"""Shared Pydantic schema for image classification results."""

from typing import Dict

from pydantic import BaseModel


class ClassificationResult(BaseModel):
    label: str
    confidence: float
    all_scores: Dict[str, float]
    device_used: str
