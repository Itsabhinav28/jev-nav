from __future__ import annotations

from typesafe_sdk import ChoiceAnswer, Image

from ..jev_client import JevAsker
from ..prompts import motion_questions
from ..schemas import MotionFindings


class MotionAgent:
    """Compares the previous and current frame: is the thing ahead approaching, leaving, crossing or still."""

    def __init__(self, jev: JevAsker, brief: str) -> None:
        self.jev, self.brief = jev, brief
        self.questions = motion_questions()

    async def run(self, previous: Image, frame: Image) -> tuple[MotionFindings, int]:
        resp = await self.jev.ask(
            {"mission": self.brief, "previous_frame": previous, "frame": frame}, self.questions
        )
        mv = resp.answers["movement"]
        assert isinstance(mv, ChoiceAnswer)
        return (
            MotionFindings(movement=mv.choice, probabilities=mv.probabilities, confidence=mv.confidence),
            resp.usage.input_tokens or 0,
        )
