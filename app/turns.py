"""Turn-taking: who gives the next reply in a runner conversation, and who cuts one short."""

import re

# Words after which a name followed by a comma still opens the sentence: "but Dave, what…".
_LEAD_WORDS = {"and", "but", "so", "oh", "well", "hey", "okay", "ok", "look", "now", "right"}

# Interruptions. A persona's eagerness is rated from its mood, 0 for one that always waits its turn
# up to EAGERNESS_SCALE for one that talks over everyone, and it is the only thing that decides how
# often that persona cuts in. Every other persona rolls against its own eagerness on every reply, so
# a bigger group interrupts more, and the step per point stays small: three calm personas cut in on
# about one reply in twelve, five pushy ones on about half.
EAGERNESS_SCALE = 4
DEFAULT_EAGERNESS = 2
INTERRUPT_STEP = 0.04  # chance of cutting in on a reply, per point of eagerness
FLOOR_BACK = 2 / 3  # chance the persona who was cut off takes the turn after the interrupter's

# Where a cut-off reply stops: part-way in, on a word boundary, with the sentence left unfinished. A
# reply shorter than this is over before anyone could cut in, so it is never interrupted.
MIN_CUT_WORDS = 6
CUT_FROM, CUT_TO = 0.4, 0.8
DASH = "—"
_TRAILING = ",.!?…;:\"'’”)]}*_—–-"


def without_label(text: str, names) -> str:
    """Remove leading transcript-style speaker labels ("Henk: …") that models copy from the history."""
    label = re.compile(rf"^[\W_]*(?:{'|'.join(map(re.escape, names))})[*_]*:\s*", re.IGNORECASE)
    stripped = text
    while (match := label.match(stripped)) and stripped[match.end():].strip():
        stripped = stripped[match.end():]
    return stripped


def _opens_sentence(before: str) -> bool:
    before = before.rstrip(" \t*_\"'")
    if not before or before[-1] in ".!?:;\n—–-(":
        return True
    words = re.findall(r"\w+", before[-20:])
    return bool(words) and words[-1].lower() in _LEAD_WORDS and before[-1].isalpha()


def _is_called_on(text: str, start: int, end: int) -> bool:
    """A name is a vocative when it is set off by a comma: "Sol, …", "…, Dave?" or "…, Sol, …"."""
    before = text[:start].rstrip(" \t*_")
    after = text[end:].lstrip("*_")
    if after.startswith((",", "?", "!")) and _opens_sentence(text[:start]):
        return True
    return before.endswith(",") and re.match(r"\s*(?:[?!.,…]|$)", after) is not None


def _sentences(text: str) -> list[tuple[int, bool]]:
    """Return (end, is_question) for each non-blank sentence of the text."""
    sentences, start = [], 0
    for match in re.finditer(r"[.!?…]+(?=[\"')*_]*(?:\s|$))|\n\s*\n", text):
        if text[start:match.start()].strip():
            sentences.append((match.end(), "?" in match.group()))
        start = match.end()
    if text[start:].strip():
        sentences.append((len(text), False))
    return sentences


def _hands_over(sentences: list[tuple[int, bool]], position: int) -> bool:
    """A call hands over the turn when its sentence is a question or only questions follow it.

    That separates "Sol, what do you think?" from "Great point, Sol. Anyway, …".
    """
    index = next((i for i, (end, _) in enumerate(sentences) if end > position), len(sentences) - 1)
    return all(is_question for _, is_question in sentences[index + 1:]) or sentences[index][1]


def addressee(text: str, speaker: str, names) -> str | None:
    """Return the persona this reply hands the turn to by name, the last one if several; None if nobody."""
    sentences = _sentences(text)
    found = None
    for name in names:
        if name == speaker:
            continue
        for match in re.finditer(rf"(?<![\w']){re.escape(name)}(?![\w'])", text):
            if (_is_called_on(text, match.start(), match.end()) and _hands_over(sentences, match.start())
                    and (found is None or match.start() > found[0])):
                found = (match.start(), name)
    return found[1] if found else None


def pick_interrupter(speaker: str, names, eagerness: dict[str, int], rng) -> str | None:
    """Return the persona that cuts in on the speaker's reply, or None if nobody does.

    Every other persona would cut in with a chance set by its eagerness; when several would, the most
    eager one gets there first, and between equally eager ones the one listed first.
    """
    def rating(name: str) -> int:
        return eagerness.get(name, DEFAULT_EAGERNESS)

    would = [name for name in names
             if name != speaker and rng.random() < rating(name) * INTERRUPT_STEP]
    return max(would, key=rating, default=None)


def cut_off(text: str, rng) -> str | None:
    """Return the reply as far as it got before being cut off, or None if it is too short to cut.

    The cut falls on a word boundary part-way in and leaves the sentence unfinished, so the reply
    reads and sounds like someone talked over it.
    """
    words = text.split()
    if len(words) < MIN_CUT_WORDS:
        return None
    kept = rng.randint(max(2, int(len(words) * CUT_FROM)), max(2, int(len(words) * CUT_TO)))
    heard = " ".join(words[:kept]).rstrip(_TRAILING).strip()
    return f"{heard}{DASH}" if heard else None


def pick_next(speaker: str, names, last_turns: dict[str, int], rng) -> str:
    """Pick anyone but the current speaker at random, favouring whoever has been silent longest.

    `last_turns` maps each persona who has spoken to the index of their most recent turn;
    the current speaker's entry is the turn just taken.
    """
    turn = last_turns[speaker]
    candidates = [name for name in names if name != speaker]
    weights = [turn - last_turns.get(name, -1) for name in candidates]
    return rng.choices(candidates, weights)[0]
