from __future__ import annotations

from typesafe_sdk import ChoiceAnswer, Image, ScoreAnswer

from ..jev_client import JevAsker
from ..prompts import hazard_questions
from ..schemas import Distance, HazardFindings


class HazardAgent:
    """How dangerous is walking straight on, how close is it, and what kind of hazard."""

    def __init__(self, jev: JevAsker, brief: str) -> None:
        self.jev, self.brief = jev, brief
        self.questions = hazard_questions()

    async def run(self, frame: Image) -> tuple[HazardFindings, int]:
        resp = await self.jev.ask({"mission": self.brief, "frame": frame}, self.questions)
        risk, dist, kind = resp.answers["risk"], resp.answers["distance"], resp.answers["kind"]
        assert isinstance(risk, ScoreAnswer) and isinstance(dist, ChoiceAnswer) and isinstance(kind, ChoiceAnswer)
        return (
            HazardFindings(
                risk=risk.score,
                risk_confidence=risk.confidence,
                distance=Distance(dist.choice),
                kind=kind.choice,
                kind_probabilities=kind.probabilities,
            ),
            resp.usage.input_tokens or 0,
        )
