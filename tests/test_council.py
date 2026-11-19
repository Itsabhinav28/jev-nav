import numpy as np
import pytest

from jev_nav.arbiter import least_risky, mistake_cost
from jev_nav.config import Settings
from jev_nav.frames import Frame
from jev_nav.orchestrator import Orchestrator
from jev_nav.schemas import Action, StairsMode, Urgency

from .fakes import FakeJev, Scene

RNG = np.random.default_rng(0)


def settings(**kw) -> Settings:
    return Settings(_env_file=None, typesafe_api_key="x", openrouter_api_key="x", **kw)


def frame(i: int) -> Frame:
    return Frame(i, i * 0.5, RNG.integers(0, 255, (480, 640, 3), dtype=np.uint8))


async def run(scene: Scene, n: int = 1, **kw):
    orch = Orchestrator(FakeJev(scene), settings(**kw))
    return [await orch.step(frame(i)) for i in range(n)], orch


def test_wrong_go_costs_more_than_wrong_stop():
    assert mistake_cost(Action.MOVE_FORWARD, Action.STOP) > mistake_cost(Action.STOP, Action.MOVE_FORWARD)
    # Navigator leans "go" at 60/40, but the least-risk choice is still to stop.
    pick, _ = least_risky({Action.MOVE_FORWARD: 0.6, Action.STOP: 0.4}, set(Action))
    assert pick == Action.STOP


async def test_clear_path_moves_forward():
    (d,), _ = await run(Scene())
    assert d.action == Action.MOVE_FORWARD and d.urgency == Urgency.INFO


async def test_stairs_down_is_critical_stop():
    scene = Scene(present={"stairs_down": 0.9}, boxes={"stairs_down": (0.6, 0.8, 0.2, 0.8)}, risk=2.5, distance="mid", kind="stairs_down")
    (d,), orch = await run(scene)
    assert d.action == Action.STOP_AT_STAIRS and d.urgency == Urgency.CRITICAL
    assert orch.arbiter.stairs_mode == StairsMode.AT_EDGE


async def test_near_high_risk_stops():
    scene = Scene(present={"path_obstacle": 0.9}, boxes={"path_obstacle": (0.5, 0.95, 0.4, 0.6)}, risk=2.8, distance="near",
                  kind="fixed_obstacle", free_side="neither", ahead_clear=0.1, nav={"GO_LEFT": 0.7, "STOP": 0.3})
    (d,), _ = await run(scene)
    assert d.action == Action.STOP and d.urgency == Urgency.CRITICAL


async def test_turn_needs_a_clear_side():
    scene = Scene(present={"path_obstacle": 0.9}, boxes={"path_obstacle": (0.4, 0.7, 0.4, 0.6)}, risk=1.8, distance="mid",
                  kind="fixed_obstacle", free_side="right", ahead_clear=0.2, nav={"GO_LEFT": 0.9, "GO_RIGHT": 0.1})
    (d,), _ = await run(scene)
    assert d.action == Action.GO_RIGHT


async def test_oncoming_person_keep_left():
    scene = Scene(present={"person_ahead": 0.9}, boxes={"person_ahead": (0.3, 0.6, 0.45, 0.55)}, risk=1.0, distance="mid",
                  kind="person", free_side="both", movement="approaching", nav={"SLOW_DOWN": 0.8, "MOVE_FORWARD": 0.2})
    decisions, _ = await run(scene, n=2)  # motion agent needs a previous frame
    assert decisions[-1].action == Action.GO_LEFT


async def test_crossing_object_waits():
    scene = Scene(present={"crossing_object": 0.9}, boxes={"crossing_object": (0.3, 0.7, 0.3, 0.5)}, risk=1.5, distance="mid",
                  kind="moving_vehicle_or_cycle", movement="crossing", nav={"MOVE_FORWARD": 0.9, "WAIT": 0.1})
    (d,), _ = await run(scene)
    assert d.action == Action.WAIT


async def test_relaxing_from_stop_needs_agreeing_frames():
    scene = Scene(present={"path_obstacle": 0.9}, boxes={"path_obstacle": (0.5, 0.95, 0.4, 0.6)}, risk=2.8, distance="near",
                  free_side="neither", ahead_clear=0.1, nav={"STOP": 0.9, "MOVE_FORWARD": 0.1})
    fake = FakeJev(scene)
    orch = Orchestrator(fake, settings(release_stop_frames=2))
    assert (await orch.step(frame(0))).action == Action.STOP
    fake.scene = Scene()
    assert (await orch.step(frame(1))).action == Action.STOP  # held
    assert (await orch.step(frame(2))).action == Action.MOVE_FORWARD  # confirmed


async def test_unreadable_frame_stops_without_calling_jev():
    fake = FakeJev(Scene())
    orch = Orchestrator(fake, settings())
    d = await orch.step(Frame(0, 0.0, np.zeros((480, 640, 3), np.uint8)))
    assert d.action == Action.STOP and fake.calls == []


async def test_agent_failure_stops():
    class Broken(FakeJev):
        async def ask(self, state, questions):
            if any(q.endswith("__present") for q in questions):
                raise TimeoutError("jev slow")
            return await super().ask(state, questions)

    orch = Orchestrator(Broken(Scene()), settings())
    d = await orch.step(frame(0))
    assert d.action == Action.STOP and "perception" in d.reasons[0]


async def test_images_and_brief_are_sent_in_state():
    fake = FakeJev(Scene())
    orch = Orchestrator(fake, settings(full_council_every_n=1))
    await orch.step(frame(0))
    await orch.step(frame(1))
    keys = [c["state_keys"] for c in fake.calls]
    assert ["frame", "mission"] in keys
    assert ["frame", "mission", "previous_frame"] in keys  # motion agent
    assert ["findings", "frame", "mission"] in keys  # navigator


@pytest.mark.parametrize("n", [3])
async def test_fast_lane_skips_navigator(n):
    fake = FakeJev(Scene())
    orch = Orchestrator(fake, settings(full_council_every_n=n))
    for i in range(n):
        await orch.step(frame(i))
    nav_calls = sum(1 for c in fake.calls if c["questions"] == ["action"])
    assert nav_calls == 1


# ── fixes from the first real-video run ─────────────────────────────────────


async def test_clear_path_is_not_slowed_by_a_hedging_navigator():
    # Real corridor frame: far plant boxed off to the side, navigator split three ways.
    scene = Scene(present={"path_obstacle": 0.68}, boxes={"path_obstacle": (0.45, 0.53, 0.24, 0.34)}, risk=0.37,
                  distance="none", free_side="both", ahead_clear=0.92,
                  nav={"MOVE_FORWARD": 0.37, "SLOW_DOWN": 0.21, "GO_RIGHT": 0.32, "STOP": 0.1})
    (d,), _ = await run(scene)
    assert d.action == Action.MOVE_FORWARD and "agree the path is clear" in d.reasons[-1]


async def test_close_obstacle_in_corridor_is_not_overridden():
    scene = Scene(present={"path_obstacle": 0.8}, boxes={"path_obstacle": (0.5, 0.75, 0.4, 0.6)}, risk=0.8,
                  distance="mid", ahead_clear=0.85, nav={"SLOW_DOWN": 0.6, "MOVE_FORWARD": 0.4})
    (d,), _ = await run(scene)
    assert d.action == Action.SLOW_DOWN


async def test_smeared_frame_keeps_last_decision_then_stops_as_caution():
    fake = FakeJev(Scene())
    orch = Orchestrator(fake, settings(blur_hold_frames=2))
    assert (await orch.step(frame(0))).action == Action.MOVE_FORWARD
    smeared = np.full((480, 640, 3), 128, np.uint8)
    d1 = await orch.step(Frame(1, 0.5, smeared))
    d2 = await orch.step(Frame(2, 1.0, smeared))
    d3 = await orch.step(Frame(3, 1.5, smeared))
    assert d1.action == d2.action == Action.MOVE_FORWARD and "keeping last decision" in d1.reasons[0]
    assert d3.action == Action.STOP and d3.urgency == Urgency.CAUTION
    assert (await orch.step(frame(4))).action == Action.STOP  # relaxing still needs confirmation


async def test_stairs_climb_starts_after_walker_stops_then_moves():
    from jev_nav.schemas import EgoMotion, UserActivity

    scene = Scene(present={"stairs_down": 0.9}, boxes={"stairs_down": (0.6, 0.8, 0.2, 0.8)}, risk=2.5,
                  distance="mid", kind="stairs_down", nav={"STOP_AT_STAIRS": 0.9, "STOP": 0.1})
    orch = Orchestrator(FakeJev(scene), settings())
    acts = iter([UserActivity.WALKING, UserActivity.WALKING, UserActivity.STANDING, UserActivity.WALKING])
    orch.ego.update = lambda bgr: EgoMotion(magnitude=1, horizontal=0, vertical=0, activity=next(acts))
    seq = [(await orch.step(frame(i))).action for i in range(4)]
    assert seq == [Action.STOP_AT_STAIRS, Action.STOP_AT_STAIRS, Action.STOP_AT_STAIRS, Action.CLIMB_DOWN]
