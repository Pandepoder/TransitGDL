from __future__ import annotations

import asyncio

from bot.whatsapp_handler import ConversationHandler


def test_feedback_does_not_match_route_number(engine):
    """Una consulta nueva con dígitos no se confunde con una calificación 1–5."""
    handler = ConversationHandler(engine)
    sender = "test_fb_231"

    async def run() -> None:
        await handler._reset_session(sender)
        first = await handler.handle(sender, "cuanto falta la 231 en CUCEI")
        assert "T15 (antes 231)" in first
        reply = await handler.handle(sender, "cuanto falta la 55 en CUCEI")
        assert "calificación" not in reply.lower()
        assert "T11B (antes 55)" in reply

    asyncio.run(run())
