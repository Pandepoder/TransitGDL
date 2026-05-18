from __future__ import annotations

import asyncio

from bot.whatsapp_handler import ConversationHandler


def test_feedback_does_not_match_route_number_646(engine):
    handler = ConversationHandler(engine)
    sender = "test_fb_646"

    async def run() -> None:
        await handler._reset_session(sender)
        await handler.handle(sender, "cuanto falta la 646 en CUCEI")
        reply = await handler.handle(sender, "cuanto falta la 55 en CUCEI")
        assert "calificación" not in reply.lower()
        assert "55" in reply

    asyncio.run(run())
