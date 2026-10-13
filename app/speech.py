"""Read-aloud support: speak persona replies through an OpenAI-compatible speech endpoint."""

import json
import queue
import re
import struct
import subprocess
import threading
import time
import urllib.error
import urllib.request
from array import array

DEFAULT_VOICES = "af_heart,am_michael,bf_emma,am_fenrir,af_bella,bm_george"

# A reply is spoken in pieces so sound starts on its first sentence instead of its last. Each request
# carries about a second of fixed overhead, so pieces that are too small are slower, not quicker: only
# the opening piece is deliberately small.
MIN_OPENING = 40
MIN_PIECE = 200

# The engine pads every piece with about 50ms of silence before and 340ms after, which would stack up
# where two pieces meet. Trim that away and insert a pause of our own instead. A seam always falls on a
# sentence boundary, so the pause matches the pauses the engine leaves between sentences itself
# (measured 230-390ms across voices), and a seam sounds like every other sentence break.
SILENCE = 300
PAUSE_MS = 280

# A cloud engine refuses a piece of speech now and then: Microsoft turned down about one request in
# three while edge-tts was tried, and those refusals came and went with no pattern. Each further try
# waits longer, so a conversation survives a spell of them instead of losing a reply to every one.
RETRIES = 4
RETRY_PAUSE = 2.0

_LINK = re.compile(r"\[([^\]]+)\]\([^)]*\)")
_LINE_MARKER = re.compile(r"^[ \t]*(?:#+|[-*+]|\d+[.)])[ \t]+", re.MULTILINE)
_EMPHASIS = re.compile(r"\*+|`+|~~|(?<!\w)_+|_+(?!\w)")
_EMOJI = re.compile("[\U0001F000-\U0001FAFF☀-➿️‍]")
_SPACED_PUNCTUATION = re.compile(r"\s+(?=[.,;:!?…])")
# A dash ends a line that was cut off by an interruption; it needs no full stop adding.
_ENDS_SENTENCE = re.compile(r"[.!?…—–][\"'’”)\]]*$")
_SENTENCE = re.compile(r"[^.!?…]*[.!?…]+[\"'’”)\]]*\s*|[^.!?…]+$")


def speakable(text: str, name: str) -> str:
    """Return the reply text as it should be spoken: without its own name prefix, markdown, or emoji."""
    text = re.sub(rf"^\W*{re.escape(name)}\W*?:\s*", "", text, count=1, flags=re.IGNORECASE)
    text = _LINK.sub(r"\1", text)
    text = _LINE_MARKER.sub("", text)
    text = _EMPHASIS.sub("", text)
    text = _EMOJI.sub("", text)
    text = _SPACED_PUNCTUATION.sub("", text)
    lines = []
    for line in text.splitlines():
        line = re.sub(r"[ \t]+", " ", line).strip()
        if line:
            lines.append(line if _ENDS_SENTENCE.search(line) else f"{line}.")
    return " ".join(lines)


# Serbian is written in both alphabets, and the voices for it expect Cyrillic: given Latin they sound
# the letters out one by one, which listeners take for another language entirely. The digraphs are one
# letter each in Cyrillic, so they go first; q, w, x and y belong to no Serbian word and are left be.
_SERBIAN_VOICE = re.compile(r"(?<![a-z])sr[-_]rs(?![a-z])", re.IGNORECASE)
_DIGRAPHS = (("DŽ", "Џ"), ("Dž", "Џ"), ("dž", "џ"), ("LJ", "Љ"), ("Lj", "Љ"), ("lj", "љ"),
             ("NJ", "Њ"), ("Nj", "Њ"), ("nj", "њ"))
_LETTERS = str.maketrans({
    "a": "а", "b": "б", "c": "ц", "č": "ч", "ć": "ћ", "d": "д", "đ": "ђ", "e": "е", "f": "ф",
    "g": "г", "h": "х", "i": "и", "j": "ј", "k": "к", "l": "л", "m": "м", "n": "н", "o": "о",
    "p": "п", "r": "р", "s": "с", "š": "ш", "t": "т", "u": "у", "v": "в", "z": "з", "ž": "ж",
    "A": "А", "B": "Б", "C": "Ц", "Č": "Ч", "Ć": "Ћ", "D": "Д", "Đ": "Ђ", "E": "Е", "F": "Ф",
    "G": "Г", "H": "Х", "I": "И", "J": "Ј", "K": "К", "L": "Л", "M": "М", "N": "Н", "O": "О",
    "P": "П", "R": "Р", "S": "С", "Š": "Ш", "T": "Т", "U": "У", "V": "В", "Z": "З", "Ž": "Ж",
})


def wants_cyrillic(voice: str) -> bool:
    """Does this voice need its text in Cyrillic? True for the Serbian voices of either engine."""
    return bool(_SERBIAN_VOICE.search(voice))


def cyrillic(text: str) -> str:
    """Transliterate Serbian written in the Latin alphabet into Cyrillic.

    Only what is spoken goes through this: the reply stays in the alphabet the persona wrote it in, so
    it reads as written and the names in it still match the personas it calls on. Text already in
    Cyrillic passes through untouched. A persona that drops its diacritics loses the letters they
    carried ("c" cannot be told apart from "č" or "ć" afterwards), which is the persona's own doing.
    """
    for latin, letter in _DIGRAPHS:
        text = text.replace(latin, letter)
    return text.translate(_LETTERS)


def voice_model(voice: str, default_model: str) -> tuple[str, str]:
    """Split a persona's voice into the model that speaks it and the voice to ask that model for.

    Engines name voices two ways. Kokoro has many voices in one model, so a plain name like
    "af_heart" is a voice inside the run's own model. Piper has one voice per model, so a voice that
    names a model ("speaches-ai/piper-nl_NL-mls-medium") is its own model and brings its own voice;
    that is what lets personas in one conversation speak different languages.
    """
    if "/" not in voice:
        return default_model, voice
    return voice, voice.rsplit("/", 1)[-1]


def assign_voices(personas: list[tuple[str, str, str | None]], available: list[str]) -> dict[str, str]:
    """Give each persona its chosen voice, or the next unclaimed voice from the available list."""
    chosen = {name: voice for name, _, voice in personas if voice}
    spare = [voice for voice in available if voice not in chosen.values()] or available
    if not spare and len(chosen) < len(personas):
        raise RuntimeError("set TTS_VOICES or give every --agent a voice as NAME|MOOD|VOICE")

    voices = {}
    unvoiced = 0
    for name, _, voice in personas:
        if voice:
            voices[name] = voice
        else:
            voices[name] = spare[unvoiced % len(spare)]
            unvoiced += 1
    return voices


def pieces(text: str) -> list[str]:
    """Split a reply into the pieces it is synthesized in, one sentence or a few.

    The opening piece is kept short so the reply starts being heard sooner; the rest are grouped to
    stay well clear of the per-request overhead.
    """
    sentences = [found.group().strip() for found in _SENTENCE.finditer(text) if found.group().strip()]
    if not sentences:
        return []

    grouped = []
    for sentence in sentences:
        wanted = MIN_OPENING if len(grouped) == 1 else MIN_PIECE
        if grouped and len(grouped[-1]) < wanted:
            grouped[-1] += f" {sentence}"
        else:
            grouped.append(sentence)
    return grouped


def trim(audio: bytes, lead: bool, tail: bool) -> bytes:
    """Return the audio without the near-silent padding the engine adds at the requested ends."""
    samples = array("h")
    samples.frombytes(audio[: len(audio) - len(audio) % samples.itemsize])
    loud = [index for index, sample in enumerate(samples) if abs(sample) > SILENCE]
    if not loud:
        return b"" if lead or tail else audio
    start = loud[0] if lead else 0
    stop = loud[-1] + 1 if tail else len(samples)
    return samples[start:stop].tobytes()


def samples(audio: bytes, sample_rate: int) -> bytes:
    """Return the raw samples to play, unwrapping the WAV container when an engine sends one.

    Engines differ in what they can produce: some give raw samples, others only a WAV file. The pieces
    of a reply are played into one stream, so a header in the middle would be heard as a click, and a
    file recorded at another rate would play at the wrong pitch. Both are refused rather than played.
    """
    if audio[:4] != b"RIFF" or audio[8:12] != b"WAVE":
        return audio

    fmt, data, at = None, None, 12
    while at + 8 <= len(audio):
        chunk, size = audio[at:at + 4], int.from_bytes(audio[at + 4:at + 8], "little")
        body = audio[at + 8:at + 8 + size]
        if chunk == b"fmt ":
            fmt = struct.unpack("<HHIIHH", body[:16])
        elif chunk == b"data":
            data = body
        at += 8 + size + size % 2
    if fmt is None or data is None:
        raise RuntimeError("TTS endpoint returned a WAV file without audio in it")

    _, channels, rate, _, _, bits = fmt
    if (rate, channels, bits) != (sample_rate, 1, 16):
        raise RuntimeError(
            f"TTS endpoint returned {rate} Hz {channels}-channel {bits}-bit audio, but the runner "
            f"plays {sample_rate} Hz mono 16-bit; set --tts-sample-rate {rate} to match"
        )
    return data


class SpeechEngine:
    """Client for an OpenAI-compatible text-to-speech endpoint."""

    def __init__(self, base_url: str, model: str, api_key: str, speed: float = 1.0,
                 response_format: str = "pcm"):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key
        self.speed = speed
        self.response_format = response_format

    def synthesize(self, text: str, voice: str, model: str | None = None) -> bytes:
        """Ask the endpoint for one piece of speech, trying again while it keeps refusing.

        A refusal the endpoint means, such as a voice it does not have, is not retried: that will not
        come right on a second ask, and failing at once says so plainly.
        """
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        request = urllib.request.Request(
            f"{self.base_url}/audio/speech",
            data=json.dumps({
                "model": model or self.model,
                "input": text,
                "voice": voice,
                "response_format": self.response_format,
                "speed": self.speed,
            }).encode(),
            headers=headers,
        )

        failure = None
        for attempt in range(1, RETRIES + 1):
            if attempt > 1:
                time.sleep(RETRY_PAUSE * (attempt - 1))
            try:
                with urllib.request.urlopen(request, timeout=60) as response:
                    audio = response.read()
            except urllib.error.HTTPError as error:
                detail = error.read().decode(errors="replace")
                failure = RuntimeError(f"TTS endpoint returned HTTP {error.code}: {detail}")
                if error.code < 500:
                    break
            except OSError as error:
                failure = error
            else:
                if audio:
                    return audio
                failure = RuntimeError("TTS endpoint returned no audio")
        raise failure


class PulsePlayer:
    """Plays raw audio on the host's PulseAudio or PipeWire server as it arrives."""

    def __init__(self, sample_rate: int = 24000):
        self.sample_rate = sample_rate

    def check(self) -> None:
        try:
            result = subprocess.run(["pactl", "info"], capture_output=True, text=True, timeout=10)
        except FileNotFoundError as error:
            raise RuntimeError("paplay is not installed; use the agent-chat-voice service") from error
        if result.returncode != 0:
            raise RuntimeError(
                "no audio output: is the PulseAudio/PipeWire socket mounted? "
                f"({result.stderr.strip() or 'pactl info failed'})"
            )

    def open(self) -> subprocess.Popen:
        return subprocess.Popen(
            [
                "paplay", "--raw",
                "--format=s16le",
                f"--rate={self.sample_rate}",
                "--channels=1",
            ],
            stdin=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

    def silence(self, milliseconds: int) -> bytes:
        return bytes(2 * (self.sample_rate * milliseconds // 1000))

    def play(self, audio) -> None:
        """Play everything the given iterable yields, writing each piece as soon as it is available."""
        player = self.open()
        try:
            for piece in audio:
                player.stdin.write(piece)
        except BrokenPipeError:
            pass
        finally:
            try:
                player.stdin.close()
            except BrokenPipeError:
                pass
            stderr = player.stderr.read().decode(errors="replace")
            player.stderr.close()
            player.wait()
        if player.returncode != 0:
            raise RuntimeError(f"playback failed: {stderr.strip() or 'paplay exited with an error'}")


class Clip:
    """A reply's audio: its opening piece ready to play, the rest arriving while that one is heard."""

    def __init__(self, opening: bytes, pending: queue.Queue):
        self.opening = opening
        self.pending = pending
        self.failure: Exception | None = None

    def audio(self, pause: bytes = b""):
        """Yield the pieces in order, trimming the padding at each seam and pausing between them.

        A piece that failed to synthesize ends the clip: what was already synthesized is still spoken,
        and the failure is raised once it has been.
        """
        piece = trim(self.opening, lead=False, tail=True)
        while (following := self.pending.get()) is not None:
            yield piece
            yield pause
            piece = trim(following, lead=True, tail=True)
        yield piece
        if self.failure:
            raise self.failure


class Speaker:
    """Speaks each persona's replies in that persona's voice."""

    def __init__(self, engine, player, voices: dict[str, str]):
        self.engine = engine
        self.player = player
        self.voices = voices

    def check(self) -> None:
        """Fail before the conversation starts if any voice or the audio output is unusable."""
        for voice in dict.fromkeys(self.voices.values()):
            model, spoken = voice_model(voice, self.engine.model)
            try:
                samples(self.engine.synthesize("Ready.", spoken, model), self.player.sample_rate)
            except RuntimeError as error:
                raise RuntimeError(f"voice {voice!r} rejected: {error}") from error
            except OSError as error:
                raise RuntimeError(
                    f"cannot reach the TTS endpoint at {self.engine.base_url}: {error} "
                    "(is it running and finished starting?)"
                ) from error
        self.player.check()

    def open(self, name: str, text: str) -> Clip:
        """Start synthesizing a reply and return once its opening piece can be played."""
        said = speakable(text, name)
        if wants_cyrillic(self.voices[name]):
            said = cyrillic(said)
        spoken = pieces(said)
        if not spoken:
            raise RuntimeError("nothing to speak")
        model, voice = voice_model(self.voices[name], self.engine.model)
        rate = self.player.sample_rate
        opening = samples(self.engine.synthesize(spoken[0], voice, model), rate)

        pending = queue.Queue(maxsize=len(spoken))
        clip = Clip(opening, pending)

        def synthesize_rest() -> None:
            try:
                for piece in spoken[1:]:
                    pending.put(samples(self.engine.synthesize(piece, voice, model), rate))
            except (OSError, RuntimeError) as error:
                clip.failure = error
            finally:
                pending.put(None)

        threading.Thread(target=synthesize_rest, daemon=True).start()
        return clip

    def speak(self, clip: Clip) -> None:
        self.player.play(clip.audio(self.player.silence(PAUSE_MS)))
