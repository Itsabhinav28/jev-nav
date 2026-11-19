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
    assert ev and ev.channel == Channel.FIXED and ev.text == "Ruko!"
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
    assert ev and ev.text == "Rasta saaf hai, chalo."
    assert p.consider(dec(Action.MOVE_FORWARD, Urgency.INFO, changed=False, kind="person"), now=30) is None  # walking
    ev = p.consider(dec(Action.MOVE_FORWARD, Urgency.INFO, changed=False, kind="person", activity=UserActivity.STANDING), now=31)
    assert ev and ev.channel == Channel.COMPANION


def test_companion_cannot_contradict_jev():
    c = Companion(Settings(_env_file=None, openrouter_api_key="x"))
    stop = dec(Action.STOP, Urgency.CRITICAL)
    assert c._validate('{"say": "Haan chalo, sab theek hai", "ack_action": "STOP"}', stop) == "Ruko!"
    assert c._validate('{"say": "Aage badh jao", "ack_action": "MOVE_FORWARD"}', stop) == "Ruko!"
    assert c._validate("not json", stop) == "Ruko!"
    ok = c._validate('{"say": "Ruko, saamne pillar hai.", "ack_action": "STOP"}', stop)
    assert ok == "Ruko, saamne pillar hai."


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
