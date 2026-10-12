"""Read-aloud support: speak persona replies through an OpenAI-compatible speech endpoint."""

import json
import queue
import re
import subprocess
import threading
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

_LINK = re.compile(r"\[([^\]]+)\]\([^)]*\)")
_LINE_MARKER = re.compile(r"^[ \t]*(?:#+|[-*+]|\d+[.)])[ \t]+", re.MULTILINE)
_EMPHASIS = re.compile(r"\*+|`+|~~|(?<!\w)_+|_+(?!\w)")
_EMOJI = re.compile("[\U0001F000-\U0001FAFF☀-➿️‍]")
_SPACED_PUNCTUATION = re.compile(r"\s+(?=[.,;:!?…])")
_ENDS_SENTENCE = re.compile(r"[.!?…][\"'’”)\]]*$")
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


class SpeechEngine:
    """Client for an OpenAI-compatible text-to-speech endpoint."""

    def __init__(self, base_url: str, model: str, api_key: str, speed: float = 1.0):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key
        self.speed = speed

    def synthesize(self, text: str, voice: str) -> bytes:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        request = urllib.request.Request(
            f"{self.base_url}/audio/speech",
            data=json.dumps({
                "model": self.model,
                "input": text,
                "voice": voice,
                "response_format": "pcm",
                "speed": self.speed,
            }).encode(),
            headers=headers,
        )
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                audio = response.read()
        except urllib.error.HTTPError as error:
            detail = error.read().decode(errors="replace")
            raise RuntimeError(f"TTS endpoint returned HTTP {error.code}: {detail}") from error
        if not audio:
            raise RuntimeError("TTS endpoint returned no audio")
        return audio


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
            try:
                self.engine.synthesize("Ready.", voice)
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
        spoken = pieces(speakable(text, name))
        if not spoken:
            raise RuntimeError("nothing to speak")
        voice = self.voices[name]
        opening = self.engine.synthesize(spoken[0], voice)

        pending = queue.Queue(maxsize=len(spoken))
        clip = Clip(opening, pending)

        def synthesize_rest() -> None:
            try:
                for piece in spoken[1:]:
                    pending.put(self.engine.synthesize(piece, voice))
            except (OSError, RuntimeError) as error:
                clip.failure = error
            finally:
                pending.put(None)

        threading.Thread(target=synthesize_rest, daemon=True).start()
        return clip

    def speak(self, clip: Clip) -> None:
        self.player.play(clip.audio(self.player.silence(PAUSE_MS)))
