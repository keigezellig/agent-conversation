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


def _system_prompt(name: str, peer: str, mood: str, subject: str) -> str:
    return (
        f"You are {name}, one of several AI agents having a natural group conversation with {peer}. "
        f"The subject is: {subject}. Your conversational mood is: {mood}. "
        "Respond directly to what the group has said, add a useful thought, and ask a question "
        "when it helps the conversation continue. Keep each reply to a few sentences. "
        "Do not claim personal experiences or repeat points just to fill space."
    )


def _append_history(history: list[dict], role: str, content: str) -> None:
    if history and history[-1]["role"] == role:
        history[-1]["content"] += f"\n\n{content}"
    else:
        history.append({"role": role, "content": content})


def _personas(args: argparse.Namespace) -> list[tuple[str, str]]:
    if args.agent_specs:
        if any((args.name_a, args.name_b, args.mood_a, args.mood_b)):
            raise RuntimeError("use repeated --agent options or --name-a/--name-b, not both")
        personas = []
        for spec in args.agent_specs:
            if "|" not in spec:
                raise RuntimeError(f"invalid --agent {spec!r}; expected NAME|MOOD")
            name, mood = (part.strip() for part in spec.split("|", 1))
            if not name or not mood:
                raise RuntimeError("each --agent must have a non-empty name and mood")
            personas.append((name, mood))
    else:
        personas = [
            (args.name_a or "Curious", args.mood_a or "curious and open-minded"),
            (args.name_b or "Skeptic", args.mood_b or "thoughtful and skeptical"),
        ]

    names = [name for name, _ in personas]
    if len(personas) < 2:
        raise RuntimeError("provide at least two agents")
    if len(set(names)) != len(names):
        raise RuntimeError("agent names must be unique")
    return personas


async def run_conversation(args: argparse.Namespace) -> str:
    token = args.api_token
    if not token:
        raise RuntimeError("set API_TOKEN or pass --api-token")
    personas = _personas(args)
    if args.turns < len(personas):
        raise RuntimeError("--turns must be at least the number of agents so everyone can speak")
    names = tuple(name for name, _ in personas)

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

        for turn in range(args.turns):
            name, mood = personas[turn % len(personas)]
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
                {"role": "system", "content": _system_prompt(name, ", ".join(peers), mood, args.subject)},
                *history,
                ],
            )
            _append_history(history, "assistant", response)
            print(f"{name} ({mood}): {response}", flush=True)

            sender = sockets[name]
            await _send_text(sender, response, turn + 1)
            deliveries = await asyncio.gather(
                *(_receive_type(sockets[peer], "message") for peer in peers)
            )
            for peer, delivered in zip(peers, deliveries):
                _append_history(histories[peer], "user", f"{name}: {delivered['payload']['text']}")

    return room["id"]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Have configurable AI agents talk through agent-bridge.")
    parser.add_argument("--subject", required=True, help="Any topic for the agents to discuss")
    parser.add_argument("--agent", dest="agent_specs", action="append", help="Agent persona as NAME|MOOD; repeat for each agent")
    parser.add_argument("--name-a", help="Legacy name for the first agent")
    parser.add_argument("--name-b", help="Legacy name for the second agent")
    parser.add_argument("--mood-a", help="Legacy mood for the first agent")
    parser.add_argument("--mood-b", help="Legacy mood for the second agent")
    parser.add_argument("--turns", type=int, default=8, help="Number of replies to generate (default: 8)")
    parser.add_argument("--bridge-url", default=os.getenv("AGENT_BRIDGE_URL", "http://localhost:8080"))
    parser.add_argument("--api-token", default=os.getenv("API_TOKEN", ""))
    parser.add_argument("--llm-base-url", default=os.getenv("LLM_BASE_URL", "http://localhost:11434/v1"))
    parser.add_argument("--model", default=os.getenv("LLM_MODEL", "llama3.2"))
    parser.add_argument("--llm-api-key", default=os.getenv("LLM_API_KEY", ""))
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