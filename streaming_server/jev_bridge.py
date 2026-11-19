"""Connects the phone stream to the Jev council.

Frames arrive from hooks.on_frame (a worker thread). The bridge runs the jev_nav Orchestrator on its own
asyncio loop in a background thread, one frame at a time: if Jev is still busy with the previous frame,
the new one is dropped, so the stream never lags behind the analysis.

The phone's motion sensors replace the camera-motion guess for "what is the walker doing", because a real
accelerometer/gyroscope is far more reliable than optical flow.
"""

from __future__ import annotations

import asyncio
import sys
import threading
import time
from collections import deque
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from jev_nav.config import Settings  # noqa: E402
from jev_nav.frames import EgoMotionEstimator, Frame  # noqa: E402
from jev_nav.jev_client import JevClient  # noqa: E402
from jev_nav.orchestrator import Orchestrator  # noqa: E402
from jev_nav.render import annotate  # noqa: E402
from jev_nav.schemas import EgoMotion, NavDecision, UserActivity  # noqa: E402

TURN_DPS = 45.0  # phone gyro rotation rate above which the walker is turning
STILL_STATES = {"still", "holding (steady)"}
MOVING_STATES = {"walking", "running"}


class SensorEgo:
    """Phone sensors when available, optical flow otherwise."""

    def __init__(self) -> None:
        self.flow = EgoMotionEstimator()
        self.sensors: dict | None = None

    def update(self, bgr: np.ndarray) -> EgoMotion:
        est = self.flow.update(bgr)  # keep its history warm even when sensors win
        s = self.sensors
        if not s or s.get("state") in (None, "unknown"):
            return est
        if (s.get("turn_dps") or 0) > TURN_DPS:
            act = UserActivity.TURNING
        elif s["state"] in STILL_STATES:
            act = UserActivity.STANDING
        elif s["state"] in MOVING_STATES:
            act = UserActivity.WALKING
        else:
            act = est.activity
        return est.model_copy(update={"activity": act})


class JevBridge:
    def __init__(self, voice: bool = False, mic: bool = False) -> None:
        self.settings = Settings(_env_file=ROOT / ".env")
        self.loop = asyncio.new_event_loop()
        threading.Thread(target=self.loop.run_forever, daemon=True, name="jev-loop").start()
        self.jev = JevClient(self.settings)
        self.orch = Orchestrator(self.jev, self.settings, live=True)
        self.ego = SensorEgo()
        self.orch.ego = self.ego
        self.busy = False
        self.index = 0
        self.t0 = time.time()
        self.latest: NavDecision | None = None
        self.latest_jpeg: bytes | None = None
        self.latest_latency = 0.0
        self.frames_analysed = 0
        self.frames_skipped = 0
        self.last_error: str | None = None
        self.voice = voice
        self.policy = self.speaker = self.companion = self.listener = None
        self.spoken: deque[dict] = deque(maxlen=8)  # newest last: {t, who, text}
        self.voice_error: str | None = None
        if voice or mic:
            from jev_nav.companion.nemotron import Companion
            from jev_nav.companion.phrases import lines
            from jev_nav.companion.speech_policy import SpeechPolicy
            from jev_nav.companion.tts import Speaker

            s = self.settings
            self.policy = SpeechPolicy(s.caution_cooldown_s, s.info_cooldown_s, s.companion_language)
            self.speaker = Speaker(s, ROOT / s.output_dir / "tts_cache")
            self.companion = Companion(s)
            asyncio.run_coroutine_threadsafe(self.speaker.warm(list(lines(s.companion_language).values())), self.loop)
        if mic:
            from jev_nav.companion.listener import Listener

            self.listener = Listener(lambda: self.speaker.speaking, self._heard)
            self.listener.start(self.loop)  # callbacks are delivered on the bridge loop
            print("[voice] microphone on: speak any time")

    # ── talking ──────────────────────────────────────────────────────────
    async def _say(self, text: str, who: str, critical: bool = False) -> None:
        self.spoken.append({"t": round(time.time() - self.t0, 1), "who": who, "text": text})
        print(f"[say:{who}] {text}")
        try:
            await self.speaker.say(text, interrupt=critical)
        except Exception as e:  # noqa: BLE001 — speech must never break navigation
            self.voice_error = f"speech: {type(e).__name__}: {e}"
            print("[voice error]", self.voice_error)

    async def _companion(self, **kw) -> str | None:
        try:
            reply = await self.companion.respond(self.latest, self.orch.last_jpeg, **kw)
            self.voice_error = None
            return reply
        except Exception as e:  # noqa: BLE001
            msg = str(e)
            if "402" in msg or "balance" in msg:
                self.voice_error = "Nemotron needs at least $0.50 OpenRouter credit to accept voice input. Typed questions still work."
            else:
                self.voice_error = f"Nemotron: {type(e).__name__}: {msg[:200]}"
            print("[voice error]", self.voice_error)
            return None

    def _heard(self, wav: bytes) -> None:
        """Mic utterance (runs on the bridge loop)."""
        self.spoken.append({"t": round(time.time() - self.t0, 1), "who": "you", "text": "(spoke)"})
        asyncio.ensure_future(self._answer(audio=wav))

    async def _answer(self, audio: bytes | None = None, text: str | None = None) -> None:
        reply = await self._companion(user_audio_wav=audio, user_text=text)
        if reply:
            await self._say(reply, "nemotron")

    def ask(self, text: str) -> None:
        """Typed question from the dashboard (any thread)."""
        self.spoken.append({"t": round(time.time() - self.t0, 1), "who": "you", "text": text})
        asyncio.run_coroutine_threadsafe(self._answer(text=text), self.loop)

    async def _proactive(self, decision: NavDecision) -> None:
        reply = await self._companion(proactive=True)
        if reply:
            await self._say(reply, "nemotron")

    @property
    def interval_s(self) -> float:
        return 1 / self.settings.sample_fps

    def submit(self, img: np.ndarray, ts: float, sensors: dict | None) -> None:
        """Called from the hook thread. Never blocks."""
        if self.busy:
            self.frames_skipped += 1
            return
        self.busy = True
        asyncio.run_coroutine_threadsafe(self._step(img.copy(), ts, sensors), self.loop)

    async def _step(self, img: np.ndarray, ts: float, sensors: dict | None) -> None:
        try:
            self.ego.sensors = sensors
            frame = Frame(self.index, ts - self.t0, img)
            self.index += 1
            started = time.monotonic()
            decision = await self.orch.step(frame)
            self.latest_latency = time.monotonic() - started
            self.latest = decision
            self.frames_analysed += 1
            self.last_error = None
            shown = annotate(img, decision, self.orch.last_report, self.settings.presence_threshold)
            h, w = shown.shape[:2]
            if w > 960:
                shown = cv2.resize(shown, (960, round(h * 960 / w)), interpolation=cv2.INTER_AREA)
            ok, buf = cv2.imencode(".jpg", shown, [cv2.IMWRITE_JPEG_QUALITY, 80])
            if ok:
                self.latest_jpeg = buf.tobytes()
            print(f"[jev] #{decision.frame_index:<4} {decision.action.value:<15} {decision.urgency.value:<8} "
                  f"{decision.activity.value:<9} {self.latest_latency:.2f}s  {'; '.join(decision.reasons)[-90:]}")
            if self.policy and self.speaker:
                talking = bool(self.listener and self.listener.user_talking)
                ev = self.policy.consider(decision, time.time() - self.t0, user_talking=talking)
                if ev and ev.channel.value == "fixed":
                    asyncio.ensure_future(self._say(ev.text, "alert", critical=ev.urgency.value == "critical"))
                elif ev:
                    asyncio.ensure_future(self._proactive(decision))
        except Exception as e:  # noqa: BLE001 — keep the stream alive, surface the error on the dashboard
            self.last_error = f"{type(e).__name__}: {e}"
            print("[jev error]", self.last_error)
        finally:
            self.busy = False

    def status(self) -> dict:
        d = self.latest
        return {
            "enabled": True,
            "voice": self.speaker is not None,
            "mic": self.listener is not None,
            "spoken": list(self.spoken),
            "voice_error": self.voice_error,
            "decision": d.model_dump(mode="json") if d else None,
            "latency_s": round(self.latest_latency, 2),
            "frames_analysed": self.frames_analysed,
            "frames_skipped": self.frames_skipped,
            "jev_calls": self.jev.calls,
            "cost_usd": round(self.jev.cost_usd, 4),
            "error": self.last_error,
        }


_bridge: JevBridge | None = None
_disabled_reason: str | None = None


def start(voice: bool = False, mic: bool = False) -> None:
    global _bridge, _disabled_reason
    try:
        _bridge = JevBridge(voice=voice, mic=mic)
    except Exception as e:  # noqa: BLE001 — e.g. missing keys: stream still works without Jev
        _disabled_reason = f"{type(e).__name__}: {e}"
        print("[jev] disabled:", _disabled_reason)


def get() -> JevBridge | None:
    return _bridge


def status() -> dict:
    return _bridge.status() if _bridge else {"enabled": False, "error": _disabled_reason}
