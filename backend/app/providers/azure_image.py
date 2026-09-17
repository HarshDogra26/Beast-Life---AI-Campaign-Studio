"""Azure OpenAI image adapter.
"""

from __future__ import annotations
import base64
import re
import httpx
from ..config import Settings
from ..logging import get_logger
from .azure_llm import map_http_error
from .base import (
    CredentialsMissingError,
    DeploymentNotFoundError,
    ImageProvider,
    ImageResult,
    MalformedOutputError,
    ProviderError,
    ProviderTimeoutError,
    SizeRejectedError,
    TransientProviderError,
    UnsupportedParameterError,
)
from .capabilities import ImageModelCapabilities, resolve_capabilities

log = get_logger(__name__)

_SIZE_REJECTION_MARKERS = (
    "size",
    "dimension",
    "multiple of 16",
    "aspect ratio",
    "resolution",
)

_UNSUPPORTED_PARAM = re.compile(
    r"does not support the '([a-z_]+)' parameter", re.IGNORECASE
)


class AzureImageProvider(ImageProvider):
    name = "azure_openai_image"

    def __init__(self, settings: Settings, client: httpx.AsyncClient) -> None:
        if not settings.azure_openai_endpoint or not settings.azure_openai_api_key.get_secret_value():
            raise CredentialsMissingError(
                "AZURE_OPENAI_ENDPOINT and AZURE_OPENAI_API_KEY are required in live mode"
            )
        self._settings = settings
        self._client = client
        self._deployment = settings.azure_openai_image_deployment
        self.model = settings.azure_openai_image_model
        self.capabilities: ImageModelCapabilities = resolve_capabilities(
            settings.azure_openai_image_model or settings.azure_openai_image_deployment
        )
        base = (
            f"{settings.azure_openai_endpoint.rstrip('/')}"
            f"/openai/deployments/{settings.azure_openai_image_deployment}/images"
        )
        params = f"?api-version={settings.azure_openai_image_api_version}"
        self._generations_url = f"{base}/generations{params}"
        self._edits_url = f"{base}/edits{params}"
        self._input_fidelity_supported = self.capabilities.supports_input_fidelity

    @property
    def _headers(self) -> dict[str, str]:
        return {"api-key": self._settings.azure_openai_api_key.get_secret_value()}

    async def generate(self, *, prompt: str, size: str, quality: str) -> ImageResult:
        payload = {
            "prompt": prompt,
            "n": 1,
            "size": size,
            "quality": self.capabilities.coerce_quality(quality),
            "output_format": "png",
        }
        body = await self._post_json(self._generations_url, payload, size=size)
        return self._to_result(body)

    async def edit(
        self,
        *,
        prompt: str,
        size: str,
        quality: str,
        images: list[tuple[str, bytes, str]],
        input_fidelity: str | None = None,
    ) -> ImageResult:
        """Edit/re-frame one or more input images.
        """
        if not images:
            raise ValueError("edit requires at least one input image")

        field = "image" if len(images) == 1 else "image[]"
        files = [
            (field, (name, content, media_type)) for name, content, media_type in images
        ]

        def build(with_fidelity: bool) -> dict[str, str]:
            data: dict[str, str] = {
                "prompt": prompt,
                "n": "1",
                "size": size,
                "quality": self.capabilities.coerce_quality(quality),
                "output_format": "png",
            }
            if with_fidelity and input_fidelity and self._input_fidelity_supported:
                data["input_fidelity"] = input_fidelity
            return data

        try:
            body = await self._post_multipart(
                self._edits_url, data=build(True), files=files, size=size
            )
        except UnsupportedParameterError as exc:
            if exc.parameter != "input_fidelity":
                raise
            log.warning(
                "azure_image.dropping_unsupported_parameter",
                parameter=exc.parameter,
                model=self.model,
                detail="deployment rejected it despite the published capability table",
            )
            self._input_fidelity_supported = False
            body = await self._post_multipart(
                self._edits_url, data=build(False), files=files, size=size
            )

        return self._to_result(body)

    # -- transport ---------------------------------------------------------

    async def _post_json(self, url: str, payload: dict, *, size: str) -> dict:
        try:
            response = await self._client.post(
                url,
                json=payload,
                headers=self._headers,
                timeout=self._settings.image_timeout_s,
            )
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise self._map_image_error(exc, size=size) from exc
        except httpx.TimeoutException as exc:
            raise ProviderTimeoutError(
                f"image request exceeded IMAGE_TIMEOUT_S="
                f"{self._settings.image_timeout_s:.0f}s ({type(exc).__name__})"
            ) from exc
        except httpx.HTTPError as exc:
            raise TransientProviderError(f"image transport error: {exc}") from exc
        return response.json()

    async def _post_multipart(
        self, url: str, *, data: dict, files: list, size: str
    ) -> dict:
        try:
            response = await self._client.post(
                url,
                data=data,
                files=files,
                headers=self._headers,
                timeout=self._settings.image_timeout_s,
            )
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise self._map_image_error(exc, size=size) from exc
        except httpx.TimeoutException as exc:
            raise ProviderTimeoutError(
                f"image edit exceeded IMAGE_TIMEOUT_S="
                f"{self._settings.image_timeout_s:.0f}s ({type(exc).__name__})"
            ) from exc
        except httpx.HTTPError as exc:
            raise TransientProviderError(f"image edit transport error: {exc}") from exc
        return response.json()

    def _map_image_error(self, exc: httpx.HTTPStatusError, *, size: str) -> ProviderError:
        """Extend the shared HTTP mapping with image-specific 400 causes."""
        mapped = map_http_error(exc)
        status = exc.response.status_code

        if status == 404:
            return DeploymentNotFoundError(
                f"no deployment named {self._deployment!r} exists in this resource "
                f"(AZURE_OPENAI_IMAGE_DEPLOYMENT). Azure said: {str(mapped)[:200]}",
                status_code=404,
            )

        if status == 400:
            text = exc.response.text.lower()
            if match := _UNSUPPORTED_PARAM.search(text):
                return UnsupportedParameterError(
                    f"deployment {self.model!r} does not support {match.group(1)!r}",
                    parameter=match.group(1),
                )
            if any(marker in text for marker in _SIZE_REJECTION_MARKERS):
                return SizeRejectedError(
                    f"deployment rejected size {size!r}: {str(mapped)[:300]}",
                    status_code=400,
                )
        return mapped

    def _to_result(self, body: dict) -> ImageResult:
        data = body.get("data") or []
        if not data:
            raise MalformedOutputError("image response contained no data")

        b64 = data[0].get("b64_json")
        if not b64:
            raise MalformedOutputError(
                "image response had no b64_json (GPT-image models never return URLs)"
            )
        try:
            raw = base64.b64decode(b64, validate=True)
        except Exception as exc:
            raise MalformedOutputError(f"image payload was not valid base64: {exc}") from exc
        if not raw:
            raise MalformedOutputError("decoded image payload was empty")

        usage = body.get("usage") or {}
        input_details = usage.get("input_tokens_details") or {}
        return ImageResult(
            data=raw,
            media_type="image/png",
            model=self.model,
            input_tokens=usage.get("input_tokens") or input_details.get("image_tokens"),
            output_tokens=usage.get("output_tokens"),
        )
