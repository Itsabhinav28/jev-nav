import numpy as np

from jev_nav.companion.listener import RATE, VoiceDetector
from jev_nav.companion.nemotron import Companion
from jev_nav.companion.speech_policy import Channel, SpeechPolicy
from jev_nav.companion.tts import decode_audio
from jev_nav.config import Settings
from jev_nav.schemas import Action, NavDecision, StairsMode, Urgency, UserActivity


def dec(action, urgency, changed=True, activity=UserActivity.WALKING, stairs=StairsMode.NORMAL, kind=None, i=0):
    return NavDecision(frame_index=i, timestamp_s=i, action=action, urgency=urgency, reasons=["r"],
                       changed=changed, activity=activity, stairs_mode=stairs, hazard_kind=kind)


def test_critical_always_speaks_fixed_line():
    p = SpeechPolicy(4, 12)
    ev = p.consider(dec(Action.STOP, Urgency.CRITICAL), now=0)
    assert ev and ev.channel == Channel.FIXED and ev.text == "Stop!"
    assert p.consider(dec(Action.STOP, Urgency.CRITICAL, changed=False), now=1) is None  # no nagging
    assert p.consider(dec(Action.STOP, Urgency.CRITICAL, changed=False), now=5) is not None  # still there → remind


def test_quiet_while_climbing_except_critical():
    p = SpeechPolicy(4, 12)
    assert p.consider(dec(Action.SLOW_DOWN, Urgency.CAUTION, stairs=StairsMode.CLIMBING_DOWN), now=0) is None
    assert p.consider(dec(Action.STOP, Urgency.CRITICAL, stairs=StairsMode.CLIMBING_DOWN), now=1) is not None


def test_caution_cooldown_and_quiet_when_user_talks():
    p = SpeechPolicy(4, 12)
    assert p.consider(dec(Action.GO_LEFT, Urgency.CAUTION), now=0)
    assert p.consider(dec(Action.GO_RIGHT, Urgency.CAUTION), now=1) is None  # cooldown
    assert p.consider(dec(Action.SLOW_DOWN, Urgency.CAUTION), now=10, user_talking=True) is None


def test_all_clear_after_halt_and_proactive_only_when_standing():
    p = SpeechPolicy(4, 12)
    p.consider(dec(Action.WAIT, Urgency.CAUTION), now=0)
    ev = p.consider(dec(Action.MOVE_FORWARD, Urgency.INFO), now=5)
    assert ev and ev.text == "Path is clear, go ahead."
    assert p.consider(dec(Action.MOVE_FORWARD, Urgency.INFO, changed=False, kind="person"), now=30) is None  # walking
    ev = p.consider(dec(Action.MOVE_FORWARD, Urgency.INFO, changed=False, kind="person", activity=UserActivity.STANDING), now=31)
    assert ev and ev.channel == Channel.COMPANION


def test_companion_cannot_contradict_jev():
    c = Companion(Settings(_env_file=None, openrouter_api_key="x"))
    stop = dec(Action.STOP, Urgency.CRITICAL)
    assert c._validate('{"say": "Haan chalo, sab theek hai", "ack_action": "STOP"}', stop) == "Stop!"
    assert c._validate('{"say": "Aage badh jao", "ack_action": "MOVE_FORWARD"}', stop) == "Stop!"
    assert c._validate("not json", stop) == "Stop!"
    ok = c._validate('{"say": "Stop, pillar right in front.", "ack_action": "STOP"}', stop)
    assert ok == "Stop, pillar right in front."


def test_voice_detector_finds_an_utterance():
    vad = VoiceDetector()
    block = int(RATE * 0.03)
    quiet = lambda: (np.random.randn(block) * 50).astype(np.int16)  # noqa: E731
    loud = lambda: (np.sin(np.arange(block) / 3) * 8000).astype(np.int16)  # noqa: E731
    out = [vad.feed(quiet()) for _ in range(30)] + [vad.feed(loud()) for _ in range(30)] + [vad.feed(quiet()) for _ in range(40)]
    wavs = [w for w in out if w]
    assert len(wavs) == 1 and wavs[0][:4] == b"RIFF"


def test_decode_raw_pcm():
    samples, rate = decode_audio(np.arange(10, dtype=np.int16).tobytes(), 44100)
    assert rate == 44100 and samples.tolist() == list(range(10))


def test_hinglish_still_available():
    p = SpeechPolicy(4, 12, language="hinglish")
    assert p.consider(dec(Action.STOP, Urgency.CRITICAL), now=0).text == "Ruko!"
    from jev_nav.prompts import companion_system_prompt
    assert "Hinglish" in companion_system_prompt("hinglish") and "plain, simple English" in companion_system_prompt("english")


async def test_speaker_plays_on_one_thread_and_interrupts(monkeypatch, tmp_path):
    """Regression: play + stop from different threads crashed CoreAudio (SIGTRAP) when alerts overlapped."""
    import asyncio
    import sys
    import threading
    import types

    from jev_nav.companion.tts import Speaker

    threads, writes = set(), []

    class FakeStream:
        def __init__(self, **kw): threads.add(threading.current_thread().name)
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def write(self, chunk):
            threads.add(threading.current_thread().name); writes.append(len(chunk)); import time; time.sleep(0.01)

    monkeypatch.setitem(sys.modules, "sounddevice", types.SimpleNamespace(OutputStream=FakeStream))
    sp = Speaker(Settings(_env_file=None, openrouter_api_key="x"), tmp_path)
    long_line = np.zeros(44100 * 3, np.int16).tobytes()  # 3 s
    short_line = np.zeros(4410, np.int16).tobytes()
    async def fake_synth(text): return long_line if text == "long" else short_line
    sp.synthesize = fake_synth

    first = asyncio.create_task(sp.say("long"))
    await asyncio.sleep(0.05)
    await sp.say("Stop!", interrupt=True)
    await asyncio.wait_for(first, 2)  # the long line was cut off, not played for 3 s
    await sp.aclose()
    assert threads == {"speaker"}
    assert sum(writes) < 44100 * 3
