from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

from fastapi.testclient import TestClient
from twilio.request_validator import RequestValidator

from api.main import app
from core.config import CONFIG
from core.models import BusPosition, Incident, IncidentSeverity, Route, Stop, StopMatch


def _patch_startup_engine(monkeypatch):
    engine = MagicMock()
    engine._initialized = True
    engine.initialize = AsyncMock()
    engine.close = AsyncMock()
    engine.cache_backend = "memory"
    monkeypatch.setattr("api.main.get_engine", lambda: engine)
    return engine


def test_internal_errors_are_redacted(monkeypatch):
    _patch_startup_engine(monkeypatch)

    engine = MagicMock()
    engine.get_eta = AsyncMock(side_effect=RuntimeError("postgres password leaked"))
    monkeypatch.setattr("api.routes.eta.get_engine", lambda: engine)

    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get(
            "/eta/",
            params={"route": "45", "stop": "Vallarta"},
            headers={"Accept-Language": "en"},
        )

    assert response.status_code == 500
    payload = response.json()
    assert payload["detail"] == "Internal server error."
    assert payload["request_id"]
    assert response.headers["X-Request-ID"] == payload["request_id"]
    assert "postgres" not in response.text.lower()


def test_whatsapp_webhook_rejects_invalid_signature(monkeypatch):
    _patch_startup_engine(monkeypatch)

    handler = MagicMock()
    handler.handle = AsyncMock(return_value="ok")
    monkeypatch.setattr("api.routes.whatsapp.get_bot_handler", lambda: handler)
    monkeypatch.setattr(CONFIG, "twilio_auth_token", "secret-token")
    monkeypatch.setattr(CONFIG, "require_twilio_signature", True)

    with TestClient(app) as client:
        response = client.post(
            "/webhook/whatsapp",
            data={"Body": "hola", "From": "+521"},
            headers={"X-Twilio-Signature": "bad-signature"},
        )

    assert response.status_code == 403
    assert handler.handle.await_count == 0


def test_whatsapp_webhook_accepts_valid_signature(monkeypatch):
    _patch_startup_engine(monkeypatch)

    handler = MagicMock()
    handler.handle = AsyncMock(return_value="respuesta segura")
    monkeypatch.setattr("api.routes.whatsapp.get_bot_handler", lambda: handler)
    monkeypatch.setattr(CONFIG, "twilio_auth_token", "secret-token")
    monkeypatch.setattr(CONFIG, "require_twilio_signature", True)

    with TestClient(app) as client:
        payload = {"Body": "hola", "From": "+521"}
        signature = RequestValidator(CONFIG.twilio_auth_token).compute_signature(
            "http://testserver/webhook/whatsapp",
            payload,
        )
        response = client.post(
            "/webhook/whatsapp",
            data=payload,
            headers={"X-Twilio-Signature": signature},
        )

    assert response.status_code == 200
    assert handler.handle.await_count == 1
    assert "respuesta segura" in response.text


def test_stop_search_returns_route_context(monkeypatch):
    _patch_startup_engine(monkeypatch)

    engine = MagicMock()
    engine.search_stops_with_routes.return_value = [
        StopMatch(
            stop_id="S004",
            stop_name="Av. Vallarta y Chapultepec",
            lat=20.675,
            lon=-103.384,
            route_ids=["R045"],
            route_numbers=["45"],
        )
    ]
    monkeypatch.setattr("api.routes.eta.get_engine", lambda: engine)

    with TestClient(app) as client:
        response = client.get("/eta/stops/search", params={"q": "Vallarta"})

    assert response.status_code == 200
    payload = response.json()
    assert payload["results"][0]["route_numbers"] == ["45"]


def test_route_endpoints_return_shape_and_positions(monkeypatch):
    _patch_startup_engine(monkeypatch)

    route = Route(
        route_id="R045", route_short_name="45", route_long_name="Minerva Centro"
    )
    stop_a = Stop(stop_id="S001", stop_name="Parada A", lat=20.670, lon=-103.340)
    stop_b = Stop(stop_id="S002", stop_name="Parada B", lat=20.680, lon=-103.350)
    position = BusPosition(
        vehicle_id="vehicle-1",
        route_id="R045",
        lat=20.675,
        lon=-103.346,
        speed_kmh=22.5,
        timestamp=datetime.now(timezone.utc),
    )

    engine = MagicMock()
    engine._gtfs = MagicMock()
    engine._gtfs.get_routes.return_value = [route]
    engine._gtfs.find_route.return_value = route
    engine._gtfs.get_stops_for_route.return_value = [stop_a, stop_b]
    engine._gps = MagicMock()
    engine._gps.get_positions_for_route = AsyncMock(return_value=[position])

    monkeypatch.setattr("api.routes.routes.get_engine", lambda: engine)

    with TestClient(app) as client:
        list_response = client.get("/routes/", params={"limit": 10})
        shape_response = client.get("/routes/45/shape")
        positions_response = client.get("/routes/45/positions")

    assert list_response.status_code == 200
    assert list_response.json()["routes"][0]["short_name"] == "45"
    assert shape_response.status_code == 200
    assert shape_response.json()["route_id"] == "R045"
    assert len(shape_response.json()["stops"]) == 2
    assert positions_response.status_code == 200
    assert positions_response.json()["positions"][0]["vehicle_id"] == "vehicle-1"


def test_route_websocket_streams_positions(monkeypatch):
    _patch_startup_engine(monkeypatch)

    route = Route(
        route_id="R045", route_short_name="45", route_long_name="Minerva Centro"
    )
    position = BusPosition(
        vehicle_id="vehicle-1",
        route_id="R045",
        lat=20.675,
        lon=-103.346,
        speed_kmh=18.0,
        timestamp=datetime.now(timezone.utc),
    )

    engine = MagicMock()
    engine._gtfs = MagicMock()
    engine._gtfs.find_route.return_value = route
    engine._gps = MagicMock()
    engine._gps.get_positions_for_route = AsyncMock(return_value=[position])
    monkeypatch.setattr("api.routes.routes.get_engine", lambda: engine)

    with TestClient(app) as client:
        with client.websocket_connect("/routes/45/ws") as ws:
            payload = ws.receive_json()

    assert payload["route_id"] == "R045"
    assert payload["positions"][0]["vehicle_id"] == "vehicle-1"


def test_incidents_list_and_resolve(monkeypatch):
    _patch_startup_engine(monkeypatch)

    incident = Incident(
        incident_id="abc123",
        description="Bloqueo parcial",
        affected_routes=["R045"],
        stop_ids=["S004"],
        severity=IncidentSeverity.HIGH,
    )
    engine = MagicMock()
    engine.list_incidents.return_value = [incident]
    engine.resolve_incident.return_value = incident
    monkeypatch.setattr("api.routes.incidents.get_engine", lambda: engine)

    with TestClient(app) as client:
        listed = client.get("/incidents/")
        resolved = client.post("/incidents/abc123/resolve")

    assert listed.status_code == 200
    assert listed.json()["count"] == 1
    assert resolved.status_code == 200
    assert resolved.json()["incident"]["incident_id"] == "abc123"
