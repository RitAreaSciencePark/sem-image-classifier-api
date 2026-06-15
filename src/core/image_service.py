"""Image-input layer for async non-LLM BentoML services."""

import base64
import io
import logging
from abc import ABC
from typing import Optional
from urllib.parse import urlparse

import bentoml
import requests
from PIL import Image

try:
    from core.job_payload import encode_image_payload, encode_url_payload
    from core.model_service import BaseAsyncModelService, JobSubmitResponse
except ModuleNotFoundError:
    try:
        from job_payload import encode_image_payload, encode_url_payload
        from model_service import BaseAsyncModelService, JobSubmitResponse
    except ModuleNotFoundError:
        from src.core.job_payload import encode_image_payload, encode_url_payload
        from src.core.model_service import BaseAsyncModelService, JobSubmitResponse

logger = logging.getLogger("bentoml.service")


class ImageAsyncModelService(BaseAsyncModelService, ABC):
    """Adds PIL image input handling to the generic async service."""

    MAX_IMAGE_SIZE = 50 * 1024 * 1024

    def _serialize_inference_input(self, inference_input: Image.Image) -> str:
        buffer = io.BytesIO()
        inference_input.save(buffer, format="PNG")
        return encode_image_payload(base64.b64encode(buffer.getvalue()).decode("utf-8"))

    def _deserialize_inference_input(self, payload: str) -> Image.Image:
        return self._resolve_payload_to_image(payload)

    def _resolve_payload_to_image(self, payload: str) -> Image.Image:
        kind, data = self._parse_job_payload(payload)
        if kind == "url":
            return self._load_image_from_url(str(data["url"]))
        if kind in ("image_b64", "legacy_image_b64"):
            image_bytes = base64.b64decode(str(data["data"]))
            return Image.open(io.BytesIO(image_bytes))
        raise ValueError(f"Unsupported payload kind: {kind}")

    def _parse_job_payload(self, payload: str) -> tuple[str, dict]:
        try:
            from core.job_payload import decode_payload_kind
        except ModuleNotFoundError:
            try:
                from job_payload import decode_payload_kind
            except ModuleNotFoundError:
                from src.core.job_payload import decode_payload_kind
        return decode_payload_kind(payload)

    def _load_image_from_url(self, image_url: str) -> Image.Image:
        parsed = urlparse(image_url)
        if parsed.scheme not in ("http", "https"):
            raise ValueError(
                f"Invalid URL scheme '{parsed.scheme}'. Only http/https allowed."
            )
        if not parsed.netloc:
            raise ValueError(f"Invalid URL: {image_url}")

        logger.info("Fetching image from URL: %s", image_url)
        try:
            response = requests.get(
                image_url,
                timeout=10,
                stream=True,
                headers={"User-Agent": "BentoML-ImageInference/1.0"},
            )
            response.raise_for_status()
        except requests.exceptions.Timeout as exc:
            raise RuntimeError("URL request timed out (10s)") from exc
        except requests.exceptions.RequestException as exc:
            raise RuntimeError(f"Failed to fetch URL: {exc}") from exc

        content_type = response.headers.get("Content-Type", "").lower()
        if not content_type.startswith("image/"):
            raise ValueError(
                f"URL did not return an image (Content-Type: {content_type})"
            )

        content_length = response.headers.get("Content-Length")
        if content_length and int(content_length) > self.MAX_IMAGE_SIZE:
            max_mb = self.MAX_IMAGE_SIZE // (1024 * 1024)
            raise ValueError(
                f"Image too large: {int(content_length)} bytes (max {max_mb}MB)"
            )

        chunks = []
        downloaded = 0
        for chunk in response.iter_content(chunk_size=8192):
            downloaded += len(chunk)
            if downloaded > self.MAX_IMAGE_SIZE:
                max_mb = self.MAX_IMAGE_SIZE // (1024 * 1024)
                raise ValueError(f"Image download exceeded {max_mb}MB limit")
            chunks.append(chunk)

        try:
            image = Image.open(io.BytesIO(b"".join(chunks)))
            logger.info(
                "Image downloaded: %s %s (%d bytes)", image.size, image.mode, downloaded
            )
            return image
        except Exception as exc:
            raise RuntimeError(f"Failed to decode image from URL: {exc}") from exc

    def _resolve_image_input(
        self,
        image: Optional[Image.Image],
        image_url: Optional[str],
    ) -> Image.Image:
        if image is None and image_url is None:
            raise ValueError(
                "Provide either 'image' (file upload) or 'image_url' (JSON)."
            )
        if image is not None and image_url is not None:
            raise ValueError("Provide either 'image' or 'image_url', not both.")
        if image_url is not None:
            return self._load_image_from_url(image_url)
        return image

    @bentoml.api
    def inference(
        self,
        image: Optional[Image.Image] = None,
        image_url: Optional[str] = None,
    ) -> JobSubmitResponse:
        """Submit an image inference job and return a tracking id immediately."""
        if image_url is not None and image is None:
            payload = encode_url_payload(image_url)
            return self._submit_raw_payload(payload)
        resolved_input = self._resolve_image_input(image=image, image_url=image_url)
        return self._submit_inference_input(resolved_input)
