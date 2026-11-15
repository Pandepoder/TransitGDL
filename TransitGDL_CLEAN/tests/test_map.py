from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

from fastapi.testclient import TestClient

from api.main import app
from core.map_catalog import (
    build_featured_shapes,
    build_route_catalog,
    build_route_shape,
    build_stop_layers,
    find_nearby_stops,
    route_layer,
    route_mode,
)
from core.models import haversine_km


def test_route_layer_classification(gtfs_loader):
    layers = {
        route_id: route_layer(gtfs_loader.find_route(route_id))
        for route_id in ("MT_L3", "MC-L1", "T15", "C38-V1")
    }
    assert layers == {
        "MT_L3": "metro",
        "MC-L1": "macro",
        "T15": "troncal",
        "C38-V1": "complementaria",
    }
    assert route_mode(gtfs_loader.find_route("MT_L3")) == "train"
    assert route_mode(gtfs_loader.find_route("MC-L1")) == "brt"
    assert route_mode(gtfs_loader.find_route("T15")) == "bus"


def test_build_stop_layers(engine):
    layers = build_stop_layers(engine._gtfs, {"metro", "macro"})
    assert layers["metro"]
    assert layers["macro"]
    assert len(layers["metro"]) >= 10
    assert any("MT_L3" in stop["route_ids"] for stop in layers["metro"])


def test_route_catalog_exposes_legacy_number_and_directions(engine):
    catalog = {r["route_id"]: r for r in build_route_catalog(engine._gtfs)}

    t15 = catalog["T15"]
    assert t15["legacy_number"] == "231"
    assert t15["label"] == "T15 (antes 231)"
    assert {d["headsign"] for d in t15["directions"]} == {
        "La Guadalupana",
        "La Coronilla",
    }
    assert t15["color"].startswith("#")


def test_route_shape_has_both_directions(engine):
    shape = build_route_shape(engine._gtfs, "231")  # número antiguo

    assert shape["route_id"] == "T15"
    assert len(shape["paths"]) == 2
    assert all(len(path["coordinates"]) > 20 for path in shape["paths"])
    directions = {stop["direction_id"] for stop in shape["stops"]}
    assert directions == {0, 1}
    # GeoJSON: [lon, lat]
    lon, lat = shape["paths"][0]["coordinates"][0]
    assert -104 < lon < -103 and 20 < lat < 21


def test_region_shapes_omit_stops_to_stay_light(engine):
    shapes = build_featured_shapes(engine._gtfs, region="tonala_cucei")

    assert {s["route_id"] for s in shapes} >= {"T15", "T11B", "C38-V1", "MT_L3"}
    assert all("stops" not in shape for shape in shapes)


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
    monkeypatch.setattr("api.routes.map.get_engine", lambda: engine)


def test_map_stops_endpoint(engine, monkeypatch):
    _patch_engine(monkeypatch, engine)

    with TestClient(app) as client:
        response = client.get("/map/stops?layer=metro,macro")

    assert response.status_code == 200
    body = response.json()
    assert body["total_stops"] > 0
    assert body["layers"]["metro"]["count"] >= 10


def test_map_bootstrap_and_detail_endpoints(engine, monkeypatch):
    _patch_engine(monkeypatch, engine)

    with TestClient(app) as client:
        boot = client.get("/map/bootstrap")
        detail = client.get("/map/routes/231/detail")
        missing = client.get("/map/routes/NOPE/detail")

    assert boot.status_code == 200
    payload = boot.json()
    assert payload["region"] == "tonala_cucei"
    assert payload["feed"]["feed_end"] == "20241231"
    assert {r["route_id"] for r in payload["routes"]} >= {"T15", "T11B"}
    assert detail.status_code == 200
    assert detail.json()["route_id"] == "T15"
    assert (
        detail.json()["headway_minutes"] is None or detail.json()["headway_minutes"] > 0
    )
    assert missing.status_code == 404


def test_find_nearby_stops(engine):
    cucei = engine._gtfs.find_landmark("CUCEI")
    stops = find_nearby_stops(
        engine._gtfs, cucei.lat, cucei.lon, radius_km=0.5, limit=10
    )

    assert len(stops) >= 1
    assert stops[0]["distance_m"] <= 500
    assert stops[0]["routes"]
    assert stops == sorted(stops, key=lambda s: s["distance_km"])


def test_simulator_publishes_both_directions_with_bearing(engine):
    from core.demo_simulator import FleetSimulator

    async def run():
        sim = FleetSimulator(engine, seed=7)
        await sim.start()
        await sim._tick(2.5)
        positions = await engine._gps.get_all_positions()
        await sim.stop()
        return positions

    try:
        positions = asyncio.run(run())
    finally:
        engine._gps._memory.clear()

    assert len(positions) >= 12
    # Solo se simulan las rutas de la zona CUCEI/Tonalá (MC-L1 queda fuera).
    assert {p.route_id for p in positions} >= {"MT_L3", "T15", "T11B"}
    assert "MC-L1" not in {p.route_id for p in positions}
    t15_patterns = {p.pattern_id for p in positions if p.route_id == "T15"}
    assert t15_patterns == {"T15:0", "T15:1"}
    assert all(p.bearing is not None and 0 <= p.bearing < 360 for p in positions)
    assert all(p.progress_m is not None and p.progress_m >= 0 for p in positions)


def test_simulator_vehicles_never_teleport(engine):
    """Una unidad solo salta al dar la vuelta en la terminal, y poco."""
    from core.demo_simulator import FleetSimulator

    sim = FleetSimulator(engine, seed=11)

    async def start():
        await sim.start()
        await sim.stop()

    try:
        asyncio.run(start())
    finally:
        engine._gps._memory.clear()

    step = 10.0
    last = {v.vehicle_id: v.to_position() for v in sim.vehicles}
    turnarounds = 0
    for _ in range(1500):  # más de cuatro horas simuladas
        before = {v.vehicle_id: v.pattern.pattern_id for v in sim.vehicles}
        sim.step(step)
        for vehicle in sim.vehicles:
            position = vehicle.to_position()
            previous = last[vehicle.vehicle_id]
            jump_m = (
                haversine_km(previous.lat, previous.lon, position.lat, position.lon)
                * 1000
            )
            if before[vehicle.vehicle_id] != vehicle.pattern.pattern_id:
                turnarounds += 1
                # Las terminales de ida y vuelta están cerca entre sí.
                assert jump_m < 3000, (vehicle.vehicle_id, jump_m)
            else:
                # En marcha: nunca más rápido que 110 km/h.
                assert jump_m <= 110 / 3.6 * step, (vehicle.vehicle_id, jump_m)
            last[vehicle.vehicle_id] = position
    assert turnarounds > 0
