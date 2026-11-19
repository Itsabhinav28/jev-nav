"""HeyGen voice via OpenRouter's speech endpoint, with an on-disk cache and interruptible playback."""

from __future__ import annotations

import asyncio
import hashlib
import io
import queue
import threading
import wave
from pathlib import Path

import httpx
import numpy as np

from ..config import Settings

# HeyGen voices on OpenRouter: nadine, tabitha, bram, gareth, noelle, meredith, sabrina, lucian, weston, vincent.
DEFAULT_VOICE = "nadine"


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
        # All audio-device calls happen on ONE thread. PortAudio/CoreAudio crash the process (SIGTRAP)
        # when play and stop are called from different threads at the same time.
        self._queue: queue.Queue[tuple[np.ndarray, int, threading.Event] | None] = queue.Queue()
        self._interrupt = threading.Event()
        self._player: threading.Thread | None = None

    @property
    def speaking(self) -> bool:
        return self._speaking.is_set()

    async def synthesize(self, text: str) -> bytes:
        """Returns raw audio bytes (PCM16 mono by default, or WAV). Cached by text + voice + model."""
        voice = self.s.tts_voice or DEFAULT_VOICE  # HeyGen rejects requests without a voice
        key = hashlib.sha1(f"{self.s.tts_model}|{voice}|{self.s.tts_format}|{text}".encode()).hexdigest()
        path = self.cache_dir / f"{key}.{self.s.tts_format}"
        if path.exists():
            return path.read_bytes()
        body: dict = {"model": self.s.tts_model, "input": text, "voice": voice, "response_format": self.s.tts_format}
        r = await self._http.post("/audio/speech", json=body)
        r.raise_for_status()
        path.write_bytes(r.content)
        return r.content

    async def warm(self, lines: list[str]) -> int:
        """Pre-voice the fixed alert lines so critical alerts play with no network wait."""
        results = await asyncio.gather(*(self.synthesize(t) for t in lines), return_exceptions=True)
        return sum(1 for r in results if not isinstance(r, BaseException))

    async def say(self, text: str, interrupt: bool = False) -> None:
        """Queue a line and wait until it has played (or was cut off). `interrupt` drops whatever is playing or queued."""
        audio = await self.synthesize(text)
        if not self.play_audio:
            return
        samples, rate = decode_audio(audio, self.s.tts_sample_rate)
        if interrupt:
            self.stop()
        self._ensure_player()
        done = threading.Event()
        self._queue.put((samples, rate, done))
        await asyncio.to_thread(done.wait, 30)

    def stop(self) -> None:
        """Cut off the current line and drop queued ones. Never touches the audio device directly."""
        while True:
            try:
                item = self._queue.get_nowait()
            except queue.Empty:
                break
            if item is not None:
                item[2].set()
        self._interrupt.set()

    def _ensure_player(self) -> None:
        if self._player is None or not self._player.is_alive():
            self._player = threading.Thread(target=self._play_loop, daemon=True, name="speaker")
            self._player.start()

    def _play_loop(self) -> None:
        import sounddevice as sd

        chunk_s = 0.05  # how quickly an interrupt takes effect
        while True:
            item = self._queue.get()
            if item is None:
                return
            samples, rate, done = item
            self._interrupt.clear()
            self._speaking.set()
            try:
                channels = 1 if samples.ndim == 1 else samples.shape[1]
                step = int(rate * chunk_s)
                with sd.OutputStream(samplerate=rate, channels=channels, dtype="int16") as out:
                    for i in range(0, len(samples), step):
                        if self._interrupt.is_set():
                            break
                        out.write(np.ascontiguousarray(samples[i : i + step]))
            except Exception as e:  # noqa: BLE001 — a broken audio device must not kill guidance
                print("[speaker] playback failed:", e)
            finally:
                self._speaking.clear()
                done.set()

    async def aclose(self) -> None:
        self.stop()
        self._queue.put(None)
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
