from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Path,
    Query,
    WebSocket,
    WebSocketDisconnect,
)

from api.deps import get_engine, get_locale
from core.i18n import translate as t

router = APIRouter(prefix="/routes", tags=["routes"])

ROUTE_WS_INTERVAL = 1.0


@router.get("/")
async def list_routes(limit: int = Query(50, ge=1, le=200)):
    engine = get_engine()
    routes = engine._gtfs.get_routes()
    payload = [
        {
            "route_id": route.route_id,
            "short_name": route.route_short_name,
            "long_name": route.route_long_name,
            "route_type": route.route_type,
        }
        for route in routes[:limit]
    ]
    return {"count": len(routes), "routes": payload}


@router.get("/{route_id}/shape")
async def get_route_shape(
    route_id: str = Path(
        ..., min_length=1, max_length=32, pattern=r"^[A-Za-z0-9 _-]+$"
    ),
    locale: str = Depends(get_locale),
):
    engine = get_engine()
    route = engine._gtfs.find_route(route_id)
    if route is None:
        raise HTTPException(
            status_code=404,
            detail=t("errors.route_not_found", locale=locale),
        )

    stops = engine._gtfs.get_stops_for_route(route.route_id)
    if not stops:
        raise HTTPException(
            status_code=404,
            detail=t("errors.route_no_stops", locale=locale),
        )

    return {
        "route_id": route.route_id,
        "short_name": route.route_short_name,
        "long_name": route.route_long_name,
        "stops": [
            {
                "stop_id": stop.stop_id,
                "stop_name": stop.stop_name,
                "lat": stop.lat,
                "lon": stop.lon,
            }
            for stop in stops
        ],
    }


@router.get("/{route_id}/positions")
async def get_route_positions(
    route_id: str = Path(
        ..., min_length=1, max_length=32, pattern=r"^[A-Za-z0-9 _-]+$"
    ),
    locale: str = Depends(get_locale),
):
    engine = get_engine()
    route = engine._gtfs.find_route(route_id)
    if route is None:
        raise HTTPException(
            status_code=404,
            detail=t("errors.route_not_found", locale=locale),
        )

    positions = await engine._gps.get_positions_for_route(route.route_id)
    return {
        "route_id": route.route_id,
        "positions": [pos.model_dump(mode="json") for pos in positions],
    }


@router.websocket("/{route_id}/ws")
async def route_positions_ws(
    websocket: WebSocket,
    route_id: str = Path(
        ..., min_length=1, max_length=32, pattern=r"^[A-Za-z0-9 _-]+$"
    ),
):
    await websocket.accept()
    engine = get_engine()
    route = engine._gtfs.find_route(route_id)
    if route is None:
        await websocket.close(code=1008)
        return

    try:
        while True:
            positions = await engine._gps.get_positions_for_route(route.route_id)
            await websocket.send_json(
                {
                    "route_id": route.route_id,
                    "positions": [pos.model_dump(mode="json") for pos in positions],
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                }
            )
            await asyncio.sleep(ROUTE_WS_INTERVAL)
    except WebSocketDisconnect:
        return
    except Exception:
        await websocket.close()
