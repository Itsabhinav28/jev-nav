# Jev Nav

Spoken guidance for someone walking with a first-person (FPV) camera: **stop, wait, slow down, step left or right, or handle stairs.**

- **Jev decides.** A council of 5 agents asks TypeSafe's Jev model typed questions about each frame. Plain Python code (the arbiter) turns their answers into one action. This is the only source of truth.
- **Nemotron talks.** NVIDIA Nemotron 3 Nano Omni (via OpenRouter) hears you (always-on mic), sees the frame, reads Jev's decision and answers in Hinglish. It can explain the decision but never change it.
- **HeyGen speaks.** Alerts are pre-recorded at startup, so "Ruko!" plays instantly. Replies are voiced on demand.

```
camera ─► local checks (blur, light, body motion — OpenCV, no AI)
       ─► Jev council:  Perception · Hazard · Clearance   (every frame, in parallel)
                        + Motion (prev vs current) + Navigator  (every Nth frame, or when anything is going on)
       ─► Arbiter: least-risk pick → safety vetoes → clear-side check / keep-left → hysteresis
       ─► NavDecision  ──► when-to-speak filter ──► fixed Hinglish line (instant) or Nemotron line ──► HeyGen
mic ─► voice detector ─► Nemotron (your audio + typed text + frame + NavDecision) ─► text + HeyGen
```

## Setup

```bash
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
```

Fill in the two secrets in `.env` by hand: `TYPESAFE_API_KEY` (console.typesafe.ai) and `OPENROUTER_API_KEY`. Then:

```bash
.venv/bin/jev-nav check
```

`check` makes one Jev image + Box call, one HeyGen call, and one Nemotron call that listens to that HeyGen audio. That last call proves audio input works end to end.

## Use

```bash
.venv/bin/jev-nav analyze path/to/fpv.mp4        # offline → runs/<time>/report.html, annotated.mp4, decisions.jsonl
.venv/bin/jev-nav analyze path/to/frames_folder/
.venv/bin/jev-nav live                           # webcam 0, always-on mic, voice on
.venv/bin/jev-nav live --camera path/to/fpv.mp4  # replay a video at real speed, with voice
.venv/bin/pytest                                 # 23 tests, fake Jev, no cost
python eval/evaluate.py eval/labels/my_set.csv   # score against your own labelled frames
```

## Actions

| Action | When |
|---|---|
| `MOVE_FORWARD` | Path is clear |
| `SLOW_DOWN` | Something needs care a few steps ahead |
| `STOP` | A fixed hazard is near, no side is clear, the camera can't see, or a decision arrived late |
| `WAIT` | Something moving is crossing; it will clear |
| `GO_LEFT` / `GO_RIGHT` | Step around; only if that side is clear. People coming towards you are passed on the left (`TRAFFIC_SIDE`) |
| `STOP_AT_STAIRS` | Always first when stairs appear (going down is critical) |
| `CLIMB_UP` / `CLIMB_DOWN` | You've started on the stairs; quiet except critical alerts until the landing |

## Safety design

- **Wrong "go" costs 3× more than wrong "stop"** per safety level. The arbiter picks the action with the lowest expected cost over the navigator's probabilities, not simply the most likely one.
- **Time to contact** comes from how fast an object's Jev box grows: `TTC = 2·area / (d area/dt)`. Below `TTC_STOP_S`, it stops; below `TTC_SLOW_S`, it slows down.
- **Vetoes only make things safer:** near high-risk, head-height or ground hazards, crossing traffic, a path not confirmed clear.
- **Memory:** an obstacle that just slipped out past the frame edge blocks a turn to that side for 3 s.
- **Hysteresis:** escalation to a safer action and sidesteps happen instantly; relaxing needs `RELEASE_STOP_FRAMES` agreeing frames.
- **Every Box is gated by a presence check**, because Jev always returns a box even when the object isn't there.
- **Any agent error, unreadable frame or late answer** (live, `MAX_DECISION_LATENCY_S`) → `STOP`.

## When it speaks

Silence is the default.
- **Critical:** always, instantly, a fixed line.
- **Caution:** only when the action changes, with a cooldown.
- **All-clear:** one line after a stop or wait ends.
- **Proactive Nemotron line:** only when you're standing still and something new is ahead.
- **While you're on stairs, turning or talking:** critical only.
- **Mic:** muted while it speaks, so it doesn't hear itself.

## Caveats

- **Jev image input and Box are experimental:** TypeSafe says they are "not production ready", and they need the `TypeSafe-Danger-Zone-Images: true` header (sent automatically). This is a prototype and an aid, not a replacement for a cane, a guide or your own judgement.
- **`heygen/heygen-voice-1-turbo:free` is listed as retiring on 2026-11-30.** After that, set `TTS_MODEL=heygen/heygen-voice-1-turbo` in `.env`.
- **The free Nemotron model is rate-limited.** That's acceptable because it only speaks occasionally; navigation never waits on it.
- **Image token cost isn't published by TypeSafe.** The run report shows the real token count and cost.
