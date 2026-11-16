"""Búsqueda unificada: rutas (clave o número antiguo), lugares y paradas."""

from __future__ import annotations

import re

from fastapi import APIRouter, Query

from api.deps import get_engine
from core.map_catalog import route_summary
from core.models import haversine_km

router = APIRouter(tags=["search"])

# "231 en CUCEI", "T15 por Minerva", "la 646 hacia Tonalá"
_INTENT_RE = re.compile(
    r"^(?:la\s+|ruta\s+)?(?P<route>[\w-]+)\s+(?:en|por|hacia|a)\s+(?P<stop>.+)$",
    re.IGNORECASE,
)


def _matching_routes(store, text: str, limit: int) -> list:
    """Rutas por alias exacto y, si faltan, por prefijo de clave o número antiguo."""
    found = list(store.find_routes(text))
    normalized = store._normalize(text).replace(" ", "")
    if len(found) < limit and normalized:
        prefix = []
        for route in store.get_routes():
            if route in found:
                continue
            short = store._normalize(route.route_short_name).replace(" ", "")
            legacy = (route.legacy_number or "").lower()
            if short.startswith(normalized) or (
                legacy and legacy.startswith(normalized)
            ):
                prefix.append(route)
        # Las claves más cortas primero: "T1" antes que "T10-C01".
        prefix.sort(key=lambda r: (len(r.route_short_name), r.route_short_name))
        found.extend(prefix)
    return found[:limit]


def _matching_places(store, text: str, limit: int) -> list[dict]:
    normalized = store._normalize(text)
    if len(normalized) < 2:
        return []
    places = []
    for landmark in store.get_landmarks():
        if any(
            alias.startswith(normalized) or f" {normalized}" in f" {alias}"
            for alias in landmark.aliases
        ):
            places.append(
                {
                    "name": landmark.name,
                    "kind": landmark.kind,
                    "lat": landmark.lat,
                    "lon": landmark.lon,
                }
            )
    return places[:limit]


def _matching_stops(store, text: str, limit: int) -> list[dict]:
    """Paradas por nombre; las del mismo nombre en el mismo cruce se agrupan."""
    groups: list[dict] = []
    for stop in store.search_stops(text, limit=limit * 6):
        same = next(
            (
                g
                for g in groups
                if g["stop_name"] == stop.stop_name
                and haversine_km(g["lat"], g["lon"], stop.lat, stop.lon) <= 0.4
            ),
            None,
        )
        labels = [route.label for route in store.get_routes_at_stop(stop.stop_id)]
        if same is None:
            groups.append(
                {
                    "stop_id": stop.stop_id,
                    "stop_name": stop.stop_name,
                    "lat": stop.lat,
                    "lon": stop.lon,
                    "routes": labels,
                }
            )
        else:
            same["routes"] = list(dict.fromkeys(same["routes"] + labels))
        if len(groups) >= limit:
            break
    return groups


@router.get("/search", summary="Buscar rutas, lugares y paradas")
async def search(
    q: str = Query(..., min_length=1, max_length=120),
    limit: int = Query(6, ge=1, le=20),
):
    """
    Busca en un solo paso. Acepta claves oficiales (T15), números antiguos
    (231), lugares (CUCEI) y nombres de parada. Una consulta del tipo
    "231 en CUCEI" se devuelve además como `intent` (ruta y parada).
    """
    store = get_engine()._gtfs
    text = q.strip()

    intent = None
    match = _INTENT_RE.match(text)
    if match and store.find_routes(match.group("route")):
        intent = {"route": match.group("route"), "stop": match.group("stop").strip()}

    # "231 en CUCEI": la ruta y el lugar se buscan cada uno por su lado.
    route_text = intent["route"] if intent else text
    place_text = intent["stop"] if intent else text
    return {
        "query": text,
        "intent": intent,
        "routes": [
            route_summary(r) for r in _matching_routes(store, route_text, limit)
        ],
        "places": _matching_places(store, place_text, limit),
        "stops": _matching_stops(store, place_text, limit),
    }
