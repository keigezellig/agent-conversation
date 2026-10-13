import argparse
import asyncio
import threading
import time

import pytest

from app.agents import _personas, _system_prompt, _take_turns, run_conversation

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
        self.scripted = {}

    def log(self, *event):
        with self.lock:
            self.events.append(event)

    async def compose(self, turn, name, mood, addressed_by=None, closing=False, brief=False,
                      interrupting=None, cut_off_by=None):
        self.log("compose", turn + 1, self.finished)
        self.log("turn", name, addressed_by, closing)
        self.log("brief", brief)
        self.log("cutting", interrupting, cut_off_by)
        await asyncio.sleep(0)
        return self.scripted.get(turn + 1, f"reply {turn + 1}")

    async def relay(self, turn, name, text):
        self.log("relay", turn + 1)

    def open(self, name, text):
        if text in self.failing_synthesis:
            raise RuntimeError("TTS timed out")
        return text

    def speak(self, clip):
        assert not self.playing, "replies were spoken over each other"
        self.playing = True
        self.log("play", clip)
        time.sleep(self.play_seconds)
        self.playing = False
        if clip in self.failing_playback:
            raise RuntimeError("playback failed")
        self.finished += 1

    def show(self, line):
        self.log("show", line)


def _run(recorder, turns, speaker=True, personas=PERSONAS, rng=None, eagerness=None):
    asyncio.run(_take_turns(
        personas, turns, recorder.compose, recorder.relay,
        recorder if speaker else None, show=recorder.show, rng=rng, eagerness=eagerness,
    ))


def _turns(recorder):
    return [event[1:] for event in recorder.events if event[0] == "turn"]


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

    async def broken_compose(turn, name, mood, addressed_by=None, closing=False, brief=False,
                             interrupting=None, cut_off_by=None):
        raise RuntimeError("model endpoint returned an empty response")

    with pytest.raises(RuntimeError, match="empty response"):
        asyncio.run(_take_turns(PERSONAS, 3, broken_compose, recorder.relay, recorder, show=recorder.show))


TRIO = [("Mira", "curious", None), ("Sol", "skeptical", None), ("Dave", "quiet", None)]


class FirstChoice:
    def __init__(self, roll=0.9):
        self.roll = roll

    def choices(self, population, weights):
        return [population[0]]

    def random(self):
        return self.roll


def test_the_first_listed_persona_opens_and_an_addressee_speaks_next():
    recorder = Recorder(play_seconds=0)
    recorder.scripted = {1: "Cars are loud. Dave, what do you think?", 2: "Hmm. Mira, maybe."}
    _run(recorder, 4, speaker=False, personas=TRIO, rng=FirstChoice())
    assert _turns(recorder) == [
        ("Mira", None, False),
        ("Dave", "Mira", False),
        ("Mira", "Dave", False),
        ("Sol", None, True),
    ]


def test_a_leading_speaker_label_is_removed_before_showing_relaying_and_addressing():
    recorder = Recorder(play_seconds=0)
    recorder.scripted = {1: "Dave: Are you serious, Sol?"}
    relayed = []

    async def relay(turn, name, text):
        relayed.append(text)

    asyncio.run(_take_turns(TRIO, 2, recorder.compose, relay, show=recorder.show))
    assert relayed[0] == "Are you serious, Sol?"
    assert ("show", "Mira (curious): Are you serious, Sol?") in recorder.events
    assert _turns(recorder)[1][0] == "Sol"


def test_without_an_addressee_the_last_speaker_never_goes_twice():
    recorder = Recorder(play_seconds=0)
    _run(recorder, 6, speaker=False, personas=TRIO)
    speakers = [name for name, _, _ in _turns(recorder)]
    assert speakers[0] == "Mira"
    assert all(a != b for a, b in zip(speakers, speakers[1:]))


def test_only_the_last_turn_is_the_closing_turn():
    recorder = Recorder(play_seconds=0)
    _run(recorder, 3, speaker=False)
    assert [closing for _, _, closing in _turns(recorder)] == [False, False, True]


def test_system_prompt_introduces_the_others_and_drops_the_escalation_line():
    prompt = _system_prompt("Mira", "curious", [("Sol", "skeptical"), ("Dave", "quiet")], "cars")
    assert "Sol (skeptical)" in prompt and "Dave (quiet)" in prompt
    assert "by name" in prompt
    assert "escalate" not in prompt
    assert "name label" in prompt


def test_system_prompt_marks_a_direct_question_and_the_closing_turn():
    others = [("Sol", "skeptical")]
    assert "Sol just asked you directly" in _system_prompt("Mira", "curious", others, "cars", addressed_by="Sol")
    assert "last reply" in _system_prompt("Mira", "curious", others, "cars", closing=True)
    plain = _system_prompt("Mira", "curious", others, "cars")
    assert "asked you directly" not in plain and "last reply" not in plain


class Interrupting:
    """Fake rng that lets every eager persona cut in, cuts replies as early as allowed, and picks first.

    `rolls` scripts the rolls one by one, for tests that need a particular one to fail; once it runs
    out, every roll comes out as `roll`.
    """

    def __init__(self, roll=0.0, rolls=()):
        self.roll = roll
        self.rolls = list(rolls)

    def random(self):
        return self.rolls.pop(0) if self.rolls else self.roll

    def randint(self, low, high):
        return low

    def choices(self, population, weights):
        return [population[0]]


EAGER_DAVE = {"Mira": 0, "Sol": 0, "Dave": 4}
SPEECHES = {
    1: "Cars are loud and I think the city should ban them for good.",
    2: "Honestly I have heard that one before and it never gets better.",
}


def _relayed(recorder):
    return [event[1] for event in recorder.events if event[0] == "show"]


def test_an_eager_persona_cuts_a_reply_short_and_takes_the_turn():
    recorder = Recorder(play_seconds=0)
    recorder.scripted = dict(SPEECHES)
    _run(recorder, 4, speaker=False, personas=TRIO, rng=Interrupting(), eagerness=EAGER_DAVE)
    assert [name for name, _, _ in _turns(recorder)] == ["Mira", "Sol", "Dave", "Sol"]
    assert _relayed(recorder)[1] == "Sol (skeptical): Honestly I have heard—"
    assert "never gets better" not in " ".join(_relayed(recorder))


def test_the_interrupter_is_told_who_it_cuts_off_and_cuts_in_briefly():
    recorder = Recorder(play_seconds=0)
    recorder.scripted = dict(SPEECHES)
    _run(recorder, 4, speaker=False, personas=TRIO, rng=Interrupting(), eagerness=EAGER_DAVE)
    cutting = [event[1:] for event in recorder.events if event[0] == "cutting"]
    assert cutting == [(None, None), (None, None), ("Sol", None), (None, "Dave")]
    assert [event[1] for event in recorder.events if event[0] == "brief"][2] is True


# The rolls of a run where Dave cuts Sol off on the second turn: Sol's brief roll, then one roll per
# persona that might cut in on Sol, Mira before Dave. Dave's own cut-in is heard out, so the next
# roll is the floor-back one.
BEFORE_FLOOR_BACK = [0.9, 0.9, 0.0]


@pytest.mark.parametrize("floor_back, expected", [(0.5, "Sol"), (0.9, "Mira")])
def test_the_persona_that_was_cut_off_usually_gets_the_floor_back(floor_back, expected):
    recorder = Recorder(play_seconds=0)
    recorder.scripted = dict(SPEECHES)
    rng = Interrupting(roll=0.9, rolls=[*BEFORE_FLOOR_BACK, floor_back])
    _run(recorder, 4, speaker=False, personas=TRIO, rng=rng, eagerness=EAGER_DAVE)
    assert [name for name, _, _ in _turns(recorder)] == ["Mira", "Sol", "Dave", expected]


def test_a_reply_too_short_to_cut_is_not_interrupted():
    recorder = Recorder(play_seconds=0)
    recorder.scripted = {1: SPEECHES[1], 2: "No."}
    _run(recorder, 3, speaker=False, personas=TRIO, rng=Interrupting(), eagerness=EAGER_DAVE)
    assert _relayed(recorder)[1] == "Sol (skeptical): No."
    assert [name for name, _, _ in _turns(recorder)] == ["Mira", "Sol", "Mira"]


def test_an_interrupter_takes_the_floor_from_the_persona_the_reply_called_on():
    recorder = Recorder(play_seconds=0)
    recorder.scripted = {1: SPEECHES[1], 2: "That is just wrong, Dave, what would you do about it?"}
    eagerness = {"Mira": 4, "Sol": 0, "Dave": 0}
    _run(recorder, 3, speaker=False, personas=TRIO, rng=Interrupting(), eagerness=eagerness)
    assert _turns(recorder)[2] == ("Mira", None, True)


def test_the_opening_and_closing_turns_are_never_cut_short():
    recorder = Recorder(play_seconds=0)
    recorder.scripted = {1: SPEECHES[1], 2: SPEECHES[2], 3: SPEECHES[1]}
    eagerness = {name: 4 for name, _, _ in TRIO}
    _run(recorder, 3, speaker=False, personas=TRIO, rng=Interrupting(), eagerness=eagerness)
    shown = _relayed(recorder)
    assert shown[0].endswith(SPEECHES[1]) and shown[2].endswith(SPEECHES[1])
    assert shown[1].endswith("—")


def test_a_reply_that_cuts_in_is_heard_out():
    recorder = Recorder(play_seconds=0)
    recorder.scripted = {1: SPEECHES[1], 2: SPEECHES[2], 3: SPEECHES[1]}
    eagerness = {name: 4 for name, _, _ in TRIO}
    _run(recorder, 4, speaker=False, personas=TRIO, rng=Interrupting(), eagerness=eagerness)
    # Mira cuts Sol off, so Mira's own reply stands whole and Sol picks its point back up after it.
    assert [name for name, _, _ in _turns(recorder)] == ["Mira", "Sol", "Mira", "Sol"]
    assert _relayed(recorder)[2].endswith(SPEECHES[1])


def test_nothing_is_interrupted_without_eagerness_ratings():
    recorder = Recorder(play_seconds=0)
    recorder.scripted = dict(SPEECHES)
    _run(recorder, 4, speaker=False, personas=TRIO, rng=Interrupting())
    assert "—" not in " ".join(_relayed(recorder))


def test_system_prompt_binds_the_mood_and_names_the_language_last():
    mood = "practical and skeptical, you always speak Dutch"
    others = [("Mira", "curious")]
    assert f"follow it exactly: {mood}" in _system_prompt("Jeroen", mood, others, "cars")

    # A known language is stated outright, and last, because that is the instruction replies drift from.
    told = _system_prompt("Jeroen", mood, others, "cars", closing=True, language="Dutch")
    assert told.endswith("Write this reply in Dutch, whatever language the others are using, "
                         "and do not add a translation.")
    # Without one, the persona is left to follow whatever its mood says.
    untold = _system_prompt("Jeroen", mood, others, "cars")
    assert "Write this reply in" not in untold
    assert "If you are told to speak a particular language" in untold


def test_system_prompt_marks_cutting_in_and_being_cut_off():
    others = [("Sol", "skeptical")]
    assert "cutting Sol off" in _system_prompt("Mira", "curious", others, "cars", interrupting="Sol")
    assert "Sol just talked over you" in _system_prompt("Mira", "curious", others, "cars", cut_off_by="Sol")
    plain = _system_prompt("Mira", "curious", others, "cars")
    assert "cutting" not in plain and "talked over" not in plain


def _conversation_args(turns):
    return argparse.Namespace(api_token="secret", agent_specs=["Mira|curious", "Sol|skeptical", "Dave|quiet"],
                              turns=turns, read_aloud=False, interruptions=False)


def test_at_least_two_turns_are_required():
    with pytest.raises(RuntimeError, match="at least 2"):
        asyncio.run(run_conversation(_conversation_args(1)))


def test_unaddressed_middle_turns_may_be_brief_but_the_opening_answers_and_closing_never_are():
    recorder = Recorder(play_seconds=0)
    recorder.scripted = {1: "Dave, thoughts?"}
    _run(recorder, 4, speaker=False, personas=TRIO, rng=FirstChoice(roll=0.0))
    briefs = [event[1] for event in recorder.events if event[0] == "brief"]
    # Opening, Dave answering a question, a free middle turn, the closing turn.
    assert briefs == [False, False, True, False]


def test_system_prompt_asks_for_the_chosen_length():
    others = [("Sol", "skeptical")]
    assert "very short" in _system_prompt("Mira", "curious", others, "cars", brief=True)
    normal = _system_prompt("Mira", "curious", others, "cars")
    assert "two or three sentences" in normal and "very short" not in normal
