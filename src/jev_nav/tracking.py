"""Time-to-contact from box growth, plus short object memory for things that left the frame."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field

from .schemas import BBox, PerceptionFindings, TrackInfo

TRACKED = ("person_ahead", "path_obstacle", "crossing_object", "stairs_down", "stairs_up")


@dataclass
class _Track:
    history: deque[tuple[float, float]] = field(default_factory=lambda: deque(maxlen=4))  # (t, area)
    last_box: BBox | None = None
    last_seen_t: float = -1.0


class Tracker:
    """
    For an object at distance Z, image area A ∝ 1/Z², so dA/dt / A = 2 / TTC  →  TTC = 2A / (dA/dt).
    No depth sensor needed: growth rate of Jev's box is enough to estimate seconds until contact.
    """

    def __init__(self, memory_s: float = 3.0) -> None:
        self.tracks: dict[str, _Track] = {k: _Track() for k in TRACKED}
        self.memory_s = memory_s

    def update(self, t: float, perception: PerceptionFindings) -> dict[str, TrackInfo]:
        out: dict[str, TrackInfo] = {}
        for key in TRACKED:
            det = getattr(perception, key)
            tr = self.tracks[key]
            if det.box is not None:
                tr.history.append((t, det.box.area))
                tr.last_box, tr.last_seen_t = det.box, t
                out[key] = TrackInfo(label=key, ttc_s=_ttc(tr.history))
            else:
                tr.history.clear()
                remembered = None
                if tr.last_box is not None and 0 < t - tr.last_seen_t <= self.memory_s:
                    side = tr.last_box.side()
                    # Only remember things that slipped out past the frame edge, near the walker.
                    if side != "center" and tr.last_box.distance_band().value in ("near", "mid"):
                        remembered = side
                out[key] = TrackInfo(label=key, ttc_s=None, remembered_side=remembered)
        return out


def _ttc(history: deque[tuple[float, float]]) -> float | None:
    if len(history) < 2:
        return None
    (t0, a0), (t1, a1) = history[0], history[-1]
    dt = t1 - t0
    if dt <= 0 or a0 <= 1e-4:
        return None
    growth = (a1 - a0) / dt
    if growth <= 0:
        return None  # same size or shrinking → not approaching
    return 2 * a1 / growth
