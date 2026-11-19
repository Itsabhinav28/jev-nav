"""HeyGen voice via OpenRouter's speech endpoint, with an on-disk cache and interruptible playback."""

from __future__ import annotations

import asyncio
import hashlib
import io
import threading
import wave
from pathlib import Path

import httpx
import numpy as np

from ..config import Settings


class Speaker:
    def __init__(self, settings: Settings, cache_dir: Path, play_audio: bool = True) -> None:
        settings.require("openrouter_api_key")
        self.s = settings
        self.cache_dir = cache_dir
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.play_audio = play_audio
        self._http = httpx.AsyncClient(
            base_url=settings.openrouter_base_url,
            headers={"Authorization": f"Bearer {settings.openrouter_api_key.get_secret_value()}"},
            timeout=20,
        )
        self._speaking = threading.Event()
        self._lock = asyncio.Lock()

    @property
    def speaking(self) -> bool:
        return self._speaking.is_set()

    async def synthesize(self, text: str) -> bytes:
        """Returns raw audio bytes (PCM16 mono by default, or WAV). Cached by text + voice + model."""
        key = hashlib.sha1(f"{self.s.tts_model}|{self.s.tts_voice}|{self.s.tts_format}|{text}".encode()).hexdigest()
        path = self.cache_dir / f"{key}.{self.s.tts_format}"
        if path.exists():
            return path.read_bytes()
        body: dict = {"model": self.s.tts_model, "input": text, "response_format": self.s.tts_format}
        if self.s.tts_voice:
            body["voice"] = self.s.tts_voice
        r = await self._http.post("/audio/speech", json=body)
        r.raise_for_status()
        path.write_bytes(r.content)
        return r.content

    async def warm(self, lines: list[str]) -> int:
        """Pre-voice the fixed alert lines so critical alerts play with no network wait."""
        results = await asyncio.gather(*(self.synthesize(t) for t in lines), return_exceptions=True)
        return sum(1 for r in results if not isinstance(r, BaseException))

    async def say(self, text: str, interrupt: bool = False) -> None:
        audio = await self.synthesize(text)
        if not self.play_audio:
            return
        if interrupt:
            self.stop()
        async with self._lock:
            await asyncio.to_thread(self._play_blocking, audio)

    def stop(self) -> None:
        if self.play_audio:
            import sounddevice as sd

            sd.stop()

    def _play_blocking(self, audio: bytes) -> None:
        import sounddevice as sd

        samples, rate = decode_audio(audio, self.s.tts_sample_rate)
        self._speaking.set()
        try:
            sd.play(samples, rate)
            sd.wait()
        finally:
            self._speaking.clear()

    async def aclose(self) -> None:
        await self._http.aclose()


def decode_audio(audio: bytes, default_rate: int) -> tuple[np.ndarray, int]:
    """WAV if it has a RIFF header, otherwise raw little-endian PCM16 mono."""
    if audio[:4] == b"RIFF":
        with wave.open(io.BytesIO(audio)) as w:
            rate, ch = w.getframerate(), w.getnchannels()
            data = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
            if ch > 1:
                data = data.reshape(-1, ch)
            return data, rate
    usable = len(audio) - (len(audio) % 2)
    return np.frombuffer(audio[:usable], dtype=np.int16), default_rate
