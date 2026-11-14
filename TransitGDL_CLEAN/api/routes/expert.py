from __future__ import annotations

from typing import Any, Literal, Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from api.deps import get_engine

router = APIRouter(prefix="/expert", tags=["expert"])

Clima = Literal["clear", "cloudy", "rain", "thunderstorm", "fog", "snow"]


class InferRequest(BaseModel):
    hechos: dict[str, Any] = Field(
        ...,
        description="Hechos iniciales, por ejemplo {'clima': 'rain', 'hora': 8}",
        max_length=40,
    )


@router.get("/diagnose", summary="Diagnóstico experto de una ruta")
async def diagnose(
    route_id: str = Query(
        ..., min_length=1, max_length=32, pattern=r"^[A-Za-z0-9 _-]+$"
    ),
    vehicles: Optional[int] = Query(
        None, ge=0, le=500, description="Vehículos en ruta (por omisión, los en vivo)"
    ),
    clima: Optional[Clima] = Query(None, description="Escenario: forzar el clima"),
    hora: Optional[int] = Query(None, ge=0, le=23, description="Escenario: hora"),
):
    """
    Reúne los hechos actuales de la ruta y ejecuta el motor de inferencia.
    `clima` y `hora` permiten plantear escenarios "¿qué pasaría si…?".
    """
    overrides: dict[str, Any] = {}
    if clima is not None:
        overrides["clima"] = clima
    if hora is not None:
        overrides["hora"] = hora

    engine = get_engine()
    diagnostico = await engine.diagnose_route(
        route_id.strip(), vehicles=vehicles, overrides=overrides
    )
    if diagnostico is None:
        raise HTTPException(status_code=404, detail="Ruta no encontrada.")
    payload = diagnostico.model_dump(mode="json")
    payload["escenario"] = overrides
    return payload


@router.post("/infer", summary="Inferencia sobre hechos arbitrarios")
async def infer(body: InferRequest):
    """Consulta directa al motor: recibe hechos y devuelve el diagnóstico."""
    experto = get_engine().expert
    desconocidos = experto.validar_hechos(body.hechos)
    if desconocidos:
        raise HTTPException(
            status_code=400,
            detail="Hechos no declarados en la base de conocimiento: "
            + ", ".join(desconocidos),
        )
    for nombre, valor in body.hechos.items():
        if valor is not None and not isinstance(valor, (str, int, float, bool)):
            raise HTTPException(
                status_code=400,
                detail=f"El hecho '{nombre}' debe ser texto, número o booleano.",
            )
    return experto.diagnosticar(body.hechos).model_dump(mode="json")


@router.get("/rules", summary="Base de conocimiento")
async def rules():
    """Lista las reglas, los hechos iniciales y las acciones declaradas."""
    return get_engine().expert.base.to_dict()
