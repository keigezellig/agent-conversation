import pytest

from app.turns import addressee, pick_next, without_label

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
