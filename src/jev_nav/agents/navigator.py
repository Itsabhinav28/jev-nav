from __future__ import annotations

from typing import Any

from typesafe_sdk import ChoiceAnswer, Image

from ..jev_client import JevAsker
from ..prompts import navigator_questions
from ..schemas import Action, NavigatorProposal


class NavigatorAgent:
    """Reads the frame plus the other members' typed findings and proposes an action with probabilities."""

    def __init__(self, jev: JevAsker, brief: str) -> None:
        self.jev, self.brief = jev, brief
        self.questions = navigator_questions()

    async def run(self, frame: Image, findings: dict[str, Any]) -> tuple[NavigatorProposal, int]:
        resp = await self.jev.ask({"mission": self.brief, "frame": frame, "findings": findings}, self.questions)
        ans = resp.answers["action"]
        assert isinstance(ans, ChoiceAnswer)
        probs = {Action(k): v for k, v in ans.probabilities.items() if k in Action.__members__}
        return (
            NavigatorProposal(action=Action(ans.choice), probabilities=probs, confidence=ans.confidence),
            resp.usage.input_tokens or 0,
        )
