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
    """Devuelve (hechos iniciales, nombres de rutas alternas)."""
    ahora = ahora or datetime.now(GDL_TZ)
    estado = await engine._weather.get_current_state()

    # La cobertura de flota solo es significativa con un conteo real: el
    # simulador de demostración muestra una muestra reducida de unidades.
    conteo_real = vehiculos is not None or not engine.simulator_active
    if vehiculos is None:
        vehiculos = len(await engine._gps.get_positions_for_route(route.route_id))

    hechos: dict[str, Any] = {
        "clima": estado.condition.value,
        "hora": ahora.hour,
        "es_fin_de_semana": ahora.weekday() >= 5,
        "es_festivo": es_festivo(ahora),
        "tipo_ruta": route.route_type,
        "vehiculos_en_ruta": int(vehiculos),
    }

    factor = engine._weather.travel_factor(estado, route.mode)
    plan = fleet_plan(engine._gtfs, route.route_id, ahora, factor)
    if plan is not None and plan.in_service:
        hechos["frecuencia_min"] = round(plan.headway_min, 1)
        if plan.is_peak is not None:
            hechos["es_hora_pico_ruta"] = plan.is_peak
        hechos["unidades_necesarias"] = plan.required_vehicles
        cobertura = coverage(int(vehiculos), plan)
        if conteo_real and cobertura is not None:
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
