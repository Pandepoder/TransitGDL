from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Query, WebSocket, WebSocketDisconnect

from api.deps import get_engine
from core import TransitEngine
from core.config import CONFIG
from core.map_catalog import (
    build_featured_shapes,
    build_route_catalog,
    build_route_shape,
    build_stop_layers,
    find_nearby_stops,
    pattern_geometry,
    route_color,
    route_mode,
    stop_to_feature,
)

router = APIRouter(prefix="/map", tags=["map"])

MAP_WS_INTERVAL = 2.0
VALID_LAYERS = frozenset({"metro", "sitren", "macro", "bus"})


def _region_key(region: str | None) -> str | None:
    return None if region in (None, "", "all") else region


def _compact(positions: list[dict]) -> list[list]:
    """
    Posiciones en arreglos cortos para el canal en vivo:
    [id, ruta, patrón, avance_m, lat, lon, rumbo, km/h, marca de tiempo en ms].
    """
    rows = []
    for p in positions:
        stamp = datetime.fromisoformat(p["timestamp"]).timestamp() * 1000
        rows.append(
            [
                p["vehicle_id"],
                p["route_id"],
                p.get("pattern_id"),
                p.get("progress_m"),
                round(p["lat"], 6),
                round(p["lon"], 6),
                p.get("bearing"),
                p.get("speed_kmh"),
                int(stamp),
            ]
        )
    return rows


async def _live_positions(engine, region: str | None = None) -> list[dict]:
    """Posiciones en vivo con los datos de su ruta; con `region`, solo las de la zona."""
    positions = await engine._gps.get_all_positions()
    store = engine._gtfs
    allowed = store.route_ids_in_region(region) if region else None
    items: list[dict] = []
    for pos in positions:
        if allowed is not None and pos.route_id not in allowed:
            continue
        route = store.get_route(pos.route_id)
        pattern = store.get_pattern(pos.pattern_id) if pos.pattern_id else None
        items.append(
            {
                **pos.model_dump(mode="json"),
                "short_name": route.route_short_name if route else pos.route_id,
                "headsign": pattern.headsign if pattern else None,
                "mode": route_mode(route),
                "color": route_color(route),
            }
        )
    return items


@router.get("/ping")
async def map_ping():
    return {"ok": True, "map_api": True}


@router.get("/bootstrap")
async def map_bootstrap(
    region: str | None = Query(
        "tonala_cucei",
        description="tonala_cucei | all — filtra rutas de la zona CUCEI/Tonalá",
    ),
):
    """
    Una sola llamada para cargar el mapa: configuración, rutas, trazos
    (polilíneas codificadas) y vehículos.
    """
    engine = get_engine()
    if isinstance(engine, TransitEngine) and engine._initialized:
        await engine.ensure_simulator()
    region_key = _region_key(region)
    positions = await _live_positions(engine, region_key)
    return {
        "ok": True,
        "region": region_key or "all",
        "simulator": engine.simulator_active,
        "vehicle_count": len(positions),
        "config": {"style_url": CONFIG.map_style_url},
        "feed": engine._gtfs.feed_info(),
        "routes": build_route_catalog(engine._gtfs, region=region_key),
        "shapes": build_featured_shapes(
            engine._gtfs, include_bus=False, region=region_key, encoded=True
        ),
        "positions": positions,
    }


@router.get("/network")
async def map_network():
    """Toda la red (rutas y trazos codificados), para cargarla bajo demanda."""
    engine = get_engine()
    return {
        "routes": build_route_catalog(engine._gtfs),
        "shapes": build_featured_shapes(engine._gtfs, include_bus=True, encoded=True),
    }


@router.get("/patterns")
async def map_patterns(
    ids: str = Query(..., max_length=4000, description="Patrones separados por coma"),
):
    """Trazos de patrones concretos (los usa la animación de unidades)."""
    store = get_engine()._gtfs
    items = []
    for pattern_id in dict.fromkeys(i.strip() for i in ids.split(",") if i.strip()):
        pattern = store.get_pattern(pattern_id)
        if pattern is not None:
            items.append(pattern_geometry(store, pattern, encoded=True))
        if len(items) >= 200:
            break
    return {"patterns": items}


@router.get("/catalog")
async def map_catalog():
    engine = get_engine()
    return {
        "routes": build_route_catalog(engine._gtfs),
        "simulator": engine.simulator_active,
        "vehicle_count": engine.live_vehicle_count,
    }


@router.get("/stops")
async def map_stops(
    layer: str | None = Query(
        None,
        description="metro, sitren, macro, bus — omitir para todas",
    ),
):
    engine = get_engine()
    if layer:
        parts = {part.strip() for part in layer.split(",") if part.strip()}
        wanted = (parts & VALID_LAYERS) or set(VALID_LAYERS)
    else:
        wanted = set(VALID_LAYERS)

    layers = build_stop_layers(engine._gtfs, wanted)
    payload: dict[str, dict] = {}
    total = 0
    for name, stops in layers.items():
        total += len(stops)
        payload[name] = {
            "type": "FeatureCollection",
            "features": [stop_to_feature(stop) for stop in stops],
            "count": len(stops),
        }
    return {"layers": payload, "total_stops": total}


@router.get("/positions")
async def map_positions(
    region: str | None = Query(None, description="Limitar a las rutas de una región"),
):
    engine = get_engine()
    if isinstance(engine, TransitEngine) and engine._initialized:
        await engine.ensure_simulator()
    items = await _live_positions(engine, _region_key(region))
    return {
        "count": len(items),
        "simulator": engine.simulator_active,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "positions": items,
    }


@router.get("/nearby")
async def map_nearby(
    lat: float = Query(..., ge=14, le=23),
    lon: float = Query(..., ge=-118, le=-86),
    radius_km: float = Query(0.65, ge=0.1, le=3.0),
    limit: int = Query(20, ge=1, le=50),
):
    engine = get_engine()
    stops = find_nearby_stops(engine._gtfs, lat, lon, radius_km=radius_km, limit=limit)
    return {
        "lat": lat,
        "lon": lon,
        "radius_km": radius_km,
        "count": len(stops),
        "stops": stops,
    }


@router.get("/routes/shapes")
async def map_featured_shapes(
    transit_only: bool = Query(True, description="Solo tren, Macrobús y SiTren"),
):
    engine = get_engine()
    shapes = build_featured_shapes(engine._gtfs, include_bus=not transit_only)
    return {"count": len(shapes), "shapes": shapes}


@router.get("/routes/{route_id}/detail")
async def map_route_detail(route_id: str):
    engine = get_engine()
    shape = build_route_shape(engine._gtfs, route_id)
    if shape is None:
        raise HTTPException(status_code=404, detail="Route not found")
    positions = await engine._gps.get_positions_for_route(shape["route_id"])
    incidents = engine.get_active_incidents(route_id=shape["route_id"])
    headway = engine.get_active_headway(shape["route_id"])
    return {
        **shape,
        "live_vehicles": [p.model_dump(mode="json") for p in positions],
        "live_count": len(positions),
        "active_incidents": [i.model_dump(mode="json") for i in incidents],
        "headway_minutes": headway,
        "eta_impact_note": (
            "Las incidencias activas en esta ruta suman ~2 min al ETA "
            "y reducen la confianza de la predicción."
            if incidents
            else None
        ),
    }


@router.get("/routes/{route_id}/shape")
async def map_route_shape(route_id: str):
    engine = get_engine()
    shape = build_route_shape(engine._gtfs, route_id)
    if shape is None:
        raise HTTPException(status_code=404, detail="Route not found")
    positions = await engine._gps.get_positions_for_route(shape["route_id"])
    shape["live_count"] = len(positions)
    return shape


@router.websocket("/ws")
async def map_live_ws(
    websocket: WebSocket, region: str | None = None, compact: bool = False
):
    """
    Posiciones en vivo cada pocos segundos. Con `compact=true` cada unidad va
    como arreglo corto (ver `_compact`) y el mensaje trae la hora del servidor.
    """
    await websocket.accept()
    engine = get_engine()
    if isinstance(engine, TransitEngine) and engine._initialized:
        await engine.ensure_simulator()
    region_key = _region_key(region)
    try:
        while True:
            positions = await _live_positions(engine, region_key)
            if compact:
                message = {
                    "type": "positions",
                    "t": int(time.time() * 1000),
                    "v": _compact(positions),
                }
            else:
                message = {
                    "type": "positions",
                    "simulator": engine.simulator_active,
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                    "positions": positions,
                }
            await websocket.send_json(message)
            await asyncio.sleep(MAP_WS_INTERVAL)
    except WebSocketDisconnect:
        return
    except Exception:
        await websocket.close()
