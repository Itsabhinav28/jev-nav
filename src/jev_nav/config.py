"""Typed settings loaded from .env. Secrets stay empty until the user fills them in."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Secrets
    typesafe_api_key: SecretStr = SecretStr("")
    openrouter_api_key: SecretStr = SecretStr("")

    # Jev
    jev_model: str = "jev-1.14.0"
    jev_timeout_s: float = 15.0
    jev_max_concurrency: int = 4
    jev_price_per_m_input_tokens: float = 0.042

    # Companion
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    companion_model: str = "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free"
    companion_language: str = "hinglish"

    # Speech
    tts_model: str = "heygen/heygen-voice-1-turbo:free"
    tts_voice: str = ""
    tts_format: str = "pcm"
    tts_sample_rate: int = 44100

    # Frames
    sample_fps: float = 2.0
    full_council_every_n: int = Field(3, ge=1)
    max_decision_latency_s: float = 2.5

    # Safety
    traffic_side: Literal["left", "right"] = "left"
    presence_threshold: float = 0.5
    navigator_min_confidence: float = 0.55
    ttc_stop_s: float = 1.5
    ttc_slow_s: float = 3.5
    release_stop_frames: int = Field(2, ge=1)

    # Talking policy
    caution_cooldown_s: float = 4.0
    info_cooldown_s: float = 12.0

    output_dir: Path = Path("runs")

    def require(self, *names: str) -> None:
        missing = [n for n in names if not getattr(self, n).get_secret_value()]
        if missing:
            raise RuntimeError(
                f"Missing secrets in .env: {', '.join(n.upper() for n in missing)}. Fill them in by hand."
            )


@lru_cache
def get_settings() -> Settings:
    return Settings()
