"""
The arbiter: plain, predictable code that turns the council's findings into ONE action.

Order of authority (top wins):
  1. Can't trust the input  → STOP           (late decision, bad frame, council error)
  2. Stairs state machine   → STOP_AT_STAIRS / CLIMB_UP / CLIMB_DOWN
  3. Navigator proposal, picked by lowest expected cost (a wrong "go" costs far more than a wrong "stop")
  4. Safety vetoes          → can only make the action safer (time-to-contact, near hazards, crossing traffic…)
  5. Agreement checks       → a turn needs a clear side; keep-left when passing oncoming people
  6. Hysteresis             → get safer instantly, relax only after N agreeing frames
"""

from __future__ import annotations

from .config import Settings
from .schemas import (
    HALT_ACTIONS,
    SAFETY_RANK,
    Action,
    CouncilReport,
    Distance,
    EgoMotion,
    FrameQuality,
    NavDecision,
    StairsMode,
    TrackInfo,
    Urgency,
    UserActivity,
)

TURNS = {Action.GO_LEFT, Action.GO_RIGHT}
STAIR_EXIT_FRAMES = 3


def mistake_cost(chosen: Action, truth: Action) -> float:
    """Cost of doing `chosen` when `truth` was right. Under-reacting is 3× worse per safety level."""
    if chosen == truth:
        return 0.0
    if {chosen, truth} == TURNS:
        return 3.0  # steering into the wrong side
    rc, rt = SAFETY_RANK[chosen], SAFETY_RANK[truth]
    if rc < rt:
        return 3.0 * (rt - rc) + (6.0 if truth == Action.STOP_AT_STAIRS else 0.0)
    return 1.0 * max(1, rc - rt)


def least_risky(probabilities: dict[Action, float], allowed: set[Action]) -> tuple[Action, float]:
    best, best_cost = Action.STOP, float("inf")
    for cand in allowed:
        cost = sum(p * mistake_cost(cand, truth) for truth, p in probabilities.items())
        if cost < best_cost or (cost == best_cost and SAFETY_RANK[cand] > SAFETY_RANK[best]):
            best, best_cost = cand, cost
    return best, best_cost


class Arbiter:
    def __init__(self, settings: Settings) -> None:
        self.s = settings
        self.last: NavDecision | None = None
        self.stairs_mode = StairsMode.NORMAL
        self._stairs_dir: str | None = None
        self._stairs_absent = 0
        self._pending: Action | None = None
        self._pending_count = 0

    # ── public ───────────────────────────────────────────────────────────
    def decide(
        self,
        report: CouncilReport,
        quality: FrameQuality,
        ego: EgoMotion,
        tracks: dict[str, TrackInfo],
        stale: bool = False,
    ) -> NavDecision:
        reasons: list[str] = []
        critical = False
        hazard_kind = hazard_side = None
        distance = Distance.NONE
        ttc = _min_ttc(tracks)

        if stale:
            action, critical = Action.STOP, True
            reasons.append("decision arrived too late to be safe")
        elif not quality.ok:
            action, critical = Action.STOP, True
            reasons.append(f"cannot see clearly ({quality.reason})")
            hazard_kind = "cannot_see"
        elif report.perception is None or report.hazard is None or report.clearance is None:
            action, critical = Action.STOP, True
            reasons.append("council incomplete: " + "; ".join(report.errors or ["missing findings"]))
        else:
            action, critical, hazard_kind, hazard_side, distance = self._council_action(
                report, ego, tracks, ttc, reasons
            )

        action = self._hysteresis(action, reasons)
        urgency = _urgency(action, critical)
        conf = report.navigator.confidence if report.navigator else (report.hazard.risk_confidence if report.hazard else 0.0)
        decision = NavDecision(
            frame_index=report.frame_index,
            timestamp_s=report.timestamp_s,
            action=action,
            urgency=urgency,
            reasons=reasons,
            hazard_kind=hazard_kind,
            hazard_side=hazard_side,
            distance=distance,
            ttc_s=ttc,
            stairs_mode=self.stairs_mode,
            activity=ego.activity,
            confidence=conf,
            changed=self.last is None or self.last.action != action,
        )
        self.last = decision
        return decision

    # ── council path ─────────────────────────────────────────────────────
    def _council_action(self, r: CouncilReport, ego: EgoMotion, tracks: dict[str, TrackInfo], ttc, reasons):
        p, hz, cl, mv, nav = r.perception, r.hazard, r.clearance, r.motion, r.navigator
        assert p is not None and hz is not None and cl is not None
        thr = self.s.presence_threshold
        critical = False
        hazard_kind = hz.kind if hz.kind != "none" else None
        distance = hz.distance
        main_box = _main_box(p, thr)
        hazard_side = main_box.side() if main_box else None

        # 2. Stairs state machine
        stairs = self._stairs(p, ego, nav, reasons)
        if stairs is not None:
            action, critical = stairs
            if action in (Action.CLIMB_UP, Action.CLIMB_DOWN) and ttc is not None and ttc < self.s.ttc_stop_s:
                reasons.append(f"collision in {ttc:.1f}s while on stairs")
                return Action.STOP, True, hazard_kind, hazard_side, distance
            return action, critical, hazard_kind or "stairs", hazard_side, distance

        # 3. Proposal
        allowed = set(Action) - {Action.CLIMB_UP, Action.CLIMB_DOWN, Action.STOP_AT_STAIRS}
        if nav is not None and nav.probabilities:
            action, cost = least_risky(nav.probabilities, allowed)
            reasons.append(f"navigator: {nav.action.value} (p={nav.confidence:.2f}) → least-risk {action.value}")
            if nav.confidence < self.s.navigator_min_confidence:
                floor = Action.STOP if hz.risk >= 1.5 else Action.SLOW_DOWN
                action = _at_least(action, floor)
                reasons.append(f"navigator unsure → at least {floor.value}")
        else:
            action = self._rule_proposal(r, reasons)

        # 4. Safety vetoes (can only make it safer)
        crossing = p.crossing_object.presence >= thr or (mv is not None and mv.movement == "crossing")
        if ttc is not None and ttc < self.s.ttc_stop_s:
            action, critical = _at_least(action, Action.WAIT if crossing else Action.STOP), True
            reasons.append(f"contact in {ttc:.1f}s")
        elif ttc is not None and ttc < self.s.ttc_slow_s:
            action = _at_least(action, Action.SLOW_DOWN)
            reasons.append(f"closing in, contact in {ttc:.1f}s")
        if hz.risk >= 2.5 and hz.distance == Distance.NEAR:
            action, critical = _at_least(action, Action.STOP), True
            reasons.append(f"high risk ({hz.risk:.1f}) and near")
        elif hz.risk >= 2.0:
            action = _at_least(action, Action.SLOW_DOWN)
        if crossing and action not in HALT_ACTIONS:
            action = _at_least(action, Action.WAIT)
            reasons.append("something is crossing the path")
        for key, label in (("overhead_hazard", "head-height hazard"), ("ground_hazard", "ground hazard")):
            det = getattr(p, key)
            if det.presence >= thr and det.box is not None:
                band = det.box.distance_band()
                if band == Distance.NEAR:
                    action, critical = _at_least(action, Action.STOP), True
                    reasons.append(f"{label} right ahead")
                elif band == Distance.MID:
                    action = _at_least(action, Action.SLOW_DOWN)
                    reasons.append(f"{label} a few steps ahead")
        if action == Action.MOVE_FORWARD and cl.ahead_clear < 0.5:
            action = Action.SLOW_DOWN
            reasons.append("path ahead not confirmed clear")

        # 5. Agreement checks
        action = self._check_turn(action, cl.free_side, tracks, reasons)
        action = self._keep_side(action, p, mv, cl.free_side, tracks, reasons)
        return action, critical, hazard_kind, hazard_side, distance

    def _rule_proposal(self, r: CouncilReport, reasons: list[str]) -> Action:
        """Used on fast-lane frames, when the navigator did not run."""
        hz, cl = r.hazard, r.clearance
        assert hz is not None and cl is not None
        if hz.risk >= 2.5 and hz.distance == Distance.NEAR:
            a = Action.STOP
        elif hz.risk >= 1.5 and hz.distance in (Distance.NEAR, Distance.MID):
            if cl.free_side == "both":
                a = Action.GO_LEFT if self.s.traffic_side == "left" else Action.GO_RIGHT
            elif cl.free_side in ("left", "right"):
                a = Action.GO_LEFT if cl.free_side == "left" else Action.GO_RIGHT
            else:
                a = Action.STOP
        elif hz.risk >= 1.0:
            a = Action.SLOW_DOWN
        else:
            a = Action.MOVE_FORWARD
        reasons.append(f"fast-lane rule: risk {hz.risk:.1f} {hz.distance.value} → {a.value}")
        return a

    def _check_turn(self, action: Action, free_side: str, tracks, reasons) -> Action:
        if action not in TURNS:
            return action
        side = "left" if action == Action.GO_LEFT else "right"
        blocked_by_memory = any(t.remembered_side == side for t in tracks.values())
        if free_side in (side, "both") and not blocked_by_memory:
            return action
        other = "right" if side == "left" else "left"
        if free_side in (other, "both") and not any(t.remembered_side == other for t in tracks.values()):
            reasons.append(f"{side} not clear → going {other}")
            return Action.GO_RIGHT if other == "right" else Action.GO_LEFT
        reasons.append(f"no clear side to step around ({side} blocked)")
        return Action.STOP

    def _keep_side(self, action: Action, p, mv, free_side: str, tracks, reasons) -> Action:
        """Pass oncoming people on the configured side (keep left in India)."""
        oncoming = p.person_ahead.presence >= self.s.presence_threshold and mv is not None and mv.movement == "approaching"
        if not oncoming or action not in (Action.MOVE_FORWARD, Action.SLOW_DOWN, *TURNS):
            return action
        side = self.s.traffic_side
        want = Action.GO_LEFT if side == "left" else Action.GO_RIGHT
        if action != want and free_side in (side, "both") and not any(t.remembered_side == side for t in tracks.values()):
            reasons.append(f"person coming towards you → keep {side}")
            return want
        return action

    # ── stairs ───────────────────────────────────────────────────────────
    def _stairs(self, p, ego: EgoMotion, nav, reasons) -> tuple[Action, bool] | None:
        thr = self.s.presence_threshold
        down = p.stairs_down.presence >= thr and p.stairs_down.box is not None
        up = p.stairs_up.presence >= thr and p.stairs_up.box is not None
        close_down = down and p.stairs_down.box.distance_band() in (Distance.NEAR, Distance.MID)
        close_up = up and p.stairs_up.box.distance_band() in (Distance.NEAR, Distance.MID)
        seen = down or up

        if self.stairs_mode == StairsMode.NORMAL:
            if close_down or close_up:
                self.stairs_mode = StairsMode.AT_EDGE
                self._stairs_dir = "down" if close_down else "up"
                self._stairs_absent = 0
                reasons.append(f"stairs going {self._stairs_dir} ahead")
                return Action.STOP_AT_STAIRS, close_down  # down is critical, up is caution
            return None

        self._stairs_absent = 0 if seen else self._stairs_absent + 1
        if self._stairs_absent >= STAIR_EXIT_FRAMES:
            reasons.append("stairs finished (landing)")
            self.stairs_mode, self._stairs_dir = StairsMode.NORMAL, None
            return None

        if self.stairs_mode == StairsMode.AT_EDGE:
            nav_says_climb = nav is not None and nav.action in (Action.CLIMB_UP, Action.CLIMB_DOWN)
            # The walker decides to start: we see deliberate climbing motion, or they move on after stopping.
            if ego.activity == UserActivity.CLIMBING or (nav_says_climb and ego.activity == UserActivity.WALKING):
                self.stairs_mode = StairsMode.CLIMBING_DOWN if self._stairs_dir == "down" else StairsMode.CLIMBING_UP
            else:
                reasons.append(f"waiting at the {self._stairs_dir} stairs")
                return Action.STOP_AT_STAIRS, False

        reasons.append(f"on stairs ({self._stairs_dir})")
        return (Action.CLIMB_DOWN if self.stairs_mode == StairsMode.CLIMBING_DOWN else Action.CLIMB_UP), False

    # ── hysteresis ───────────────────────────────────────────────────────
    def _hysteresis(self, candidate: Action, reasons: list[str]) -> Action:
        if self.last is None:
            self._pending, self._pending_count = None, 0
            return candidate
        prev = self.last.action
        steer_now = candidate in TURNS and prev in (Action.MOVE_FORWARD, Action.SLOW_DOWN)  # sidestepping is reacting
        if candidate == prev or SAFETY_RANK[candidate] > SAFETY_RANK[prev] or steer_now or _is_stair_step(prev, candidate):
            self._pending, self._pending_count = None, 0
            return candidate
        # Relaxing (or switching sideways) needs N agreeing frames in a row.
        if candidate == self._pending:
            self._pending_count += 1
        else:
            self._pending, self._pending_count = candidate, 1
        if self._pending_count >= self.s.release_stop_frames:
            self._pending, self._pending_count = None, 0
            return candidate
        reasons.append(f"holding {prev.value} until {candidate.value} is confirmed")
        return prev


def _is_stair_step(prev: Action, cand: Action) -> bool:
    return prev == Action.STOP_AT_STAIRS and cand in (Action.CLIMB_UP, Action.CLIMB_DOWN) or (
        prev in (Action.CLIMB_UP, Action.CLIMB_DOWN) and cand == Action.MOVE_FORWARD
    )


def _at_least(action: Action, floor: Action) -> Action:
    """Make `action` at least as safe as `floor`. A sidestep already counts as reacting to a SLOW_DOWN-level risk."""
    if floor == Action.SLOW_DOWN and action in TURNS:
        return action
    return action if SAFETY_RANK[action] >= SAFETY_RANK[floor] else floor


def _min_ttc(tracks: dict[str, TrackInfo]) -> float | None:
    vals = [t.ttc_s for t in tracks.values() if t.ttc_s is not None]
    return min(vals) if vals else None


def _main_box(p, thr: float):
    for key in ("crossing_object", "person_ahead", "path_obstacle", "ground_hazard", "overhead_hazard"):
        det = getattr(p, key)
        if det.presence >= thr and det.box is not None:
            return det.box
    return None


def _urgency(action: Action, critical: bool) -> Urgency:
    if critical and action in HALT_ACTIONS:
        return Urgency.CRITICAL
    if action in (Action.MOVE_FORWARD, Action.CLIMB_UP, Action.CLIMB_DOWN):
        return Urgency.INFO
    return Urgency.CAUTION
