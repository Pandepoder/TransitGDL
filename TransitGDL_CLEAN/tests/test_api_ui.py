"""Pruebas de la API que usa la interfaz (Fase 3)."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from api.main import app
from core.geometry import decode_polyline
from core.models import BusPosition


@pytest.fixture
def client(engine, monkeypatch):
    """Cliente HTTP con el motor real sobre el GTFS de prueba."""
    startup = MagicMock()
    startup._initialized = True
    startup.initialize = AsyncMock()
    startup.close = AsyncMock()
    startup.cache_backend = "memory"
    monkeypatch.setattr("api.main.get_engine", lambda: startup)
    for module in ("map", "eta", "search", "fleet"):
        monkeypatch.setattr(f"api.routes.{module}.get_engine", lambda: engine)
    with TestClient(app) as test_client:
        yield test_client


def test_search_understands_route_at_place(client):
    body = client.get("/search", params={"q": "231 en CUCEI"}).json()

    assert body["intent"] == {"route": "231", "stop": "CUCEI"}
    assert [r["route_id"] for r in body["routes"]] == ["T15"]
    assert [p["name"] for p in body["places"]] == ["CUCEI"]
    assert body["stops"][0]["stop_name"] == "Cucei"


def test_search_finds_routes_by_prefix_and_legacy_number(client):
    by_number = client.get("/search", params={"q": "646"}).json()
    by_prefix = client.get("/search", params={"q": "T1"}).json()

    assert [r["route_id"] for r in by_number["routes"]] == ["T04A-1", "T04A-2"]
    assert by_number["intent"] is None
    # Las claves más cortas primero.
    assert [r["short_name"] for r in by_prefix["routes"]][:2] == ["T15", "T11B"]


def test_search_groups_stops_with_the_same_name(client):
    body = client.get("/search", params={"q": "artes plasticas"}).json()

    names = [s["stop_name"] for s in body["stops"]]
    assert names.count("Artes Plásticas") == 1
    assert "MC-L1" in body["stops"][0]["routes"]


def test_eta_options_one_per_direction(client):
    body = client.get("/eta/options", params={"route": "L3", "stop": "Cucei"}).json()

    headsigns = {o["headsign"] for o in body["options"]}
    assert headsigns == {"Arcos de Zapopan", "Central de Autobuses"}
    assert all(o["stop_lat"] and o["stop_lon"] for o in body["options"])
    missing = client.get(
        "/eta/options", params={"route": "T15", "stop": "Mercado del Mar"}
    )
    assert missing.status_code == 404


def test_arrivals_at_a_place_list_every_route(client):
    body = client.get("/eta/arrivals", params={"place": "CUCEI"}).json()

    assert body["place"]["name"] == "CUCEI"
    assert body["place"]["kind"] == "lugar"
    routes = {a["route_id"] for a in body["arrivals"]}
    assert {"T15", "MT_L3"} <= routes
    minutes = [
        (a["service_day_offset"], a["estimated_minutes"]) for a in body["arrivals"]
    ]
    assert minutes == sorted(minutes)
    assert client.get("/eta/arrivals", params={"place": "zzzz"}).status_code == 404


def test_bootstrap_sends_encoded_shapes_and_style(client):
    body = client.get("/map/bootstrap").json()

    assert body["config"]["style_url"].startswith("https://")
    shape = next(s for s in body["shapes"] if s["route_id"] == "T15")
    assert {p["headsign"] for p in shape["paths"]} == {"La Guadalupana", "La Coronilla"}
    path = shape["paths"][0]
    assert "coordinates" not in path and path["length_m"] > 20000
    lon, lat = decode_polyline(path["polyline"])[0][::-1]
    assert 20 < lat < 21 and -104 < lon < -103


def test_route_detail_sends_polyline_for_every_route(client):
    """El mapa solo puede dibujar y encuadrar una ruta si su detalle trae el trazo codificado."""
    for route in client.get("/map/network").json()["routes"]:
        detail = client.get(f"/map/routes/{route['route_id']}/detail").json()
        for path in detail["paths"]:
            points = decode_polyline(path["polyline"])
            assert len(points) >= 2, route["route_id"]
            assert isinstance(path["notes"], list)


def test_arrivals_by_stop_id_open_that_exact_stop(client):
    """Con varias paradas del mismo nombre, cada resultado abre la suya."""
    groups = client.get("/search", params={"q": "Revolucion", "limit": 10}).json()[
        "stops"
    ]
    assert len(groups) >= 2
    places = []
    for group in groups:
        body = client.get("/eta/arrivals", params={"stop_id": group["stop_id"]}).json()
        place = body["place"]
        assert group["stop_id"] in place["stop_ids"]
        assert (place["lat"], place["lon"]) == (group["lat"], group["lon"])
        for arrival in body["arrivals"]:
            assert arrival["stop_id"] in place["stop_ids"]
        places.append((place["lat"], place["lon"]))
    assert len(set(places)) == len(groups)

    assert client.get("/eta/arrivals", params={"stop_id": "NOPE"}).status_code == 404
    assert client.get("/eta/arrivals").status_code == 400


def test_fleet_without_a_count_reports_what_the_schedule_needs(client):
    plain = client.get("/fleet/recommend", params={"route_id": "231"}).json()
    assert plain["current_vehicles"] is None
    assert plain["coverage"] is None
    assert plain["recommended_vehicles"] > 0
    assert "unidades necesarias" in plain["rationale"]
    assert plain["action"] != "add_vehicles" or plain["demand_level"] in (
        "high",
        "critical",
    )

    short = client.get(
        "/fleet/recommend", params={"route_id": "231", "vehicles": 1}
    ).json()
    assert short["current_vehicles"] == 1
    assert short["action"] == "add_vehicles"
    assert short["coverage"] < 0.75


def test_bootstrap_is_compressed(client):
    response = client.get("/map/bootstrap", headers={"Accept-Encoding": "gzip"})
    assert response.headers.get("content-encoding") == "gzip"


def test_patterns_and_network_endpoints(client):
    patterns = client.get("/map/patterns", params={"ids": "T15:1,NOPE,T15:1"}).json()
    network = client.get("/map/network").json()

    assert [p["pattern_id"] for p in patterns["patterns"]] == ["T15:1"]
    assert len(network["routes"]) == 7
    assert {s["route_id"] for s in network["shapes"]} >= {"T15", "MC-L1"}


def test_live_socket_compact_format(client, engine):
    pattern = engine._gtfs.get_pattern("T15:1")
    lat, lon = pattern.line.point_at(5000)
    asyncio.run(
        engine._gps.update(
            BusPosition(
                vehicle_id="v1",
                route_id="T15",
                pattern_id="T15:1",
                progress_m=5000,
                bearing=90,
                lat=lat,
                lon=lon,
                speed_kmh=18,
            )
        )
    )
    try:
        with client.websocket_connect("/map/ws?compact=true") as ws:
            message = ws.receive_json()
    finally:
        engine._gps._memory.clear()

    assert message["type"] == "positions" and message["t"] > 0
    row = next(r for r in message["v"] if r[0] == "v1")
    assert row[1:4] == ["T15", "T15:1", 5000]
    assert row[6:8] == [90, 18]


def test_interface_files_are_served(client):
    page = client.get("/")
    assert page.status_code == 200
    assert '<script type="module" src="/static/js/main.js">' in page.text
    for path in ("/static/js/main.js", "/static/js/map.js", "/static/css/app.css"):
        assert client.get(path).status_code == 200
    # La versión de MapLibre está fijada (nunca "latest").
    assert "maplibre-gl@6.16.0" in client.get("/static/js/map.js").text
