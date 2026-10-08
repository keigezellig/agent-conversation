"""Provider-neutral WebSocket relay for group conversations between agents."""

import asyncio
import json
import logging
import secrets
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

from fastapi import Depends, FastAPI, Header, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field

from .config import settings

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("agent-bridge")

app = FastAPI(title="agent-bridge")
MAX_PENDING_MESSAGES = 1000


@dataclass
class Conversation:
    participants: tuple[str, ...]
    connections: dict[str, WebSocket] = field(default_factory=dict)
    pending: dict[str, list[dict]] = field(default_factory=dict)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


conversations: dict[str, Conversation] = {}


class ConversationRequest(BaseModel):
    participants: list[str] = Field(min_length=2)


def require_token(authorization: str = Header(default="")) -> None:
    if not settings.api_token:
        raise HTTPException(500, "API_TOKEN is not set")
    if authorization != f"Bearer {settings.api_token}":
        raise HTTPException(401, "bad token")


@app.post("/conversations", dependencies=[Depends(require_token)])
async def create_conversation(req: ConversationRequest):
    participants = tuple(name.strip() for name in req.participants)
    if any(not name or len(name) > 100 for name in participants) or len(set(participants)) != len(participants):
        raise HTTPException(400, "participants must have distinct non-empty names of at most 100 characters")

    conversation_id = secrets.token_urlsafe(18)
    conversations[conversation_id] = Conversation(
        participants=participants,
        pending={name: [] for name in participants},
    )
    return {
        "id": conversation_id,
        "participants": participants,
        "websocket": f"/conversations/{conversation_id}/ws",
    }


@app.get("/conversations/{conversation_id}", dependencies=[Depends(require_token)])
async def get_conversation(conversation_id: str):
    conversation = conversations.get(conversation_id)
    if not conversation:
        raise HTTPException(404, "no such conversation")
    return {
        "id": conversation_id,
        "participants": conversation.participants,
        "connected": list(conversation.connections),
        "pending_messages": {name: len(messages) for name, messages in conversation.pending.items()},
    }


@app.websocket("/conversations/{conversation_id}/ws")
async def connect_agent(websocket: WebSocket, conversation_id: str, participant: str):
    if not settings.api_token or websocket.headers.get("authorization") != f"Bearer {settings.api_token}":
        await websocket.close(code=1008, reason="unauthorized")
        return

    conversation = conversations.get(conversation_id)
    if not conversation or participant not in conversation.participants:
        await websocket.close(code=1008, reason="unknown conversation or participant")
        return

    await websocket.accept()
    registered = False
    peers = [name for name in conversation.participants if name != participant]
    try:
        async with conversation.lock:
            if participant in conversation.connections:
                await websocket.close(code=1008, reason="participant is already connected")
                return
            conversation.connections[participant] = websocket
            registered = True
            await websocket.send_json({
                "type": "ready",
                "conversation_id": conversation_id,
                "participant": participant,
                "peers": peers,
            })
            for pending in conversation.pending[participant]:
                await websocket.send_json(pending)
            conversation.pending[participant].clear()
            for peer_name in peers:
                peer = conversation.connections.get(peer_name)
                if peer:
                    await peer.send_json({"type": "peer_connected", "participant": participant})

        while True:
            raw = await websocket.receive_text()
            try:
                message = json.loads(raw)
            except json.JSONDecodeError:
                async with conversation.lock:
                    await websocket.send_json({"type": "error", "message": "send a JSON object"})
                continue

            if not isinstance(message, dict) or message.get("type") != "message" or "payload" not in message:
                async with conversation.lock:
                    await websocket.send_json({
                        "type": "error",
                        "message": "expected {\"type\": \"message\", \"payload\": ...}",
                    })
                continue

            envelope = {
                "type": "message",
                "id": uuid.uuid4().hex,
                "from": participant,
                "sent_at": datetime.now(timezone.utc).isoformat(),
                "payload": message["payload"],
            }
            async with conversation.lock:
                full_queues = [
                    peer_name for peer_name in peers
                    if peer_name not in conversation.connections
                    and len(conversation.pending[peer_name]) >= MAX_PENDING_MESSAGES
                ]
                if full_queues:
                    await websocket.send_json({
                        "type": "error",
                        "message": "message queue is full for: " + ", ".join(full_queues),
                    })
                    continue
                for peer_name in peers:
                    delivery = {**envelope, "to": peer_name}
                    peer = conversation.connections.get(peer_name)
                    if peer:
                        await peer.send_json(delivery)
                    else:
                        conversation.pending[peer_name].append(delivery)
                await websocket.send_json({"type": "accepted", "id": envelope["id"], "to": peers})
    except WebSocketDisconnect:
        pass
    except Exception:
        log.exception("agent connection failed for conversation %s", conversation_id)
    finally:
        if registered:
            async with conversation.lock:
                if conversation.connections.get(participant) is websocket:
                    del conversation.connections[participant]
                    for peer_name in peers:
                        peer = conversation.connections.get(peer_name)
                        if peer:
                            try:
                                await peer.send_json({"type": "peer_disconnected", "participant": participant})
                            except WebSocketDisconnect:
                                pass


@app.get("/healthz")
async def healthz():
    return {"ok": True}