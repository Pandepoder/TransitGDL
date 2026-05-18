"""
tests/test_integration.py — Tests de integración: bot + GPS cache + GTFS real.

Estos tests usan el GTFS de demo y validan el flujo completo extremo a extremo.
"""

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from bot.whatsapp_handler import ConversationHandler, State
from core.gps_cache import GPSCache
from core.models import BusPosition

# ─────────────────────────────────────────
#  GPS CACHE
# ─────────────────────────────────────────


class TestGPSCache:

    def setup_method(self):
        """Caché en memoria para tests (sin Redis)."""
        self.cache = GPSCache.__new__(GPSCache)
        self.cache._use_memory = True
        self.cache._memory = {}
        self.cache._redis = None

    def _pos(
        self, vehicle_id="b1", route_id="R045", lat=20.675, lon=-103.384, speed=25.0
    ):
        return BusPosition(
            vehicle_id=vehicle_id,
            route_id=route_id,
            trip_id="T001",
            lat=lat,
            lon=lon,
            speed_kmh=speed,
        )

    def test_update_and_retrieve(self):
        pos = self._pos()
        asyncio.run(self.cache.update(pos))
        result = asyncio.run(self.cache.get_positions_for_route("R045"))
        assert len(result) == 1
        assert result[0].vehicle_id == "b1"

    def test_multiple_vehicles_same_route(self):
        for i in range(3):
            asyncio.run(self.cache.update(self._pos(vehicle_id=f"bus_{i}")))
        result = asyncio.run(self.cache.get_positions_for_route("R045"))
        assert len(result) == 3

    def test_stale_positions_excluded(self):
        stale = BusPosition(
            vehicle_id="stale",
            route_id="R045",
            trip_id=None,
            lat=20.675,
            lon=-103.384,
            timestamp=datetime.now(timezone.utc) - timedelta(minutes=5),
        )
        self.cache._memory["stale"] = stale
        result = asyncio.run(self.cache.get_positions_for_route("R045"))
        assert all(p.vehicle_id != "stale" for p in result)

    def test_different_routes_isolated(self):
        asyncio.run(self.cache.update(self._pos(route_id="R045")))
        asyncio.run(self.cache.update(self._pos(vehicle_id="b2", route_id="R110")))
        assert len(asyncio.run(self.cache.get_positions_for_route("R045"))) == 1
        assert len(asyncio.run(self.cache.get_positions_for_route("R110"))) == 1

    def test_count_active_vehicles(self):
        for i in range(4):
            asyncio.run(self.cache.update(self._pos(vehicle_id=f"bus_{i}")))
        count = asyncio.run(self.cache.count_active_vehicles("R045"))
        assert count == 4

    def test_serialization_roundtrip(self):
        original = self._pos(speed=32.5)
        serialized = GPSCache._serialize(original)
        restored = GPSCache._deserialize(serialized)
        assert restored.vehicle_id == original.vehicle_id
        assert restored.lat == original.lat
        assert abs(restored.speed_kmh - original.speed_kmh) < 0.01
        assert restored.timestamp.tzinfo is not None  # siempre timezone-aware


# ─────────────────────────────────────────
#  BOT — CONVERSATIONHANDLER
# ─────────────────────────────────────────


def _make_engine(eta_result=None, weather_msg="☀️ Sin retrasos."):
    """Mock del TransitEngine para tests del bot."""
    engine = MagicMock()
    engine.get_eta = AsyncMock(return_value=eta_result)
    engine.ask = AsyncMock(return_value=weather_msg)
    engine.search_stops = MagicMock(return_value=[])
    engine.get_routes_at_stop = MagicMock(return_value=[])
    engine.get_alternate_routes = MagicMock(return_value=[])
    engine.report_incident = MagicMock()
    return engine


def _make_eta_result():
    from core.models import ETAResult

    return ETAResult(
        route_number="45",
        stop_id="S004",
        stop_name="Av. Vallarta y Chapultepec",
        estimated_minutes=8.0,
        confidence=0.9,
        source="schedule",
        delay_minutes=0.0,
    )


class TestConversationHandler:

    def _handler(self, engine=None):
        return ConversationHandler(engine or _make_engine())

    def _run(self, coro):
        return asyncio.run(coro)

    def test_welcome_on_first_message(self):
        h = self._handler()
        reply = self._run(h.handle("+521", "hola"))
        assert "bot" in reply.lower() or "hola" in reply.lower()

    def test_menu_command(self):
        h = self._handler()
        self._run(h.handle("+521", "hola"))  # primer mensaje
        reply = self._run(h.handle("+521", "menú"))
        assert "1" in reply and "2" in reply

    def test_option_2_calls_weather(self):
        engine = _make_engine(weather_msg="🌧️ Lluvia en GDL +4 min")
        h = self._handler(engine)
        reply = self._run(h.handle("+521", "2"))
        engine.ask.assert_called_once()
        assert "lluvia" in reply.lower() or "4" in reply

    def test_eta_direct_query(self):
        engine = _make_engine(eta_result=_make_eta_result())
        h = self._handler(engine)
        reply = self._run(h.handle("+521", "¿Cuánto falta la 45 en Vallarta?"))
        assert "45" in reply or "Vallarta" in reply

    def test_eta_two_step_route_then_stop(self):
        engine = _make_engine(eta_result=_make_eta_result())
        h = self._handler(engine)
        self._run(h.handle("+521", "1"))  # opción ETA
        state_after_1 = h._memory_sessions["+521"].state
        assert state_after_1 == State.AWAIT_ROUTE
        self._run(h.handle("+521", "45"))  # ruta
        state_after_route = h._memory_sessions["+521"].state
        assert state_after_route == State.AWAIT_STOP
        _ = self._run(h.handle("+521", "Vallarta"))  # parada
        engine.get_eta.assert_called_once()
        assert h._memory_sessions["+521"].state == State.AWAIT_FEEDBACK

    def test_incident_report_flow(self):
        engine = _make_engine()
        h = self._handler(engine)
        self._run(h.handle("+521", "4"))  # opción incidencia
        assert h._memory_sessions["+521"].state == State.AWAIT_INCIDENT
        reply = self._run(h.handle("+521", "Bloqueo en Av. Vallarta y Chapultepec"))
        engine.report_incident.assert_called_once()
        assert "registrada" in reply.lower() or "✅" in reply
        assert h._memory_sessions["+521"].state == State.IDLE

    def test_incident_report_requires_context(self):
        engine = _make_engine()
        h = self._handler(engine)
        self._run(h.handle("+521", "4"))
        reply = self._run(h.handle("+521", "bus"))
        assert "detalle" in reply.lower()
        engine.report_incident.assert_not_called()
        assert h._memory_sessions["+521"].state == State.AWAIT_INCIDENT

    def test_option_3_routes_by_stop_flow(self):
        engine = _make_engine()
        engine.search_stops.return_value = [
            MagicMock(stop_id="S004", stop_name="Av. Vallarta y Chapultepec")
        ]
        engine.get_routes_at_stop.return_value = [
            MagicMock(route_short_name="45"),
            MagicMock(route_short_name="24"),
        ]
        h = self._handler(engine)
        self._run(h.handle("+521", "3"))
        assert h._memory_sessions["+521"].state == State.AWAIT_ROUTES_STOP
        reply = self._run(h.handle("+521", "Vallarta y Chapultepec"))
        assert "45" in reply and "24" in reply
        assert h._memory_sessions["+521"].state == State.IDLE

    def test_reset_command(self):
        h = self._handler()
        self._run(h.handle("+521", "1"))  # estado AWAIT_ROUTE
        self._run(h.handle("+521", "0"))  # reset
        assert h._memory_sessions["+521"].state == State.IDLE

    def test_unknown_input(self):
        h = self._handler()
        reply = self._run(h.handle("+521", "ksdjfhaskdjfh"))
        assert (
            "menú" in reply.lower()
            or "no entendí" in reply.lower()
            or "entendí" in reply.lower()
        )

    def test_session_expires_and_resets(self):
        from datetime import timedelta

        h = self._handler()
        self._run(h.handle("+521", "1"))
        # Simular expiración
        h._memory_sessions["+521"].last_active -= timedelta(minutes=15)
        _ = self._run(h.handle("+521", "hola"))
        # La sesión expirada debe crear una nueva → bienvenida
        assert h._memory_sessions["+521"].state == State.IDLE

    def test_active_sessions_count(self):
        h = self._handler()
        for i in range(3):
            self._run(h.handle(f"+52{i}", "hola"))
        assert h.active_sessions_count() == 3


# ─────────────────────────────────────────
#  INTEGRACIÓN COMPLETA — GTFS DEMO + BOT
# ─────────────────────────────────────────


class TestFullIntegration:
    """Tests con GTFSLoader real cargado desde el GTFS de demo."""

    @pytest.fixture(autouse=True)
    def setup(self, engine):
        self.engine = engine

    def test_gtfs_has_routes(self, gtfs_loader):
        routes = gtfs_loader.get_routes()
        assert len(routes) >= 3
        ids = [r.route_id for r in routes]
        assert "MACRO01_R" in ids  # Macrobús L-1 del GTFS real de GDL

    def test_gtfs_has_stops(self, gtfs_loader):
        stops = gtfs_loader.search_stops("Orozco")
        assert len(stops) >= 1

    def test_gtfs_has_stop_times(self, gtfs_loader):
        route = gtfs_loader.find_route("MACRO01_R")
        assert route is not None
        departures = gtfs_loader.get_next_departures(
            route.route_id, "MACRO_001_003", 0, limit=3
        )
        assert len(departures) >= 1

    def test_eta_from_schedule_with_real_gtfs(self):
        result = asyncio.run(self.engine.get_eta("MACRO01_R", "Artes Plásticas"))
        assert result is not None
        assert result.estimated_minutes >= 0
        assert result.source == "schedule"

    def test_fleet_recommendation_with_real_engine(self):
        result = asyncio.run(self.engine.get_fleet_recommendation("R045", 3))
        assert result is not None
        assert result.route_id is not None
        from core.models import FleetAction

        assert result.action in list(FleetAction)

    def test_bot_eta_end_to_end(self):
        handler = ConversationHandler(self.engine)
        reply = asyncio.run(
            handler.handle("+521234567890", "¿Cuánto falta la MACRO01_R en Artes Plásticas?")
        )
        # Debe dar ETA real o mensaje de no encontrado — nunca crash
        assert isinstance(reply, str) and len(reply) > 10

    def test_menu_eta_accepts_gtfs_route_ids(self):
        handler = ConversationHandler(self.engine)
        asyncio.run(handler.handle("+521", "1"))
        asyncio.run(handler.handle("+521", "MACRO01_R"))
        reply = asyncio.run(handler.handle("+521", "Artes Plásticas"))
        assert "min" in reply.lower()

    def test_resolve_route_input_supports_gtfs_ids(self):
        handler = ConversationHandler(self.engine)
        assert handler._resolve_route_input("MACRO01_R") == "MACRO01_R"
        assert handler._resolve_route_input("MT_L1") == "MT_L1"
