"""A fake Jev that answers from a scripted scene, so tests cost nothing."""

from __future__ import annotations

from dataclasses import dataclass, field

from typesafe_sdk import BoxAnswer, BoxCoordinates, ChoiceAnswer, NoulAnswer, ScoreAnswer, SystemOneResponse, Usage


@dataclass
class Scene:
    present: dict[str, float] = field(default_factory=dict)  # perception key → presence
    boxes: dict[str, tuple[float, float, float, float]] = field(default_factory=dict)  # key → top,bottom,left,right
    risk: float = 0.0
    distance: str = "none"
    kind: str = "none"
    free_side: str = "both"
    ahead_clear: float = 0.9
    movement: str = "none"
    nav: dict[str, float] = field(default_factory=lambda: {"MOVE_FORWARD": 0.9, "SLOW_DOWN": 0.1})


def _choice(probs: dict[str, float]) -> ChoiceAnswer:
    best = max(probs, key=probs.get)
    return ChoiceAnswer(choice=best, confidence=probs[best], probabilities=probs)


class FakeJev:
    def __init__(self, scene: Scene) -> None:
        self.scene = scene
        self.calls: list[dict] = []

    async def ask(self, state, questions) -> SystemOneResponse:
        self.calls.append({"state_keys": sorted(state) if isinstance(state, dict) else None, "questions": sorted(questions)})
        s, out = self.scene, {}
        for q in questions:
            if q.endswith("__present"):
                out[q] = NoulAnswer(noul=s.present.get(q.removesuffix("__present"), 0.05))
            elif q.endswith("__box"):
                t, b, l, r = s.boxes.get(q.removesuffix("__box"), (0.4, 0.5, 0.4, 0.5))
                out[q] = BoxAnswer(box=BoxCoordinates(top=t, bottom=b, left=l, right=r), confidence=0.8)
            elif q == "risk":
                out[q] = ScoreAnswer(score=s.risk, confidence=0.8, legend={0: "a"}, probabilities={0: 1.0})
            elif q == "distance":
                out[q] = _choice({s.distance: 0.9})
            elif q == "kind":
                out[q] = _choice({s.kind: 0.9})
            elif q == "free_side":
                out[q] = _choice({s.free_side: 0.85})
            elif q == "ahead_clear":
                out[q] = NoulAnswer(noul=s.ahead_clear)
            elif q == "movement":
                out[q] = _choice({s.movement: 0.8})
            elif q == "action":
                out[q] = _choice(s.nav)
        return SystemOneResponse(model="jev-fake", usage=Usage(input_tokens=1000, output_tokens=0), answers=out)
