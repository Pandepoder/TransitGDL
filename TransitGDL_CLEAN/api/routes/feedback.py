from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, field_validator

from api.deps import get_engine, get_locale
from api.rate_limiter import check_feedback_rate_limit
from core.i18n import translate as t

router = APIRouter(prefix="/feedback", tags=["feedback"])


class FeedbackCreate(BaseModel):
    prediction_id: str = Field(
        ..., min_length=8, max_length=64, pattern=r"^[A-Za-z0-9_-]+$"
    )
    # Opción A: tiempo real en minutos (más preciso)
    actual_minutes: Optional[float] = Field(None, ge=0, le=300)
    # Opción B: calificación cualitativa
    #   1 = llegó antes de lo estimado
    #   2 = llegó puntual
    #   3 = tardó un poco (≈5 min extra)
    #   4 = tardó bastante (≈12 min extra)
    #   5 = tardó mucho (≈20 min extra)
    rating: Optional[int] = Field(None, ge=1, le=5)

    @field_validator("prediction_id")
    @classmethod
    def validate_prediction_id(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned or not value:
            raise ValueError("prediction_id inválido.")
        return cleaned

    def model_post_init(self, __context) -> None:
        if self.actual_minutes is None and self.rating is None:
            raise ValueError("Debes indicar actual_minutes o rating.")


@router.post(
    "/",
    summary="Calificar una predicción",
    dependencies=[Depends(check_feedback_rate_limit)],
)
async def submit_feedback(payload: FeedbackCreate, locale: str = Depends(get_locale)):
    """
    Registra la calificación del usuario sobre una predicción de ETA.

    El sistema usa esta información para corregir futuras predicciones
    en la misma franja horaria, ruta y parada.

    - **actual_minutes**: tiempo que realmente esperaste (más preciso)
    - **rating**: del 1 (llegó antes) al 5 (tardó mucho) si no tienes el tiempo exacto
    """
    engine = get_engine()
    accepted = engine.record_feedback(
        payload.prediction_id,
        actual_minutes=payload.actual_minutes,
        rating=payload.rating,
    )
    if not accepted:
        raise HTTPException(
            status_code=404,
            detail=t("errors.prediction_not_found", locale=locale),
        )
    return {
        "status": "accepted",
        "prediction_id": payload.prediction_id,
        "message": t("messages.feedback_received", locale=locale),
    }


@router.get("/stats", summary="Estadísticas del sistema de aprendizaje")
async def feedback_stats(
    route_id: Optional[str] = Query(None, description="Filtrar por ruta (ej: 45)"),
):
    """
    Muestra cuántas predicciones y calificaciones se han registrado,
    y el error promedio corregido por ruta/parada.
    """
    engine = get_engine()
    return engine.get_feedback_stats(route_id=route_id)
