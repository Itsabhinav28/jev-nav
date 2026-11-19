"""Main loop: frames → council → decision → (maybe) speech; mic → Nemotron → speech, in parallel."""

from __future__ import annotations

import asyncio
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

from rich.console import Console

from .companion.phrases import ACTION_LINES
from .companion.speech_policy import Channel, SpeechPolicy
from .config import Settings
from .frames import iter_frames
from .jev_client import JevClient
from .orchestrator import Orchestrator
from .render import RunRecorder
from .schemas import Urgency

console = Console()
STYLE = {Urgency.INFO: "green", Urgency.CAUTION: "yellow", Urgency.CRITICAL: "bold red"}


async def run(
    source: str,
    settings: Settings,
    live: bool,
    voice: bool,
    mic: bool,
    play_audio: bool = True,
    save_frames: bool = True,
    out_dir: Path | None = None,
) -> Path:
    out_dir = out_dir or settings.output_dir / datetime.now().strftime("%Y%m%d-%H%M%S")
    jev = JevClient(settings)
    orch = Orchestrator(jev, settings, live=live)
    rec = RunRecorder(out_dir, settings.sample_fps, settings.presence_threshold, save_frames)
    policy = SpeechPolicy(settings.caution_cooldown_s, settings.info_cooldown_s)
    t0 = time.monotonic()

    speaker = companion = listener = None
    background: set[asyncio.Task] = set()
    if voice:
        from .companion.nemotron import Companion
        from .companion.tts import Speaker

        speaker = Speaker(settings, settings.output_dir / "tts_cache", play_audio=play_audio)
        companion = Companion(settings)
        with console.status("Pre-voicing alert lines…"):
            n = await speaker.warm(list(ACTION_LINES.values()))
        console.print(f"[dim]{n}/{len(ACTION_LINES)} alert lines ready[/]")

    def spawn(coro) -> None:
        task = asyncio.create_task(coro)
        background.add(task)
        task.add_done_callback(background.discard)

    async def speak(text: str, channel: str, critical: bool) -> None:
        rec.note_speech(time.monotonic() - t0, channel, text)
        console.print(f"[bold magenta]🗣  {text}[/]")
        if speaker:
            try:
                await speaker.say(text, interrupt=critical)
            except Exception as e:  # noqa: BLE001 — never let speech break navigation
                console.print(f"[red]TTS failed: {e}[/]")

    async def answer_user(audio: bytes | None, text: str | None) -> None:
        assert companion is not None
        try:
            reply = await companion.respond(orch.arbiter.last, orch.last_jpeg, user_audio_wav=audio, user_text=text)
        except Exception as e:  # noqa: BLE001
            console.print(f"[red]Companion failed: {e}[/]")
            return
        await speak(reply, "companion", critical=False)

    if voice and mic:
        from .companion.listener import Listener

        loop = asyncio.get_running_loop()
        listener = Listener(lambda: speaker.speaking, lambda wav: spawn(answer_user(wav, None)))
        listener.start(loop)
        console.print("[dim]Mic is on (always listening). Speak anytime, or type a message and press Enter.[/]")

    if voice and live:
        loop = asyncio.get_running_loop()

        def read_stdin() -> None:
            for line in sys.stdin:
                if line.strip():
                    loop.call_soon_threadsafe(lambda t=line.strip(): spawn(answer_user(None, t)))

        threading.Thread(target=read_stdin, daemon=True).start()

    frames = iter_frames(source, settings.sample_fps, realtime=live and not str(source).isdigit())
    try:
        while True:
            frame = await asyncio.to_thread(next, frames, None)
            if frame is None:
                break
            decision = await orch.step(frame)
            rec.add(frame.bgr, decision, orch.last_report)
            mark = "▶" if decision.changed else " "
            console.print(
                f"[{STYLE[decision.urgency]}]{mark} #{decision.frame_index:<4} {decision.action.value:<15}[/] "
                f"[dim]{decision.activity.value:<9} {'; '.join(decision.reasons)[-110:]}[/]"
            )
            event = policy.consider(decision, time.monotonic() - t0, user_talking=bool(listener and listener.user_talking))
            if event and voice:
                critical = event.urgency == Urgency.CRITICAL
                if event.channel == Channel.FIXED:
                    spawn(speak(event.text, "fixed", critical))
                elif companion is not None:
                    async def proactive(d=event.decision) -> None:
                        try:
                            text = await companion.respond(d, orch.last_jpeg, proactive=True)
                        except Exception as e:  # noqa: BLE001
                            console.print(f"[red]Companion failed: {e}[/]")
                            return
                        await speak(text, "companion", critical=False)

                    spawn(proactive())
            elif event:
                rec.note_speech(time.monotonic() - t0, event.channel.value, event.text or "(companion line)")
    except KeyboardInterrupt:
        pass
    finally:
        if listener:
            listener.stop()
        if background:
            await asyncio.gather(*background, return_exceptions=True)
        report = rec.close(jev.cost_usd)
        await jev.aclose()
        if speaker:
            await speaker.aclose()
        if companion:
            await companion.aclose()
    console.print(f"\nJev calls: {jev.calls}, input tokens: {jev.input_tokens:,}, cost ≈ ${jev.cost_usd:.4f}")
    console.print(f"Report: {report}")
    return report
