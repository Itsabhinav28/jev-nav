"""Fixed alert lines per language. These skip the LLM: they are pre-voiced at startup and play instantly."""

from __future__ import annotations

from ..schemas import Action, NavDecision

# One short line per action. Keys must stay stable: they are the TTS cache keys.
LINES: dict[str, dict[str, str]] = {
    "english": {
        "STOP": "Stop!",
        "STOP_cannot_see": "Stop, I can't see clearly.",
        "STOP_overhead_hazard": "Stop, watch your head.",
        "STOP_ground_hazard": "Stop, uneven ground ahead.",
        "STOP_AT_STAIRS_down": "Stop, stairs going down ahead.",
        "STOP_AT_STAIRS_up": "Stop, stairs going up ahead.",
        "WAIT": "Wait, something is crossing.",
        "SLOW_DOWN": "Slow down.",
        "GO_LEFT": "Step a little left.",
        "GO_RIGHT": "Step a little right.",
        "MOVE_FORWARD": "Path is clear, go ahead.",
        "CLIMB_UP": "One step at a time, up.",
        "CLIMB_DOWN": "Carefully, one step at a time, down.",
        "LANDING": "Stairs done.",
        "UNSURE": "Stop, I'm not sure right now.",
    },
    "hinglish": {
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
        "UNSURE": "Ruko, main abhi sure nahi hoon.",
    },
}


def lines(language: str) -> dict[str, str]:
    return LINES.get(language, LINES["english"])


def line_key(d: NavDecision) -> str:
    if d.action == Action.STOP_AT_STAIRS:
        return "STOP_AT_STAIRS_down" if (d.hazard_kind == "stairs_down" or "down" in " ".join(d.reasons)) else "STOP_AT_STAIRS_up"
    if d.action == Action.STOP and d.hazard_kind in ("cannot_see", "overhead_hazard", "ground_hazard"):
        return f"STOP_{d.hazard_kind}"
    return d.action.value


def line_for(d: NavDecision, language: str) -> str:
    return lines(language)[line_key(d)]
