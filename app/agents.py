"""Run configurable agents through the local WebSocket bridge."""

import argparse
import asyncio
from contextlib import AsyncExitStack
import json
import os
import sys
import urllib.error
import urllib.request
from urllib.parse import quote

import websockets

from .speech import DEFAULT_VOICES, PulsePlayer, SpeechEngine, Speaker, assign_voices


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


def _complete(base_url: str, model: str, api_key: str, messages: list[dict]) -> str:
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    request = urllib.request.Request(
        f"{base_url.rstrip('/')}/chat/completions",
        data=json.dumps({"model": model, "messages": messages}).encode(),
        headers=headers,
    )
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            result = json.load(response)
    except urllib.error.HTTPError as error:
        detail = error.read().decode(errors="replace")
        raise RuntimeError(f"model endpoint returned HTTP {error.code}: {detail}") from error

    try:
        content = result["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as error:
        raise RuntimeError("model endpoint returned an invalid chat completion") from error
    if not isinstance(content, str) or not content.strip():
        raise RuntimeError("model endpoint returned an empty response")
    return content.strip()


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


def _system_prompt(name: str, peer: str, mood: str, subject: str, read_aloud: bool = False) -> str:
    prompt = (
        f"You are {name}, one of several AI agents having a natural group conversation with {peer}. "
        f"The subject is: {subject}. Your conversational mood is: {mood}. "
        "Respond directly to what the group has said, add a useful thought, and ask a question "
        "when it helps the conversation continue. Keep each reply to a few sentences. "
        "Do not claim personal experiences or repeat points just to fill space. Use informal language. The conversation will escalate quickly if peers are aggressive."
    )
    if read_aloud:
        prompt += " Your replies are spoken aloud: write plain spoken sentences, no markdown, lists, or emoji."
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


async def _take_turns(personas, turns: int, compose, relay, speaker=None, show=print) -> None:
    """Have personas reply in turn order, showing each reply and reading it aloud when a speaker is given.

    A reply is shown when it starts being spoken. The next reply may be composed while the current
    one is spoken, but composing never runs more than one reply ahead of what has been heard.
    """
    ready = asyncio.Queue()
    lookahead = asyncio.Semaphore(1)

    async def produce() -> None:
        for turn in range(turns):
            name, mood, _ = personas[turn % len(personas)]
            await lookahead.acquire()
            text = await compose(turn, name, mood)
            await relay(turn, name, text)
            audio, problem = None, None
            if speaker:
                try:
                    audio = await asyncio.to_thread(speaker.synthesize, name, text)
                except (OSError, RuntimeError) as error:
                    problem = str(error) or type(error).__name__
            await ready.put((name, mood, text, audio, problem))
        await ready.put(None)

    async def present() -> None:
        while (reply := await ready.get()) is not None:
            name, mood, text, audio, problem = reply
            show(f"{name} ({mood}): {text}")
            lookahead.release()
            if audio is not None:
                try:
                    await asyncio.to_thread(speaker.play, audio)
                except (OSError, RuntimeError) as error:
                    problem = str(error) or type(error).__name__
            if problem:
                show(f"[not spoken: {problem}]")

    try:
        async with asyncio.TaskGroup() as group:
            group.create_task(produce())
            group.create_task(present())
    except ExceptionGroup as error:
        raise error.exceptions[0] from None


def _speaker(args: argparse.Namespace, personas: list[tuple[str, str, str | None]]) -> Speaker:
    available = [voice.strip() for voice in args.tts_voices.split(",") if voice.strip()]
    voices = assign_voices(personas, available)
    shared = sorted({voice for voice in voices.values() if list(voices.values()).count(voice) > 1})
    if shared:
        print(f"Warning: personas share voices: {', '.join(shared)}", file=sys.stderr, flush=True)
    return Speaker(SpeechEngine(args.tts_base_url, args.tts_model, args.tts_api_key), PulsePlayer(), voices)


async def run_conversation(args: argparse.Namespace) -> str:
    token = args.api_token
    if not token:
        raise RuntimeError("set API_TOKEN or pass --api-token")
    personas = _personas(args)
    if args.turns < len(personas):
        raise RuntimeError("--turns must be at least the number of agents so everyone can speak")
    names = tuple(name for name, _, _ in personas)
    speaker = None
    if args.read_aloud:
        speaker = _speaker(args, personas)
        await asyncio.to_thread(speaker.check)

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

    print(f"Conversation {room['id']} | Subject: {args.subject}", flush=True)
    async with AsyncExitStack() as stack:
        sockets = {}
        for name in names:
            url = f"{socket_url}?participant={quote(name, safe='')}"
            sockets[name] = await stack.enter_async_context(
                websockets.connect(url, additional_headers=headers)
            )
        await asyncio.gather(*(_receive_type(sockets[name], "ready") for name in names))

        async def compose(turn: int, name: str, mood: str) -> str:
            peers = [peer for peer in names if peer != name]
            history = histories[name]
            if not history:
                _append_history(
                    history,
                    "user",
                    f"Start a conversation about {args.subject}. Open with a thought or question.",
                )
            response = await asyncio.to_thread(_complete,
                args.llm_base_url,
                args.model,
                args.llm_api_key,
                [
                {"role": "system", "content": _system_prompt(
                    name, ", ".join(peers), mood, args.subject, args.read_aloud
                )},
                *history,
                ],
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
                          show=lambda line: print(line, flush=True))

    return room["id"]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Have configurable AI agents talk through agent-bridge.")
    parser.add_argument("--subject", required=True, help="Any topic for the agents to discuss")
    parser.add_argument("--agent", dest="agent_specs", action="append", help="Agent persona as NAME|MOOD or NAME|MOOD|VOICE; repeat for each agent")
    parser.add_argument("--turns", type=int, default=8, help="Number of replies to generate (default: 8)")
    parser.add_argument("--bridge-url", default=os.getenv("AGENT_BRIDGE_URL", "http://localhost:8080"))
    parser.add_argument("--api-token", default=os.getenv("API_TOKEN", ""))
    parser.add_argument("--llm-base-url", default=os.getenv("LLM_BASE_URL", "http://localhost:11434/v1"))
    parser.add_argument("--model", default=os.getenv("LLM_MODEL", "llama3.2"))
    parser.add_argument("--llm-api-key", default=os.getenv("LLM_API_KEY", ""))
    parser.add_argument("--read-aloud", action="store_true", help="Speak each reply aloud as it is shown")
    parser.add_argument("--tts-base-url", default=os.getenv("TTS_BASE_URL", "http://localhost:8880/v1"))
    parser.add_argument("--tts-model", default=os.getenv("TTS_MODEL", "kokoro"))
    parser.add_argument("--tts-api-key", default=os.getenv("TTS_API_KEY", ""))
    parser.add_argument("--tts-voices", default=os.getenv("TTS_VOICES") or DEFAULT_VOICES,
                        help="Comma-separated voices for personas without their own")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    try:
        asyncio.run(run_conversation(args))
    except (OSError, RuntimeError, urllib.error.URLError, websockets.WebSocketException) as error:
        print(f"Agent conversation failed: {error}", file=sys.stderr)
        raise SystemExit(1) from error


if __name__ == "__main__":
    main()