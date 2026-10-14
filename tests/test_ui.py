import argparse

import pytest

from app.agents import _personas, attribute, conversation_args, labelled, parse_args

CAST = [
    ("Mira", "curious and optimistic", None),
    ("Sol", "practical and skeptical", None),
    ("Dave", "quiet observer", None),
]


def test_a_shown_reply_is_split_back_into_the_persona_and_what_it_said():
    assert attribute(labelled("Sol", "practical and skeptical", "Cars are loud."), CAST) == (
        "Sol", "Cars are loud.")


def test_a_note_about_the_run_belongs_to_nobody():
    for note in ("Languages: Mira Dutch",
                 "Eagerness to interrupt: Mira 1/4, Sol 3/4",
                 "Conversation abc123 | Subject: cars",
                 "[not spoken: TTS timed out]"):
        assert attribute(note, CAST) == (None, note)


def test_a_persona_named_in_a_reply_is_not_taken_for_the_speaker():
    said = "Dave: that is what Sol always says, Mira."
    assert attribute(labelled("Dave", "quiet observer", said), CAST) == ("Dave", said)


def test_a_mood_that_reads_like_a_label_does_not_confuse_the_split():
    cast = [("Mira", "a critic (harsh): never satisfied", None), *CAST]
    line = labelled("Mira", "a critic (harsh): never satisfied", "No.")
    assert attribute(line, cast) == ("Mira", "No.")


def test_an_interrupted_reply_keeps_the_dash_it_broke_off_on():
    assert attribute(labelled("Sol", "practical and skeptical", "I was saying—"), CAST)[1].endswith("—")


BASE = argparse.Namespace(subject=None, agent_specs=None, turns=8, seed=None, interruptions=False,
                          model="claude-haiku-5-5", read_aloud=True, tts_voices="af_heart")
TWO = [("Mira", "curious", ""), ("Sol", "skeptical", "am_michael")]


def set_up(**changes):
    form = dict(subject="Cars in cities", cast=TWO, turns="12", seed="", interruptions=True) | changes
    return conversation_args(BASE, **form)


def test_the_form_sets_the_conversation_and_keeps_the_model_and_the_voices():
    args = set_up()
    assert (args.subject, args.turns, args.seed, args.interruptions) == ("Cars in cities", 12, None, True)
    assert _personas(args) == [("Mira", "curious", None), ("Sol", "skeptical", "am_michael")]
    assert (args.model, args.read_aloud, args.tts_voices) == ("claude-haiku-5-5", True, "af_heart")
    assert BASE.subject is None


def test_the_form_may_appoint_a_leader_or_none():
    assert set_up(leader="Sol").leader == "Sol"
    assert set_up().leader is None


def test_an_empty_row_in_the_cast_is_skipped_and_the_fields_are_trimmed():
    args = set_up(subject="  Cars \n", cast=[(" Mira ", "curious ", " "), ("", "", ""), *TWO[1:]], seed=" 7 ")
    assert args.subject == "Cars"
    assert args.agent_specs == ["Mira|curious|", "Sol|skeptical|am_michael"]
    assert args.seed == 7


@pytest.mark.parametrize("changes, problem", [
    ({"subject": "  \n"}, "subject"),
    ({"cast": TWO[:1]}, "at least two agents"),
    ({"cast": [("Mira", "", ""), *TWO[1:]]}, "a name and a mood"),
    ({"cast": [*TWO, ("Mira", "loud", "")]}, "two agents are called Mira"),
    ({"cast": [("Mira", "a|b", ""), *TWO[1:]]}, "cannot contain |"),
    ({"turns": "1"}, "turns"),
    ({"leader": "Zed"}, "the leader Zed is not one of the agents"),
    ({"turns": "many"}, "turns"),
    ({"seed": "abc"}, "seed"),
])
def test_the_form_says_what_to_fix(changes, problem):
    with pytest.raises(ValueError, match=problem.replace("|", r"\|")):
        set_up(**changes)


def test_the_window_can_open_without_a_subject_or_a_cast():
    args = parse_args([], need_conversation=False)
    assert args.subject is None and args.agent_specs is None
    with pytest.raises(SystemExit):
        parse_args([])
