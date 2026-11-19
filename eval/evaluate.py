"""
Score the council against hand-labelled frames (real Jev calls; costs tokens).

labels CSV (eval/labels/*.csv):  frame_path,expected_action
  eval/frames/stairs_01.jpg,STOP_AT_STAIRS
  eval/frames/corridor_03.jpg,MOVE_FORWARD

The headline number is the UNSAFE-GO RATE: how often the council chose a less safe action than the label.
Tune thresholds in .env until that is ~0, then improve accuracy.

  python eval/evaluate.py eval/labels/my_set.csv
"""

from __future__ import annotations

import asyncio
import csv
import sys
from collections import Counter

import cv2

from jev_nav.config import get_settings
from jev_nav.frames import Frame
from jev_nav.jev_client import JevClient
from jev_nav.orchestrator import Orchestrator
from jev_nav.schemas import SAFETY_RANK, Action


async def main(path: str) -> None:
    s = get_settings()
    s.full_council_every_n = 1  # score every frame with the full council
    jev = JevClient(s)
    rows = list(csv.DictReader(open(path, encoding="utf-8")))
    confusion: Counter[tuple[str, str]] = Counter()
    correct = unsafe = 0
    for i, row in enumerate(rows):
        img = cv2.imread(row["frame_path"])
        if img is None:
            print(f"skip unreadable {row['frame_path']}")
            continue
        orch = Orchestrator(jev, s)  # fresh state: each labelled frame is judged on its own
        got = (await orch.step(Frame(i, 0.0, img))).action
        want = Action(row["expected_action"])
        confusion[(want.value, got.value)] += 1
        correct += got == want
        unsafe += SAFETY_RANK[got] < SAFETY_RANK[want]
        print(f"{row['frame_path']}: want {want.value:<15} got {got.value}")
    n = max(1, len(rows))
    print(f"\naccuracy {correct / n:.1%}   unsafe-go rate {unsafe / n:.1%}   cost ≈ ${jev.cost_usd:.4f}")
    for (want, got), c in sorted(confusion.items()):
        if want != got:
            print(f"  {want} → {got}: {c}")
    await jev.aclose()


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1]))
