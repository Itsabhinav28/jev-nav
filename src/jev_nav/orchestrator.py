"""Runs one frame through the council: local checks → Jev agents in parallel → navigator → arbiter."""

from __future__ import annotations

import asyncio
import time
from typing import Any

from typesafe_sdk import Image

from .agents import ClearanceAgent, HazardAgent, MotionAgent, NavigatorAgent, PerceptionAgent
from .arbiter import Arbiter
from .config import Settings
from .frames import EgoMotionEstimator, Frame, assess_quality, prepare_for_jev
from .jev_client import JevAsker
from .prompts import brief
from .schemas import Action, CouncilReport, NavDecision
from .tracking import Tracker


class Orchestrator:
    """
    Two lanes keep it fast enough for walking speed:
      fast lane (every frame)          perception + hazard + clearance, in parallel
      full council (every Nth frame,   + motion (previous vs current frame) + navigator
      or whenever something is going on)
    """

    def __init__(self, jev: JevAsker, settings: Settings, live: bool = False) -> None:
        self.s, self.live = settings, live
        b = brief(settings.traffic_side)
        self.perception = PerceptionAgent(jev, b, settings.presence_threshold)
        self.hazard = HazardAgent(jev, b)
        self.clearance = ClearanceAgent(jev, b)
        self.motion = MotionAgent(jev, b)
        self.navigator = NavigatorAgent(jev, b)
        self.arbiter = Arbiter(settings)
        self.tracker = Tracker()
        self.ego = EgoMotionEstimator()
        self._prev_image: Image | None = None
        self.last_report: CouncilReport | None = None
        self.last_jpeg: bytes | None = None

    async def step(self, frame: Frame) -> NavDecision:
        started = time.monotonic()
        quality = assess_quality(frame.bgr)
        ego = self.ego.update(frame.bgr)
        report = CouncilReport(frame_index=frame.index, timestamp_s=frame.timestamp_s)

        if not quality.ok:  # don't spend Jev calls on a frame nobody can read
            self.last_report = report
            return self.arbiter.decide(report, quality, ego, {})

        jpeg = prepare_for_jev(frame.bgr)
        self.last_jpeg = jpeg
        image = Image(jpeg)
        full = self._want_full_council(frame.index)
        report.full_council = full

        tasks: dict[str, Any] = {
            "perception": self.perception.run(image),
            "hazard": self.hazard.run(image),
            "clearance": self.clearance.run(image),
        }
        if full and self._prev_image is not None:
            tasks["motion"] = self.motion.run(self._prev_image, image)
        results = await asyncio.gather(*tasks.values(), return_exceptions=True)
        for name, res in zip(tasks, results):
            if isinstance(res, BaseException):
                report.errors.append(f"{name}: {type(res).__name__}: {res}")
                continue
            findings, tokens = res
            setattr(report, name, findings)
            report.input_tokens += tokens

        tracks = self.tracker.update(frame.timestamp_s, report.perception) if report.perception else {}

        if full and report.perception and report.hazard and report.clearance:
            findings = {
                "perception": report.perception.model_dump(mode="json"),
                "hazard": report.hazard.model_dump(mode="json"),
                "clearance": report.clearance.model_dump(mode="json"),
                "motion": report.motion.model_dump(mode="json") if report.motion else None,
                "time_to_contact_s": {k: t.ttc_s for k, t in tracks.items() if t.ttc_s is not None},
                "recently_left_frame": {k: t.remembered_side for k, t in tracks.items() if t.remembered_side},
                "stairs_mode": self.arbiter.stairs_mode.value,
                "walker_activity": ego.activity.value,
            }
            try:
                report.navigator, tokens = await self.navigator.run(image, findings)
                report.input_tokens += tokens
            except Exception as e:  # noqa: BLE001 — the arbiter falls back to rules
                report.errors.append(f"navigator: {type(e).__name__}: {e}")

        self._prev_image = image
        report.latency_s = time.monotonic() - started
        stale = self.live and report.latency_s > self.s.max_decision_latency_s
        self.last_report = report
        return self.arbiter.decide(report, quality, ego, tracks, stale=stale)

    def _want_full_council(self, index: int) -> bool:
        last = self.arbiter.last
        something_going_on = last is not None and last.action != Action.MOVE_FORWARD
        return index % self.s.full_council_every_n == 0 or something_going_on
