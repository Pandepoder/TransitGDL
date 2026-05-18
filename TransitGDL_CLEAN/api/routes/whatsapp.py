from __future__ import annotations

import re

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import PlainTextResponse

from api.deps import get_bot_handler, get_locale
from core.config import CONFIG
from core.i18n import translate as t

router = APIRouter(tags=["whatsapp"])


def _should_validate_signature() -> bool:
    return CONFIG.require_twilio_signature or bool(CONFIG.twilio_auth_token.strip())


def _validate_twilio_signature(
    request: Request, payload: dict[str, str], locale: str
) -> None:
    if not _should_validate_signature():
        return

    if not CONFIG.twilio_auth_token.strip():
        raise HTTPException(
            status_code=503,
            detail=t("errors.twilio_validation_enabled", locale=locale),
        )

    signature = request.headers.get("X-Twilio-Signature", "").strip()
    if not signature:
        raise HTTPException(
            status_code=403, detail=t("errors.missing_twilio_signature", locale=locale)
        )

    from twilio.request_validator import RequestValidator

    validator = RequestValidator(CONFIG.twilio_auth_token)
    if not validator.validate(str(request.url), payload, signature):
        raise HTTPException(
            status_code=403, detail=t("errors.invalid_twilio_signature", locale=locale)
        )


@router.post("/webhook/whatsapp")
async def whatsapp_webhook(request: Request, locale: str = Depends(get_locale)):
    form = await request.form()
    payload = {key: str(value) for key, value in form.multi_items()}
    _validate_twilio_signature(request, payload, locale)
    body = payload.get("Body", "")
    sender = payload.get("From", "") or "unknown"

    # Basic sanitization and size limits
    body = body.strip()
    if len(body) > 1000:
        raise HTTPException(
            status_code=400, detail=t("errors.message_too_long", locale=locale)
        )

    # Normalize sender: accept international phone-like strings, otherwise mark unknown
    if sender and sender != "unknown":
        sender = sender.strip()
        if not re.match(r"^\+?\d{7,15}$", sender):
            sender = "unknown"
    handler = get_bot_handler()
    reply = await handler.handle(sender, body)
    try:
        from twilio.twiml.messaging_response import MessagingResponse

        twiml = MessagingResponse()
        twiml.message(reply)
        return PlainTextResponse(str(twiml), media_type="application/xml")
    except Exception:
        return PlainTextResponse(reply)
