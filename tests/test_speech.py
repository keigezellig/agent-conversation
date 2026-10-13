import threading
from array import array

import pytest

from app.speech import MIN_OPENING, PAUSE_MS, SILENCE, Speaker, assign_voices, pieces, speakable, trim


def _audio(*samples: int) -> bytes:
    return array("h", samples).tobytes()


def test_speakable_strips_markdown_emoji_and_own_name():
    reply = "**Mira**: I *really* think `cars` should go 🚗.\n- Fewer [crashes](http://x)\n## Cleaner air"
    assert speakable(reply, "Mira") == "I really think cars should go. Fewer crashes. Cleaner air."


def test_speakable_keeps_other_names_and_plain_underscores():
    assert speakable("Sol: snake_case is fine, Mira.", "Mira") == "Sol: snake_case is fine, Mira."


def test_speakable_closes_a_line_that_has_no_terminal_punctuation():
    assert speakable("Cars should go\nBikes should stay", "Mira") == "Cars should go. Bikes should stay."


def test_speakable_keeps_terminal_punctuation_behind_a_quote():
    assert speakable('He said "no."\nSo we waited.', "Mira") == 'He said "no." So we waited.'


def test_a_reply_cut_off_by_an_interruption_is_not_given_a_full_stop():
    assert speakable("I have said this before and—", "Mira") == "I have said this before and—"
    assert pieces("I have said this before and—") == ["I have said this before and—"]


OPENING = "I think the whole idea of banning private cars deserves a proper look."


def test_the_opening_piece_is_spoken_on_its_own():
    assert len(OPENING) >= MIN_OPENING
    assert pieces(f"{OPENING} Fewer crashes. Cleaner air.") == [OPENING, "Fewer crashes. Cleaner air."]


def test_a_short_opening_takes_the_next_sentence_with_it():
    assert pieces("Cars should go. Fewer crashes, cleaner air, and quieter streets for everyone.") == [
        "Cars should go. Fewer crashes, cleaner air, and quieter streets for everyone.",
    ]


def test_later_sentences_are_grouped_rather_than_sent_one_by_one():
    grouped = pieces(f"{OPENING} Fewer crashes. Cleaner air. Quieter streets. Cheaper transport.")
    assert grouped == [OPENING, "Fewer crashes. Cleaner air. Quieter streets. Cheaper transport."]


def test_pieces_handles_text_without_a_terminator_and_empty_text():
    assert pieces("Cars should go") == ["Cars should go"]
    assert pieces("   ") == []


def test_trim_removes_the_padding_only_at_the_requested_ends():
    padded = _audio(0, 0, 5000, -5000, 0, 0)
    assert trim(padded, lead=True, tail=True) == _audio(5000, -5000)
    assert trim(padded, lead=False, tail=True) == _audio(0, 0, 5000, -5000)
    assert trim(padded, lead=True, tail=False) == _audio(5000, -5000, 0, 0)


def test_trim_counts_quiet_samples_as_padding():
    assert trim(_audio(SILENCE, 5000, SILENCE), lead=True, tail=True) == _audio(5000)


def test_trim_of_silence_alone_leaves_nothing():
    assert trim(_audio(0, 0, 0), lead=True, tail=True) == b""


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


LOUD = 5000


class FakeEngine:
    """Speech engine returning one loud sample per word, padded with silence like a real one."""

    def __init__(self, rejected=(), failing=(), held=()):
        self.rejected = rejected
        self.failing = failing
        self.held = set(held)
        self.released = threading.Event()
        self.spoken = []

    def release(self):
        self.released.set()

    def synthesize(self, text, voice):
        if text in self.held:
            assert self.released.wait(5), "a held piece was never released"
        if voice in self.rejected:
            raise RuntimeError("TTS endpoint returned HTTP 400: unknown voice")
        if text in self.failing:
            raise RuntimeError("TTS endpoint returned no audio")
        self.spoken.append((text, voice))
        return _audio(0, *([LOUD] * len(text.split())), 0)


class FakePlayer:
    def __init__(self, working=True):
        self.working = working
        self.sample_rate = 1000
        self.played = b""

    def check(self):
        if not self.working:
            raise RuntimeError("no audio output")

    def silence(self, milliseconds):
        return _audio(*([0] * (self.sample_rate * milliseconds // 1000)))

    def play(self, audio):
        for piece in audio:
            self.played += piece


def test_check_names_the_rejected_voice():
    speaker = Speaker(FakeEngine(rejected={"af_hart"}), FakePlayer(), {"Mira": "af_hart"})
    with pytest.raises(RuntimeError, match="'af_hart' rejected"):
        speaker.check()


def test_check_fails_without_audio_output():
    speaker = Speaker(FakeEngine(), FakePlayer(working=False), {"Mira": "af_heart"})
    with pytest.raises(RuntimeError, match="no audio output"):
        speaker.check()


def test_a_reply_can_start_being_spoken_before_the_rest_is_synthesized():
    engine = FakeEngine(held={"Fewer crashes."})
    speaker = Speaker(engine, FakePlayer(), {"Mira": "af_heart"})
    clip = speaker.open("Mira", f"**{OPENING}** Fewer crashes.")
    assert engine.spoken == [(OPENING, "af_heart")]
    engine.release()
    speaker.speak(clip)
    assert engine.spoken == [(OPENING, "af_heart"), ("Fewer crashes.", "af_heart")]


def test_speaking_a_reply_plays_every_piece_in_order():
    engine = FakeEngine()
    player = FakePlayer()
    speaker = Speaker(engine, player, {"Mira": "af_heart"})
    speaker.speak(speaker.open("Mira", f"{OPENING} Fewer crashes."))
    assert player.played.count(_audio(LOUD)) == len(OPENING.split()) + 2


def test_speaking_replaces_the_padding_between_pieces_with_one_short_pause():
    player = FakePlayer()
    speaker = Speaker(FakeEngine(), player, {"Mira": "af_heart"})
    speaker.speak(speaker.open("Mira", f"{OPENING} Fewer crashes."))
    samples = array("h")
    samples.frombytes(player.played)
    # The opening keeps its own lead-in; every other silent sample is the deliberate pause.
    assert samples[0] == 0
    assert samples[-1] == LOUD
    assert list(samples).count(0) == 1 + player.sample_rate * PAUSE_MS // 1000


def test_a_failing_opening_piece_is_reported_before_the_reply_is_spoken():
    speaker = Speaker(FakeEngine(failing={OPENING}), FakePlayer(), {"Mira": "af_heart"})
    with pytest.raises(RuntimeError, match="no audio"):
        speaker.open("Mira", f"{OPENING} Fewer crashes.")


def test_a_failing_later_piece_is_reported_after_what_was_ready_is_spoken():
    player = FakePlayer()
    speaker = Speaker(FakeEngine(failing={"Fewer crashes."}), player, {"Mira": "af_heart"})
    clip = speaker.open("Mira", f"{OPENING} Fewer crashes.")
    with pytest.raises(RuntimeError, match="no audio"):
        speaker.speak(clip)
    assert player.played.count(_audio(LOUD)) == len(OPENING.split())


def test_speaking_refuses_replies_with_nothing_to_speak():
    with pytest.raises(RuntimeError, match="nothing to speak"):
        Speaker(FakeEngine(), FakePlayer(), {"Mira": "af_heart"}).open("Mira", "🚗 🚲")
