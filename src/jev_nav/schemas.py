"""Typed records passed between agents, the arbiter and the companion."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field


class Action(StrEnum):
    MOVE_FORWARD = "MOVE_FORWARD"
    SLOW_DOWN = "SLOW_DOWN"
    STOP = "STOP"
    WAIT = "WAIT"
    GO_LEFT = "GO_LEFT"
    GO_RIGHT = "GO_RIGHT"
    STOP_AT_STAIRS = "STOP_AT_STAIRS"
    CLIMB_UP = "CLIMB_UP"
    CLIMB_DOWN = "CLIMB_DOWN"


# Higher = safer. Escalating to a safer action happens immediately; relaxing needs persistence.
SAFETY_RANK: dict[Action, int] = {
    Action.MOVE_FORWARD: 0,
    Action.GO_LEFT: 1,
    Action.GO_RIGHT: 1,
    Action.CLIMB_UP: 2,
    Action.CLIMB_DOWN: 2,
    Action.SLOW_DOWN: 2,
    Action.WAIT: 3,
    Action.STOP: 4,
    Action.STOP_AT_STAIRS: 4,
}

HALT_ACTIONS = {Action.STOP, Action.WAIT, Action.STOP_AT_STAIRS}


class Urgency(StrEnum):
    INFO = "info"
    CAUTION = "caution"
    CRITICAL = "critical"


class Distance(StrEnum):
    NEAR = "near"  # roughly within 2 steps
    MID = "mid"  # roughly 2–6 steps
    FAR = "far"
    NONE = "none"


class UserActivity(StrEnum):
    STANDING = "standing"
    WALKING = "walking"
    TURNING = "turning"
    CLIMBING = "climbing"
    UNKNOWN = "unknown"


class StairsMode(StrEnum):
    NORMAL = "normal"
    AT_EDGE = "at_edge"
    CLIMBING_UP = "climbing_up"
    CLIMBING_DOWN = "climbing_down"


class BBox(BaseModel):
    """Fractions of frame height/width, origin top-left."""

    top: float
    bottom: float
    left: float
    right: float
    confidence: float | None = None

    @property
    def area(self) -> float:
        return max(0.0, self.bottom - self.top) * max(0.0, self.right - self.left)

    @property
    def center_x(self) -> float:
        return (self.left + self.right) / 2

    def distance_band(self) -> Distance:
        """FPV ground-plane heuristic: the lower an object's base sits in the frame, the closer it is."""
        if self.bottom >= 0.85:
            return Distance.NEAR
        if self.bottom >= 0.65:
            return Distance.MID
        return Distance.FAR

    def side(self) -> str:
        if self.center_x < 0.4:
            return "left"
        if self.center_x > 0.6:
            return "right"
        return "center"


# ─── Local (non-AI) signals ─────────────────────────────────────────────────


class FrameQuality(BaseModel):
    sharpness: float
    brightness: float
    ok: bool
    reason: str | None = None


class EgoMotion(BaseModel):
    magnitude: float  # mean optical-flow magnitude, px at 320 px width
    horizontal: float  # + = scene moves right (user turning left)
    vertical: float  # + = scene moves down
    activity: UserActivity


# ─── Agent findings (from Jev) ──────────────────────────────────────────────


class Detection(BaseModel):
    label: str
    presence: float
    box: BBox | None = None


class PerceptionFindings(BaseModel):
    person_ahead: Detection
    stairs_up: Detection
    stairs_down: Detection
    path_obstacle: Detection
    ground_hazard: Detection  # curb, hole, drop-off, wet floor, uneven ground
    overhead_hazard: Detection  # branch, sign, half-open shutter at head height
    crossing_object: Detection  # something moving across the path


class HazardFindings(BaseModel):
    risk: float  # 0 (none) .. 3 (immediate danger)
    risk_confidence: float
    distance: Distance
    kind: str
    kind_probabilities: dict[str, float] = Field(default_factory=dict)


class ClearanceFindings(BaseModel):
    free_side: str  # left | right | both | neither
    free_side_probabilities: dict[str, float] = Field(default_factory=dict)
    ahead_clear: float  # probability the straight path is walkable


class MotionFindings(BaseModel):
    movement: str  # approaching | moving_away | crossing | static | none
    probabilities: dict[str, float] = Field(default_factory=dict)
    confidence: float


class NavigatorProposal(BaseModel):
    action: Action
    probabilities: dict[Action, float]
    confidence: float


class TrackInfo(BaseModel):
    label: str
    ttc_s: float | None  # time to contact from box growth; None = not approaching
    remembered_side: str | None = None  # object left the frame recently on this side


class CouncilReport(BaseModel):
    frame_index: int
    timestamp_s: float
    perception: PerceptionFindings | None = None
    hazard: HazardFindings | None = None
    clearance: ClearanceFindings | None = None
    motion: MotionFindings | None = None
    navigator: NavigatorProposal | None = None
    full_council: bool = False
    latency_s: float = 0.0
    input_tokens: int = 0
    errors: list[str] = Field(default_factory=list)


class NavDecision(BaseModel):
    """The single source of truth published to the rest of the system."""

    frame_index: int
    timestamp_s: float
    action: Action
    urgency: Urgency
    reasons: list[str]
    hazard_kind: str | None = None
    hazard_side: str | None = None
    distance: Distance = Distance.NONE
    ttc_s: float | None = None
    stairs_mode: StairsMode = StairsMode.NORMAL
    activity: UserActivity = UserActivity.UNKNOWN
    confidence: float = 0.0
    changed: bool = False
