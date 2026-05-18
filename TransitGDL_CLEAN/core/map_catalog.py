from __future__ import annotations

from typing import Any

from .gtfs_loader import GTFSLoader
from .models import Route, Stop, haversine_km

METRO_TRAIN_IDS = frozenset({"MT_L1", "MT_L2", "MT_L3"})
METRO_TRAM_IDS = frozenset({"ST_L1", "ST_L1B", "ST_L2", "ST_L3", "ST_L4"})
MACRO_IDS = frozenset({"MACRO01_R", "MACRO02_E"})

# Paleta apagada y distinguible (evita neones que se mezclan en el mapa).
ROUTE_COLORS: dict[str, str] = {
    "MACRO01_R": "#6BA3C7",
    "MACRO02_E": "#7A9BB5",
    "MT_L1": "#A898C9",
    "MT_L2": "#B5A8D4",
    "MT_L3": "#D4A088",
    "ST_L1": "#6DB5A8",
    "ST_L1B": "#7DBFB3",
    "ST_L2": "#8DC9BE",
    "ST_L3": "#9DD3C9",
    "ST_L4": "#C99A8F",
    # Alimentadoras Mi Macro (visibles sobre mapa oscuro)
    "MC-L1": "#E8C547",
    "MC-L1E": "#D4B23E",
    "MP-A04": "#7EC8E8",
    "MP-A05-2": "#6BB8DA",
    # Líneas urbanas overlay (55, 231, 646, 615)
    "RG_55": "#E9C46A",
    "RG_231": "#4CC9F0",
    "RG_646": "#F4A261",
    "RG_615": "#90BE6D",
}

MODE_BY_ROUTE: dict[str, str] = {
    "MACRO01_R": "brt",
    "MACRO02_E": "brt",
    "MT_L1": "train",
    "MT_L2": "train",
    "MT_L3": "train",
    "ST_L1": "tram",
    "ST_L1B": "tram",
    "ST_L2": "tram",
    "ST_L3": "tram",
    "ST_L4": "tram",
}


def route_layer(route_id: str, route_type: int = 3) -> str:
    if route_id.startswith("RG_"):
        return "regional"
    if route_id in METRO_TRAIN_IDS or route_type == 0:
        return "metro"
    if route_id in METRO_TRAM_IDS or route_id.startswith("ST_"):
        return "sitren"
    if route_id in MACRO_IDS or route_id.startswith("MACRO"):
        return "macro"
    if route_id.startswith("MC-") or route_id.startswith("MP-"):
        return "feeder"
    return "bus"


def route_color(route_id: str) -> str:
    if route_id in ROUTE_COLORS:
        return ROUTE_COLORS[route_id]
    if route_id.startswith("MC-"):
        return "#C9B458"
    if route_id.startswith("MP-"):
        return "#6EB5D8"
    return "#8A96A3"


def route_mode(route_id: str, route_type: int = 3) -> str:
    if route_id in MODE_BY_ROUTE:
        return MODE_BY_ROUTE[route_id]
    if route_type == 0:
        return "train"
    return "bus"


def build_route_catalog(
    gtfs: GTFSLoader, *, region: str | None = None
) -> list[dict[str, Any]]:
    from .regional_overlay import regional_route_ids

    allowed: set[str] | None = None
    if region == "tonala_cucei":
        allowed = regional_route_ids(gtfs)

    catalog: list[dict[str, Any]] = []
    for route in gtfs.get_routes():
        if allowed is not None and route.route_id not in allowed:
            continue
        stops = gtfs.get_stops_for_route(route.route_id)
        catalog.append(
            {
                "route_id": route.route_id,
                "short_name": route.route_short_name,
                "long_name": route.route_long_name,
                "route_type": route.route_type,
                "layer": route_layer(route.route_id, route.route_type),
                "mode": route_mode(route.route_id, route.route_type),
                "color": route_color(route.route_id),
                "stop_count": len(stops),
            }
        )
    return catalog


def build_stop_layers(
    gtfs: GTFSLoader, layers: set[str] | None = None
) -> dict[str, list[dict[str, Any]]]:
    wanted = layers or {"metro", "sitren", "macro", "bus"}
    buckets: dict[str, dict[str, dict[str, Any]]] = {
        key: {} for key in wanted
    }

    for route in gtfs.get_routes():
        layer = route_layer(route.route_id, route.route_type)
        if layer not in buckets:
            continue
        for stop in gtfs.get_stops_for_route(route.route_id):
            entry = buckets[layer].get(stop.stop_id)
            if entry is None:
                entry = {
                    "stop_id": stop.stop_id,
                    "stop_name": stop.stop_name,
                    "lat": stop.lat,
                    "lon": stop.lon,
                    "route_ids": [],
                    "layer": layer,
                }
                buckets[layer][stop.stop_id] = entry
            if route.route_id not in entry["route_ids"]:
                entry["route_ids"].append(route.route_id)

    return {layer: list(stops.values()) for layer, stops in buckets.items()}


def stop_to_feature(stop: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "Feature",
        "geometry": {"type": "Point", "coordinates": [stop["lon"], stop["lat"]]},
        "properties": {
            "stop_id": stop["stop_id"],
            "stop_name": stop["stop_name"],
            "layer": stop["layer"],
            "route_ids": stop.get("route_ids", []),
        },
    }


def build_featured_shapes(
    gtfs: GTFSLoader,
    *,
    include_bus: bool = False,
    region: str | None = None,
) -> list[dict[str, Any]]:
    from .regional_overlay import regional_route_ids, route_serves_tonala_cucei

    regional_ids: set[str] | None = None
    if region == "tonala_cucei":
        regional_ids = regional_route_ids(gtfs)

    shapes: list[dict[str, Any]] = []
    for route in gtfs.get_routes():
        layer = route_layer(route.route_id, route.route_type)
        if layer == "bus" and not include_bus:
            continue
        if regional_ids is not None and route.route_id not in regional_ids:
            continue
        shape = build_route_shape(gtfs, route.route_id)
        if shape is not None:
            shapes.append(shape)
    return shapes


def find_nearby_stops(
    gtfs: GTFSLoader,
    lat: float,
    lon: float,
    *,
    radius_km: float = 0.65,
    limit: int = 20,
) -> list[dict[str, Any]]:
    gtfs._assert_loaded()
    scored: list[tuple[float, dict[str, Any]]] = []

    for stop in gtfs.get_stops():
        distance_km = haversine_km(lat, lon, stop.lat, stop.lon)
        if distance_km > radius_km:
            continue
        routes = gtfs.get_routes_at_stop(stop.stop_id)
        if not routes:
            continue
        scored.append(
            (
                distance_km,
                {
                    "stop_id": stop.stop_id,
                    "stop_name": stop.stop_name,
                    "lat": stop.lat,
                    "lon": stop.lon,
                    "distance_km": round(distance_km, 3),
                    "distance_m": int(distance_km * 1000),
                    "routes": [
                        {
                            "route_id": route.route_id,
                            "short_name": route.route_short_name,
                            "long_name": route.route_long_name,
                            "color": route_color(route.route_id),
                            "mode": route_mode(route.route_id, route.route_type),
                            "layer": route_layer(route.route_id, route.route_type),
                        }
                        for route in routes
                    ],
                },
            )
        )

    scored.sort(key=lambda item: item[0])
    return [item[1] for item in scored[:limit]]


def build_route_shape(gtfs: GTFSLoader, route_id: str) -> dict[str, Any] | None:
    from .debug_log import agent_log

    route = gtfs.find_route(route_id)
    if route is None:
        return None
    stops: list[Stop] = gtfs.get_ordered_stops_for_route(route.route_id)
    path = gtfs.get_route_path(route.route_id)
    if not stops and not path:
        return None
    coordinates = (
        [[lon, lat] for lat, lon in path]
        if path
        else [[s.lon, s.lat] for s in stops]
    )
    # #region agent log
    if coordinates:
        agent_log(
            "core/map_catalog.py:build_route_shape",
            "shape_built",
            {
                "route_id": route.route_id,
                "short_name": route.route_short_name,
                "point_count": len(coordinates),
                "first": coordinates[0],
                "last": coordinates[-1],
            },
            hypothesis_id="B",
        )
    # #endregion
    return {
        "route_id": route.route_id,
        "short_name": route.route_short_name,
        "long_name": route.route_long_name,
        "layer": route_layer(route.route_id, route.route_type),
        "mode": route_mode(route.route_id, route.route_type),
        "color": route_color(route.route_id),
        "coordinates": coordinates,
        "stops": [
            {
                "stop_id": s.stop_id,
                "stop_name": s.stop_name,
                "lat": s.lat,
                "lon": s.lon,
            }
            for s in stops
        ],
    }
