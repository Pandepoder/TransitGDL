from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query

from api.deps import get_engine, get_locale
from core.i18n import translate as t

router = APIRouter(prefix="/eta", tags=["eta"])


@router.get("/")
async def get_eta(
    route: str = Query(..., min_length=1, max_length=32, pattern=r"^[A-Za-z0-9 _-]+$"),
    stop: str = Query(..., min_length=2, max_length=120),
    locale: str = Depends(get_locale),
):
    engine = get_engine()
    route = route.strip()
    stop = stop.strip()
    if not route:
        raise HTTPException(
            status_code=400, detail=t("errors.invalid_route", locale=locale)
        )
    if len(stop) < 2:
        raise HTTPException(
            status_code=400, detail=t("errors.invalid_stop", locale=locale)
        )

    result = await engine.get_eta(route, stop)
    if result is None:
        raise HTTPException(
            status_code=404, detail=t("errors.eta_not_found", locale=locale)
        )
    return result.model_dump(mode="json")


@router.get("/stops/search")
async def search_stops(
    q: str = Query(..., min_length=2, max_length=120),
    limit: int = Query(5, ge=1, le=10),
    locale: str = Depends(get_locale),
):
    engine = get_engine()
    q = q.strip()
    if not q:
        raise HTTPException(
            status_code=400, detail=t("errors.empty_query", locale=locale)
        )
    matches = engine.search_stops_with_routes(q, limit=limit)
    return {"query": q, "results": [match.model_dump(mode="json") for match in matches]}


@router.get("/stops/routes")
async def routes_at_stop(
    stop: str = Query(..., min_length=2, max_length=120),
    locale: str = Depends(get_locale),
):
    engine = get_engine()
    stop = stop.strip()
    if not stop:
        raise HTTPException(
            status_code=400, detail=t("errors.invalid_stop", locale=locale)
        )
    matches = engine.search_stops_with_routes(stop, limit=1)
    if not matches:
        raise HTTPException(
            status_code=404, detail=t("errors.stop_not_found", locale=locale)
        )
    match = matches[0]
    return match.model_dump(mode="json")
