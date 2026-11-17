from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, field_validator

from api.deps import get_engine, get_locale
from core.i18n import translate as t
from core.models import IncidentSeverity

router = APIRouter(prefix="/incidents", tags=["incidents"])

# Tipos anotados Pydantic v2 — equivalente a constr() pero sin la deprecación
# que causaba HTTP 500 en lugar de 422 al fallar la validación de lista.
_RouteStr = Annotated[
    str, Field(min_length=1, max_length=32, pattern=r"^[A-Za-z0-9 _-]+$")
]
_StopStr = Annotated[
    str, Field(min_length=1, max_length=32, pattern=r"^[A-Za-z0-9 _-]+$")
]


class IncidentCreate(BaseModel):
    description: str = Field(..., min_length=5, max_length=500)
    affected_routes: list[_RouteStr] = Field(default_factory=list)
    severity: IncidentSeverity = IncidentSeverity.MEDIUM
    stop_ids: list[_StopStr] = Field(default_factory=list)

    @field_validator("description")
    @classmethod
    def validate_description(cls, value: str) -> str:
        cleaned = value.strip()
        if len(cleaned) < 5:
            raise ValueError("La descripcion debe incluir mas contexto.")
        return cleaned


@router.get("/")
async def list_incidents(
    active_only: bool = Query(True),
    route_id: str | None = Query(
        None,
        min_length=1,
        max_length=32,
        pattern=r"^[A-Za-z0-9 _-]+$",
    ),
    stop_id: str | None = Query(
        None,
        min_length=1,
        max_length=32,
        pattern=r"^[A-Za-z0-9 _-]+$",
    ),
    limit: int = Query(50, ge=1, le=200),
):
    engine = get_engine()
    incidents = engine.list_incidents(
        active_only=active_only,
        route_id=route_id,
        stop_id=stop_id,
        limit=limit,
    )
    return {
        "count": len(incidents),
        "items": [incident.model_dump(mode="json") for incident in incidents],
    }


@router.post("/")
async def create_incident(payload: IncidentCreate):
    engine = get_engine()
    incident = engine.report_incident(
        payload.description,
        affected_routes=payload.affected_routes,
        severity=payload.severity,
        stop_ids=payload.stop_ids,
    )
    return {
        "incident": incident.model_dump(mode="json"),
        "alternate_routes": [
            alt.model_dump(mode="json")
            for route in payload.affected_routes
            for alt in engine.get_alternate_routes(route)
        ],
    }


@router.post("/{incident_id}/resolve")
async def resolve_incident(incident_id: str, locale: str = Depends(get_locale)):
    engine = get_engine()
    incident = engine.resolve_incident(incident_id)
    if incident is None:
        raise HTTPException(
            status_code=404,
            detail=t("errors.incident_not_found", locale=locale),
        )
    return {"incident": incident.model_dump(mode="json")}
