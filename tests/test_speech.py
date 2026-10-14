import io
import json
import struct
import threading
import urllib.error
from array import array

import pytest

from app import speech
from app.speech import (MIN_OPENING, PAUSE_MS, SILENCE, SpeechEngine, Speaker, assign_voices,
                        cyrillic, pieces, samples, speakable, trim, voice_model, wants_cyrillic)


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

    def __init__(self, rejected=(), failing=(), held=(), model="kokoro"):
        self.rejected = rejected
        self.failing = failing
        self.held = set(held)
        self.released = threading.Event()
        self.spoken = []
        self.models = []
        self.model = model

    def release(self):
        self.released.set()

    def synthesize(self, text, voice, model=None):
        if text in self.held:
            assert self.released.wait(5), "a held piece was never released"
        if voice in self.rejected:
            raise RuntimeError("TTS endpoint returned HTTP 400: unknown voice")
        if text in self.failing:
            raise RuntimeError("TTS endpoint returned no audio")
        self.spoken.append((text, voice))
        self.models.append(model or self.model)
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


@pytest.mark.parametrize("latin, expected", [
    ("Nismo mi počeli ovaj rat.", "Нисмо ми почели овај рат."),
    # The digraphs are single Cyrillic letters, in either case.
    ("Džemal, ljudi i njihova deca", "Џемал, људи и њихова деца"),
    ("Ljubav i NJEGOVA kuća", "Љубав и ЊЕГОВА кућа"),
    ("ĐURO i Žarko šalju ćevape", "ЂУРО и Жарко шаљу ћевапе"),
    # Names come along, so a Serbian voice says them the Serbian way.
    ("Hrvoje, ti to dobro znaš!", "Хрвоје, ти то добро знаш!"),
    # Already Cyrillic, and punctuation and digits, are left alone.
    ("Нисмо ми почели овај рат.", "Нисмо ми почели овај рат."),
    ("1991. godine", "1991. године"),
])
def test_serbian_latin_is_transliterated_for_speech(latin, expected):
    assert cyrillic(latin) == expected


@pytest.mark.parametrize("voice, wanted", [
    ("sr-RS-NicholasNeural", True),
    ("speaches-ai/piper-sr_RS-serbski_institut-medium", True),
    ("hr-HR-SreckoNeural", False),   # Croatian is written in Latin only
    ("af_heart", False),
    ("en-US-AndrewNeural", False),
])
def test_only_serbian_voices_ask_for_cyrillic(voice, wanted):
    assert wants_cyrillic(voice) is wanted


def test_a_serbian_voice_is_given_cyrillic_and_the_reply_itself_is_untouched():
    engine = FakeEngine()
    reply = "Nismo mi počeli ovaj rat. Svi to znaju, Hrvoje."
    Speaker(engine, FakePlayer(), {"Milorad": "sr-RS-NicholasNeural"}).open("Milorad", reply)
    assert "Нисмо ми почели овај рат." in engine.spoken[0][0]
    assert "Nismo" not in engine.spoken[0][0]


def test_a_croatian_voice_keeps_the_latin_it_was_given():
    engine = FakeEngine()
    Speaker(engine, FakePlayer(), {"Hrvoje": "hr-HR-SreckoNeural"}).open(
        "Hrvoje", "To je obična laž i ti to dobro znaš.")
    assert engine.spoken[0][0].startswith("To je obična laž")


def _wav(audio: bytes, rate=24000, channels=1, bits=16, extra=b"") -> bytes:
    fmt = struct.pack("<HHIIHH", 1, channels, rate, rate * channels * bits // 8,
                      channels * bits // 8, bits)
    body = (b"WAVE" + extra + b"fmt " + struct.pack("<I", len(fmt)) + fmt
            + b"data" + struct.pack("<I", len(audio)) + audio)
    return b"RIFF" + struct.pack("<I", len(body)) + body


def test_raw_samples_are_passed_through_untouched():
    audio = _audio(0, LOUD, 0)
    assert samples(audio, 24000) == audio


def test_a_wav_file_is_unwrapped_to_its_samples():
    audio = _audio(0, LOUD, -LOUD, 0)
    assert samples(_wav(audio), 24000) == audio


def test_chunks_before_the_format_are_skipped():
    # ffmpeg writes a LIST chunk of its own before the audio.
    audio = _audio(LOUD, LOUD)
    padded = _wav(audio, extra=b"LIST" + struct.pack("<I", 4) + b"INFO")
    assert samples(padded, 24000) == audio


@pytest.mark.parametrize("kwargs, complaint", [
    ({"rate": 22050}, "22050 Hz"),
    ({"channels": 2}, "2-channel"),
    ({"bits": 8}, "8-bit"),
])
def test_audio_the_player_cannot_play_is_refused_with_what_to_change(kwargs, complaint):
    with pytest.raises(RuntimeError, match=complaint):
        samples(_wav(_audio(LOUD), **kwargs), 24000)


def test_a_wav_without_audio_is_an_error():
    with pytest.raises(RuntimeError, match="without audio"):
        samples(b"RIFF" + struct.pack("<I", 4) + b"WAVE", 24000)


def test_the_requested_format_is_what_the_engine_asks_for(monkeypatch):
    sent = _answering(monkeypatch, _wav(_audio(LOUD)))
    assert _engine("wav").synthesize("Hello.", "hr-HR-SreckoNeural")[:4] == b"RIFF"
    assert sent[0]["response_format"] == "wav"
    assert sent[0]["voice"] == "hr-HR-SreckoNeural"


def _answering(monkeypatch, *replies):
    """Answer each synthesis request with the next scripted reply: bytes, or an error to raise."""
    sent = []

    class FakeResponse(io.BytesIO):
        def __enter__(self): return self
        def __exit__(self, *exception): self.close()

    def urlopen(request, timeout=None):
        sent.append(json.loads(request.data))
        reply = replies[min(len(sent), len(replies)) - 1]
        if isinstance(reply, Exception):
            raise reply
        return FakeResponse(reply)

    monkeypatch.setattr(speech.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(speech, "RETRY_PAUSE", 0)
    return sent


def _http(code):
    return urllib.error.HTTPError("http://tts/v1/audio/speech", code, "boom", {}, io.BytesIO(b"busy"))


def _engine(fmt="pcm"):
    return SpeechEngine("http://tts/v1", "tts-1", "k", 1.0, fmt)


def test_a_passing_refusal_is_tried_once_more(monkeypatch):
    sent = _answering(monkeypatch, _http(500), _audio(LOUD))
    assert _engine().synthesize("Hello.", "af_heart") == _audio(LOUD)
    assert len(sent) == 2


def test_a_refusal_the_endpoint_means_is_not_retried(monkeypatch):
    sent = _answering(monkeypatch, _http(400))
    with pytest.raises(RuntimeError, match="HTTP 400"):
        _engine().synthesize("Hello.", "af_hart")
    assert len(sent) == 1, "a voice the endpoint does not have will not appear on a second try"


def test_refusals_all_the_way_through_are_reported(monkeypatch):
    # A fresh error each time: an HTTPError's body can only be read once.
    sent = _answering(monkeypatch, *[_http(503) for _ in range(speech.RETRIES)])
    with pytest.raises(RuntimeError, match="HTTP 503: busy"):
        _engine().synthesize("Hello.", "af_heart")
    assert len(sent) == speech.RETRIES


def test_a_piece_refused_a_few_times_still_gets_through(monkeypatch):
    sent = _answering(monkeypatch, _http(500), _http(500), _audio(LOUD))
    assert _engine().synthesize("Hello.", "af_heart") == _audio(LOUD)
    assert len(sent) == 3


def test_a_dropped_connection_is_tried_once_more(monkeypatch):
    sent = _answering(monkeypatch, OSError("connection reset"), _audio(LOUD))
    assert _engine().synthesize("Hello.", "af_heart") == _audio(LOUD)
    assert len(sent) == 2


def test_an_empty_answer_is_still_an_error(monkeypatch):
    _answering(monkeypatch, b"", b"")
    with pytest.raises(RuntimeError, match="no audio"):
        _engine().synthesize("Hello.", "af_heart")


PIPER_NL = "speaches-ai/piper-nl_NL-mls-medium"


@pytest.mark.parametrize("voice, expected", [
    # A plain name is one of many voices inside the run's own model.
    ("af_heart", ("kokoro", "af_heart")),
    # A voice that names a model is a model of its own, carrying its single voice.
    (PIPER_NL, (PIPER_NL, "piper-nl_NL-mls-medium")),
    ("speaches-ai/Kokoro-82M-v1.0-ONNX", ("speaches-ai/Kokoro-82M-v1.0-ONNX", "Kokoro-82M-v1.0-ONNX")),
])
def test_a_voice_naming_a_model_brings_its_own_model(voice, expected):
    assert voice_model(voice, "kokoro") == expected


def test_personas_can_speak_through_different_models_in_one_conversation():
    engine = FakeEngine()
    speaker = Speaker(engine, FakePlayer(), {"Mira": "af_heart", "Jeroen": PIPER_NL})
    speaker.open("Mira", "Cars should go.")
    speaker.open("Jeroen", "Dat is niet ver weg.")
    assert engine.models == ["kokoro", PIPER_NL]
    assert [voice for _, voice in engine.spoken] == ["af_heart", "piper-nl_NL-mls-medium"]


def test_check_tries_every_model_a_voice_asks_for():
    engine = FakeEngine()
    Speaker(engine, FakePlayer(), {"Mira": "af_heart", "Jeroen": PIPER_NL}).check()
    assert engine.models == ["kokoro", PIPER_NL]


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


def test_hushing_the_player_cuts_off_what_it_is_playing_and_plays_nothing_more(monkeypatch):
    import subprocess

    player = speech.PulsePlayer(1000)
    opened = []

    def open_paplay():
        opened.append(subprocess.Popen(["sh", "-c", "cat > /dev/null"],
                                       stdin=subprocess.PIPE, stderr=subprocess.PIPE))
        return opened[-1]

    monkeypatch.setattr(player, "open", open_paplay)

    def audio():
        yield b"\0" * 100
        player.hush()
        yield b"\0" * 100

    player.play(audio())  # killed, but because it was hushed, so not a playback failure
    assert opened[0].returncode != 0
    player.play(iter([b"\0" * 100]))
    assert len(opened) == 1
