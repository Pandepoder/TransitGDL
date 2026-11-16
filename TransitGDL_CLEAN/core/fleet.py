"""
Flota necesaria de una ruta a partir de su horario.

Para sostener una frecuencia de H minutos hacen falta tantas unidades como
salidas caben en un ciclo completo:

    unidades = ⌈ tiempo de ciclo ÷ H ⌉

donde el ciclo es ida + vuelta + descanso en cada terminal (una ruta circular
tiene un solo recorrido). El clima alarga el recorrido y, con él, el ciclo.
La hora pico de cada ruta se deduce de sus propias frecuencias: la ruta está
en pico cuando su frecuencia actual es cercana a la mejor del día. Del mismo
modo, el horario ya está "reducido" cuando la frecuencia vigente es bastante
más espaciada que la mejor de un día laboral (fines de semana, festivos y
noches con menos salidas).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from math import ceil
from typing import Optional

from .calendar_utils import es_festivo
from .params import FLOTA


@dataclass(frozen=True, slots=True)
class FleetPlan:
    route_id: str
    headway_min: Optional[float]  # frecuencia vigente; None = sin servicio ahora
    best_headway_min: Optional[float]  # la mejor frecuencia del día
    cycle_min: float  # ida + vuelta + descansos, con clima
    required_vehicles: int  # 0 si la ruta no opera ahora
    # None si la ruta tiene la misma frecuencia todo el día (no hay pico propio).
    is_peak: Optional[bool]
    # La mejor frecuencia de un día laboral de referencia (sin festivo).
    weekday_best_headway_min: Optional[float] = None

    @property
    def in_service(self) -> bool:
        return self.headway_min is not None

    @property
    def reduced_service(self) -> Optional[bool]:
        """¿El horario ya trae menos servicio que el mejor de un día laboral?"""
        if self.headway_min is None or not self.weekday_best_headway_min:
            return None
        threshold = float(FLOTA["umbral_recorte"])
        return self.headway_min >= self.weekday_best_headway_min * threshold


def _reference_weekday(day: date) -> date:
    """El día laboral no festivo más cercano hacia atrás (para comparar horarios)."""
    for back in range(0, 15):
        candidate = day - timedelta(days=back)
        if candidate.weekday() < 5 and not es_festivo(candidate):
            return candidate
    return day


def _best_headway(patterns, services) -> Optional[float]:
    headways = [
        window.headway_s / 60.0
        for pattern in patterns
        for window in pattern.windows
        if window.headway_s > 0 and (services is None or window.service_id in services)
    ]
    return min(headways) if headways else None


def _layover_s(run_time_s: float) -> float:
    return max(
        float(FLOTA["descanso_min_s"]), float(FLOTA["descanso_fraccion"]) * run_time_s
    )


def fleet_plan(
    store, route_id: str, now: datetime, travel_factor: float = 1.0
) -> Optional[FleetPlan]:
    """Plan de flota de la ruta en este momento, o None si la ruta no existe."""
    route = store.find_route(route_id)
    if route is None:
        return None
    patterns = store.get_patterns(route.route_id, main_only=True)
    if not patterns:
        return None

    cycle_s = sum(
        p.run_time_s * travel_factor + _layover_s(p.run_time_s) for p in patterns
    )

    headways = [h for h in (store.active_headway(p, now) for p in patterns) if h]
    headway = min(headways) if headways else None

    services = store.active_services(now.date())
    day_headways = [
        window.headway_s / 60.0
        for pattern in patterns
        for window in pattern.windows
        if window.headway_s > 0 and (services is None or window.service_id in services)
    ]
    best = min(day_headways) if day_headways else None
    worst = max(day_headways) if day_headways else None

    required = ceil(cycle_s / (headway * 60.0)) if headway else 0
    # Solo se puede hablar de hora pico si la ruta cambia su frecuencia en el día.
    is_peak: Optional[bool] = None
    if best and worst and worst - best >= 1.0:
        is_peak = bool(headway and headway <= best * float(FLOTA["umbral_pico"]))
    weekday = _reference_weekday(now.date())
    weekday_best = (
        best
        if weekday == now.date()
        else _best_headway(patterns, store.active_services(weekday))
    )
    return FleetPlan(
        route_id=route.route_id,
        headway_min=headway,
        best_headway_min=best,
        cycle_min=round(cycle_s / 60.0, 1),
        required_vehicles=required,
        is_peak=is_peak,
        weekday_best_headway_min=weekday_best,
    )


def coverage(current_vehicles: int, plan: FleetPlan) -> Optional[float]:
    """Unidades en servicio ÷ unidades necesarias (None si la ruta no opera)."""
    if not plan.required_vehicles:
        return None
    return round(current_vehicles / plan.required_vehicles, 2)
