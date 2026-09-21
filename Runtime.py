"""Factory helpers shared by the CLI and web server."""

from __future__ import annotations

from Config import Settings
from Llm_client import MockLLMClient, OpenAICompatibleClient


def build_llm(settings: Settings):
    if settings.provider == "mock":
        return MockLLMClient()
    return OpenAICompatibleClient(
        model=settings.model,
        base_url=settings.base_url,
        api_key=settings.api_key,
        temperature=settings.temperature,
        max_tokens=settings.max_tokens,
        request_timeout_sec=settings.request_timeout_sec,
        enable_etc=settings.enable_etc,
        etc_first_diff_threshold=settings.etc_first_diff_threshold,
        etc_second_diff_threshold=settings.etc_second_diff_threshold,
        etc_window=settings.etc_window,
        check_server_ready=settings.provider == "local",
    )
