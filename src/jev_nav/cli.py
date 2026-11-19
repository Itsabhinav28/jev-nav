"""Command line: `jev-nav analyze`, `jev-nav live`, `jev-nav check`."""

from __future__ import annotations

import asyncio
from pathlib import Path

import numpy as np
import typer
from rich.console import Console

from .config import get_settings

app = typer.Typer(add_completion=False, help="FPV navigation: a council of Jev agents decides, Nemotron talks.")
console = Console()


@app.command()
def analyze(
    source: str = typer.Argument(..., help="Video file or folder of frames."),
    fps: float = typer.Option(None, help="Frames per second to analyse (default from .env)."),
    voice: bool = typer.Option(False, help="Also run the speech policy with HeyGen voice (plays audio)."),
    out: Path = typer.Option(None, help="Output folder (default runs/<timestamp>)."),
) -> None:
    """Analyse a recorded first-person video offline and write a report."""
    from .runtime import run

    s = get_settings()
    if fps:
        s.sample_fps = fps
    asyncio.run(run(source, s, live=False, voice=voice, mic=False, out_dir=out))


@app.command()
def live(
    camera: str = typer.Option("0", help="Camera index, or a video file to replay at real speed."),
    mic: bool = typer.Option(True, help="Always-on microphone for talking to the companion."),
    voice: bool = typer.Option(True, help="Speak alerts and replies with HeyGen."),
) -> None:
    """Real-time: camera → council → spoken guidance; talk to it any time."""
    from .runtime import run

    asyncio.run(run(camera, get_settings(), live=True, voice=voice, mic=mic, save_frames=False))


@app.command()
def check() -> None:
    """Verify keys and that each model answers (makes 3 tiny paid/free calls)."""
    asyncio.run(_check())


async def _check() -> None:
    s = get_settings()
    ok = True
    # 1. Jev, with an image + Box, since that is what we depend on
    try:
        from typesafe_sdk import Box, Image, Noul

        from .frames import prepare_for_jev
        from .jev_client import JevClient

        jev = JevClient(s)
        img = np.full((480, 640, 3), 200, np.uint8)
        img[300:480, 250:390] = (40, 40, 40)
        r = await jev.ask(
            {"frame": Image(prepare_for_jev(img))},
            {"dark": Noul(instructions="Is there a dark rectangle in `frame`?"), "where": Box(instructions="the dark rectangle in `frame`")},
        )
        console.print(f"[green]✓ Jev {r.model}[/]: noul={r.answers['dark'].noul:.2f} box={r.answers['where'].box}")
        await jev.aclose()
    except Exception as e:  # noqa: BLE001
        ok = False
        console.print(f"[red]✗ Jev: {e}[/]")

    # 2. HeyGen TTS, then 3. Nemotron hearing that audio back (proves audio input works end to end)
    audio = None
    try:
        from .companion.tts import Speaker

        sp = Speaker(s, s.output_dir / "tts_cache", play_audio=False)
        audio = await sp.synthesize("Ruko, aage seedhiyan neeche hain.")
        console.print(f"[green]✓ TTS {s.tts_model}[/]: {len(audio):,} bytes")
        await sp.aclose()
    except Exception as e:  # noqa: BLE001
        ok = False
        console.print(f"[red]✗ TTS: {e}[/]")

    try:
        from .companion.listener import to_wav
        from .companion.nemotron import Companion
        from .companion.tts import decode_audio

        comp = Companion(s)
        wav = None
        if audio:
            samples, rate = decode_audio(audio, s.tts_sample_rate)
            wav = to_wav(samples, rate)
        reply = await comp.respond(None, user_audio_wav=wav, user_text=None if wav else "Kya aage rasta saaf hai?")
        console.print(f"[green]✓ Companion {s.companion_model}[/] ({'audio' if wav else 'text'} in): {reply}")
        await comp.aclose()
    except Exception as e:  # noqa: BLE001
        ok = False
        console.print(f"[red]✗ Companion: {e}[/]")
    raise typer.Exit(0 if ok else 1)


if __name__ == "__main__":
    app()
