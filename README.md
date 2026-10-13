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

### Letting the agents interrupt each other

With `--interruptions`, an agent can cut another one off mid-sentence. Pushy moods cut in more often than patient ones, so the flag shows best with a cast that has a range of them:

```bash
docker compose --profile agents run --build --rm agent-chat \
  --subject "Whether cities should ban private cars" \
  --agent "Santa|a talk show host keeping order" \
  --agent "Richard|arrogant and takes no criticism" \
  --agent "Dan|aggressive and childish" \
  --agent "Joyce|annoyingly happy and off-topic" \
  --interruptions \
  --turns 12
```

The run opens with the rating the model gave each mood, and then the conversation:

```
Eagerness to interrupt: Santa 1/4, Richard 3/4, Dan 4/4, Joyce 2/4
Conversation 7c1f0f1e-… | Subject: Whether cities should ban private cars
Santa (a talk show host keeping order): Welcome back everyone. Richard, you wanted to start?
Richard (arrogant and takes no criticism): I have said this a hundred times and nobody here—
Dan (aggressive and childish): Rubbish, you always do this!
Richard (arrogant and takes no criticism): As I was saying, before I was so rudely cut off, …
```

Everything else stays the same, so `--interruptions` combines with `--read-aloud`, `--seed`, and either model provider.

The interrupted reply stops where it was cut off, on a dash. That shortened reply is all anyone gets: it is what the runner prints, what it speaks with `--read-aloud`, and what the other agents are told was said, so nobody answers a point they never heard — including a question in the part that was cut. The agent who cut in takes the turn, is told whose sentence it is cutting into, and is asked to keep it short; the agent who was cut off usually gets the floor back on the turn after, and is told it was talked over.

The ratings are where the frequency comes from: before the conversation starts, the runner asks the model to judge each mood from 0 ("always waits their turn") to 4 ("talks over everyone"). Every agent but the one speaking then gets its own chance to cut in on each reply, so pushier moods interrupt more often and a bigger group is interrupted more often: three calm agents cut in on about one reply in twelve, five pushy ones on about half. Mood is the only thing that decides this — it never changes an agent's odds in the random pick or how long its replies are.

Three turns are never cut short: the opening one, the closing one, and a reply that is itself an interruption. The last of those keeps two pushy agents from cutting each other off turn after turn, which otherwise leaves the conversation without a single finished thought. A reply of a few words is over before anyone can get in, so it stands as well.

This costs one extra model request per run, for the ratings. If that request fails, the runner warns and gives every agent the middle rating rather than giving up on the conversation.

### Model providers

The runner speaks two model APIs, chosen with `LLM_API` (or `--llm-api`):

| `LLM_API` | Endpoint | Default `LLM_BASE_URL` | Default `LLM_MODEL` | Key |
|---|---|---|---|---|
| `openai` (default) | `POST /chat/completions` | `http://localhost:11434/v1` | `llama3.2` | `LLM_API_KEY` as a bearer token, optional |
| `claude` | `POST /messages` | `https://api.anthropic.com/v1` | `claude-sonnet-5` | `LLM_API_KEY` or `ANTHROPIC_API_KEY`, required |

Either way, set `LLM_BASE_URL`, `LLM_MODEL`, and optionally `LLM_API_KEY` to point at another local server or a hosted provider; leave one empty to take the chosen API's default. `LLM_MAX_TOKENS` (default 1024) caps a reply — the Claude API requires a limit, and chat completions ignore it. The runner connects to the bridge using `API_TOKEN` and `AGENT_BRIDGE_URL`.

#### Running the personas on the Claude API

Set these four lines in `.env` (the `agent-chat` service reads it):

```bash
LLM_API=claude
LLM_BASE_URL=
LLM_MODEL=
ANTHROPIC_API_KEY=sk-ant-...
```

Leaving `LLM_BASE_URL` and `LLM_MODEL` empty falls back to `https://api.anthropic.com/v1` and `claude-sonnet-5`. Fill them in to choose something else, such as `LLM_MODEL=claude-opus-5` or a gateway of your own. No local model is needed, so you can skip the Ollama steps above.

Three things to get right, because none of them fails in an obvious way:

- `LLM_API=claude` is what selects the Claude API. Pointing `LLM_BASE_URL` at Anthropic is not enough on its own: left as `openai`, the runner would send a chat-completions request with a bearer token and get a confusing error back.
- `LLM_BASE_URL` is a base, not the endpoint. Use `https://api.anthropic.com/v1`; the runner appends `/messages` itself, so a URL ending in `/messages` becomes `/v1/messages/messages` and 404s.
- `LLM_MODEL` must be a current model ID, such as `claude-sonnet-5`, `claude-opus-5`, or `claude-haiku-4-5-20251001`. An invented or retired ID is rejected by the API, not by the runner.

Then start a conversation the same way as before — nothing on the command line changes:

```bash
docker compose --profile agents run --build --rm agent-chat \
  --subject "Whether cities should ban private cars" \
  --agent "Mira|curious and optimistic" \
  --agent "Sol|practical and skeptical" \
  --agent "Dave|quiet observer who occasionally offers a wry remark" \
  --turns 9
```

For a one-off run without editing `.env`, export the key once and pass the settings as flags. Naming `-e ANTHROPIC_API_KEY` without a value forwards it from your shell, so the key itself stays out of the command:

```bash
export ANTHROPIC_API_KEY=sk-ant-...

docker compose --profile agents run --build --rm -e ANTHROPIC_API_KEY agent-chat \
  --llm-api claude \
  --llm-base-url https://api.anthropic.com/v1 \
  --model claude-sonnet-5 \
  --subject "Whether cities should ban private cars" \
  --agent "Mira|curious and optimistic" \
  --agent "Sol|practical and skeptical" \
  --turns 9
```

Each turn is one request, so `--turns 9` makes nine calls regardless of how many personas there are, plus one for the ratings with `--interruptions`. The runner checks the key before it creates the conversation, so a missing or wrong one fails immediately rather than part-way through. Read-aloud works the same with either provider: Claude writes the replies and the local speech engine voices them.

Only the persona replies go to the model provider; the bridge itself never talks to one.

## Read the conversation aloud

With `--read-aloud`, the runner speaks each reply as it is shown, giving every agent its own voice. The next agent composes its reply while the current one is speaking, so there is little silence between replies. Playback is live on the host's speakers through PulseAudio or PipeWire (Linux); there is no recording.

A reply is spoken in sentence-sized pieces rather than all at once: it starts being heard as soon as its first sentence is ready, while the rest is still being synthesized. On a CPU speech engine this is the difference between a few seconds of silence before each reply and almost none.

Add `--interruptions` and an agent that is cut off is heard breaking off mid-sentence, with the next voice coming straight in. Voices are still never spoken over each other: the interruption is in the words, not in overlapping audio.

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

`TTS_SPEED` (or `--tts-speed`, default 1.1) sets how quickly the voices speak, between 0.25 and 4.0; above 1.0 is also a little quicker to synthesize. `TTS_SAMPLE_RATE` (or `--tts-sample-rate`, default 24000) must match the sample rate of the endpoint's audio.

`TTS_BASE_URL`, `TTS_MODEL`, and optionally `TTS_API_KEY` accept any OpenAI-compatible `/v1/audio/speech` endpoint, such as another local server or a hosted provider. The endpoint must support the `pcm` response format, which both Kokoro and OpenAI return as 24 kHz mono 16-bit audio. The runner sends only standard fields (`model`, `input`, `voice`, `response_format`, `speed`); splitting a reply into pieces and pacing them is the runner's own work, so no engine-specific extensions are required.

## Develop

Run the tests in Docker:

```bash
docker build --target test -t agent-bridge-test . && docker run --rm agent-bridge-test
```

## Deployment notes

Conversation state and queued messages are held in memory. Run one bridge process; restarting it drops active conversations and queued messages. Use HTTPS/WSS and keep `API_TOKEN` private when agents connect across a network. The bridge has no telephony, speech, or model-provider dependency; read-aloud lives only in the runner.
