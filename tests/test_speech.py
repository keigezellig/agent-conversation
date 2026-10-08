import pytest

from app.speech import Speaker, assign_voices, speakable


def test_speakable_strips_markdown_emoji_and_own_name():
    reply = "**Mira**: I *really* think `cars` should go 🚗.\n- Fewer [crashes](http://x)\n## Cleaner air"
    assert speakable(reply, "Mira") == "I really think cars should go .\nFewer crashes\nCleaner air"


def test_speakable_keeps_other_names_and_plain_underscores():
    assert speakable("Sol: snake_case is fine, Mira.", "Mira") == "Sol: snake_case is fine, Mira."


def test_explicit_voices_win_and_others_take_unclaimed_voices_in_order():
    personas = [("Mira", "curious", None), ("Sol", "skeptical", "af_heart"), ("Dave", "quiet", None)]
    assert assign_voices(personas, ["af_heart", "am_adam", "bf_emma"]) == {
        "Mira": "am_adam",
        "Sol": "af_heart",
        "Dave": "bf_emma",
    }


def test_voices_repeat_when_personas_outnumber_them():
    personas = [("A", "m", None), ("B", "m", None), ("C", "m", None)]
    assert assign_voices(personas, ["x", "y"]) == {"A": "x", "B": "y", "C": "x"}


def test_missing_voices_are_an_error():
    with pytest.raises(RuntimeError, match="TTS_VOICES"):
        assign_voices([("A", "m", None), ("B", "m", "x")], [])


class FakeEngine:
    def __init__(self, rejected=()):
        self.rejected = rejected
        self.spoken = []

    def synthesize(self, text, voice):
        if voice in self.rejected:
            raise RuntimeError("TTS endpoint returned HTTP 400: unknown voice")
        self.spoken.append((text, voice))
        return f"{voice}:{text}".encode()


class FakePlayer:
    def __init__(self, working=True):
        self.working = working

    def check(self):
        if not self.working:
            raise RuntimeError("no audio output")


def test_check_names_the_rejected_voice():
    speaker = Speaker(FakeEngine(rejected={"af_hart"}), FakePlayer(), {"Mira": "af_hart"})
    with pytest.raises(RuntimeError, match="'af_hart' rejected"):
        speaker.check()


def test_check_fails_without_audio_output():
    speaker = Speaker(FakeEngine(), FakePlayer(working=False), {"Mira": "af_heart"})
    with pytest.raises(RuntimeError, match="no audio output"):
        speaker.check()


def test_synthesize_speaks_cleaned_text_in_the_persona_voice():
    engine = FakeEngine()
    Speaker(engine, FakePlayer(), {"Mira": "af_heart"}).synthesize("Mira", "**Hello** there")
    assert engine.spoken == [("Hello there", "af_heart")]


def test_synthesize_refuses_replies_with_nothing_to_speak():
    with pytest.raises(RuntimeError, match="nothing to speak"):
        Speaker(FakeEngine(), FakePlayer(), {"Mira": "af_heart"}).synthesize("Mira", "🚗 🚲")
