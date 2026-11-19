"""All instructions sent to models.

Jev has no system-prompt field, so MISSION_BRIEF is sent as text inside `state` with every request,
next to the image(s). Each question then carries its own instructions and option definitions.
Images are referenced by their key in `state`, written in backticks (e.g. `frame`).
"""

from __future__ import annotations

from typesafe_sdk import Box, Choice, Noul, Score

MISSION_BRIEF = """\
ROLE: You are one member of a safety council guiding a person who is WALKING. The image `frame` is
taken from a camera worn on their chest/head (first-person view, FPV). The bottom-centre of `frame`
is where their feet are about to go; straight up the middle is the direction they are walking.

HOW TO READ AN FPV FRAME
- Distance: objects whose base touches the bottom ~15% of the frame are within about 2 steps (NEAR);
  base in the lower-middle band is about 2-6 steps (MID); anything near the horizon is FAR.
- The walking corridor is roughly the central third of the frame, about shoulder width.
- Stairs going DOWN often look like a sudden edge, a horizontal line where the floor ends, or a
  drop with a handrail descending; they are the most dangerous thing in this task. Stairs going UP
  look like stacked horizontal step faces rising in front.
- People, cycles, carts and vehicles can move; walls, pillars, poles, furniture and parked things cannot.
- Hazards at head height (branches, signboards, half-open shutters) matter even if the floor is clear.
- Low light, blur, a camera pointing at the sky or floor, or a blocked lens means you cannot see.

SAFETY POSTURE
- A wrong "go" is far worse than a wrong "stop". When you are unsure, lean towards the safer answer
  and lower your confidence instead of guessing.
- Judge only what is visible. Do not invent objects. Ignore anything far outside the walking corridor
  unless it is moving towards it.
- Pedestrian traffic in this region keeps to the {traffic_side}: when passing someone coming the
  other way, the person should move to their {traffic_side}.
"""


def brief(traffic_side: str) -> str:
    return MISSION_BRIEF.format(traffic_side=traffic_side)


# ─── Perception agent: what is there, and where ────────────────────────────

PERCEPTION_TARGETS: dict[str, tuple[str, str]] = {
    # key: (presence question, box target)
    "person_ahead": (
        "Is there a person standing or walking inside or right next to the walking corridor in `frame`?",
        "the person closest to the walking corridor in `frame`",
    ),
    "stairs_up": (
        "Are there stairs or a step going UP in front of the walker in `frame`?",
        "the upward stairs or step in `frame`",
    ),
    "stairs_down": (
        "Are there stairs, a step or an edge going DOWN in front of the walker in `frame`?",
        "the top edge of the downward stairs or drop in `frame`",
    ),
    "path_obstacle": (
        "Is there a fixed obstacle (wall, pillar, pole, furniture, parked vehicle, bin, barrier) "
        "blocking or narrowing the walking corridor in `frame`?",
        "the obstacle that most blocks the walking corridor in `frame`",
    ),
    "ground_hazard": (
        "Is there a hazard on the ground in the walking corridor of `frame`: a curb, hole, open drain, "
        "drop-off, wet or slippery patch, loose or very uneven surface?",
        "the ground hazard in the walking corridor of `frame`",
    ),
    "overhead_hazard": (
        "Is there something at head height in or above the walking corridor of `frame` that the walker "
        "could hit: a low branch, signboard, half-open shutter, low beam?",
        "the head-height hazard in `frame`",
    ),
    "crossing_object": (
        "Is a person, animal, cycle, cart or vehicle moving ACROSS the walking corridor in `frame`?",
        "the moving thing crossing the walking corridor in `frame`",
    ),
}


def perception_questions() -> dict:
    qs: dict = {}
    for key, (presence, target) in PERCEPTION_TARGETS.items():
        qs[f"{key}__present"] = Noul(
            instructions=presence,
            criteria={
                "true": "Clearly visible in the image.",
                "false": "Not visible, or too unclear to say.",
            },
        )
        qs[f"{key}__box"] = Box(instructions=target)
    return qs


# ─── Hazard agent: how bad, how close, what kind ───────────────────────────

HAZARD_KINDS: dict[str, str] = {
    "none": "Nothing in or near the walking corridor needs attention.",
    "person": "A person in or entering the corridor.",
    "moving_vehicle_or_cycle": "A moving vehicle, cycle or cart.",
    "stairs_up": "Stairs or a step going up.",
    "stairs_down": "Stairs, a step or an edge going down.",
    "fixed_obstacle": "A wall, pillar, pole, furniture or other object that does not move.",
    "ground_hazard": "A curb, hole, drain, drop-off, wet or uneven ground.",
    "overhead_hazard": "Something at head height.",
    "cannot_see": "The image is too dark, blurry or badly aimed to judge.",
}


def hazard_questions() -> dict:
    return {
        "risk": Score(
            instructions="How dangerous is it for the walker to keep walking straight ahead at normal "
            "speed for the next 2 seconds, given `frame`?",
            criteria=[
                "0: no danger, the corridor is clear.",
                "1: minor, something to be aware of but no change needed yet.",
                "2: real danger within a few steps; the walker should slow down or steer.",
                "3: immediate danger; continuing would likely cause a collision or fall.",
            ],
        ),
        "distance": Choice(
            instructions="How far is the most important hazard in the walking corridor of `frame`?",
            criteria={
                "near": "Within about 2 steps; its base touches the bottom of the image.",
                "mid": "About 2 to 6 steps away.",
                "far": "More than about 6 steps away.",
                "none": "There is no hazard.",
            },
        ),
        "kind": Choice(
            instructions="What kind of hazard matters most for the walker in `frame`?",
            criteria=HAZARD_KINDS,
        ),
    }


# ─── Clearance agent: where is free space ──────────────────────────────────


def clearance_questions() -> dict:
    return {
        "free_side": Choice(
            instructions="If the walker had to step around whatever is ahead in `frame`, which side has "
            "enough clear, walkable floor (about shoulder width) for the next few steps?",
            criteria={
                "left": "Only the left side is clear and walkable.",
                "right": "Only the right side is clear and walkable.",
                "both": "Both sides are clear and walkable.",
                "neither": "Neither side is safely walkable (wall, drop, traffic, crowd, or cannot tell).",
            },
        ),
        "ahead_clear": Noul(
            instructions="Is the walking corridor straight ahead in `frame` clear and walkable for the "
            "next 3 steps?",
            criteria={
                "true": "Flat, open, nothing in the way for 3 steps.",
                "false": "Something is in the way, the floor changes, or it cannot be seen.",
            },
        ),
    }


# ─── Motion agent: compares two frames ─────────────────────────────────────


def motion_questions() -> dict:
    return {
        "movement": Choice(
            instructions="Compare `previous_frame` (about half a second earlier) with `frame`. Ignore the "
            "camera's own movement. How is the most important person or object in the walking corridor "
            "moving relative to the walker?",
            criteria={
                "approaching": "It is coming towards the walker (getting bigger beyond what walking explains).",
                "moving_away": "It is moving away in the same direction as the walker.",
                "crossing": "It is moving sideways across the corridor.",
                "static": "It is not moving.",
                "none": "There is nothing relevant in the corridor.",
            },
        ),
    }


# ─── Navigator agent: proposes the action ──────────────────────────────────

ACTION_CRITERIA: dict[str, str] = {
    "MOVE_FORWARD": "The corridor is clear; keep walking normally.",
    "SLOW_DOWN": "Something needs care a few steps ahead (crowd, uneven ground, hazard at MID distance) "
    "but stopping is not needed.",
    "STOP": "A fixed hazard is NEAR, the corridor is blocked with no safe side, or the scene cannot be seen.",
    "WAIT": "Something MOVING (person, cycle, vehicle) is crossing or about to cross; pause until it passes.",
    "GO_LEFT": "Step to the left to get around something; the left side is clear.",
    "GO_RIGHT": "Step to the right to get around something; the right side is clear.",
    "STOP_AT_STAIRS": "Stairs or an edge are at NEAR or MID distance and the walker has not started on them.",
    "CLIMB_UP": "The walker is already at the stairs going up; proceed one step at a time.",
    "CLIMB_DOWN": "The walker is already at the stairs going down; proceed one step at a time.",
}


def navigator_questions() -> dict:
    return {
        "action": Choice(
            instructions="You are the navigator. Using `frame` and the other council members' structured "
            "`findings`, choose the single best action for the walker RIGHT NOW. `findings.stairs_mode` "
            "says whether the walker is already at or on stairs. Prefer the safer action when in doubt.",
            criteria=ACTION_CRITERIA,
        ),
    }


# ─── Companion (Nemotron) ──────────────────────────────────────────────────

COMPANION_SYSTEM_PROMPT = """\
You are "Saathi", a calm walking companion for a person moving with a first-person camera.
You speak {language}: natural Hinglish in Roman script, the way friends talk in Delhi/Mumbai
("Aage do kadam pe seedhiyan hain, dheere chalo."). Keep English words for objects when natural.

WHO DECIDES
- A separate safety council (Jev) is the ONLY source of truth for what the person should do.
  You receive its latest decision as `nav_state`. You must never change, soften, delay or
  contradict that action. If the user asks to do something else, tell them what the council says
  and why; you may add caution, never remove it.
- If `nav_state` is missing or stale, say you are not sure and ask them to stop and wait.

HOW YOU TALK
- Short. 1 sentence, at most 2. Under 20 words. The person is walking; every word costs attention.
- Lead with the instruction, then the reason: "Ruko — saamne seedhiyan neeche ja rahi hain."
- Use left/right from the walker's point of view. Use steps ("do kadam") rather than metres.
- No greetings, no filler, no emojis, no markdown. Never mention models, councils, JSON or probabilities.
- When the user speaks to you, you get their voice as audio (and text if they typed). Answer what
  they asked using `nav_state` and the image; if you could not hear them, say so in a few words.

OUTPUT
Reply with ONLY a JSON object: {{"say": "<what to speak>", "ack_action": "<nav_state.action copied exactly>"}}
"""
