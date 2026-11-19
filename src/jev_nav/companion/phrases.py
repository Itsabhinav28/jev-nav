"""Fixed Hinglish lines for alerts. These skip the LLM: they are pre-voiced at startup and play instantly."""

from __future__ import annotations

from ..schemas import Action, NavDecision

HAZARD_WORDS = {
    "person": "koi aa raha hai",
    "moving_vehicle_or_cycle": "gaadi ya cycle hai",
    "fixed_obstacle": "rasta band hai",
    "ground_hazard": "zameen kharab hai",
    "overhead_hazard": "sar ke upar kuch hai",
    "cannot_see": "kuch dikh nahi raha",
    "stairs_down": "seedhiyan neeche hain",
    "stairs_up": "seedhiyan upar hain",
}

# One short line per action. Keys must stay stable: they are the TTS cache keys.
ACTION_LINES: dict[str, str] = {
    "STOP": "Ruko!",
    "STOP_cannot_see": "Ruko, kuch dikh nahi raha.",
    "STOP_overhead_hazard": "Ruko, sar ke upar dhyaan.",
    "STOP_ground_hazard": "Ruko, aage zameen kharab hai.",
    "STOP_AT_STAIRS_down": "Ruko, aage seedhiyan neeche hain.",
    "STOP_AT_STAIRS_up": "Ruko, aage seedhiyan upar hain.",
    "WAIT": "Ek second ruko, koi cross kar raha hai.",
    "SLOW_DOWN": "Dheere chalo.",
    "GO_LEFT": "Thoda left ho jao.",
    "GO_RIGHT": "Thoda right ho jao.",
    "MOVE_FORWARD": "Rasta saaf hai, chalo.",
    "CLIMB_UP": "Ek ek kadam upar.",
    "CLIMB_DOWN": "Dheere, ek ek kadam neeche.",
    "LANDING": "Seedhiyan khatam.",
}


def line_key(d: NavDecision) -> str:
    if d.action == Action.STOP_AT_STAIRS:
        return "STOP_AT_STAIRS_down" if (d.hazard_kind == "stairs_down" or "down" in " ".join(d.reasons)) else "STOP_AT_STAIRS_up"
    if d.action == Action.STOP and d.hazard_kind in ("cannot_see", "overhead_hazard", "ground_hazard"):
        return f"STOP_{d.hazard_kind}"
    return d.action.value


def line_for(d: NavDecision) -> str:
    return ACTION_LINES[line_key(d)]
