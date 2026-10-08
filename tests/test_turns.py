import argparse
import asyncio
import threading
import time

import pytest

from app.agents import _personas, _take_turns

PERSONAS = [("Mira", "curious", None), ("Sol", "skeptical", None)]


def _args(*specs):
    return argparse.Namespace(agent_specs=list(specs))


def test_agent_spec_accepts_an_optional_voice():
    assert _personas(_args("Mira|curious", "Sol|skeptical|am_adam")) == [
        ("Mira", "curious", None),
        ("Sol", "skeptical", "am_adam"),
    ]


def test_at_least_two_agents_are_required():
    with pytest.raises(RuntimeError, match="at least two agents"):
        _personas(argparse.Namespace(agent_specs=None))


def test_agent_spec_rejects_more_than_two_separators():
    with pytest.raises(RuntimeError, match=r"NAME\|MOOD\|VOICE"):
        _personas(_args("Mira|curious|a|b", "Sol|skeptical"))


class Recorder:
    """Fake composer, relay, and speaker that log every step of a conversation in order."""

    def __init__(self, play_seconds=0.05, failing_synthesis=(), failing_playback=()):
        self.events = []
        self.lock = threading.Lock()
        self.play_seconds = play_seconds
        self.failing_synthesis = failing_synthesis
        self.failing_playback = failing_playback
        self.finished = 0
        self.playing = False

    def log(self, *event):
        with self.lock:
            self.events.append(event)

    async def compose(self, turn, name, mood):
        self.log("compose", turn + 1, self.finished)
        await asyncio.sleep(0)
        return f"reply {turn + 1}"

    async def relay(self, turn, name, text):
        self.log("relay", turn + 1)

    def synthesize(self, name, text):
        if text in self.failing_synthesis:
            raise RuntimeError("TTS timed out")
        return text.encode()

    def play(self, audio):
        assert not self.playing, "replies were spoken over each other"
        self.playing = True
        self.log("play", audio.decode())
        time.sleep(self.play_seconds)
        self.playing = False
        if audio.decode() in self.failing_playback:
            raise RuntimeError("playback failed")
        self.finished += 1

    def show(self, line):
        self.log("show", line)


def _run(recorder, turns, speaker=True):
    asyncio.run(_take_turns(
        PERSONAS, turns, recorder.compose, recorder.relay,
        recorder if speaker else None, show=recorder.show,
    ))


def test_replies_are_spoken_in_turn_order_and_shown_as_they_start():
    recorder = Recorder()
    _run(recorder, 4)
    shown_and_played = [event for event in recorder.events if event[0] in ("show", "play")]
    assert shown_and_played == [
        event
        for turn in range(1, 5)
        for event in (("show", f"{PERSONAS[(turn - 1) % 2][0]} ({PERSONAS[(turn - 1) % 2][1]}): reply {turn}"),
                      ("play", f"reply {turn}"))
    ]


def test_composing_runs_at_most_one_reply_ahead_of_what_was_heard():
    recorder = Recorder()
    _run(recorder, 5)
    composes = [event for event in recorder.events if event[0] == "compose"]
    for _, turn, finished in composes:
        assert finished >= turn - 2
    assert any(finished == turn - 2 for _, turn, finished in composes), "the next reply was never composed ahead"


def test_runner_waits_for_the_last_reply_to_finish():
    recorder = Recorder()
    _run(recorder, 3)
    assert recorder.finished == 3


def test_failed_synthesis_is_marked_and_the_conversation_continues():
    recorder = Recorder(failing_synthesis={"reply 2"})
    _run(recorder, 3)
    shown = [event[1] for event in recorder.events if event[0] == "show"]
    assert shown == [
        "Mira (curious): reply 1",
        "Sol (skeptical): reply 2",
        "[not spoken: TTS timed out]",
        "Mira (curious): reply 3",
    ]
    assert [event[1] for event in recorder.events if event[0] == "play"] == ["reply 1", "reply 3"]


def test_failed_playback_is_marked_and_the_conversation_continues():
    recorder = Recorder(failing_playback={"reply 1"})
    _run(recorder, 2)
    shown = [event[1] for event in recorder.events if event[0] == "show"]
    assert shown == ["Mira (curious): reply 1", "[not spoken: playback failed]", "Sol (skeptical): reply 2"]


def test_without_a_speaker_replies_are_only_shown():
    recorder = Recorder()
    _run(recorder, 3, speaker=False)
    assert [event[0] for event in recorder.events if event[0] in ("show", "play")] == ["show"] * 3


def test_compose_failure_ends_the_conversation():
    recorder = Recorder()

    async def broken_compose(turn, name, mood):
        raise RuntimeError("model endpoint returned an empty response")

    with pytest.raises(RuntimeError, match="empty response"):
        asyncio.run(_take_turns(PERSONAS, 3, broken_compose, recorder.relay, recorder, show=recorder.show))
