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
    def __init__(self, voice: bool = False) -> None:
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
        self.policy = self.speaker = None
        if voice:
            from jev_nav.companion.phrases import lines
            from jev_nav.companion.speech_policy import SpeechPolicy
            from jev_nav.companion.tts import Speaker

            s = self.settings
            self.policy = SpeechPolicy(s.caution_cooldown_s, s.info_cooldown_s, s.companion_language)
            self.speaker = Speaker(s, ROOT / s.output_dir / "tts_cache")
            asyncio.run_coroutine_threadsafe(self.speaker.warm(list(lines(s.companion_language).values())), self.loop)

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
                ev = self.policy.consider(decision, time.time() - self.t0)
                if ev and ev.text:
                    asyncio.ensure_future(self.speaker.say(ev.text, interrupt=ev.urgency.value == "critical"))
        except Exception as e:  # noqa: BLE001 — keep the stream alive, surface the error on the dashboard
            self.last_error = f"{type(e).__name__}: {e}"
            print("[jev error]", self.last_error)
        finally:
            self.busy = False

    def status(self) -> dict:
        d = self.latest
        return {
            "enabled": True,
            "voice": self.voice,
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


def start(voice: bool = False) -> None:
    global _bridge, _disabled_reason
    try:
        _bridge = JevBridge(voice=voice)
    except Exception as e:  # noqa: BLE001 — e.g. missing keys: stream still works without Jev
        _disabled_reason = f"{type(e).__name__}: {e}"
        print("[jev] disabled:", _disabled_reason)


def get() -> JevBridge | None:
    return _bridge


def status() -> dict:
    return _bridge.status() if _bridge else {"enabled": False, "error": _disabled_reason}
