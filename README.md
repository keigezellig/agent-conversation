# agent-bridge

A small, provider-neutral communication bridge for groups of software agents. It uses HTTP to create a conversation and WebSockets to exchange JSON messages. Agents do not need to use the same framework, model provider, or programming language; they only need to implement the documented protocol.

```text
agent A ── WebSocket ──┐
agent B ── WebSocket ──┼── agent-bridge
agent C ── WebSocket ──┘
```

## Run

```bash
cp .env.example .env
# Set API_TOKEN in .env to a strong secret.
docker compose up -d --build
curl http://localhost:8080/healthz
```

## Create a conversation

Create a room for two or more distinct agent names:

```bash
curl -X POST http://localhost:8080/conversations \
  -H "Authorization: Bearer $API_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"participants":["planner","researcher","reviewer"]}'
```

The response contains an `id` and WebSocket path. Pass the ID and each agent's own participant name to that agent. Every participant connects to the same path, for example:

```text
ws://localhost:8080/conversations/CONVERSATION_ID/ws?participant=planner
ws://localhost:8080/conversations/CONVERSATION_ID/ws?participant=researcher
ws://localhost:8080/conversations/CONVERSATION_ID/ws?participant=reviewer
```

Each WebSocket must include `Authorization: Bearer $API_TOKEN`. Once connected, the agent receives a `ready` event listing its peers. Each participant can have one active connection per conversation.

## Message protocol

An agent sends a JSON object with `type` set to `message` and any JSON value in `payload`:

```json
{"type":"message","payload":{"task":"Find the latest deployment status","context":{"project":"alpha"}}}
```

Every other connected participant receives the payload with sender, its own recipient name, message ID, and timestamp:

```json
{
  "type": "message",
  "id": "...",
  "from": "planner",
  "to": "reviewer",
  "sent_at": "2026-10-08T12:00:00+00:00",
  "payload": {"task": "Find the latest deployment status", "context": {"project": "alpha"}}
}
```

The sender receives an `accepted` event listing the recipients. If someone is not connected yet, the bridge queues up to 1,000 messages for that participant and delivers them when they join. Other events are `peer_connected`, `peer_disconnected`, and `error`. `GET /conversations/{id}` returns connection and queue status; it also requires the bearer token.

The bridge only transports messages. The agents decide how to interpret them, when to respond, and when their conversation is complete.

## Run an AI group conversation

The included runner creates a group, connects each agent through the bridge, and lets them take turns the way a group would. Set the subject and give each agent a name and free-form mood. The default model is local Ollama, so no hosted AI account is required.

Start the optional local model and download a model once:

```bash
docker compose --profile local-model up -d ollama
docker compose exec ollama ollama pull llama3.2
```

Start a conversation:

```bash
docker compose --profile agents run --build --rm agent-chat \
  --subject "Whether cities should ban private cars" \
  --agent "Mira|curious and optimistic" \
  --agent "Sol|practical and skeptical" \
  --agent "Dave|quiet observer who occasionally offers a wry remark" \
  --turns 9
```

Repeat `--agent "NAME|MOOD"` for every participant (a mood cannot contain `|`); at least two are required and names must be unique. Change `--subject` to discuss anything. `--turns` is the total number of replies (at least 2). The runner prints each reply and exits when the limit is reached.

The first agent listed opens. Each agent knows the others' names and moods. When a reply calls on someone by name ("Sol, …" or "…, what do you think, Dave?"), that agent answers next; a name merely mentioned ("what Mira said") or used to acknowledge someone before moving on ("Great point, Mira. Anyway, …") does not count. Otherwise the next speaker is picked at random, never the one who just spoke, with better odds for whoever has been silent longest, so an agent nobody calls on may stay quiet. Pass `--seed N` to make those picks repeatable. The agent taking the last turn is asked to bring the conversation to a close.

The runner accepts any OpenAI-compatible chat-completions endpoint. Set `LLM_BASE_URL`, `LLM_MODEL`, and optionally `LLM_API_KEY` in `.env` to use another local server or a hosted provider. It connects to the bridge using `API_TOKEN` and `AGENT_BRIDGE_URL`.

## Read the conversation aloud

With `--read-aloud`, the runner speaks each reply as it is shown, giving every agent its own voice. The next agent composes its reply while the current one is speaking, so there is little silence between replies. Playback is live on the host's speakers through PulseAudio or PipeWire (Linux); there is no recording.

Start the optional local speech engine (Kokoro, CPU-only) and add the `TTS_*` settings from `.env.example` to `.env`:

```bash
docker compose --profile local-tts up -d kokoro
```

Then run the conversation with the `agent-chat-voice` service, which mounts your audio socket (`$XDG_RUNTIME_DIR/pulse/native`) and passes `--read-aloud`:

```bash
docker compose --profile agents run --build --rm agent-chat-voice \
  --subject "Whether cities should ban private cars" \
  --agent "Mira|curious and optimistic|af_heart" \
  --agent "Sol|practical and skeptical|am_michael" \
  --agent "Dave|quiet observer who occasionally offers a wry remark" \
  --turns 9
```

The optional third part of `--agent` picks that agent's voice. Agents without one get the next unused voice from `TTS_VOICES`; if there are more agents than voices, voices repeat and the runner prints a warning. Voice names depend on the speech engine.

Before the conversation starts, the runner checks every voice with the speech engine and checks that it can reach the audio server, and exits with an error if either fails. If speaking a single reply fails later, that reply is printed followed by `[not spoken: REASON]` and the conversation continues. Agents are told their replies are spoken, and markdown, emoji, and an agent's own name prefix are removed from the spoken audio; the printed text is unchanged.

`TTS_BASE_URL`, `TTS_MODEL`, and optionally `TTS_API_KEY` accept any OpenAI-compatible `/v1/audio/speech` endpoint, such as another local server or a hosted provider. The endpoint must support the `wav` response format.

## Develop

Run the tests in Docker:

```bash
docker build --target test -t agent-bridge-test . && docker run --rm agent-bridge-test
```

## Deployment notes

Conversation state and queued messages are held in memory. Run one bridge process; restarting it drops active conversations and queued messages. Use HTTPS/WSS and keep `API_TOKEN` private when agents connect across a network. The bridge has no telephony, speech, or model-provider dependency; read-aloud lives only in the runner.
