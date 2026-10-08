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

The included runner creates a group, connects each agent through the bridge, and rotates through them in order. Set the subject and give each agent a name and free-form mood. The default model is local Ollama, so no hosted AI account is required.

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

Repeat `--agent "NAME|MOOD"` for every participant; at least two are required and names must be unique. Change `--subject` to discuss anything. `--turns` limits total replies and must be at least the number of agents so everyone speaks once. The runner prints each reply and exits when the limit is reached. The older `--name-a`/`--mood-a` and `--name-b`/`--mood-b` options remain available for two-agent conversations.

The runner accepts any OpenAI-compatible chat-completions endpoint. Set `LLM_BASE_URL`, `LLM_MODEL`, and optionally `LLM_API_KEY` in `.env` to use another local server or a hosted provider. It connects to the bridge using `API_TOKEN` and `AGENT_BRIDGE_URL`.

## Deployment notes

Conversation state and queued messages are held in memory. Run one bridge process; restarting it drops active conversations and queued messages. Use HTTPS/WSS and keep `API_TOKEN` private when agents connect across a network. There is no telephony, speech, or model-provider dependency.
