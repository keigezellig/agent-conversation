"""Read-aloud support: speak persona replies through an OpenAI-compatible speech endpoint."""

import json
import re
import subprocess
import tempfile
import urllib.error
import urllib.request

DEFAULT_VOICES = "af_heart,am_michael,bf_emma,am_fenrir,af_bella,bm_george"

_LINK = re.compile(r"\[([^\]]+)\]\([^)]*\)")
_LINE_MARKER = re.compile(r"^[ \t]*(?:#+|[-*+]|\d+[.)])[ \t]+", re.MULTILINE)
_EMPHASIS = re.compile(r"\*+|`+|~~|(?<!\w)_+|_+(?!\w)")
_EMOJI = re.compile("[\U0001F000-\U0001FAFF☀-➿️‍]")


def speakable(text: str, name: str) -> str:
    """Return the reply text as it should be spoken: without its own name prefix, markdown, or emoji."""
    text = re.sub(rf"^\W*{re.escape(name)}\W*?:\s*", "", text, count=1, flags=re.IGNORECASE)
    text = _LINK.sub(r"\1", text)
    text = _LINE_MARKER.sub("", text)
    text = _EMPHASIS.sub("", text)
    text = _EMOJI.sub("", text)
    return re.sub(r"[ \t]+", " ", text).strip()


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


class SpeechEngine:
    """Client for an OpenAI-compatible text-to-speech endpoint."""

    def __init__(self, base_url: str, model: str, api_key: str):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key

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
                "response_format": "wav",
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
    """Plays WAV audio on the host's PulseAudio or PipeWire server."""

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

    def play(self, audio: bytes) -> None:
        with tempfile.NamedTemporaryFile(suffix=".wav") as file:
            file.write(audio)
            file.flush()
            result = subprocess.run(["paplay", file.name], capture_output=True, text=True)
        if result.returncode != 0:
            raise RuntimeError(f"playback failed: {result.stderr.strip() or 'paplay exited with an error'}")


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

    def synthesize(self, name: str, text: str) -> bytes:
        spoken = speakable(text, name)
        if not spoken:
            raise RuntimeError("nothing to speak")
        return self.engine.synthesize(spoken, self.voices[name])

    def play(self, audio: bytes) -> None:
        self.player.play(audio)
