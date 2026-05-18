from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

from bot.whatsapp_handler import ConversationHandler
from core import TransitEngine
from core.config import DEFAULT_GTFS_PATH, TransitEngineConfig


REAL_ROUTE = "MACRO01_R"       # Macrobús L-1 — 54 paradas, headway 6 min
REAL_STOP  = "MACRO_001_003"   # Artes Plásticas


def test_engine_smoke(gtfs_loader):
    engine = TransitEngine()
    engine.initialize_with_loader(gtfs_loader)
    result = asyncio.run(engine.get_eta(REAL_ROUTE, "Artes Plásticas"))
    assert result is not None
    assert result.estimated_minutes >= 0
    assert result.average_headway_minutes is not None
    assert result.next_departures
    assert result.window_end_minutes is not None
    assert result.window_start_minutes is not None
    assert result.window_end_minutes >= result.window_start_minutes


def test_engine_asks_for_stop_when_route_is_missing_context(gtfs_loader):
    engine = TransitEngine()
    engine.initialize_with_loader(gtfs_loader)
    reply = asyncio.run(engine.ask(f"Cuanto falta la {REAL_ROUTE}"))
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
    asyncio.run(engine.close())


def test_schedule_next_day_is_flagged(gtfs_loader):
    # Pedir salidas a las 23:55 — el sistema debe envolver al día siguiente
    route = gtfs_loader.find_route(REAL_ROUTE)
    assert route is not None
    departures = gtfs_loader.get_next_departures(
        route.route_id, REAL_STOP, 23 * 60 + 55, limit=1
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


def test_schedule_departures_do_not_duplicate_service_variants(gtfs_loader):
    route = gtfs_loader.find_route(REAL_ROUTE)
    assert route is not None
    departures = gtfs_loader.get_next_departures(route.route_id, REAL_STOP, 0, limit=3)

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


def test_whatsapp_menu_is_clean_ascii():
    engine = MagicMock()
    engine.ask = AsyncMock(return_value="ok")
    handler = ConversationHandler(engine)

    reply = asyncio.run(handler.handle("whatsapp:+521", "hola"))

    assert "transitgdl" in reply.lower()
    assert "646" in reply
    assert "\ufffd" not in reply
