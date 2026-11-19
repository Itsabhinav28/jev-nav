"""
When to open our mouth. The default is silence: the walker is busy, every word costs attention.

  critical  → always, immediately, fixed line (no LLM)
  caution   → only when the action changes, with a cooldown; fixed line
  info      → only when the walker is standing still and something new is worth saying; Nemotron phrases it
  on stairs / while turning → critical only
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from ..schemas import Action, NavDecision, StairsMode, Urgency, UserActivity
from .phrases import ACTION_LINES, line_for, line_key


class Channel(StrEnum):
    FIXED = "fixed"  # pre-voiced line, instant
    COMPANION = "companion"  # Nemotron writes it, HeyGen voices it


@dataclass
class SpeechEvent:
    channel: Channel
    urgency: Urgency
    text: str
    cache_key: str | None
    decision: NavDecision


class SpeechPolicy:
    def __init__(self, caution_cooldown_s: float, info_cooldown_s: float) -> None:
        self.caution_cd, self.info_cd = caution_cooldown_s, info_cooldown_s
        self._last_spoken_t: dict[Urgency, float] = {}
        self._last_key: str | None = None
        self._last_info_scene: tuple | None = None

    def consider(self, d: NavDecision, now: float, user_talking: bool = False) -> SpeechEvent | None:
        key = line_key(d)
        busy = d.stairs_mode in (StairsMode.CLIMBING_UP, StairsMode.CLIMBING_DOWN) or d.activity == UserActivity.TURNING

        if d.urgency == Urgency.CRITICAL:
            # Re-announce a critical only if it's a new one, or it has persisted past the cooldown.
            if key != self._last_key or self._elapsed(Urgency.CRITICAL, now) >= self.caution_cd:
                return self._emit(SpeechEvent(Channel.FIXED, d.urgency, line_for(d), key, d), now)
            return None

        if busy or user_talking:
            return None

        if d.urgency == Urgency.CAUTION:
            if d.changed and key != self._last_key and self._elapsed(Urgency.CAUTION, now) >= self.caution_cd:
                return self._emit(SpeechEvent(Channel.FIXED, d.urgency, line_for(d), key, d), now)
            return None

        # INFO: the all-clear after a halt is worth one short line; otherwise only when standing still.
        if d.changed and self._last_key in (
            Action.STOP.value, Action.WAIT.value, "STOP_AT_STAIRS_down", "STOP_AT_STAIRS_up", Action.CLIMB_DOWN.value, Action.CLIMB_UP.value,
        ):
            text_key = "LANDING" if self._last_key in (Action.CLIMB_DOWN.value, Action.CLIMB_UP.value) else key
            return self._emit(SpeechEvent(Channel.FIXED, d.urgency, ACTION_LINES[text_key], text_key, d), now)

        scene = (d.hazard_kind, d.distance.value)
        if (
            d.activity == UserActivity.STANDING
            and d.hazard_kind
            and scene != self._last_info_scene
            and self._elapsed(Urgency.INFO, now) >= self.info_cd
        ):
            self._last_info_scene = scene
            return self._emit(SpeechEvent(Channel.COMPANION, d.urgency, "", None, d), now)
        return None

    def _elapsed(self, u: Urgency, now: float) -> float:
        return now - self._last_spoken_t.get(u, float("-inf"))

    def _emit(self, ev: SpeechEvent, now: float) -> SpeechEvent:
        self._last_spoken_t[ev.urgency] = now
        if ev.cache_key:
            self._last_key = ev.cache_key
        return ev
