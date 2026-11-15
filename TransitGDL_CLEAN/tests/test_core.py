from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

from bot.whatsapp_handler import ConversationHandler
from core import TransitEngine
from core.config import DEFAULT_GTFS_PATH, TransitEngineConfig
from tests.conftest import BRT_ROUTE, BRT_STOP_ID, BRT_STOP_NAME


def test_engine_smoke(gtfs_loader):
    engine = TransitEngine()
    engine.initialize_with_loader(gtfs_loader)
    result = asyncio.run(engine.get_eta(BRT_ROUTE, BRT_STOP_NAME))
    assert result is not None
    assert result.estimated_minutes >= 0
    assert result.average_headway_minutes is not None
    assert result.next_departures
    assert result.window_end_minutes is not None
    assert result.window_start_minutes is not None
    assert result.window_end_minutes >= result.window_start_minutes
    assert result.headsign


def test_engine_asks_for_stop_when_route_is_missing_context(gtfs_loader):
    engine = TransitEngine()
    engine.initialize_with_loader(gtfs_loader)
    reply = asyncio.run(engine.ask(f"Cuanto falta la {BRT_ROUTE}"))
    assert "parada" in reply.lower()


def test_engine_generates_demo_gtfs_when_default_file_is_missing(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    gtfs_path = tmp_path / DEFAULT_GTFS_PATH
    engine = TransitEngine(
        TransitEngineConfig(
            gtfs_path=DEFAULT_GTFS_PATH, redis_url="redis://localhost:6379/0"
        )
    )

    asyncio.run(engine.initialize())

    assert gtfs_path.exists()
    assert engine._initialized is True
    # La base de transporte se construye sola a partir del zip de demostración.
    assert (tmp_path / "data" / "transit.db").exists()
    assert engine._gtfs.get_routes()
    asyncio.run(engine.close())


def test_schedule_next_day_is_flagged(gtfs_loader):
    # Pedir salidas a las 23:55 — el sistema debe envolver al día siguiente
    route = gtfs_loader.find_route(BRT_ROUTE)
    assert route is not None
    departures = gtfs_loader.get_next_departures(
        route.route_id, BRT_STOP_ID, 23 * 60 + 55, limit=1
    )
    assert len(departures) >= 1
    assert departures[0]["service_day_offset"] >= 1


def test_whatsapp_eta_formats_next_day_context():
    from core.models import ETAResult

    reply = ETAResult(
        route_number="45",
        stop_id="S004",
        stop_name="Av. Vallarta y Chapultepec",
        estimated_minutes=307,
        confidence=0.68,
        source="schedule",
        service_day_offset=1,
    ).to_whatsapp_message()

    assert "mañana" in reply.lower()
    assert "5 h 7 min" in reply


def test_whatsapp_eta_shows_direction_and_place():
    from core.models import ETAResult

    reply = ETAResult(
        route_number="T15",
        route_label="T15 (antes 231)",
        stop_id="X",
        stop_name="Calzada Revolución",
        estimated_minutes=6,
        headsign="La Guadalupana",
        place_name="CUCEI",
        walk_meters=83,
    ).to_whatsapp_message()

    assert "T15 (antes 231)" in reply
    assert "hacia *La Guadalupana*" in reply
    assert "a 80 m de CUCEI" in reply


def test_schedule_departures_do_not_duplicate_service_variants(gtfs_loader):
    route = gtfs_loader.find_route(BRT_ROUTE)
    assert route is not None
    departures = gtfs_loader.get_next_departures(
        route.route_id, BRT_STOP_ID, 0, limit=3
    )

    departure_times = [item["departure_time"] for item in departures]
    assert len(departure_times) == len(set(departure_times))


def test_incident_persistence_and_resolution(gtfs_loader, tmp_path):
    config = TransitEngineConfig(
        gtfs_path=str(tmp_path / "missing.zip"),
        incidents_db_path=str(tmp_path / "incidents.db"),
        redis_url="redis://localhost:6379/0",
    )
    engine = TransitEngine(config)
    engine.initialize_with_loader(gtfs_loader)

    incident = engine.report_incident(
        "Bloqueo parcial en Av. Vallarta",
        affected_routes=["R045"],
        stop_ids=["S004"],
        severity="high",
    )

    active = engine.list_incidents(route_id="R045", stop_id="S004")
    assert len(active) == 1
    assert active[0].incident_id == incident.incident_id

    resolved = engine.resolve_incident(incident.incident_id)
    assert resolved is not None
    assert resolved.active is False
    assert engine.list_incidents(route_id="R045", stop_id="S004") == []


def test_incident_with_legacy_number_covers_every_branch(gtfs_loader, tmp_path):
    """Reportar la "646" afecta a sus dos ramales oficiales."""
    config = TransitEngineConfig(
        gtfs_path=str(tmp_path / "missing.zip"),
        incidents_db_path=str(tmp_path / "incidents.db"),
        redis_url="redis://localhost:6379/0",
    )
    engine = TransitEngine(config)
    engine.initialize_with_loader(gtfs_loader)

    incident = engine.report_incident("Manifestación", affected_routes=["646"])

    assert incident.affected_routes == ["T04A-1", "T04A-2"]
    assert len(engine.list_incidents(route_id="T04A-2")) == 1
    assert len(engine.list_incidents(route_id="646")) == 1


def test_whatsapp_menu_is_clean_ascii():
    engine = MagicMock()
    engine.ask = AsyncMock(return_value="ok")
    handler = ConversationHandler(engine)

    reply = asyncio.run(handler.handle("whatsapp:+521", "hola"))

    assert "transitgdl" in reply.lower()
    assert "646" in reply
    assert "�" not in reply
