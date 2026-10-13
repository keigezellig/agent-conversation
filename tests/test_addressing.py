import pytest

from app.turns import MIN_CUT_WORDS, addressee, cut_off, pick_interrupter, pick_next, without_label

NAMES = ("Mira", "Sol", "Dave")


@pytest.mark.parametrize("speaker, text, expected", [
    ("Mira", "Sol, you can't be serious.", "Sol"),
    ("Sol", "That's a fair point. What do you think, Dave?", "Dave"),
    ("Sol", "I agree with what Mira said about buses.", None),
    ("Sol", "I agree with Mira, buses are great.", None),
    ("Sol", "Mira, I hear you, but Dave, what would you actually ban?", "Dave"),
    ("Mira", "And you, Sol, where do you stand?", "Sol"),
    ("Sol", "Honestly? **Dave**, come on.", "Dave"),
    ("Sol", "What do you two think? Anyone?", None),
    ("Sol", "Sol: Mira, you're wrong about that.", "Mira"),
    ("Mira", "Thanks for that.\n\nSol, any thoughts", "Sol"),
    # Acknowledging someone by name before going on does not hand them the turn.
    ("Sol", "Those are great points, Mira. While transit helps, many people rely on cars.", None),
    ("Mira", "Mira, are you serious? I think cars are fine.", None),
    ("Sol", "Mira, are you serious? I think cars are fine.", "Mira"),
    ("Mira", "Great direction, Dave. Incentives could help. Sol, I'd love your view. How would you design it?", "Sol"),
    ("Dave", "Fair, Mira. But Sol, what about deliveries?", "Sol"),
])
def test_addressee_is_the_last_persona_called_on_by_name(speaker, text, expected):
    assert addressee(text, speaker, NAMES) == expected


def test_a_persona_cannot_address_itself():
    assert addressee("Mira, stop talking to yourself.", "Mira", NAMES) is None


def test_names_inside_other_words_are_not_addresses():
    assert addressee("Solar power, Dave?", "Mira", NAMES) == "Dave"
    assert addressee("Solar, though.", "Mira", NAMES) is None


class FakeRandom:
    def __init__(self, pick=0):
        self.calls = []
        self.pick = pick

    def choices(self, population, weights):
        self.calls.append((list(population), list(weights)))
        return [population[self.pick]]


def test_fallback_never_picks_the_last_speaker_and_favours_the_longest_silent():
    rng = FakeRandom()
    # Mira spoke on turn 0, Sol on turn 1, Dave never.
    pick_next("Sol", NAMES, {"Mira": 0, "Sol": 1}, rng)
    assert rng.calls == [(["Mira", "Dave"], [1, 2])]


def test_fallback_returns_the_random_choice():
    assert pick_next("Mira", NAMES, {"Mira": 0}, FakeRandom(pick=1)) == "Dave"


class FakeRolls:
    """Fake rng whose rolls all come out the same, and whose cut lands where asked."""

    def __init__(self, roll=0.0, keep="low"):
        self.roll = roll
        self.keep = keep
        self.ranges = []

    def random(self):
        return self.roll

    def randint(self, low, high):
        self.ranges.append((low, high))
        return low if self.keep == "low" else high


SPEECH = "I have said this before and I will say it again, cars are simply here to stay."


def test_a_cut_off_reply_stops_part_way_in_with_the_sentence_unfinished():
    assert cut_off(SPEECH, FakeRolls()) == "I have said this before and—"
    assert cut_off(SPEECH, FakeRolls(keep="high")) == "I have said this before and I will say it again, cars are—"


def test_the_cut_falls_between_two_fifths_and_four_fifths_of_the_way_in():
    rng = FakeRolls()
    cut_off(SPEECH, rng)
    assert rng.ranges == [(6, 13)]  # the reply is seventeen words long


def test_a_cut_takes_the_punctuation_of_the_last_word_it_keeps():
    # Mid-sentence, the comma goes; on a sentence end, the full stop goes, so nothing sounds finished.
    assert cut_off("Cars are loud. Cars are also here to stay, whatever you say.", FakeRolls(keep="high")) == (
        "Cars are loud. Cars are also here to stay—"
    )
    assert cut_off("Cars are loud and bad. Buses win.", FakeRolls(keep="high")) == "Cars are loud and bad—"


def test_a_reply_too_short_to_cut_is_left_alone():
    assert cut_off(" ".join(["word"] * (MIN_CUT_WORDS - 1)), FakeRolls()) is None
    assert cut_off("No.", FakeRolls()) is None


def test_nobody_cuts_in_on_a_persona_nobody_is_eager_to_interrupt():
    eagerness = {"Mira": 0, "Sol": 0, "Dave": 0}
    assert pick_interrupter("Mira", NAMES, eagerness, FakeRolls()) is None


def test_the_most_eager_persona_cuts_in_and_never_the_speaker_itself():
    eagerness = {"Mira": 4, "Sol": 1, "Dave": 3}
    assert pick_interrupter("Sol", NAMES, eagerness, FakeRolls()) == "Mira"
    assert pick_interrupter("Mira", NAMES, eagerness, FakeRolls()) == "Dave"


def test_eagerness_sets_how_often_a_persona_cuts_in():
    eagerness = {"Mira": 4, "Sol": 2, "Dave": 0}
    # A roll Mira cuts in on is still too high for the half as eager Sol, and Dave never cuts in.
    assert pick_interrupter("Dave", NAMES, eagerness, FakeRolls(roll=0.1)) == "Mira"
    assert pick_interrupter("Mira", NAMES, eagerness, FakeRolls(roll=0.1)) is None
    assert pick_interrupter("Mira", NAMES, eagerness, FakeRolls(roll=0.05)) == "Sol"
    assert pick_interrupter("Sol", NAMES, eagerness, FakeRolls(roll=0.2)) is None


@pytest.mark.parametrize("text, expected", [
    ("Henk: Are you serious, Truus?", "Are you serious, Truus?"),
    ("**Mira**: Fair point.", "Fair point."),
    ("Mira: Sol: well, maybe.", "well, maybe."),
    ("mira:  lowercase label", "lowercase label"),
    ("Sol, you said cars: why?", "Sol, you said cars: why?"),
    ("Dave:", "Dave:"),
])
def test_leading_speaker_labels_are_removed(text, expected):
    assert without_label(text, ("Mira", "Sol", "Dave", "Henk")) == expected
