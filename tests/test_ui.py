from app.agents import attribute, labelled

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
