from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query

from api.deps import get_engine, get_locale
from core.config import CONFIG
from core.i18n import translate as t
from core.models import BusPosition, WeatherCondition

router = APIRouter(prefix="/demo", tags=["demo"])


def _require_development() -> None:
    if CONFIG.environment.strip().lower() == "production":
        raise HTTPException(status_code=403, detail="Demo controls are disabled in production.")


@router.post("/weather", summary="Simular clima (solo desarrollo)")
async def set_demo_weather(
    condition: str = Query(
        ...,
        description="clear, cloudy, rain, thunderstorm, fog, snow",
    ),
):
    _require_development()
    engine = get_engine()
    parsed = engine._weather._coerce_condition(condition)
    if parsed == WeatherCondition.UNKNOWN:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown weather condition: {condition}",
        )
    engine._weather.set_demo_override(parsed)
    return {"status": "ok", "condition": parsed.value}


@router.delete("/weather", summary="Restaurar clima real")
async def clear_demo_weather():
    _require_development()
    engine = get_engine()
    engine._weather.clear_demo_override()
    return {"status": "ok", "condition": None}


@router.get("/weather", summary="Clima activo (real o simulado)")
async def get_demo_weather():
    engine = get_engine()
    condition = await engine._weather.get_current_condition()
    return {
        "condition": condition.value,
        "simulated": engine._weather._demo_override is not None,
        "delay_minutes": engine._weather.get_delay_minutes(condition),
    }


@router.post("/inject-vehicle", summary="Inyectar vehículo cerca de una parada")
async def inject_vehicle(
    route_id: str = Query(..., min_length=1, max_length=32),
    stop: str | None = Query(None, description="Nombre de parada (ej. Artes Plásticas)"),
    lat: float | None = Query(None, ge=14, le=23),
    lon: float | None = Query(None, ge=-118, le=-86),
    speed_kmh: float = Query(42.0, ge=5, le=120),
    locale: str = Depends(get_locale),
):
    _require_development()
    engine = get_engine()
    route = engine._gtfs.find_route(route_id.strip())
    if route is None:
        raise HTTPException(
            status_code=404,
            detail=t("errors.eta_not_found", locale=locale),
        )

    if lat is None or lon is None:
        if not stop or not stop.strip():
            raise HTTPException(
                status_code=400,
                detail="Provide stop name or lat/lon coordinates.",
            )
        eta = await engine.get_eta(route.route_id, stop.strip())
        if eta is not None:
            stop_obj = engine._gtfs.get_stop(eta.stop_id)
            if stop_obj is not None:
                lat, lon = stop_obj.stop_lat, stop_obj.stop_lon
        if lat is None:
            q = stop.strip().lower()
            route_stops = engine._gtfs.get_stops_for_route(route.route_id)
            pick = next((s for s in route_stops if q in s.stop_name.lower()), None)
            if pick is None:
                matches = engine._gtfs.search_stops(stop.strip(), limit=5)
                pick = matches[0] if matches else None
            if pick is None:
                raise HTTPException(status_code=404, detail="Stop not found.")
            lat, lon = pick.stop_lat, pick.stop_lon

    position = BusPosition(
        vehicle_id=f"DEMO_{route.route_id}",
        route_id=route.route_id,
        lat=lat,
        lon=lon,
        speed_kmh=speed_kmh,
    )
    await engine._gps.update(position)
    return {
        "status": "injected",
        "vehicle_id": position.vehicle_id,
        "route_id": route.route_id,
        "lat": lat,
        "lon": lon,
    }


@router.get("/route-insights/{route_id}", summary="Demanda y flota recomendada")
async def route_insights(route_id: str):
    engine = get_engine()
    route = engine._gtfs.find_route(route_id)
    if route is None:
        raise HTTPException(status_code=404, detail="Route not found")
    live = await engine._gps.get_positions_for_route(route.route_id)
    current = len(live)
    snapshot = await engine.get_demand_snapshot(route.route_id, current)
    fleet = await engine.get_fleet_recommendation(route.route_id, current)
    condition = await engine._weather.get_current_condition()
    return {
        "route_id": route.route_id,
        "short_name": route.route_short_name,
        "live_vehicles": current,
        "weather": condition.value,
        "weather_simulated": engine._weather._demo_override is not None,
        "demand": snapshot.model_dump(mode="json"),
        "fleet": fleet.model_dump(mode="json"),
    }
