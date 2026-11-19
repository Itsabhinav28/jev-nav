"""Always-on microphone with a simple energy voice detector. Muted while we are speaking (no self-echo)."""

from __future__ import annotations

import asyncio
import io
import wave
from collections import deque
from collections.abc import Callable

import numpy as np

RATE = 16_000
BLOCK_S = 0.03


class VoiceDetector:
    """Adaptive noise floor; an utterance starts above floor×ratio and ends after `hangover_s` of quiet."""

    def __init__(self, ratio: float = 3.0, min_speech_s: float = 0.4, hangover_s: float = 0.8, max_s: float = 12.0) -> None:
        self.ratio, self.min_speech_s, self.hangover_s, self.max_s = ratio, min_speech_s, hangover_s, max_s
        self.floor = 200.0
        self._buf: list[np.ndarray] = []
        self._pre: deque[np.ndarray] = deque(maxlen=int(0.3 / BLOCK_S))  # keep the first syllable
        self._voiced_s = 0.0
        self._quiet_s = 0.0
        self.active = False

    def feed(self, block: np.ndarray) -> bytes | None:
        rms = float(np.sqrt(np.mean(block.astype(np.float32) ** 2)) + 1e-6)
        loud = rms > self.floor * self.ratio
        if not self.active:
            self.floor = 0.95 * self.floor + 0.05 * rms if not loud else self.floor
            self._pre.append(block)
            if loud:
                self.active, self._buf, self._voiced_s, self._quiet_s = True, list(self._pre), 0.0, 0.0
            return None
        self._buf.append(block)
        self._voiced_s += BLOCK_S if loud else 0.0
        self._quiet_s = 0.0 if loud else self._quiet_s + BLOCK_S
        total = len(self._buf) * BLOCK_S
        if self._quiet_s >= self.hangover_s or total >= self.max_s:
            self.active = False
            pcm, self._buf = np.concatenate(self._buf), []
            self._pre.clear()
            return to_wav(pcm) if self._voiced_s >= self.min_speech_s else None
        return None


def to_wav(pcm: np.ndarray, rate: int = RATE) -> bytes:
    out = io.BytesIO()
    with wave.open(out, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm.astype(np.int16).tobytes())
    return out.getvalue()


class Listener:
    def __init__(self, is_speaking: Callable[[], bool], on_utterance: Callable[[bytes], None]) -> None:
        self.is_speaking, self.on_utterance = is_speaking, on_utterance
        self.vad = VoiceDetector()
        self._stream = None

    def start(self, loop: asyncio.AbstractEventLoop) -> None:
        import sounddevice as sd

        def callback(indata, frames, time_info, status) -> None:  # runs on the audio thread
            if self.is_speaking():
                return  # half-duplex: ignore our own voice
            wav = self.vad.feed(indata[:, 0].copy())
            if wav:
                loop.call_soon_threadsafe(self.on_utterance, wav)

        self._stream = sd.InputStream(samplerate=RATE, channels=1, dtype="int16", blocksize=int(RATE * BLOCK_S), callback=callback)
        self._stream.start()

    @property
    def user_talking(self) -> bool:
        return self.vad.active

    def stop(self) -> None:
        if self._stream is not None:
            self._stream.stop()
            self._stream.close()
