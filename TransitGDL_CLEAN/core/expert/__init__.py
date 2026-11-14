"""Sistema experto de diagnóstico operativo de rutas (base de reglas + inferencia)."""

from .inference import MotorInferencia, ResultadoInferencia
from .knowledge_base import (
    BaseConocimiento,
    ErrorBaseConocimiento,
    cargar_base,
    construir_base,
)
from .system import AccionRecomendada, Diagnostico, SistemaExperto

__all__ = [
    "AccionRecomendada",
    "BaseConocimiento",
    "Diagnostico",
    "ErrorBaseConocimiento",
    "MotorInferencia",
    "ResultadoInferencia",
    "SistemaExperto",
    "cargar_base",
    "construir_base",
]
