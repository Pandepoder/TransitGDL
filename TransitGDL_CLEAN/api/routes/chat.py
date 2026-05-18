from __future__ import annotations

import re
from uuid import uuid4

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, Field

from api.deps import get_bot_handler
from core.debug_log import agent_log

router = APIRouter(prefix="/chat", tags=["chat"])


class ChatRequest(BaseModel):
    message: str = Field(..., min_length=1, max_length=1000)
    session_id: str | None = Field(None, max_length=64)


class ChatResponse(BaseModel):
    reply: str
    session_id: str


def _session_key(raw: str | None) -> str:
    if raw and re.fullmatch(r"web_[a-zA-Z0-9_-]{8,48}", raw):
        return raw
    return f"web_{uuid4().hex[:16]}"


@router.post("/message", response_model=ChatResponse)
async def chat_message(
    body: ChatRequest,
    x_session_id: str | None = Header(None, alias="X-Session-Id"),
):
    session_id = _session_key(body.session_id or x_session_id)
    text = body.message.strip()
    if not text:
        raise HTTPException(status_code=400, detail="Mensaje vacío")

    handler = get_bot_handler()
    reply = await handler.handle(session_id, text)

    # #region agent log
    agent_log(
        "api/routes/chat.py:chat_message",
        "web_chat_turn",
        {
            "session_id": session_id[:12],
            "msg_len": len(text),
            "reply_len": len(reply),
            "preview": text[:40],
        },
        hypothesis_id="E",
    )
    # #endregion

    return ChatResponse(reply=reply, session_id=session_id)
