"""Calendario: días festivos nacionales de México."""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from functools import lru_cache

try:
    import holidays  # type: ignore[import-untyped]
except Exception:  # pragma: no cover - dependencia opcional
    holidays = None

logger = logging.getLogger(__name__)


@lru_cache(maxsize=16)
def _festivos_del_anio(anio: int) -> frozenset[date]:
    if holidays is None:
        return frozenset()
    try:
        # La librería no define subdivisiones para México: se usan los
        # festivos nacionales, que son los que cambian el servicio de transporte.
        return frozenset(holidays.country_holidays("MX", years=anio).keys())
    except Exception as exc:  # pragma: no cover - fallo inesperado de la librería
        logger.warning("No se pudieron cargar los festivos de %s: %s", anio, exc)
        return frozenset()


def es_festivo(fecha: date | datetime) -> bool:
    """True si la fecha es un día festivo oficial en México."""
    dia = fecha.date() if isinstance(fecha, datetime) else fecha
    return dia in _festivos_del_anio(dia.year)


TIPOS_DE_DIA = ("laboral", "sabado", "domingo", "festivo")


def dia_del_tipo(tipo: str, hasta: date) -> date:
    """
    El día más reciente (en o antes de `hasta`) del tipo pedido: laboral sin
    festivo, sábado, domingo o festivo. Sirve para plantear escenarios.
    """
    if tipo not in TIPOS_DE_DIA:
        raise ValueError(f"Tipo de día desconocido: {tipo}")
    for atras in range(0, 400):
        dia = hasta - timedelta(days=atras)
        festivo = es_festivo(dia)
        if tipo == "festivo" and festivo:
            return dia
        if tipo == "laboral" and dia.weekday() < 5 and not festivo:
            return dia
        if tipo == "sabado" and dia.weekday() == 5 and not festivo:
            return dia
        if tipo == "domingo" and dia.weekday() == 6:
            return dia
    return hasta
