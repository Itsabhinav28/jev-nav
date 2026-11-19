"""Thin async wrapper around TypeSafe's SDK: image opt-in header, concurrency cap, usage + cost tracking."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Any, Protocol

from typesafe_sdk import AsyncTypeSafeClient, Image, RetryPolicy, SystemOneResponse

from .config import Settings

IMAGE_OPT_IN_HEADER = {"TypeSafe-Danger-Zone-Images": "true"}


class JevAsker(Protocol):
    """Anything that can answer a Jev System One request. The real client and test fakes both fit."""

    async def ask(self, state: Any, questions: Mapping[str, Any]) -> SystemOneResponse: ...


class JevClient:
    def __init__(self, settings: Settings) -> None:
        settings.require("typesafe_api_key")
        self._settings = settings
        self._client = AsyncTypeSafeClient(
            api_key=settings.typesafe_api_key.get_secret_value(),
            model=settings.jev_model,
            timeout=settings.jev_timeout_s,
            headers=IMAGE_OPT_IN_HEADER,  # required on every image/Box request, retries included
            retry=RetryPolicy(max_retries=2, timeout=settings.jev_timeout_s),
        )
        self._sem = asyncio.Semaphore(settings.jev_max_concurrency)
        self.input_tokens = 0
        self.calls = 0

    async def ask(self, state: Any, questions: Mapping[str, Any]) -> SystemOneResponse:
        async with self._sem:
            resp = await self._client.system_one(
                state=state, questions=questions, extra_headers=IMAGE_OPT_IN_HEADER
            )
        self.calls += 1
        self.input_tokens += resp.usage.input_tokens or 0
        return resp

    @property
    def cost_usd(self) -> float:
        return self.input_tokens / 1_000_000 * self._settings.jev_price_per_m_input_tokens

    async def aclose(self) -> None:
        await self._client.aclose()


def jev_image(jpeg_bytes: bytes) -> Image:
    return Image(jpeg_bytes)
