from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from api.deps import get_engine, get_locale
from core.i18n import translate as t

router = APIRouter(prefix="/fleet", tags=["fleet"])


@router.get("/recommend")
async def recommend(
    route_id: str = Query(
        ..., min_length=1, max_length=32, pattern=r"^[A-Za-z0-9 _-]+$"
    ),
    vehicles: Optional[int] = Query(
        None,
        ge=0,
        le=500,
        description="Flota en servicio (conteo del operador). Sin ella, solo se "
        "informa lo que pide el horario.",
    ),
    locale: str = Depends(get_locale),
):
    route_id = route_id.strip()
    if not route_id:
        raise HTTPException(
            status_code=400,
            detail=t("errors.invalid_route_id", locale=locale),
        )

    engine = get_engine()
    recommendation = await engine.get_fleet_recommendation(route_id, vehicles)
    return recommendation.model_dump(mode="json")
