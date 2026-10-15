# agent-bridge

A provider-neutral relay that lets a group of software agents exchange messages, plus a runner that stages AI group conversations through it and can read them aloud.

## Language

### Bridge

**Conversation**:
A room on the bridge with a fixed set of named participants, through which messages are relayed.
_Avoid_: Room, session, chat

**Participant**:
A named member of a conversation that can connect, send messages, and receive every other participant's messages.
_Avoid_: Peer (except when meaning "the other participants"), member, client

### Runner

**Runner**:
The program that creates a conversation, drives the agents' turns, and presents the conversation to the person watching.
_Avoid_: Client, orchestrator

**Persona**:
An AI agent in a runner conversation, defined by a name, a mood, and a voice.
_Avoid_: Bot, character

**Reply**:
One persona's turn: the text it contributes to the conversation.
_Avoid_: Response, utterance

**Turn**:
One step of a runner conversation in which exactly one **Persona** gives a **Reply**. Who takes the next turn follows from the conversation so far, not from a fixed rotation.
_Avoid_: Round, slot

**Leader**:
The **Persona** appointed talk show host for a **Conversation**: told it hosts, it opens the **Conversation** and closes it. At most one per **Conversation**, and none unless asked for on a given run.
_Avoid_: Moderator, chair, host (as a domain term)

**Tone**:
The register a whole **Conversation** is held in ("formal and academic", "a heated debate"), told to every **Persona** alike. Without one, every **Persona** keeps its wording informal and very simple; none unless asked for on a given run.
_Avoid_: Style, register, atmosphere

**Addressee**:
The **Persona** a **Reply** hands the floor to by calling on them by name ("Sol, …" or "…, Dave?"), as opposed to merely mentioning them ("what Mira said") or acknowledging them before moving on ("Great point, Mira. Anyway, …"). A call hands over the floor when it is a question or only questions follow it. When a **Reply** calls on several, the last one called on is the addressee. The addressee takes the next **Turn**.
_Avoid_: Target, recipient (the bridge's per-delivery `to` is a different thing)

**Interruption**:
A **Turn** cut short because another **Persona** starts talking before it is finished. The **Reply** breaks off mid-sentence, and only as far as it got is shown, spoken, and relayed; the rest is never heard of again. The **Persona** who cut in takes the next **Turn**; off unless asked for on a given run.
_Avoid_: Barge-in, override, cut-in (as a noun)

**Eagerness**:
How readily a **Persona** cuts the others off, rated from 0 (always waits its turn) to 4 (talks over everyone), and the only thing that decides how often it interrupts.
_Avoid_: Aggression, rudeness, interrupt rate

**Language**:
The language a **Persona** writes its **Replies** in, read from its mood at the start of a run. A **Persona** whose mood names none has no **Language** and follows the **Conversation**.
_Avoid_: Locale, tongue, accent (which belongs to the **Voice**)

**Voice**:
The speaking voice a persona's replies are read aloud in, chosen per persona or assigned automatically.
_Avoid_: Speaker, TTS voice

**Read-aloud**:
The runner speaking each reply out loud, live, as the conversation happens; off unless asked for on a given run.
_Avoid_: Narration, TTS (as a domain term), recording

**Window**:
Where a **Conversation** is set up and then watched on screen, in place of the command line and the terminal: a form for its subject, **Personas** and **Turns**, then the cast down one side and each **Reply** as it arrives. Once a **Conversation** has started, the **Window** shows it and can stop it, but does not change it.
_Avoid_: GUI, dashboard, viewer

## Relationships

- A **Conversation** has two or more **Participants**
- In a runner conversation, each **Persona** is exactly one **Participant**
- The **Runner** produces one **Reply** per **Turn**, and **Read-aloud** speaks each **Reply**
- With **Read-aloud**, each **Reply** is shown at the moment it starts being spoken, and **Replies** are never spoken over each other; the next **Persona** may already be composing its **Reply** while the current one is spoken, but at most one **Reply** ahead
- The first **Persona** listed for a run opens the **Conversation**, unless there is a **Leader**
- A **Leader** takes the first and the last **Turn**, overruling any **Addressee** for the last one; the **Turn** before the last is neither the **Leader**'s, where another **Persona** can take it, nor interrupted, so the **Leader** never closes straight after itself and nobody cuts in on the floor it closes on
- A **Reply** with an **Addressee** hands them the next **Turn**; otherwise the **Runner** picks the next **Persona** at random, never the one who just spoke, with the odds favouring whoever has been silent longest, so no fixed order emerges
- An **Interruption** overrules both: the **Persona** who cut in takes the next **Turn**, and nobody the cut-off **Reply** called on gets it, not even an **Addressee** named in the part that was still heard
- Each **Persona** knows the other **Personas**' names and moods, and may call on one of them, but usually speaks to the group
- A mood is binding, not decoration: whatever it names, the **Persona** follows
- A **Tone** belongs to the **Conversation** and a mood to one **Persona**: the **Tone** sets how everyone words their **Replies**, and the mood who each one is within it
- A **Persona** with a **Language** writes every **Reply** in it, whatever language the others are using, and never adds a translation — left to the mood alone it drifts into the language of the **Replies** it reads, since everything it is given to read is in that language
- A **Language** is read once at the start of a run, by asking the model which language each mood names, and each **Turn** is then told its **Language** outright as its last instruction; if the answer cannot be read, no **Persona** has a **Language** and the mood carries it alone
- A **Language** is about what a **Persona** writes and a **Voice** about how it sounds: they are set separately and can disagree, so a **Persona** writing Serbian in an English-sounding **Voice** is a mistake the **Runner** does not correct
- A **Voice** may name the model that speaks it, for engines that keep one voice per model; that is how **Personas** in one **Conversation** can be read aloud in different languages
- Serbian is written in two alphabets and its **Voices** expect Cyrillic, so a **Reply** spoken in one of them is transliterated first; the shown **Reply** keeps the alphabet the **Persona** wrote in, which is what keeps its **Addressee** findable
- A **Reply** is at most a few sentences; the **Runner** asks for a brief one on about a third of **Turns**, never on the opening or closing **Turn** or when the **Persona** is answering its addresser
- A **Persona**'s mood shapes what it says and, through its **Eagerness**, how readily it interrupts; it never changes how long its **Reply** is or its odds in the random pick
- **Eagerness** is rated once at the start of a run, by asking the model to judge each mood; a rating that cannot be read falls back to the middle of the scale, so an unhelpful answer costs realism but never the run
- Every **Persona** but the one speaking may cut in on a **Reply**, each with a chance set by its own **Eagerness**, so a larger **Conversation** is interrupted more often; when several would cut in, the most eager one does
- Any **Turn** may be interrupted except the opening one, the closing one, and one that is itself an **Interruption** — a **Persona** who cut in is heard out, or two pushy **Personas** would cut each other off turn after turn and no thought would ever be finished
- A **Reply** of a few words is over before anyone can cut in, so it is never interrupted
- The **Persona** who was cut off usually takes the **Turn** after the interrupter's, to pick its point back up, unless that **Reply** called on someone; it is told who talked over it, as the interrupter is told whom it is cutting off
- An **Interruption** makes for a quick reaction, so the interrupter is always asked for a brief **Reply**
- A **Persona** is not guaranteed a **Turn**: one who is never addressed or picked may stay silent for the whole **Conversation**
- The last **Turn** is the closing **Turn**: its **Persona** is told to bring the **Conversation** to a natural close, and no **Turn** follows even if the **Reply** addresses someone
- The next **Turn**'s **Persona** is known as soon as the current **Reply** is composed, so the next **Reply** can be composed while the current one is spoken
- **Read-aloud** speaks a **Reply** in sentence-sized pieces, each one synthesized while the previous one is heard, so a **Reply** starts being spoken before all of it has been synthesized; the pieces are heard as one continuous **Reply**, with the pause between them chosen by the **Runner** rather than left to the speech engine
- **Read-aloud** speaks only **Replies**, without formatting symbols; the shown **Reply** stays exactly as the **Persona** wrote it, except that a leading speaker label ("Henk: …") is removed everywhere, since it is formatting copied from the transcript, not something the **Persona** says, and it never counts as addressing, and except that an **Interruption** cuts it short
- What the other **Personas** are told a **Persona** said is exactly what was heard, so an interrupted **Reply** is relayed only as far as it got; nothing a **Persona** never got to say can be answered
- A **Reply** that could not be spoken is still shown, marked as not spoken; it never ends the **Conversation**
- Each **Persona** has exactly one **Voice**; personas get distinct voices unless there are more personas than available voices
- **Read-aloud** belongs to the **Runner**, not the bridge; the bridge has no notion of listeners or audio
- The **Runner** hands every line it produces to one presenter, the terminal or a **Window**; which one is listening changes nothing about the **Conversation**
- A **Window** shows one **Conversation** per tab, and starts another only once the current one has closed, so two runs never speak over each other
- A **Window** sets up what is particular to a **Conversation** — its subject, its **Tone**, its **Personas** (each with an optional **Voice**), its **Turns**, the seed and whether **Interruptions** are allowed — and takes the model and the **Read-aloud** settings from the command line, unchanged for the whole run
- A **Window** reads a **Reply**'s **Persona** from the label the **Runner** shows it under, so what it attributes is exactly what was shown
- A stopped **Conversation** ends at once, the **Reply** being spoken included: it breaks off rather than playing out, and nothing the run says after the stop is shown
- With **Read-aloud**, the **Persona** lit in a **Window** is the one being heard, since a **Reply** is shown at the moment it starts being spoken

## Example dialogue

> **Dev:** "Can a **Conversation** started by some other agent framework be read aloud?"
> **Domain expert:** "No — **Read-aloud** is part of the **Runner**. The bridge only relays messages between **Participants**; it has no observer that could speak them."

> **Dev:** "Dan cut Richard off while Richard was asking Hank something. Does Hank still answer?"
> **Domain expert:** "No. An **Interruption** hands the **Turn** to Dan, and Richard's question is not even in what the others were told he said — only what was heard is relayed. Richard usually gets the floor back after Dan and can ask again."

## Flagged ambiguities

- A silent listener joining the bridge to speak messages was considered — rejected: every bridge member is a full **Participant** (it shows up in peers, gets a message queue, can send), so a listener would need a new observer concept on the bridge.
- "Read out loud" could mean live playback or an audio file of the conversation — resolved: **Read-aloud** is live only; a recording is a separate, not-yet-planned feature.
- A neutral narrator announcing the subject and personas was considered — rejected: only **Personas** speak, so there is no narrator **Voice**.
- A **Persona** marking its **Addressee** with an explicit tag was considered — rejected: the tag would have to be hidden from the shown **Reply**, which must stay exactly as written, and small models forget it.
- Letting a mood like "quiet observer" lower a **Persona**'s chance of a **Turn** was considered — rejected: the random pick stays mood-blind, and quietness shows in what the **Persona** says when it does speak. Mood does set **Eagerness**, so a calm **Persona** interrupts less; that is about cutting in, not about being picked.
- Deriving **Eagerness** from words in the mood text was considered — rejected: moods are free text in any language, so the model judges them instead, which is also why the rating is one request at the start of a run rather than a decision per **Turn**.
- Giving each **Persona** its own **Eagerness** in `--agent` was considered — rejected: a mood like "never lets anyone finish" already says it, and a second knob could contradict it.
- Cutting the audio off mid-playback, as a live interruption would, was considered — rejected: an **Interruption** is decided when the **Reply** is composed, so what is shown, relayed, and spoken stays the same text; stopping playback part-way would leave the other **Personas** answering words nobody heard.
- Giving a **Persona** a language of its own in `--agent`, beside its mood, was considered — rejected: the mood is free text and already says it ("you write Serbian answers"), so a second field could only contradict it, and a **Persona** without a **Voice** would need an empty placeholder. The model reads the **Language** out of the mood instead, the way it rates **Eagerness**.
- Relying on the mood alone, with the prompt asking the **Persona** to honour any language it names, was tried first — rejected: the instruction sits in the middle of a long mood and competes with a whole history in another language, and a measured comparison showed no improvement. A **Language** is now stated outright, as the **Turn**'s last instruction.
- Taking a **Persona**'s **Language** from its **Voice** (`piper-sr_RS-…` meaning Serbian) was considered — rejected: **Read-aloud** is off on most runs, so the written language would depend on whether anyone is listening.
- A **Persona** whose **Language** uses another script writes **Personas**' names in that script ("Хрвоје"), and an **Addressee** is found by matching the names as given, so such a **Reply** hands over the floor to nobody and the **Runner** picks at random — known, not yet resolved.
- Letting the cut-off **Persona** carry on from the exact word it was cut off at was considered — rejected: it composes a fresh **Reply** knowing it was talked over, which is what someone picking a point back up actually does.
- Showing several **Conversations** at once in a **Window** was considered — rejected: **Read-aloud** has one pair of speakers, so two live runs would talk over each other. The next **Conversation** opens only after the current one closes.
- Letting a **Window** start, stop, or steer a **Conversation** was at first rejected, keeping the cast and the subject command-line arguments — revised: a **Window** now sets up and starts each **Conversation**, since holding several in one sitting meant restarting it for every new cast or subject. Stopping one that runs was added after; steering one is still left out.
- Putting the model and the speech settings in the **Window**'s form was considered — rejected: they belong to the setup (an API key, an engine that is running), not to a **Conversation**, and changing them between runs is rare.
- "Turns" could mean rounds where everyone speaks once — resolved: a **Turn** is one **Reply**, and `--turns` counts **Replies**, not rounds.
