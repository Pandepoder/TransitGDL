"""Trazado de ruta sobre vialidades vía OSRM (fallback a waypoints locales)."""
from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from typing import Sequence

logger = logging.getLogger(__name__)

_OSRM_BASE = "https://router.project-osrm.org/route/v1/driving"
_CACHE: dict[str, list[tuple[float, float]]] = {}


def fetch_road_path(
    waypoints: Sequence[tuple[float, float]],
    *,
    route_id: str = "",
) -> list[tuple[float, float]] | None:
    """
    waypoints: secuencia (lat, lon).
    Devuelve lista (lat, lon) siguiendo calles, o None si OSRM falla.
    """
    if len(waypoints) < 2:
        return None

    cache_key = route_id or str(len(waypoints))
    if cache_key in _CACHE:
        return list(_CACHE[cache_key])

    # OSRM: lon,lat pairs separated by ;
    coord_str = ";".join(f"{lon:.6f},{lat:.6f}" for lat, lon in waypoints)
    url = f"{_OSRM_BASE}/{coord_str}?overview=full&geometries=geojson"
    try:
        with urllib.request.urlopen(url, timeout=12) as response:
            payload = json.loads(response.read().decode())
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
        logger.debug("OSRM no disponible para %s: %s", route_id or "route", exc)
        return None

    if payload.get("code") != "Ok":
        return None

    routes = payload.get("routes") or []
    if not routes:
        return None

    coords = routes[0].get("geometry", {}).get("coordinates") or []
    if len(coords) < 2:
        return None

    path = [(lat, lon) for lon, lat in coords]
    if route_id:
        _CACHE[route_id] = path
    return path
