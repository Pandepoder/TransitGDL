"""
Base de hechos: reúne en un solo lugar el estado de una ruta (clima, calendario,
flota, frecuencia, incidencias y aprendizaje) para entregarlo al motor.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import TYPE_CHECKING, Any

from ..calendar_utils import es_festivo
from ..models import IncidentSeverity, Route
from ..prediction_learner import MIN_SAMPLES_TO_APPLY, _day_type, _hour_bucket
from ..timezone_utils import GDL_TZ

if TYPE_CHECKING:
    from .. import TransitEngine

logger = logging.getLogger(__name__)

_ORDEN_SEVERIDAD = [
    IncidentSeverity.LOW,
    IncidentSeverity.MEDIUM,
    IncidentSeverity.HIGH,
    IncidentSeverity.CRITICAL,
]
_RADIO_ALTERNA_KM = 0.3


async def recolectar_hechos(
    engine: "TransitEngine",
    route: Route,
    vehiculos: int | None = None,
    ahora: datetime | None = None,
) -> tuple[dict[str, Any], list[str]]:
    """Devuelve (hechos iniciales, nombres de rutas alternas)."""
    ahora = ahora or datetime.now(GDL_TZ)
    clima = await engine._weather.get_current_condition()

    if vehiculos is None:
        vehiculos = len(await engine._gps.get_positions_for_route(route.route_id))

    hechos: dict[str, Any] = {
        "clima": clima.value,
        "hora": ahora.hour,
        "es_fin_de_semana": ahora.weekday() >= 5,
        "es_festivo": es_festivo(ahora),
        "tipo_ruta": route.route_type,
        "vehiculos_en_ruta": int(vehiculos),
    }

    frecuencia = _frecuencia(engine, route, ahora)
    if frecuencia is not None:
        hechos["frecuencia_min"] = round(frecuencia, 1)

    incidentes = engine.get_active_incidents(route_id=route.route_id)
    hechos["incidentes_activos"] = len(incidentes)
    if incidentes:
        peor = max(incidentes, key=lambda i: _ORDEN_SEVERIDAD.index(i.severity))
        hechos["severidad_max_incidente"] = peor.severity.value

    alternas = _rutas_alternas(engine, route)
    hechos["hay_ruta_alterna"] = bool(alternas)

    hechos.update(_aprendizaje(engine, route, ahora))
    return hechos, alternas


def _frecuencia(engine: "TransitEngine", route: Route, ahora: datetime) -> float | None:
    minutos = ahora.hour * 60 + ahora.minute
    try:
        activa = engine._gtfs.get_active_headway(route.route_id, minutos)
        if activa:
            return float(activa)
        paradas = engine._gtfs.get_stops_for_route(route.route_id)
        if not paradas:
            return None
        parada = paradas[len(paradas) // 2]
        promedio = engine._gtfs.get_average_headway(
            route.route_id, parada.stop_id, current_minutes=minutos
        )
        return float(promedio) if promedio else None
    except Exception:  # pragma: no cover - GTFS incompleto
        logger.debug("No se pudo calcular la frecuencia de %s", route.route_id)
        return None


def _rutas_alternas(engine: "TransitEngine", route: Route) -> list[str]:
    """Otras rutas con paradas a menos de 300 m de alguna parada de esta."""
    try:
        vecinas = engine._gtfs.routes_near_route(route.route_id, _RADIO_ALTERNA_KM)
    except Exception:  # pragma: no cover - base incompleta
        return []
    return [vecina.label for vecina in vecinas]


def _aprendizaje(
    engine: "TransitEngine", route: Route, ahora: datetime
) -> dict[str, Any]:
    """Agrega el error aprendido de la ruta en la franja horaria actual."""
    franja = _hour_bucket(ahora.hour)
    tipo_dia = _day_type(ahora.weekday())
    filas = [
        fila
        for fila in engine._feedback_store.get_route_stats_summary(route.route_id)
        if fila["hour_bucket"] == franja and fila["day_type"] == tipo_dia
    ]
    total = sum(int(fila["sample_count"]) for fila in filas)
    hechos: dict[str, Any] = {"muestras_feedback": total}
    if total < MIN_SAMPLES_TO_APPLY:
        return hechos
    media = sum(fila["mean_error"] * fila["sample_count"] for fila in filas) / total
    varianza = sum(fila["variance"] * fila["sample_count"] for fila in filas) / total
    hechos["error_medio_min"] = round(media, 1)
    hechos["desviacion_error_min"] = round(varianza**0.5, 1)
    return hechos
