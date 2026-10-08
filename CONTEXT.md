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

**Voice**:
The speaking voice a persona's replies are read aloud in, chosen per persona or assigned automatically.
_Avoid_: Speaker, TTS voice

**Read-aloud**:
The runner speaking each reply out loud, live, as the conversation happens; off unless asked for on a given run.
_Avoid_: Narration, TTS (as a domain term), recording

## Relationships

- A **Conversation** has two or more **Participants**
- In a runner conversation, each **Persona** is exactly one **Participant**
- The **Runner** produces one **Reply** per turn, and **Read-aloud** speaks each **Reply**
- With **Read-aloud**, each **Reply** is shown at the moment it starts being spoken, and **Replies** are never spoken over each other; the next **Persona** may already be composing its **Reply** while the current one is spoken, but at most one **Reply** ahead
- **Read-aloud** speaks only **Replies**, without formatting symbols; the shown **Reply** stays exactly as the **Persona** wrote it
- A **Reply** that could not be spoken is still shown, marked as not spoken; it never ends the **Conversation**
- Each **Persona** has exactly one **Voice**; personas get distinct voices unless there are more personas than available voices
- **Read-aloud** belongs to the **Runner**, not the bridge; the bridge has no notion of listeners or audio

## Example dialogue

> **Dev:** "Can a **Conversation** started by some other agent framework be read aloud?"
> **Domain expert:** "No — **Read-aloud** is part of the **Runner**. The bridge only relays messages between **Participants**; it has no observer that could speak them."

## Flagged ambiguities

- A silent listener joining the bridge to speak messages was considered — rejected: every bridge member is a full **Participant** (it shows up in peers, gets a message queue, can send), so a listener would need a new observer concept on the bridge.
- "Read out loud" could mean live playback or an audio file of the conversation — resolved: **Read-aloud** is live only; a recording is a separate, not-yet-planned feature.
- A neutral narrator announcing the subject and personas was considered — rejected: only **Personas** speak, so there is no narrator **Voice**.
