from __future__ import annotations

from functools import lru_cache

from fastapi import Header, Query

from bot.whatsapp_handler import ConversationHandler
from core import TransitEngine
from core.i18n import get_preferred_locale


@lru_cache(maxsize=1)
def get_engine() -> TransitEngine:
    return TransitEngine()


@lru_cache(maxsize=1)
def get_bot_handler() -> ConversationHandler:
    return ConversationHandler(get_engine())


def get_locale(
    accept_language: str | None = Header(None, alias="Accept-Language"),
    lang: str | None = Query(None, alias="lang"),
) -> str:
    return get_preferred_locale(lang=lang, accept_language=accept_language)
