from __future__ import annotations

SUPPORTED_LOCALES = ("es", "es-mx", "en")
DEFAULT_LOCALE = "es-mx"
# Variantes regionales como es-419 se resuelven a la variante mexicana de español.

TRANSLATIONS: dict[str, dict[str, str]] = {
    "errors.invalid_route": {
        "es": "Ruta inválida.",
        "en": "Invalid route.",
    },
    "errors.invalid_stop": {
        "es": "Parada inválida.",
        "en": "Invalid stop.",
    },
    "errors.empty_query": {
        "es": "Consulta vacía.",
        "en": "Empty query.",
    },
    "errors.eta_not_found": {
        "es": "No se encontró ETA para esa ruta/parada.",
        "en": "ETA not found for that route/stop.",
    },
    "errors.stop_not_found": {
        "es": "No se encontró la parada solicitada.",
        "en": "Stop not found.",
    },
    "errors.route_not_found": {
        "es": "Ruta no encontrada.",
        "en": "Route not found.",
    },
    "errors.route_no_stops": {
        "es": "Ruta sin paradas definidas.",
        "en": "Route has no defined stops.",
    },
    "errors.invalid_route_id": {
        "es": "route_id inválido.",
        "en": "Invalid route_id.",
    },
    "errors.incident_not_found": {
        "es": "No se encontró la incidencia solicitada.",
        "en": "Incident not found.",
    },
    "errors.prediction_not_found": {
        "es": "No se encontró la predicción indicada. Verifica el prediction_id o que no hayan pasado más de 2 horas.",
        "en": "Prediction not found. Verify the prediction_id or that no more than 2 hours have passed.",
    },
    "errors.invalid_incident_route": {
        "es": "Asegúrate de que las rutas afectadas tengan un formato válido.",
        "en": "Ensure affected routes use a valid format.",
    },
    "errors.invalid_incident_stop": {
        "es": "Asegúrate de que las paradas afectadas tengan un formato válido.",
        "en": "Ensure affected stops use a valid format.",
    },
    "errors.invalid_prediction_id": {
        "es": "prediction_id inválido.",
        "en": "Invalid prediction_id.",
    },
    "errors.invalid_request": {
        "es": "Solicitud inválida. Revisa los datos enviados.",
        "en": "Invalid request. Check the submitted data.",
    },
    "errors.too_many_requests": {
        "es": "Demasiadas solicitudes. Espera un momento antes de enviar otra calificación.",
        "en": "Too many requests. Please wait before sending another rating.",
    },
    "errors.not_found": {
        "es": "Recurso no encontrado.",
        "en": "Not found.",
    },
    "errors.method_not_allowed": {
        "es": "Método no permitido.",
        "en": "Method not allowed.",
    },
    "errors.dashboard_not_found": {
        "es": "Dashboard no encontrado.",
        "en": "Dashboard not found.",
    },
    "errors.internal_server_error": {
        "es": "Error interno del servidor.",
        "en": "Internal server error.",
    },
    "messages.feedback_received": {
        "es": "Gracias. Tu calificación mejorará las predicciones futuras.",
        "en": "Thanks. Your rating will improve future predictions.",
    },
    "errors.twilio_validation_enabled": {
        "es": "La validación del webhook está habilitada pero falta TWILIO_AUTH_TOKEN.",
        "en": "Webhook validation is enabled but TWILIO_AUTH_TOKEN is missing.",
    },
    "errors.missing_twilio_signature": {
        "es": "Falta la firma de Twilio.",
        "en": "Missing Twilio signature.",
    },
    "errors.invalid_twilio_signature": {
        "es": "Firma de Twilio inválida.",
        "en": "Invalid Twilio signature.",
    },
    "errors.missing_route_id": {
        "es": "route_id inválido.",
        "en": "Invalid route_id.",
    },
    "errors.message_too_long": {
        "es": "Mensaje demasiado largo.",
        "en": "Message too long.",
    },
}

for _translations in TRANSLATIONS.values():
    if "es-mx" not in _translations and "es" in _translations:
        _translations["es-mx"] = _translations["es"]


def _normalize_locale(value: str | None) -> str:
    if not value:
        return DEFAULT_LOCALE
    locale = value.strip().lower()
    if locale in SUPPORTED_LOCALES:
        return locale
    if locale.startswith("es-"):
        return "es-mx"
    if locale[:2] in SUPPORTED_LOCALES:
        return locale[:2]
    return DEFAULT_LOCALE


def parse_accept_language(header: str | None) -> str:
    if not header:
        return DEFAULT_LOCALE
    for part in header.split(","):
        code = part.split(";")[0].strip().lower()
        if not code:
            continue
        if code in SUPPORTED_LOCALES:
            return code
        if code.startswith("es-"):
            return "es-mx"
        if code[:2] in SUPPORTED_LOCALES:
            return code[:2]
    return DEFAULT_LOCALE


def get_preferred_locale(
    *, lang: str | None = None, accept_language: str | None = None
) -> str:
    if lang:
        return _normalize_locale(lang)
    return parse_accept_language(accept_language)


def translate(key: str, locale: str | None = None, **kwargs) -> str:
    locale = _normalize_locale(locale)
    translations = TRANSLATIONS.get(key, {})
    message = translations.get(locale)
    if message is None and "-" in locale:
        language_base = locale.split("-", 1)[0]
        message = translations.get(language_base)
    if message is None:
        message = translations.get(DEFAULT_LOCALE, key)
    return message.format(**kwargs)
