"""Typed models for dev tooling and E2E tests."""

from __future__ import annotations

from dataclasses import dataclass, field, replace


@dataclass(frozen=True)
class ServiceContext:
    service_id: str
    namespace: str
    base_url: str
    auth_token_url: str
    auth_client_id: str
    auth_host_header: str
    auth_client_secret: str

    def with_overrides(
        self,
        *,
        base_url: str = "",
        auth_token_url: str = "",
        auth_client_id: str = "",
        auth_host_header: str = "",
        auth_client_secret: str = "",
    ) -> ServiceContext:
        return replace(
            self,
            base_url=base_url or self.base_url,
            auth_token_url=auth_token_url or self.auth_token_url,
            auth_client_id=auth_client_id or self.auth_client_id,
            auth_host_header=auth_host_header or self.auth_host_header,
            auth_client_secret=auth_client_secret or self.auth_client_secret,
        )


@dataclass(frozen=True)
class E2EConfig:
    test_image_url: str = ""
    inference_timeout_s: float = 60.0
    expected_labels: tuple[str, ...] = field(default_factory=tuple)
