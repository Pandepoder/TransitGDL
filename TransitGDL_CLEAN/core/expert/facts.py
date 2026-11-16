"""
Base de hechos: reúne en un solo lugar el estado de una ruta (clima, calendario,
flota, frecuencia, incidencias y aprendizaje) para entregarlo al motor.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import TYPE_CHECKING, Any

from ..calendar_utils import es_festivo
from ..fleet import coverage, fleet_plan
from ..models import IncidentSeverity, Route
from ..prediction_learner import MIN_SAMPLES_TO_APPLY
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
    """
    Devuelve (hechos iniciales, nombres de rutas alternas).

    `vehiculos` es la flota en servicio que informa el operador. Las unidades
    que reportan GPS son otro dato: con rastreo parcial (o en la demostración)
    son una muestra, así que solo cuentan como flota si así se configura
    (`GPS_COBERTURA_COMPLETA`).
    """
    ahora = ahora or datetime.now(GDL_TZ)
    estado = await engine._weather.get_current_state()

    con_gps = len(await engine._gps.get_positions_for_route(route.route_id))
    en_servicio = vehiculos
    if en_servicio is None and engine.fleet_count_from_gps():
        en_servicio = con_gps

    hechos: dict[str, Any] = {
        "clima": estado.condition.value,
        "hora": ahora.hour,
        "es_fin_de_semana": ahora.weekday() >= 5,
        "es_festivo": es_festivo(ahora),
        "tipo_ruta": route.route_type,
        "vehiculos_en_ruta": con_gps,
        "gps_esperado": engine.gps_expected(route.route_id),
    }
    if en_servicio is not None:
        hechos["unidades_en_servicio"] = int(en_servicio)

    factor = engine._weather.travel_factor(estado, route.mode)
    plan = fleet_plan(engine._gtfs, route.route_id, ahora, factor)
    if plan is not None and plan.in_service:
        hechos["frecuencia_min"] = round(plan.headway_min, 1)
        if plan.is_peak is not None:
            hechos["es_hora_pico_ruta"] = plan.is_peak
        hechos["unidades_necesarias"] = plan.required_vehicles
        # Sin un conteo real de la flota no se puede saber si falta o sobra.
        if en_servicio is not None:
            cobertura = coverage(int(en_servicio), plan)
            if cobertura is not None:
                hechos["cobertura_flota"] = cobertura
    elif plan is not None:
        hechos["sin_servicio_programado"] = True

    incidentes = engine.get_active_incidents(route_id=route.route_id)
    hechos["incidentes_activos"] = len(incidentes)
    if incidentes:
        peor = max(incidentes, key=lambda i: _ORDEN_SEVERIDAD.index(i.severity))
        hechos["severidad_max_incidente"] = peor.severity.value

    alternas = _rutas_alternas(engine, route)
    hechos["hay_ruta_alterna"] = bool(alternas)

    hechos.update(_aprendizaje(engine, route, ahora))
    return hechos, alternas


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
    """Error aprendido de la ruta (ambos sentidos) en la franja actual."""
    total, media, desviacion = engine._learner.route_summary(
        route.route_id, ahora.hour, ahora.weekday()
    )
    hechos: dict[str, Any] = {"muestras_feedback": total}
    if total >= MIN_SAMPLES_TO_APPLY:
        hechos["error_medio_min"] = round(media, 1)
        hechos["desviacion_error_min"] = round(desviacion, 1)
    return hechos
