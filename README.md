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

The included runner creates a group, connects each agent through the bridge, and lets them take turns the way a group would. Set the subject and give each agent a name and free-form mood. The default model is local Ollama, so no hosted AI account is required — start it once, or pick another provider from [which model writes the replies](#which-model-writes-the-replies):

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

Repeat `--agent "NAME|MOOD"` for every participant (a mood cannot contain `|`); at least two are required and names must be unique. A mood is free text and is treated as instructions the agent follows, not just as flavour. Change `--subject` to discuss anything. `--turns` is the total number of replies (at least 2). The runner prints each reply and exits when the limit is reached.

The first agent listed opens. Each agent knows the others' names and moods. When a reply calls on someone by name ("Sol, …" or "…, what do you think, Dave?"), that agent answers next; a name merely mentioned ("what Mira said") or used to acknowledge someone before moving on ("Great point, Mira. Anyway, …") does not count. Otherwise the next speaker is picked at random, never the one who just spoke, with better odds for whoever has been silent longest, so an agent nobody calls on may stay quiet. Pass `--seed N` to make those picks repeatable. The agent taking the last turn is asked to bring the conversation to a close.

### Agents that write in their own language

Say it in the mood and the agent writes in that language:

```bash
docker compose --profile agents run --build --rm agent-chat \
  --subject "Who was to blame for the breakup of Yugoslavia?" \
  --agent "Santa|a talk show host who tries to keep order" \
  --agent "Milorad|a serbian nationalist who writes Serbian answers, arrogant and angry" \
  --agent "Hrvoje|a croat nationalist, aggressive and hostile" \
  --turns 12
```

Before the first reply, the runner asks the model which language each mood names and prints what it found:

```
Languages: Milorad Serbian
```

Every turn that agent takes then ends with the instruction outright — *"Write this reply in Serbian, whatever language the others are using, and do not add a translation."* That last part matters more than it looks. A language instruction buried in a long mood loses against the conversation itself: everything an agent reads is in the others' language, and a model follows the language it is given. Stating it per turn, last, is what makes it stick.

This costs one extra model request per run. If it fails, the runner warns and leaves the language to the moods, and nothing in the conversation stops.

Two things to know:

- Mood decides what an agent *writes*; a voice only decides how it *sounds*. They are set separately, so for read-aloud say the language in the mood **and** give that agent a matching voice (see [which voice each agent gets](#which-voice-each-agent-gets)) — otherwise you get Serbian read in an American accent.
- An agent whose mood names no language just follows the conversation, which is what you want for everyone else.

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

### Which model writes the replies

Two model APIs, picked with `LLM_API`, each with its own defaults so that choosing one is usually a single line. `.env.example` has a block per provider — copy one into `.env`.

| `LLM_API` | Talks to | Default endpoint | Default model | Key |
|---|---|---|---|---|
| `openai` (default) | `POST /chat/completions` | `http://localhost:11434/v1` | `llama3.2` | optional |
| `claude` | `POST /messages` | `https://api.anthropic.com/v1` | `claude-sonnet-5` | required |

Only the agents' replies go to the provider; the bridge itself never talks to one.

#### Provider 1 of 2: a local OpenAI-compatible server

The default, and the reason the project needs no AI account. The bundled Ollama service is the two commands [above](#run-an-ai-group-conversation); nothing else needs setting.

The same setting reaches any other server speaking chat completions — another local one, or a hosted provider — by pointing `LLM_BASE_URL` at it and naming its model in `LLM_MODEL`, with `LLM_API_KEY` if it wants a bearer token.

One thing to watch with local models: each reply gets 120 seconds. A big model on CPU can take longer than that per turn and the run fails on its first reply, so prefer a small model unless you have a GPU.

#### Provider 2 of 2: the Claude API

Four lines in `.env`, and no local model needed:

```bash
LLM_API=claude
LLM_BASE_URL=
LLM_MODEL=
ANTHROPIC_API_KEY=sk-ant-...
```

Leaving the middle two empty takes `https://api.anthropic.com/v1` and `claude-sonnet-5`. Fill them in for something else, such as `LLM_MODEL=claude-opus-5` or a gateway of your own. Nothing on the command line changes — the earlier `agent-chat` example runs as it is.

For a one-off run without editing `.env`, export the key and pass the settings as flags. Naming `-e ANTHROPIC_API_KEY` without a value forwards it from your shell, so the key stays out of the command:

```bash
export ANTHROPIC_API_KEY=sk-ant-...

docker compose --profile agents run --build --rm -e ANTHROPIC_API_KEY agent-chat \
  --llm-api claude \
  --model claude-sonnet-5 \
  --subject "Whether cities should ban private cars" \
  --agent "Mira|curious and optimistic" \
  --agent "Sol|practical and skeptical" \
  --turns 9
```

Three mistakes worth naming, because none of them fails in an obvious way:

- **`LLM_API=claude` is what selects the Claude API.** Pointing `LLM_BASE_URL` at Anthropic is not enough: left as `openai`, the runner sends a chat-completions request with a bearer token and gets a confusing error back.
- **`LLM_BASE_URL` is a base, not the endpoint.** Use `https://api.anthropic.com/v1`; the runner appends `/messages` itself, so a URL ending in `/messages` becomes `/v1/messages/messages` and 404s.
- **`LLM_MODEL` must be a current model ID**, such as `claude-sonnet-5`, `claude-opus-5` or `claude-haiku-4-5-20251001`. An invented or retired ID is rejected by the API, not by the runner.

#### Every model setting

The flag names do not all match their settings, so both are spelled out. The flag wins over the setting.

| Setting | Flag | Default | What it does |
|---|---|---|---|
| `LLM_API` | `--llm-api` | `openai` | Which API to speak: `openai` or `claude` |
| `LLM_BASE_URL` | `--llm-base-url` | per API, see above | Where that API lives; empty takes the default |
| `LLM_MODEL` | `--model` | per API, see above | Which model writes the replies; empty takes the default |
| `LLM_API_KEY` | `--llm-api-key` | empty | The provider's key. Required for `claude`, optional otherwise |
| `ANTHROPIC_API_KEY` | — | empty | Accepted as the Claude key too, under its conventional name. `LLM_API_KEY` wins if both are set |
| `LLM_MAX_TOKENS` | `--llm-max-tokens` | `1024` | Most tokens one reply may use. The Claude API requires a limit; chat completions ignore it |
| `API_TOKEN` | `--api-token` | none | The bridge's bearer token, not the model's |
| `AGENT_BRIDGE_URL` | `--bridge-url` | `http://localhost:8080` | Which bridge to hold the conversation on |

#### How many requests a run makes

One per turn, so `--turns 9` is nine requests however many agents there are, plus one at the start to read the agents' languages from their moods, plus one more for the interruption ratings with `--interruptions`. Both of those extras fail soft: the runner warns and carries on without that feature rather than losing the conversation.

The key is checked before the conversation is created, so a missing or wrong one fails immediately rather than part-way through. Read-aloud is unaffected by this choice — the provider writes the replies, the speech engine voices them.

## Read the conversation aloud

With `--read-aloud`, the runner speaks each reply as it is shown, giving every agent its own voice. The next agent composes its reply while the current one is speaking, so there is little silence between replies. Playback is live on the host's speakers through PulseAudio or PipeWire (Linux); there is no recording.

A reply is spoken in sentence-sized pieces rather than all at once: it starts being heard as soon as its first sentence is ready, while the rest is still being synthesized. On a CPU speech engine this is the difference between a few seconds of silence before each reply and almost none.

Add `--interruptions` and an agent that is cut off is heard breaking off mid-sentence, with the next voice coming straight in. Voices are still never spoken over each other: the interruption is in the words, not in overlapping audio.

Agents are told their replies are spoken, and markdown, emoji and an agent's own name prefix are left out of the audio. What is printed stays exactly as the agent wrote it.

Setting it up is three decisions: which **speech engine** serves the voices, which **voice** each agent gets, and the handful of **settings** that connect the two. `.env.example` has a block per engine — copy one into `.env` and you are done.

The quickest start is the bundled Kokoro engine, whose settings are the defaults:

```bash
docker compose --profile local-tts up -d kokoro

docker compose --profile agents run --build --rm agent-chat-voice \
  --subject "Whether cities should ban private cars" \
  --agent "Mira|curious and optimistic|af_heart" \
  --agent "Sol|practical and skeptical|am_michael" \
  --agent "Dave|quiet observer who occasionally offers a wry remark" \
  --turns 9
```

The `agent-chat-voice` service mounts your audio socket (`$XDG_RUNTIME_DIR/pulse/native`) and passes `--read-aloud` for you.

### Which speech engine

Any endpoint that speaks OpenAI's `POST /v1/audio/speech` will do. Three come with the project, and `TTS_BASE_URL` decides which one a run uses — one endpoint per run, so a cast cannot mix two of them.

| Engine | Voices | Languages | Runs | Needs |
|---|---|---|---|---|
| **Kokoro** (default) | 54 | English, Spanish, French, Italian, Portuguese, Hindi, Japanese, Mandarin | locally, on CPU | nothing; `--profile local-tts` |
| **Speaches** | Kokoro's 54 plus ~124 Piper voices | ~30, most of Europe | locally, on CPU | a model download per voice |
| **edge-tts** | 322 | ~70, including Serbian, Croatian, Bosnian, Albanian | at Microsoft | the internet, `TTS_FORMAT=wav` |

**Kokoro** is the default and needs no setup beyond starting it. Good voices, limited languages.

**Speaches** serves Kokoro *and* Piper, which covers Dutch, German, Polish, Czech, Hungarian, Greek, Swedish, Danish, Finnish, Icelandic, Welsh, Ukrainian and more. Models are fetched on demand, a few seconds each:

```bash
docker compose --profile speaches-tts up -d speaches

curl -X POST http://localhost:8001/v1/models/speaches-ai/Kokoro-82M-v1.0-ONNX-int8
curl -X POST http://localhost:8001/v1/models/speaches-ai/piper-nl_NL-mls-medium
curl -X POST http://localhost:8001/v1/models/speaches-ai/piper-de_DE-thorsten-medium

# browse the catalogue, and check what is installed
curl -s "https://huggingface.co/api/models?author=speaches-ai&limit=300"
curl -s http://localhost:8001/v1/audio/models
```

A voice that is not installed gives HTTP 404, and the runner checks every voice before the conversation starts, so a typo or a forgotten download fails immediately rather than part-way through. Piper is quicker than Kokoro on CPU but noticeably more robotic, so English agents are better left on Kokoro voices. Everything is resampled to 24 kHz, so the default `TTS_SAMPLE_RATE` stays right even though Piper's own models are 16 or 22.05 kHz.

Two gaps to know before picking a Piper voice: **there is no Croatian or Bosnian voice at all**, and the one Serbian voice, `sr_RS-serbski_institut-medium`, is mislabelled — its model card names a [Lower Sorbian](https://github.com/marytts/serbski-institut-dsb-data) dataset recorded at the Sorbian Institute in Germany, finetuned from a US English voice. Serbian text is phonemised as Serbian, so it is intelligible, but the voice is not a Serbian speaker.

**edge-tts** reaches Microsoft Edge's neural voices, which include the ones Piper lacks:

```
sr-RS-NicholasNeural   sr-RS-SophieNeural       Serbian
hr-HR-SreckoNeural     hr-HR-GabrijelaNeural    Croatian
bs-BA-GoranNeural      bs-BA-VesnaNeural        Bosnian
sq-AL-IlirNeural       sq-AL-AnilaNeural        Albanian
```

```bash
docker compose --profile edge-tts up -d --build edge-tts

# every voice it offers, for any language
curl -s -H "Authorization: Bearer your_api_key_here" http://localhost:5050/v1/voices/all \
  | python3 -c "import json,sys; print([v['name'] for v in json.load(sys.stdin)])"
```

Be clear about what this one is: synthesis happens at Microsoft through the endpoint Edge's read-aloud uses, so it needs the internet, it is not an official API, and it can stop working without notice. Two consequences:

- **`TTS_FORMAT=wav` is required.** Asking this server for `pcm` fails outright, and in the published image it answers *every* format with mp3 while labelling it `audio/L16`, which the runner would play as noise. The project's `edge-tts.Dockerfile` adds the ffmpeg that makes `wav` a real 24 kHz file.
- **Expect refusals.** Measured from one machine, Microsoft turned down roughly one request in three with a 403, coming and going with no pattern, on the newest `edge-tts` as much as the pinned one. The runner tries each piece of speech four times, waiting longer each time, and a piece still refused leaves that reply printed with `[not spoken: …]` while the conversation carries on — so expect the odd silent reply over a long run. If 403s turn constant rather than occasional, rebuild the image (`--build`) for a newer `edge-tts`, which is what Microsoft checks.

For a conversation that has to be reliable, the same voices come from Azure's own Speech API with a key — an official endpoint with a free tier, but it does not speak the OpenAI shape, so it would need an adapter this project does not have.

### Which voice each agent gets

The optional third field of `--agent` names it. Agents without one take the next unused voice from `TTS_VOICES`; if there are more agents than voices, voices repeat and the runner warns. Voice names come from the engine, so they look different per engine:

```bash
--agent "Mira|curious and optimistic|af_heart"                               # Kokoro: a voice name
--agent "Jeroen|practical|speaches-ai/piper-nl_NL-mls-medium"                # Piper: a model name
--agent "Milorad|a serbian nationalist|sr-RS-NicholasNeural"                 # edge-tts: a voice name
--agent "Dave|quiet observer"                                                # take one from TTS_VOICES
```

The rule behind the second line: **a voice containing a `/` is a model of its own.** Kokoro keeps many voices in one model, so `af_heart` means "that voice in `TTS_MODEL`"; Piper has one voice per model, so naming the model picks the voice. The runner sends the right model per agent, which is how one conversation can mix Kokoro and Piper voices.

Two things a voice does *not* decide:

- **What language an agent writes in.** That comes from its mood — see [agents that write in their own language](#agents-that-write-in-their-own-language). A Dutch voice reading English text sounds Dutch-accented, not Dutch, so say the language in the mood *and* give that agent a matching voice.
- **The alphabet.** Serbian voices expect Cyrillic and a model will usually answer in Latin, which these voices sound out letter by letter — it comes out wrong enough that listeners hear another language entirely. The runner transliterates for the Serbian voices of either engine (`sr-RS-…`, `piper-sr_RS-…`) and for nothing else, since Croatian and Bosnian are written in Latin. Only the audio changes:

  ```
  printed:  Nismo mi počeli ovaj rat. Svi to znaju, Hrvoje.
  spoken:   Нисмо ми почели овај рат. Сви то знају, Хрвоје.
  ```

  Keeping the printed reply as written is what lets that agent still hand the floor to someone by name. An agent that writes Serbian without diacritics has already lost the letters they carried, since "c" can no longer be told from "č" or "ć".

### Every speech setting

Each has a flag of the same name, so `TTS_SPEED=1.4` and `--tts-speed 1.4` are the same thing. The flag wins.

| Setting | Default | What it does |
|---|---|---|
| `TTS_BASE_URL` | `http://kokoro:8880/v1` | Which engine to use; any OpenAI-compatible `/v1/audio/speech` |
| `TTS_MODEL` | `kokoro` | The model to ask for, for voices that do not name one themselves |
| `TTS_API_KEY` | empty | Bearer token, when the endpoint wants one (`edge-tts` does) |
| `TTS_VOICES` | six Kokoro voices | Voices handed out to agents without their own, in order |
| `TTS_FORMAT` | `pcm` | `pcm`, or `wav` for an engine that cannot produce raw samples |
| `TTS_SPEED` | `1.1` | How quickly the voices speak, 0.25 to 4.0; above 1.0 also synthesizes quicker |
| `TTS_SAMPLE_RATE` | `24000` | Must match the engine's audio. A WAV file recorded at another rate is refused, naming the rate to set |

The runner sends only the standard fields (`model`, `input`, `voice`, `response_format`, `speed`), so no engine-specific extensions are needed; splitting a reply into pieces and pacing them is its own work.

### What happens when speech fails

Before the conversation starts, the runner synthesizes a word in every voice and checks it can reach the audio server, and exits with an error naming the voice if either fails. After that, a reply whose speech fails — all four tries refused, a voice withdrawn, the engine restarted — is printed followed by `[not spoken: REASON]`, and the conversation carries on.

## Watch the conversation in a window

The same run, in a window instead of the terminal. `agent-chat-ui` takes exactly the flags
`agent-chat` takes, read-aloud included, and shows the cast beside the replies as they arrive:

```bash
docker compose --profile agents run --build --rm agent-chat-ui \
  --subject "Whether cities should ban private cars" \
  --agent "Santa|a talk show host keeping order|af_heart" \
  --agent "Richard|arrogant and takes no criticism|am_michael" \
  --agent "Dan|aggressive and childish|am_fenrir" \
  --agent "Joyce|annoyingly happy and off-topic|af_bella" \
  --interruptions \
  --read-aloud \
  --turns 20
```

Down the left is the cast: each agent's mood, the eagerness rating it was given, the language its
mood asks for, and the voice it speaks in. The agent speaking is lit and marked, and the count beside
each name is how many replies it has got in — an agent nobody calls on stays dark all run.

The replies themselves read as a transcript. An interrupted one breaks off on an orange dash and is
marked `cut off`; the reply that took the floor from it is marked `cuts in`. A reply that could not be
spoken keeps its `[not spoken: …]` line. The bar underneath says which turn the conversation is on and
who is speaking.

With `--read-aloud` the lit agent is the one you are hearing, because a reply is shown at the moment
it starts being spoken. Without it the window is the same, minus the audio.

When a conversation closes, **Hold another conversation** runs the same cast and subject again in a new
tab. It stays disabled while one is running, so two conversations never speak over each other.

The window needs your display, and the service is set up for it: it passes `DISPLAY` through,
mounts your X cookie (`$XAUTHORITY`, or `~/.Xauthority` when that is unset), and runs as your user,
because under Wayland the Xwayland socket only lets its owner connect. If your user id isn't 1000,
run it with `UID=$(id -u) GID=$(id -g) docker compose ...`. To run it on the host instead of in Docker, it needs
nothing but the dependencies in `requirements.txt` and a Python with tkinter:

```bash
python3 -m app.ui --subject "..." --agent "..." --agent "..." --turns 12
```

## Develop

Run the tests in Docker:

```bash
docker build --target test -t agent-bridge-test . && docker run --rm agent-bridge-test
```

## Deployment notes

Conversation state and queued messages are held in memory. Run one bridge process; restarting it drops active conversations and queued messages. Use HTTPS/WSS and keep `API_TOKEN` private when agents connect across a network. The bridge has no telephony, speech, or model-provider dependency; read-aloud lives only in the runner.
