"""Run configurable agents through the local WebSocket bridge."""

import argparse
import asyncio
from contextlib import AsyncExitStack
import json
import os
import random
import re
import sys
import urllib.error
import urllib.request
from urllib.parse import quote

import websockets

from .speech import DEFAULT_VOICES, PulsePlayer, SpeechEngine, Speaker, assign_voices
from .turns import (DEFAULT_EAGERNESS, EAGERNESS_SCALE, FLOOR_BACK, addressee, cut_off, pick_interrupter,
                    pick_next, without_label)


def _http_json(url: str, token: str, payload: dict) -> dict:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


def _create_conversation(base_url: str, token: str, names: tuple[str, ...]) -> dict:
    return _http_json(
        f"{base_url.rstrip('/')}/conversations",
        token,
        {"participants": list(names)},
    )


# Which API a model endpoint speaks, and where each one lives by default.
ANTHROPIC_VERSION = "2023-06-01"
API_DEFAULTS = {
    "openai": {"base_url": "http://localhost:11434/v1", "model": "llama3.2"},
    "claude": {"base_url": "https://api.anthropic.com/v1", "model": "claude-sonnet-5"},
}
NO_CLAUDE_KEY = "set ANTHROPIC_API_KEY or pass --llm-api-key to use the Claude API"


def _post_model(url: str, headers: dict, payload: dict) -> dict:
    request = urllib.request.Request(url, data=json.dumps(payload).encode(), headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        detail = error.read().decode(errors="replace")
        raise RuntimeError(f"model endpoint returned HTTP {error.code}: {detail}") from error


def _chat_completion(base_url: str, model: str, api_key: str, system: str, history: list[dict]):
    """Ask for a reply through the OpenAI chat-completions API, which takes the system prompt as a message."""
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    result = _post_model(
        f"{base_url.rstrip('/')}/chat/completions",
        headers,
        {"model": model, "messages": [{"role": "system", "content": system}, *history]},
    )
    try:
        return result["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as error:
        raise RuntimeError("model endpoint returned an invalid chat completion") from error


def _claude_message(base_url: str, model: str, api_key: str, system: str, history: list[dict],
                    max_tokens: int):
    """Ask for a reply through the Claude API, which takes the system prompt beside the messages."""
    if not api_key:
        raise RuntimeError(NO_CLAUDE_KEY)
    result = _post_model(
        f"{base_url.rstrip('/')}/messages",
        {
            "Content-Type": "application/json",
            "x-api-key": api_key,
            "anthropic-version": ANTHROPIC_VERSION,
        },
        {"model": model, "max_tokens": max_tokens, "system": system, "messages": history},
    )
    try:
        return "".join(block["text"] for block in result["content"] if block.get("type") == "text")
    except (KeyError, TypeError) as error:
        raise RuntimeError("model endpoint returned an invalid message") from error


def _complete(api: str, base_url: str, model: str, api_key: str, system: str, history: list[dict],
              max_tokens: int) -> str:
    if api == "claude":
        content = _claude_message(base_url, model, api_key, system, history, max_tokens)
    else:
        content = _chat_completion(base_url, model, api_key, system, history)
    if not isinstance(content, str) or not content.strip():
        raise RuntimeError("model endpoint returned an empty response")
    return content.strip()


RATER_PROMPT = "You rate characters for a group conversation. Judge only from the personality given."


def _rate_eagerness(api: str, base_url: str, model: str, api_key: str, personas,
                    max_tokens: int) -> dict[str, int]:
    """Rate from each persona's mood how readily it cuts the others off, 0 to EAGERNESS_SCALE.

    One request before the conversation starts. A rating that cannot be read falls back to the middle,
    so an unhelpful answer costs some realism but never the run.
    """
    described = "\n".join(f"{name}: {mood}" for name, mood, _ in personas)
    question = (
        f"How readily does each of these characters cut someone off mid-sentence?\n\n{described}\n\n"
        f"Rate each one from 0 (always waits their turn) to {EAGERNESS_SCALE} (talks over everyone). "
        'Answer with one line per character, "NAME: NUMBER", and nothing else.'
    )
    try:
        answer = _complete(api, base_url, model, api_key, RATER_PROMPT,
                           [{"role": "user", "content": question}], max_tokens)
    except (OSError, RuntimeError) as error:
        print(f"Warning: cannot rate how readily the agents interrupt ({error}); "
              f"using {DEFAULT_EAGERNESS} for everyone", file=sys.stderr, flush=True)
        return {name: DEFAULT_EAGERNESS for name, _, _ in personas}

    eagerness = {}
    for name, _, _ in personas:
        rated = re.search(rf"^\W*{re.escape(name)}[^\d\n]*(\d+)", answer, re.IGNORECASE | re.MULTILINE)
        eagerness[name] = min(int(rated[1]), EAGERNESS_SCALE) if rated else DEFAULT_EAGERNESS
    return eagerness


NO_LANGUAGE = {"none", "no", "na", "any", "unspecified", "unknown", "nothing", "not"}


def _named_in(language: str, mood: str) -> bool:
    """Is this language actually named in the mood?

    The model reading the moods is free with them: asked which language a Serbian nationalist writes,
    a small one answers Croatian, and it hands a language to characters whose mood says nothing about
    one. A mood that does not name the language cannot have asked for it, whatever the answer says.
    """
    return any(word[:4].lower() in mood.lower() for word in language.split())


def _read_languages(api: str, base_url: str, model: str, api_key: str, personas,
                    max_tokens: int) -> dict[str, str | None]:
    """Read from each persona's mood which language it writes in, or None where the mood is silent.

    One request before the conversation starts, so that every turn can be told its language outright
    instead of hoping the model picks it out of a long mood. If the answer cannot be read, no persona
    gets a language and the mood has to carry it alone.
    """
    described = "\n".join(f"{name}: {mood}" for name, mood, _ in personas)
    question = (
        "Each of these characters has a description. Which language does the description tell the "
        f"character to speak or write in?\n\n{described}\n\n"
        'Answer "none" unless the description actually says to speak or write a language. Where a '
        "character is from, or which people they belong to, is not such an instruction: a Croatian "
        'nationalist with nothing said about language is "none".\n'
        'Answer with one line per character, "NAME: LANGUAGE" or "NAME: none", naming the language '
        "in English and writing nothing else."
    )
    try:
        answer = _complete(api, base_url, model, api_key, RATER_PROMPT,
                           [{"role": "user", "content": question}], max_tokens)
    except (OSError, RuntimeError) as error:
        print(f"Warning: cannot read which language the agents speak ({error}); "
              "leaving that to their moods", file=sys.stderr, flush=True)
        return {name: None for name, _, _ in personas}

    languages = {}
    for name, mood, _ in personas:
        found = re.search(rf"^\W*{re.escape(name)}\W*:\s*(.+)$", answer, re.IGNORECASE | re.MULTILINE)
        # Models pad the line they were asked for: "Milorad: Serbian", "English: English", or the
        # name back with a typo. Whatever label they repeat, the language follows the last colon.
        spoken = found[1].rsplit(":", 1)[-1] if found else ""
        # A language is a word or two ("Serbian", "Brazilian Portuguese"), never a sentence.
        words = [word for word in spoken.strip(" .*_\"'").split() if word.isalpha()]
        named = words[0].lower() if words else ""
        answered = " ".join(words[:2]) if named and named not in NO_LANGUAGE else None
        if answered and (answered.isupper() or answered.islower()):
            answered = answered.title()  # Models answer "SERBIAN" as readily as "Serbian".
        languages[name] = answered if answered and _named_in(answered, mood) else None
    return languages


async def _receive_type(socket, expected: str) -> dict:
    while True:
        event = json.loads(await socket.recv())
        if event.get("type") == "error":
            raise RuntimeError(event.get("message", "bridge error"))
        if event.get("type") == expected:
            return event


async def _send_text(socket, text: str, turn: int) -> None:
    await socket.send(json.dumps({"type": "message", "payload": {"text": text, "turn": turn}}))
    await _receive_type(socket, "accepted")


def _system_prompt(name: str, mood: str, others: list[tuple[str, str]], subject: str, read_aloud: bool = False,
                   addressed_by: str | None = None, closing: bool = False, brief: bool = False,
                   interrupting: str | None = None, cut_off_by: str | None = None,
                   language: str | None = None) -> str:
    people = ", ".join(f"{other} ({other_mood})" for other, other_mood in others)
    prompt = (
        f"You are {name}, one of several AI agents having a natural group conversation. "
        f"The others are {people}. The subject is: {subject}. "
        f"This is who you are, and you follow it exactly: {mood}. "
        "Respond to what the group has said and add a thought of your own. Usually you speak to the whole group; "
        "if you want one particular person to answer, call on them by name. "
        "Write only what you say, without a name label in front. "
        "Do not repeat points just to fill space. Keep your wording informal and very simple."
    )
    if brief:
        prompt += " Keep this reply very short: a few words or one sentence, like a quick reaction."
    else:
        prompt += " Keep this reply to two or three sentences."
    if read_aloud:
        prompt += (
            " Your replies are spoken aloud: write plain spoken sentences, no markdown, lists, or emoji. "
            "Talk the way people actually talk, in contractions and short clauses."
        )
    if addressed_by:
        prompt += f" {addressed_by} just asked you directly; answer them."
    if interrupting:
        prompt += (f" You are cutting {interrupting} off mid-sentence, before they are done talking: "
                   "butt straight in with your reaction and do not be polite about it.")
    if cut_off_by:
        prompt += (f" {cut_off_by} just talked over you and cut you off mid-sentence; "
                   "pick your point back up.")
    if closing:
        prompt += " This is the last reply of the conversation: bring it to a natural close and do not ask anyone anything."
    # Last, because this is the instruction a reply drifts away from: everything the persona reads is
    # in the language the others happen to use, and a model follows the conversation it is given.
    if language:
        prompt += (f" Write this reply in {language}, whatever language the others are using, "
                   "and do not add a translation.")
    else:
        prompt += (" If you are told to speak a particular language, write every reply in that language, "
                   "even when the others write in a different one, and never add a translation.")
    return prompt


def _append_history(history: list[dict], role: str, content: str) -> None:
    if history and history[-1]["role"] == role:
        history[-1]["content"] += f"\n\n{content}"
    else:
        history.append({"role": role, "content": content})


def _personas(args: argparse.Namespace) -> list[tuple[str, str, str | None]]:
    personas = []
    for spec in args.agent_specs or []:
        parts = [part.strip() for part in spec.split("|")]
        if len(parts) not in (2, 3):
            raise RuntimeError(f"invalid --agent {spec!r}; expected NAME|MOOD or NAME|MOOD|VOICE")
        name, mood, voice = (*parts, "")[:3]
        if not name or not mood:
            raise RuntimeError("each --agent must have a non-empty name and mood")
        personas.append((name, mood, voice or None))

    names = [name for name, _, _ in personas]
    if len(personas) < 2:
        raise RuntimeError("provide at least two agents with --agent NAME|MOOD")
    if len(set(names)) != len(names):
        raise RuntimeError("agent names must be unique")
    return personas


BRIEF_SHARE = 1 / 3

# How a reply is shown, and how to read one back. A presenter other than the terminal has only these
# lines to go on, so the format is written and parsed in one place rather than guessed at.
NOT_SPOKEN = "[not spoken: {}]"


def labelled(name: str, mood: str, text: str) -> str:
    return f"{name} ({mood}): {text}"


def attribute(line: str, personas) -> tuple[str | None, str]:
    """Split a shown line into (persona, reply), or (None, line) for a note the runner made.

    The labels are built from the same personas, so they are matched in full: a mood that happens to
    read like a label, or a persona named in the reply itself, cannot be mistaken for the speaker.
    """
    for name, mood, _ in personas:
        label = labelled(name, mood, "")
        if line.startswith(label):
            return name, line[len(label):]
    return None, line


async def _take_turns(personas, turns: int, compose, relay, speaker=None, show=print, rng=None,
                      eagerness: dict[str, int] | None = None) -> None:
    """Have personas take turns, showing each reply and reading it aloud when a speaker is given.

    The first persona opens. Whoever a reply calls on by name speaks next; otherwise the next persona
    is picked at random, favouring the longest silent. The last turn closes the conversation.
    About a third of the other unaddressed turns ask for a brief reply.
    With an eagerness rating per persona, any turn but the opening, the closing one, and one that is
    itself a cut-in may be cut off by a persona butting in: the reply is cut short mid-sentence and
    only that much is shown, spoken, and relayed, the interrupter takes the next turn, and the persona
    who was cut off usually takes the one after that to pick its point back up.
    A reply is shown when it starts being spoken. The next reply may be composed while the current
    one is spoken, but composing never runs more than one reply ahead of what has been heard.
    A reply starts being spoken as soon as its opening piece is synthesized, not when all of it is.
    """
    rng = rng or random.Random()
    moods = {name: mood for name, mood, _ in personas}
    names = tuple(moods)
    ready = asyncio.Queue()
    lookahead = asyncio.Semaphore(1)

    async def produce() -> None:
        name, addressed_by, interrupting, cut_off_by, last_turns = names[0], None, None, None, {}
        for turn in range(turns):
            mood = moods[name]
            closing = turn == turns - 1
            # Butting in is a quick reaction by nature, so an interrupter always gets the brief prompt.
            brief = bool(interrupting) or (
                turn > 0 and not closing and not addressed_by and not cut_off_by
                and rng.random() < BRIEF_SHARE
            )
            await lookahead.acquire()
            text = without_label(
                await compose(turn, name, mood, addressed_by, closing, brief, interrupting, cut_off_by),
                names,
            )
            interrupter, heard = None, None
            # A reply that is itself a cut-in is heard out: otherwise two pushy personas cut each
            # other off turn after turn and the conversation never gets a finished sentence in.
            if eagerness and turn > 0 and not closing and not interrupting:
                interrupter = pick_interrupter(name, names, eagerness, rng)
                heard = cut_off(text, rng) if interrupter else None
                # A reply too short to cut was over before anyone could get a word in.
                interrupter = interrupter if heard else None
            text = heard or text
            await relay(turn, name, text)
            last_turns[name] = turn
            # An interrupter takes the floor uninvited, so nobody this reply called on gets it.
            called_on = None if interrupter else addressee(text, name, names)
            if interrupter:
                next_name = interrupter
            elif called_on:
                next_name = called_on
            elif interrupting and rng.random() < FLOOR_BACK:
                next_name = interrupting  # the persona cut off here usually gets the floor back
            else:
                next_name = pick_next(name, names, last_turns, rng)
            clip, problem = None, None
            if speaker:
                try:
                    clip = await asyncio.to_thread(speaker.open, name, text)
                except (OSError, RuntimeError) as error:
                    problem = str(error) or type(error).__name__
            await ready.put((name, mood, text, clip, problem))
            if interrupter:
                addressed_by, interrupting, cut_off_by = None, name, None
            elif next_name == interrupting:
                addressed_by, interrupting, cut_off_by = None, None, name
            else:
                addressed_by, interrupting, cut_off_by = (name if called_on else None), None, None
            name = next_name
        await ready.put(None)

    async def present() -> None:
        while (reply := await ready.get()) is not None:
            name, mood, text, clip, problem = reply
            show(labelled(name, mood, text))
            lookahead.release()
            if clip is not None:
                try:
                    await asyncio.to_thread(speaker.speak, clip)
                except (OSError, RuntimeError) as error:
                    problem = str(error) or type(error).__name__
            if problem:
                show(NOT_SPOKEN.format(problem))

    try:
        async with asyncio.TaskGroup() as group:
            group.create_task(produce())
            group.create_task(present())
    except ExceptionGroup as error:
        raise error.exceptions[0] from None
    except asyncio.CancelledError:
        # Stopped: playback runs in a thread that cancelling does not reach, so the reply being
        # spoken would otherwise play out to its end.
        if speaker:
            speaker.hush()
        raise


def _speaker(args: argparse.Namespace, personas: list[tuple[str, str, str | None]]) -> Speaker:
    available = [voice.strip() for voice in args.tts_voices.split(",") if voice.strip()]
    voices = assign_voices(personas, available)
    shared = sorted({voice for voice in voices.values() if list(voices.values()).count(voice) > 1})
    if shared:
        print(f"Warning: personas share voices: {', '.join(shared)}", file=sys.stderr, flush=True)
    return Speaker(
        SpeechEngine(args.tts_base_url, args.tts_model, args.tts_api_key, args.tts_speed,
                     args.tts_format),
        PulsePlayer(args.tts_sample_rate),
        voices,
    )


async def run_conversation(args: argparse.Namespace, show=None, on_start=None) -> str:
    """Hold one conversation, handing every line it produces to `show`.

    `show` defaults to the terminal, and `on_start` is handed the cast and the settings the run
    settled on once the conversation exists, for a presenter that wants them as values rather than
    as the lines that announce them.
    """
    show = show or (lambda line: print(line, flush=True))
    token = args.api_token
    if not token:
        raise RuntimeError("set API_TOKEN or pass --api-token")
    personas = _personas(args)
    if args.turns < 2:
        raise RuntimeError("--turns must be at least 2")
    if args.llm_api == "claude" and not args.llm_api_key:
        raise RuntimeError(NO_CLAUDE_KEY)
    if args.llm_max_tokens < 1:
        raise RuntimeError("--llm-max-tokens must be at least 1")
    names = tuple(name for name, _, _ in personas)
    speaker = None
    if args.read_aloud:
        if not 0.25 <= args.tts_speed <= 4.0:
            raise RuntimeError("--tts-speed must be between 0.25 and 4.0")
        if args.tts_sample_rate < 1:
            raise RuntimeError("--tts-sample-rate must be a positive number of hertz")
        speaker = _speaker(args, personas)
        await asyncio.to_thread(speaker.check)

    languages = await asyncio.to_thread(
        _read_languages, args.llm_api, args.llm_base_url, args.model, args.llm_api_key, personas,
        args.llm_max_tokens,
    )
    if spoken := {name: language for name, language in languages.items() if language}:
        show("Languages: " + ", ".join(f"{name} {language}" for name, language in spoken.items()))

    eagerness = None
    if args.interruptions:
        eagerness = await asyncio.to_thread(
            _rate_eagerness, args.llm_api, args.llm_base_url, args.model, args.llm_api_key, personas,
            args.llm_max_tokens,
        )
        show("Eagerness to interrupt: "
             + ", ".join(f"{name} {rating}/{EAGERNESS_SCALE}" for name, rating in eagerness.items()))

    room = await asyncio.to_thread(
        _create_conversation,
        args.bridge_url,
        token,
        names,
    )
    socket_base = args.bridge_url.replace("https://", "wss://", 1).replace("http://", "ws://", 1)
    socket_url = f"{socket_base.rstrip('/')}{room['websocket']}"
    headers = {"Authorization": f"Bearer {token}"}
    histories = {name: [] for name in names}

    if on_start:
        on_start({
            "id": room["id"],
            "subject": args.subject,
            "model": args.model,
            "personas": personas,
            "languages": languages,
            "eagerness": eagerness,
            "turns": args.turns,
            "read_aloud": speaker is not None,
        })
    show(f"Conversation {room['id']} | Subject: {args.subject}")
    async with AsyncExitStack() as stack:
        sockets = {}
        for name in names:
            url = f"{socket_url}?participant={quote(name, safe='')}"
            sockets[name] = await stack.enter_async_context(
                websockets.connect(url, additional_headers=headers)
            )
        await asyncio.gather(*(_receive_type(sockets[name], "ready") for name in names))

        async def compose(turn: int, name: str, mood: str, addressed_by: str | None, closing: bool,
                          brief: bool, interrupting: str | None = None,
                          cut_off_by: str | None = None) -> str:
            others = [(other, other_mood) for other, other_mood, _ in personas if other != name]
            history = histories[name]
            if not history:
                _append_history(
                    history,
                    "user",
                    f"Start a conversation about {args.subject}. Open with a thought or question.",
                )
            response = await asyncio.to_thread(_complete,
                args.llm_api,
                args.llm_base_url,
                args.model,
                args.llm_api_key,
                _system_prompt(
                    name, mood, others, args.subject, args.read_aloud, addressed_by, closing, brief,
                    interrupting, cut_off_by, languages.get(name),
                ),
                list(history),
                args.llm_max_tokens,
            )
            _append_history(history, "assistant", response)
            return response

        async def relay(turn: int, name: str, text: str) -> None:
            peers = [peer for peer in names if peer != name]
            await _send_text(sockets[name], text, turn + 1)
            deliveries = await asyncio.gather(
                *(_receive_type(sockets[peer], "message") for peer in peers)
            )
            for peer, delivered in zip(peers, deliveries):
                _append_history(histories[peer], "user", f"{name}: {delivered['payload']['text']}")

        await _take_turns(personas, args.turns, compose, relay, speaker,
                          show=show, rng=random.Random(args.seed),
                          eagerness=eagerness)

    return room["id"]


def parse_args(argv=None, need_conversation: bool = True) -> argparse.Namespace:
    """The run's settings. The window asks for the subject and the cast itself, so it passes
    `need_conversation=False` and takes whatever the command line gives it as a starting point."""
    parser = argparse.ArgumentParser(description="Have configurable AI agents talk through agent-bridge.")
    parser.add_argument("--subject", required=need_conversation, help="Any topic for the agents to discuss")
    parser.add_argument("--agent", dest="agent_specs", action="append", help="Agent persona as NAME|MOOD or NAME|MOOD|VOICE; repeat for each agent")
    parser.add_argument("--turns", type=int, default=8, help="Number of replies to generate (default: 8)")
    parser.add_argument("--seed", type=int, help="Seed for picking who speaks next when nobody is called on")
    parser.add_argument("--interruptions", action="store_true",
                        help="Let the agents cut each other off mid-sentence, the pushier moods more often")
    parser.add_argument("--bridge-url", default=os.getenv("AGENT_BRIDGE_URL", "http://localhost:8080"))
    parser.add_argument("--api-token", default=os.getenv("API_TOKEN", ""))
    parser.add_argument("--llm-api", choices=tuple(API_DEFAULTS), default=os.getenv("LLM_API") or "openai",
                        help="Which API the model endpoint speaks (default: openai)")
    parser.add_argument("--llm-base-url", default=os.getenv("LLM_BASE_URL"),
                        help="Default: local Ollama, or the Claude API with --llm-api claude")
    parser.add_argument("--model", default=os.getenv("LLM_MODEL"),
                        help="Default: llama3.2, or claude-sonnet-5 with --llm-api claude")
    parser.add_argument("--llm-api-key", default=os.getenv("LLM_API_KEY") or os.getenv("ANTHROPIC_API_KEY", ""))
    parser.add_argument("--llm-max-tokens", type=int, default=int(os.getenv("LLM_MAX_TOKENS") or 1024),
                        help="Most tokens a reply may use; the Claude API requires a limit (default: 1024)")
    parser.add_argument("--read-aloud", action="store_true", help="Speak each reply aloud as it is shown")
    parser.add_argument("--tts-base-url", default=os.getenv("TTS_BASE_URL", "http://localhost:8880/v1"))
    parser.add_argument("--tts-model", default=os.getenv("TTS_MODEL", "kokoro"))
    parser.add_argument("--tts-api-key", default=os.getenv("TTS_API_KEY", ""))
    parser.add_argument("--tts-voices", default=os.getenv("TTS_VOICES") or DEFAULT_VOICES,
                        help="Comma-separated voices for personas without their own")
    parser.add_argument("--tts-speed", type=float, default=float(os.getenv("TTS_SPEED") or 1.1),
                        help="How quickly the voices speak, 0.25 to 4.0 (default: 1.1)")
    parser.add_argument("--tts-sample-rate", type=int, default=int(os.getenv("TTS_SAMPLE_RATE") or 24000),
                        help="Sample rate in hertz of the speech endpoint's raw audio (default: 24000)")
    parser.add_argument("--tts-format", choices=("pcm", "wav"), default=os.getenv("TTS_FORMAT") or "pcm",
                        help="What to ask the speech endpoint for; wav is for engines that cannot do pcm")
    args = parser.parse_args(argv)
    defaults = API_DEFAULTS[args.llm_api]
    args.llm_base_url = args.llm_base_url or defaults["base_url"]
    args.model = args.model or defaults["model"]
    return args


def conversation_args(base: argparse.Namespace, subject: str, cast, turns: str, seed: str,
                      interruptions: bool) -> argparse.Namespace:
    """The settings for one conversation set up in the window, over the run's own in `base`.

    `cast` is a (name, mood, voice) row per persona as typed, voice "" for one from the pool; a row
    left wholly empty is skipped. The model and the voices are not asked for here: they stay as the
    command line set them. Raises ValueError saying what to fix.
    """
    subject = subject.strip()
    if not subject:
        raise ValueError("give the conversation a subject")
    specs, names = [], []
    for row in cast:
        name, mood, voice = (field.strip() for field in row)
        if not (name or mood or voice):
            continue
        if not (name and mood):
            raise ValueError("every agent needs a name and a mood")
        if any("|" in field for field in (name, mood, voice)):
            raise ValueError("a name, mood or voice cannot contain |")
        if name in names:
            raise ValueError(f"two agents are called {name}")
        names.append(name)
        specs.append(f"{name}|{mood}|{voice}")
    if len(specs) < 2:
        raise ValueError("a conversation needs at least two agents")
    try:
        turns = int(str(turns).strip())
    except ValueError:
        turns = 0
    if turns < 1:
        raise ValueError("turns must be a whole number of at least 1")
    seed = str(seed or "").strip()
    try:
        seed = int(seed) if seed else None
    except ValueError:
        raise ValueError("the seed must be a whole number, or left empty") from None
    return argparse.Namespace(**{**vars(base), "subject": subject, "agent_specs": specs,
                                 "turns": turns, "seed": seed, "interruptions": interruptions})


def main() -> None:
    args = parse_args()
    try:
        asyncio.run(run_conversation(args))
    except (OSError, RuntimeError, urllib.error.URLError, websockets.WebSocketException) as error:
        print(f"Agent conversation failed: {error}", file=sys.stderr)
        raise SystemExit(1) from error


if __name__ == "__main__":
    main()