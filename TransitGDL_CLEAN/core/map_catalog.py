"""Catálogo de rutas, trazos y paradas que consume el mapa."""

from __future__ import annotations

from typing import Any

from .models import Route
from .transit import TransitStore

# Capa del mapa según el grupo de la ruta en la base de transporte.
LAYER_BY_GROUP: dict[str, str] = {
    "tren": "metro",
    "macro": "macro",
    "sitren": "sitren",
    "alimentadora": "feeder",
    "troncal": "troncal",
    "complementaria": "complementaria",
}
# Icono de vehículo según el modo.
ICON_BY_MODE: dict[str, str] = {
    "tren": "train",
    "brt": "brt",
    "trolebus": "bus",
    "bus": "bus",
}
DEFAULT_COLOR = "#8A96A3"
MASS_TRANSIT_LAYERS = frozenset({"metro", "macro", "sitren"})


def route_layer(route: Route) -> str:
    return LAYER_BY_GROUP.get(route.group, "bus")


def route_color(route: Route | None) -> str:
    return (route.color if route else None) or DEFAULT_COLOR


def route_mode(route: Route | None) -> str:
    return ICON_BY_MODE.get(route.mode if route else "bus", "bus")


def route_summary(route: Route) -> dict[str, Any]:
    return {
        "route_id": route.route_id,
        "short_name": route.route_short_name,
        "long_name": route.route_long_name,
        "legacy_number": route.legacy_number,
        "branch_name": route.branch_name,
        "label": route.label,
        "route_type": route.route_type,
        "layer": route_layer(route),
        "mode": route_mode(route),
        "color": route_color(route),
    }


def build_route_catalog(
    store: TransitStore, *, region: str | None = None
) -> list[dict[str, Any]]:
    catalog: list[dict[str, Any]] = []
    for route in store.get_routes(region):
        patterns = store.get_patterns(route.route_id, main_only=True)
        catalog.append(
            {
                **route_summary(route),
                "stop_count": len(store.get_stops_for_route(route.route_id)),
                "directions": [
                    {
                        "pattern_id": pattern.pattern_id,
                        "direction_id": pattern.direction_id,
                        "headsign": pattern.headsign,
                        "notes": pattern.notes,
                    }
                    for pattern in patterns
                ],
            }
        )
    return catalog


def pattern_geometry(store: TransitStore, pattern, *, encoded: bool) -> dict[str, Any]:
    """Trazo de un patrón para el mapa: polilínea codificada o lista [lon, lat]."""
    payload: dict[str, Any] = {
        "pattern_id": pattern.pattern_id,
        "route_id": pattern.route_id,
        "direction_id": pattern.direction_id,
        "headsign": pattern.headsign,
        "length_km": round(pattern.length_m / 1000.0, 1),
        # Longitud exacta: la usa la animación para ubicar el avance de cada unidad.
        "length_m": round(pattern.length_m, 1),
        # El feed no trae un trazo confiable: se unieron las paradas.
        "approximate": pattern.shape_source == "paradas",
    }
    if encoded:
        payload["polyline"] = pattern.map_polyline
    else:
        payload["coordinates"] = store.map_coordinates(pattern)
    return payload


def build_route_shape(
    store: TransitStore,
    route_id: str,
    *,
    include_stops: bool = True,
    encoded: bool = False,
) -> dict[str, Any] | None:
    """
    Trazos de la ruta (uno por sentido) y, si se pide, sus paradas. Con
    `encoded` los trazos van como polilínea codificada en lugar de coordenadas.
    """
    route = store.find_route(route_id)
    if route is None:
        return None
    patterns = store.get_patterns(route.route_id, main_only=True) or store.get_patterns(
        route.route_id
    )
    if not patterns:
        return None

    paths = [pattern_geometry(store, p, encoded=encoded) for p in patterns]
    payload: dict[str, Any] = {**route_summary(route), "paths": paths}
    if not encoded:
        payload["coordinates"] = paths[0]["coordinates"]
    if include_stops:
        stops: list[dict[str, Any]] = []
        for pattern in patterns:
            for sequence, stop_id in enumerate(pattern.stop_ids):
                stop = store.get_stop(stop_id)
                if stop is None:
                    continue
                stops.append(
                    {
                        "stop_id": stop.stop_id,
                        "stop_name": stop.stop_name,
                        "lat": stop.lat,
                        "lon": stop.lon,
                        "direction_id": pattern.direction_id,
                        "headsign": pattern.headsign,
                        "pattern_id": pattern.pattern_id,
                        "sequence": sequence,
                    }
                )
        payload["stops"] = stops
    return payload


def build_featured_shapes(
    store: TransitStore,
    *,
    include_bus: bool = False,
    region: str | None = None,
    encoded: bool = False,
) -> list[dict[str, Any]]:
    """
    Trazos para dibujar la red. Con `region` se incluyen todas las rutas de la
    zona; sin ella, solo tren, Macrobús y SiTren salvo que se pida `include_bus`.
    """
    shapes: list[dict[str, Any]] = []
    for route in store.get_routes(region):
        if (
            region is None
            and not include_bus
            and route_layer(route) not in MASS_TRANSIT_LAYERS
        ):
            continue
        shape = build_route_shape(
            store, route.route_id, include_stops=False, encoded=encoded
        )
        if shape is not None:
            shapes.append(shape)
    return shapes


def build_stop_layers(
    store: TransitStore, layers: set[str] | None = None
) -> dict[str, list[dict[str, Any]]]:
    wanted = layers or {"metro", "sitren", "macro", "bus"}
    buckets: dict[str, dict[str, dict[str, Any]]] = {key: {} for key in wanted}

    for route in store.get_routes():
        layer = route_layer(route)
        if layer not in MASS_TRANSIT_LAYERS:
            layer = "bus"
        if layer not in buckets:
            continue
        for stop in store.get_stops_for_route(route.route_id):
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


def find_nearby_stops(
    store: TransitStore,
    lat: float,
    lon: float,
    *,
    radius_km: float = 0.65,
    limit: int = 20,
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for stop, distance_km in store.stops_near(lat, lon, radius_km, limit=limit):
        routes = store.get_routes_at_stop(stop.stop_id)
        if not routes:
            continue
        results.append(
            {
                "stop_id": stop.stop_id,
                "stop_name": stop.stop_name,
                "lat": stop.lat,
                "lon": stop.lon,
                "distance_km": round(distance_km, 3),
                "distance_m": int(distance_km * 1000),
                "routes": [route_summary(route) for route in routes],
            }
        )
    return results
