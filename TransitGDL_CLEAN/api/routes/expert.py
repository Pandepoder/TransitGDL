from __future__ import annotations

from typing import Annotated, Any, Literal, Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from api.deps import get_engine

router = APIRouter(prefix="/expert", tags=["expert"])

Clima = Literal["clear", "cloudy", "rain", "thunderstorm", "fog", "snow"]
Dia = Literal["laboral", "sabado", "domingo", "festivo"]


class InferRequest(BaseModel):
    hechos: dict[str, Any] = Field(
        ...,
        description="Hechos iniciales, por ejemplo {'clima': 'rain', 'hora': 8}",
        max_length=40,
    )
    por_que_no: Optional[str] = Field(
        None, max_length=40, description="Acción a explicar si no se recomendó"
    )


def _validar_accion(accion: Optional[str]) -> None:
    if accion and accion not in get_engine().expert.base.acciones:
        raise HTTPException(status_code=400, detail=f"Acción desconocida: {accion}.")


@router.get("/diagnose", summary="Diagnóstico experto de una ruta")
async def diagnose(
    route_id: str = Query(..., min_length=1, max_length=32, pattern=r"^[\w -]+$"),
    vehicles: Optional[int] = Query(
        None,
        ge=0,
        le=500,
        description="Flota en servicio (conteo del operador). Sin ella, el "
        "diagnóstico no compara la flota: las unidades con GPS son una muestra.",
    ),
    clima: Annotated[
        Optional[Clima], Query(description="Escenario: forzar el clima")
    ] = None,
    hora: Optional[int] = Query(None, ge=0, le=23, description="Escenario: hora"),
    dia: Annotated[
        Optional[Dia],
        Query(description="Escenario: tipo de día (por omisión, hoy)"),
    ] = None,
    por_que_no: Optional[str] = Query(
        None,
        max_length=40,
        pattern=r"^[a-z_]+$",
        description="Explica por qué no se recomendó esta acción",
    ),
):
    """
    Reúne los hechos actuales de la ruta y ejecuta el motor de inferencia.
    `clima`, `hora` y `dia` permiten plantear escenarios "¿qué pasaría si…?", y
    `por_que_no` explica qué faltó para recomendar una acción.
    """
    _validar_accion(por_que_no)
    overrides: dict[str, Any] = {}
    if clima is not None:
        overrides["clima"] = clima
    if hora is not None:
        overrides["hora"] = hora
    if dia is not None:
        overrides["dia"] = dia
    escenario = dict(overrides)

    engine = get_engine()
    diagnostico = await engine.diagnose_route(
        route_id.strip(), vehicles=vehicles, overrides=overrides, por_que_no=por_que_no
    )
    if diagnostico is None:
        raise HTTPException(status_code=404, detail="Ruta no encontrada.")
    payload = diagnostico.model_dump(mode="json")
    payload["escenario"] = escenario
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
    _validar_accion(body.por_que_no)
    return experto.diagnosticar(body.hechos, por_que_no=body.por_que_no).model_dump(
        mode="json"
    )


@router.get("/rules", summary="Base de conocimiento")
async def rules():
    """Lista las reglas, los hechos iniciales y las acciones declaradas."""
    return get_engine().expert.base.to_dict()
