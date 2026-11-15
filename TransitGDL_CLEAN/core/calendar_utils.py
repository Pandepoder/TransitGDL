"""Calendario: días festivos nacionales de México."""

from __future__ import annotations

import logging
from datetime import date, datetime
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
