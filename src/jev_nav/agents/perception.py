from __future__ import annotations

from typesafe_sdk import BoxAnswer, Image, NoulAnswer

from ..jev_client import JevAsker
from ..prompts import PERCEPTION_TARGETS, perception_questions
from ..schemas import BBox, Detection, PerceptionFindings


class PerceptionAgent:
    """What is there and where. Presence (Noul) gates every Box: Jev always returns a box, even for nothing."""

    def __init__(self, jev: JevAsker, brief: str, presence_threshold: float) -> None:
        self.jev, self.brief, self.threshold = jev, brief, presence_threshold
        self.questions = perception_questions()

    async def run(self, frame: Image) -> tuple[PerceptionFindings, int]:
        resp = await self.jev.ask({"mission": self.brief, "frame": frame}, self.questions)
        found: dict[str, Detection] = {}
        for key in PERCEPTION_TARGETS:
            pres = resp.answers[f"{key}__present"]
            assert isinstance(pres, NoulAnswer)
            box_ans = resp.answers[f"{key}__box"]
            box = None
            if pres.noul >= self.threshold and isinstance(box_ans, BoxAnswer):
                b = box_ans.box
                box = BBox(top=b.top, bottom=b.bottom, left=b.left, right=b.right, confidence=box_ans.confidence)
            found[key] = Detection(label=key, presence=pres.noul, box=box)
        return PerceptionFindings(**found), resp.usage.input_tokens or 0
