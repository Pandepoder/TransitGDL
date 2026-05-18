from __future__ import annotations

import time
from collections import deque
from unittest.mock import AsyncMock, MagicMock

from fastapi.testclient import TestClient

from api.main import app


def _patch_startup_engine(monkeypatch):
    engine = MagicMock()
    engine._initialized = True
    engine.initialize = AsyncMock()
    engine.close = AsyncMock()
    engine.cache_backend = "memory"
    monkeypatch.setattr("api.main.get_engine", lambda: engine)
    return engine


def test_eta_returns_english_error_with_accept_language_header(monkeypatch):
    _patch_startup_engine(monkeypatch)

    engine = MagicMock()
    engine.search_stops_with_routes.return_value = []
    monkeypatch.setattr("api.routes.eta.get_engine", lambda: engine)

    with TestClient(app) as client:
        response = client.get(
            "/eta/stops/routes",
            params={"stop": "Vallarta"},
            headers={"Accept-Language": "en-US,en;q=0.9"},
        )

    assert response.status_code == 404
    assert response.json()["detail"] == "Stop not found."


def test_whatsapp_invalid_signature_returns_english_error(monkeypatch):
    _patch_startup_engine(monkeypatch)

    import core.config

    handler = MagicMock()
    handler.handle = AsyncMock(return_value="ok")
    monkeypatch.setattr("api.routes.whatsapp.get_bot_handler", lambda: handler)
    monkeypatch.setattr(core.config.CONFIG, "twilio_auth_token", "secret-token")
    monkeypatch.setattr(core.config.CONFIG, "require_twilio_signature", True)

    with TestClient(app) as client:
        response = client.post(
            "/webhook/whatsapp",
            data={"Body": "hola", "From": "+521"},
            headers={
                "X-Twilio-Signature": "bad-signature",
                "Accept-Language": "en",
            },
        )

    assert response.status_code == 403
    assert response.json()["detail"] == "Invalid Twilio signature."


def test_feedback_invalid_prediction_id_returns_english_error(monkeypatch):
    _patch_startup_engine(monkeypatch)

    with TestClient(app) as client:
        response = client.post(
            "/feedback/",
            json={"prediction_id": "bad id!", "rating": 3},
            headers={"Accept-Language": "en"},
        )

    assert response.status_code == 422
    assert response.json()["detail"] == "Invalid request. Check the submitted data."
    assert any(
        error["loc"] == ["body", "prediction_id"] for error in response.json()["errors"]
    )


def test_default_locale_returns_mexican_spanish_error(monkeypatch):
    _patch_startup_engine(monkeypatch)

    with TestClient(app) as client:
        response = client.post(
            "/feedback/",
            json={"prediction_id": "bad id!", "rating": 3},
        )

    assert response.status_code == 422
    assert response.json()["detail"] == "Solicitud inválida. Revisa los datos enviados."
    assert any(
        error["loc"] == ["body", "prediction_id"] for error in response.json()["errors"]
    )


def test_incident_create_invalid_route_format_returns_english_error(monkeypatch):
    _patch_startup_engine(monkeypatch)

    payload = {
        "description": "Hay un bloqueo en línea 45.",
        "affected_routes": ["45!"],
        "stop_ids": ["S001"],
    }

    with TestClient(app) as client:
        response = client.post(
            "/incidents/",
            json=payload,
            headers={"Accept-Language": "en"},
        )

    assert response.status_code == 422
    assert response.json()["detail"] == "Invalid request. Check the submitted data."
    assert any(
        error["loc"] == ["body", "affected_routes", 0]
        for error in response.json()["errors"]
    )


def test_route_not_found_returns_english_error_with_accept_language_header(monkeypatch):
    _patch_startup_engine(monkeypatch)

    engine = MagicMock()
    engine._gtfs = MagicMock()
    engine._gtfs.find_route.return_value = None
    monkeypatch.setattr("api.routes.routes.get_engine", lambda: engine)

    with TestClient(app) as client:
        response = client.get(
            "/routes/unknown/shape",
            headers={"Accept-Language": "en-US,en;q=0.9"},
        )

    assert response.status_code == 404
    assert response.json()["detail"] == "Route not found."


def test_feedback_invalid_prediction_id_returns_spanish_error(monkeypatch):
    _patch_startup_engine(monkeypatch)

    with TestClient(app) as client:
        response = client.post(
            "/feedback/",
            json={"prediction_id": "bad id!", "rating": 3},
            headers={"Accept-Language": "es"},
        )

    assert response.status_code == 422
    assert response.json()["detail"] == "Solicitud inválida. Revisa los datos enviados."
    assert any(
        error["loc"] == ["body", "prediction_id"] for error in response.json()["errors"]
    )


def test_incident_create_invalid_route_format_returns_spanish_error(monkeypatch):
    _patch_startup_engine(monkeypatch)

    payload = {
        "description": "Hay un bloqueo en línea 45.",
        "affected_routes": ["45!"],
        "stop_ids": ["S001"],
    }

    with TestClient(app) as client:
        response = client.post(
            "/incidents/",
            json=payload,
            headers={"Accept-Language": "es"},
        )

    assert response.status_code == 422
    assert response.json()["detail"] == "Solicitud inválida. Revisa los datos enviados."
    assert any(
        error["loc"] == ["body", "affected_routes", 0]
        for error in response.json()["errors"]
    )


def test_feedback_invalid_prediction_id_returns_spanish_error_with_es_mx_header(
    monkeypatch,
):
    _patch_startup_engine(monkeypatch)

    with TestClient(app) as client:
        response = client.post(
            "/feedback/",
            json={"prediction_id": "bad id!", "rating": 3},
            headers={"Accept-Language": "es-MX"},
        )

    assert response.status_code == 422
    assert response.json()["detail"] == "Solicitud inválida. Revisa los datos enviados."
    assert any(
        error["loc"] == ["body", "prediction_id"] for error in response.json()["errors"]
    )


def test_feedback_invalid_prediction_id_returns_spanish_error_with_es_mx_lang_param(
    monkeypatch,
):
    _patch_startup_engine(monkeypatch)

    with TestClient(app) as client:
        response = client.post(
            "/feedback/?lang=es-MX",
            json={"prediction_id": "bad id!", "rating": 3},
            headers={"Accept-Language": "en"},
        )

    assert response.status_code == 422
    assert response.json()["detail"] == "Solicitud inválida. Revisa los datos enviados."
    assert any(
        error["loc"] == ["body", "prediction_id"] for error in response.json()["errors"]
    )


def test_accept_language_es_419_resolves_to_mexican_spanish(monkeypatch):
    _patch_startup_engine(monkeypatch)

    engine = MagicMock()
    engine.search_stops_with_routes.return_value = []
    monkeypatch.setattr("api.routes.eta.get_engine", lambda: engine)

    with TestClient(app) as client:
        response = client.get(
            "/eta/stops/routes",
            params={"stop": "Vallarta"},
            headers={"Accept-Language": "es-419,es;q=0.8,en;q=0.6"},
        )

    assert response.status_code == 404
    assert response.json()["detail"] == "No se encontró la parada solicitada."


def test_translate_uses_direct_es_mx_lookup():
    from core.i18n import translate

    assert translate("errors.invalid_route", "es-MX") == "Ruta inválida."


def test_feedback_rate_limit_returns_spanish_error(monkeypatch):
    _patch_startup_engine(monkeypatch)

    from api.rate_limiter import _feedback_limiter

    _feedback_limiter._windows.clear()
    _feedback_limiter._windows["127.0.0.1"] = deque([time.monotonic()] * 10)

    engine = MagicMock()
    engine.record_feedback.return_value = True
    monkeypatch.setattr("api.routes.feedback.get_engine", lambda: engine)

    with TestClient(app) as client:
        response = client.post(
            "/feedback/",
            json={"prediction_id": "ABC12345", "rating": 3},
            headers={
                "Accept-Language": "es",
                "X-Forwarded-For": "127.0.0.1",
            },
        )

    assert response.status_code == 429
    assert (
        response.json()["detail"]
        == "Demasiadas solicitudes. Espera un momento antes de enviar otra calificación."
    )


def test_dashboard_not_found_returns_english_error(monkeypatch):
    _patch_startup_engine(monkeypatch)

    import api.main

    fake_path = MagicMock()
    fake_path.exists.return_value = False
    monkeypatch.setattr(api.main, "_DASHBOARD_HTML", fake_path)

    with TestClient(app) as client:
        response = client.get(
            "/dashboard",
            headers={"Accept-Language": "en"},
        )

    assert response.status_code == 404
    assert response.json()["detail"] == "Dashboard not found."


def test_dashboard_not_found_returns_spanish_error_with_es_mx_header(monkeypatch):
    _patch_startup_engine(monkeypatch)

    import api.main

    fake_path = MagicMock()
    fake_path.exists.return_value = False
    monkeypatch.setattr(api.main, "_DASHBOARD_HTML", fake_path)

    with TestClient(app) as client:
        response = client.get(
            "/dashboard",
            headers={"Accept-Language": "es-MX"},
        )

    assert response.status_code == 404
    assert response.json()["detail"] == "Dashboard no encontrado."


def test_route_not_found_returns_spanish_error_with_es_mx_header(monkeypatch):
    _patch_startup_engine(monkeypatch)

    with TestClient(app) as client:
        response = client.get(
            "/ruta-inexistente",
            headers={"Accept-Language": "es-MX"},
        )

    assert response.status_code == 404
    assert response.json()["detail"] == "Recurso no encontrado."


def test_method_not_allowed_returns_spanish_error_with_es_mx_header(monkeypatch):
    _patch_startup_engine(monkeypatch)

    with TestClient(app) as client:
        response = client.post(
            "/eta/stops/routes",
            json={"stop": "Vallarta"},
            headers={"Accept-Language": "es-MX"},
        )

    assert response.status_code == 405
    assert response.json()["detail"] == "Método no permitido."


def test_global_exception_returns_spanish_error(monkeypatch):
    _patch_startup_engine(monkeypatch)

    engine = MagicMock()
    engine.get_eta = AsyncMock(side_effect=RuntimeError("boom"))
    monkeypatch.setattr("api.routes.eta.get_engine", lambda: engine)

    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get(
            "/eta/",
            params={"route": "45", "stop": "Central"},
            headers={"Accept-Language": "es"},
        )

    assert response.status_code == 500
    assert response.json()["detail"] == "Error interno del servidor."


def test_fleet_invalid_route_id_returns_english_error_with_accept_language_header(
    monkeypatch,
):
    _patch_startup_engine(monkeypatch)

    with TestClient(app) as client:
        response = client.get(
            "/fleet/recommend",
            params={"route_id": " ", "vehicles": 3},
            headers={"Accept-Language": "en"},
        )

    assert response.status_code == 400
    assert response.json()["detail"] == "Invalid route_id."
