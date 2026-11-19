"""Nemotron Omni via OpenRouter: hears the user (audio + optional text), sees the frame, reads Jev's decision."""

from __future__ import annotations

import base64
import json
import re

from openai import AsyncOpenAI
from pydantic import BaseModel, ValidationError

from ..config import Settings
from ..schemas import HALT_ACTIONS, NavDecision
from ..prompts import COMPANION_SYSTEM_PROMPT
from .phrases import line_for

# Words that would tell a halted walker to move. If Nemotron says these during a halt, we drop its reply.
GO_WORDS = re.compile(r"\b(chalo|chal do|aage badho|badhte raho|go ahead|move forward|keep going|walk on)\b", re.I)


class CompanionReply(BaseModel):
    say: str
    ack_action: str


class Companion:
    def __init__(self, settings: Settings) -> None:
        settings.require("openrouter_api_key")
        self.s = settings
        self._client = AsyncOpenAI(
            api_key=settings.openrouter_api_key.get_secret_value(), base_url=settings.openrouter_base_url
        )
        self._system = COMPANION_SYSTEM_PROMPT.format(language=settings.companion_language)
        self.history: list[dict] = []  # last few spoken exchanges, text only

    async def respond(
        self,
        decision: NavDecision | None,
        frame_jpeg: bytes | None = None,
        user_audio_wav: bytes | None = None,
        user_text: str | None = None,
        proactive: bool = False,
    ) -> str:
        nav_state = decision.model_dump(mode="json") if decision else None
        parts: list[dict] = [{"type": "text", "text": "nav_state = " + json.dumps(nav_state, ensure_ascii=False)}]
        if proactive:
            parts.append({"type": "text", "text": "The walker is standing still. In one short line, tell them what matters ahead, consistent with nav_state."})
        if user_text:
            parts.append({"type": "text", "text": f"User typed: {user_text}"})
        if user_audio_wav:
            parts.append({"type": "text", "text": "User said (audio):"})
            parts.append({"type": "input_audio", "input_audio": {"data": base64.b64encode(user_audio_wav).decode(), "format": "wav"}})
        if frame_jpeg:
            parts.append({"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + base64.b64encode(frame_jpeg).decode()}})

        messages = [{"role": "system", "content": self._system}, *self.history[-6:], {"role": "user", "content": parts}]
        resp = await self._client.chat.completions.create(
            model=self.s.companion_model,
            messages=messages,
            max_tokens=600,  # reasoning model: leave room for thinking before the short JSON answer
            extra_body={"reasoning": {"effort": "low", "exclude": True}},
        )
        raw = resp.choices[0].message.content or ""
        say = self._validate(raw, decision)
        if user_text or user_audio_wav:
            self.history.append({"role": "user", "content": user_text or "(spoke)"})
        self.history.append({"role": "assistant", "content": say})
        return say

    def _validate(self, raw: str, decision: NavDecision | None) -> str:
        """Jev is the source of truth: a reply that names a different action, or says 'go' during a halt, is dropped."""
        fallback = line_for(decision) if decision else "Ruko, main abhi sure nahi hoon."
        match = re.search(r"\{.*\}", raw, re.S)
        if not match:
            return fallback
        try:
            reply = CompanionReply.model_validate_json(match.group(0))
        except ValidationError:
            return fallback
        if decision is not None:
            if reply.ack_action != decision.action.value:
                return fallback
            if decision.action in HALT_ACTIONS and GO_WORDS.search(reply.say):
                return fallback
        return reply.say.strip()[:300] or fallback

    async def aclose(self) -> None:
        await self._client.close()
