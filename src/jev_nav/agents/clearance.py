from __future__ import annotations

from typesafe_sdk import ChoiceAnswer, Image, NoulAnswer

from ..jev_client import JevAsker
from ..prompts import clearance_questions
from ..schemas import ClearanceFindings


class ClearanceAgent:
    """Where is there free, walkable space: ahead, left, right."""

    def __init__(self, jev: JevAsker, brief: str) -> None:
        self.jev, self.brief = jev, brief
        self.questions = clearance_questions()

    async def run(self, frame: Image) -> tuple[ClearanceFindings, int]:
        resp = await self.jev.ask({"mission": self.brief, "frame": frame}, self.questions)
        side, ahead = resp.answers["free_side"], resp.answers["ahead_clear"]
        assert isinstance(side, ChoiceAnswer) and isinstance(ahead, NoulAnswer)
        return (
            ClearanceFindings(
                free_side=side.choice, free_side_probabilities=side.probabilities, ahead_clear=ahead.noul
            ),
            resp.usage.input_tokens or 0,
        )
