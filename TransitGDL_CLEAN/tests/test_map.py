from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from api.main import app
from core.map_catalog import build_stop_layers, route_layer


def test_route_layer_classification():
    assert route_layer("MT_L1", 0) == "metro"
    assert route_layer("ST_L3", 3) == "sitren"
    assert route_layer("MACRO01_R", 3) == "macro"
    assert route_layer("MC-A03", 3) == "feeder"
    assert route_layer("RG_646", 3) == "regional"


def test_build_stop_layers(engine):
    layers = build_stop_layers(engine._gtfs, {"metro", "macro"})
    assert layers["metro"]
    assert layers["macro"]
    assert len(layers["metro"]) >= 10
    assert any("MT_L1" in stop["route_ids"] for stop in layers["metro"])


def _patch_engine(monkeypatch, engine):
    engine.config.demo_simulator_enabled = False
    mock = MagicMock()
    mock._initialized = True
    mock.initialize = AsyncMock()
    mock.close = AsyncMock()
    mock.cache_backend = "memory"
    mock.simulator_active = False
    mock.live_vehicle_count = 0
    monkeypatch.setattr("api.main.get_engine", lambda: mock)

    def use_real():
        monkeypatch.setattr("api.routes.map.get_engine", lambda: engine)
        return engine

    return use_real


def test_map_stops_endpoint(engine, monkeypatch):
    _patch_engine(monkeypatch, engine)
    monkeypatch.setattr("api.routes.map.get_engine", lambda: engine)

    with TestClient(app) as client:
        response = client.get("/map/stops?layer=metro,macro")

    assert response.status_code == 200
    body = response.json()
    assert body["total_stops"] > 0
    assert body["layers"]["metro"]["count"] >= 10


def test_find_nearby_stops(engine):
    from core.map_catalog import find_nearby_stops

    # Coordenadas aprox. Estación Artes Plásticas / Macrobús
    stops = find_nearby_stops(engine._gtfs, 20.685, -103.356, radius_km=0.8, limit=10)
    assert len(stops) >= 1
    assert stops[0]["routes"]


def test_regional_overlay_routes_eta(engine):
    from core.regional_overlay import apply_regional_overlay

    apply_regional_overlay(engine._gtfs)

    async def run():
        for route_id, stop in [
            ("RG_55", "CUCEI"),
            ("RG_231", "CUCEI"),
            ("RG_646", "Loma Dorada"),
            ("RG_615", "Tonaltecas"),
        ]:
            result = await engine.get_eta(route_id, stop)
            assert result is not None, f"ETA failed for {route_id} @ {stop}"
            path = engine._gtfs.get_route_path(route_id)
            assert len(path) >= 10

    asyncio.run(run())


def test_regional_overlay_no_phantom_stops(engine):
    """Paradas fuera de corredor (Arcos/Patria Zapopan en 646) no deben existir."""
    from core.regional_overlay import OVERLAY_ROUTES, _max_stop_leg_km, apply_regional_overlay

    apply_regional_overlay(engine._gtfs)
    by_id = {r.route_id: r for r in OVERLAY_ROUTES}
    names_646 = {s.name for s in by_id["RG_646"].stops}
    assert "Arcos del Milenio" not in names_646
    assert "Av. Patria (Zapopan)" not in names_646
    assert _max_stop_leg_km(by_id["RG_646"].stops) <= 8.5
    assert _max_stop_leg_km(by_id["RG_231"].stops) <= 6.0
    assert _max_stop_leg_km(by_id["RG_615"].stops) <= 8.5


def test_map_positions_after_simulator_tick(engine):
    from core.demo_simulator import FleetSimulator

    async def run():
        sim = FleetSimulator(engine)
        await sim.start()
        await sim._tick(2.5)
        positions = await engine._gps.get_all_positions()
        await sim.stop()
        return positions

    positions = asyncio.run(run())
    assert len(positions) >= 20
    route_ids = {p.route_id for p in positions}
    assert "MT_L1" in route_ids
    assert "MACRO01_R" in route_ids
