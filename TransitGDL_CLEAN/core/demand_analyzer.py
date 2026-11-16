"""
Demanda relativa y flota recomendada de una ruta.

La flota recomendada ya no multiplica las unidades actuales (eso era circular:
dependía de cuántas unidades reportaban). Ahora se calcula lo que la ruta
necesita para sostener su frecuencia programada (ver `core.fleet`) y se compara
con lo que hay en servicio. La demanda relativa (clima, hora pico, calendario)
se reporta aparte como contexto y alimenta al sistema experto.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional

from .calendar_utils import es_festivo
from .fleet import FleetPlan, coverage, fleet_plan
from .models import DemandSnapshot, FleetAction, FleetRecommendation, WeatherCondition
from .params import FLOTA
from .timezone_utils import GDL_TZ
from .weather_client import WeatherClient

logger = logging.getLogger(__name__)

_FIXED_PEAK_HOURS = {7, 8, 9, 13, 14, 15, 18, 19, 20}


class DemandAnalyzer:
    """Estima la demanda relativa y recomienda flota."""

    def __init__(self, weather_client: WeatherClient, store=None):
        self.weather = weather_client
        self.store = store

    async def get_demand_snapshot(
        self, route_id: str, current_vehicles: int, now: Optional[datetime] = None
    ) -> DemandSnapshot:
        now = now or datetime.now(GDL_TZ)
        state = await self.weather.get_current_state()
        condition = state.condition
        weather_factor = self.weather.get_demand_factor(condition)

        plan = self._plan(route_id, now, state)
        peak_hour = (
            plan.is_peak
            if plan and plan.in_service and plan.is_peak is not None
            else self._is_peak_hour(now)
        )
        holiday = self._is_holiday(now)
        day_factor = 1.15 if peak_hour else 1.0
        day_factor *= 0.9 if holiday else 1.0
        weekend_factor = 0.88 if now.weekday() >= 5 else 1.0
        demand_factor = weather_factor * day_factor * weekend_factor

        required = plan.required_vehicles if plan else None
        cover = coverage(current_vehicles, plan) if plan else None
        recommended = required if required is not None else max(1, current_vehicles)

        return DemandSnapshot(
            route_id=plan.route_id if plan else route_id,
            current_vehicles=current_vehicles,
            weather_condition=condition,
            demand_factor=round(demand_factor, 2),
            recommended_vehicles=recommended,
            demand_level=self._demand_level(demand_factor),
            peak_hour=peak_hour,
            holiday=holiday,
            rationale=self._build_rationale(
                state.label, plan, cover, peak_hour, holiday, weekend_factor
            ),
            headway_minutes=plan.headway_min if plan else None,
            cycle_minutes=plan.cycle_min if plan else None,
            required_vehicles=required,
            coverage=cover,
        )

    def get_fleet_recommendation(self, snapshot: DemandSnapshot) -> FleetRecommendation:
        if snapshot.demand_level == "critical":
            action = FleetAction.ALERT
        elif snapshot.required_vehicles == 0:
            # La ruta no opera a esta hora.
            action = (
                FleetAction.REDUCE_VEHICLES
                if snapshot.current_vehicles
                else FleetAction.KEEP
            )
        elif snapshot.coverage is None:
            action = FleetAction.MONITOR
        elif snapshot.coverage < float(FLOTA["cobertura_insuficiente"]):
            action = FleetAction.ADD_VEHICLES
        elif snapshot.coverage > float(FLOTA["cobertura_excedente"]):
            action = FleetAction.REDUCE_VEHICLES
        else:
            action = FleetAction.KEEP

        return FleetRecommendation(
            route_id=snapshot.route_id,
            current_vehicles=snapshot.current_vehicles,
            recommended_vehicles=snapshot.recommended_vehicles,
            action=action,
            demand_level=snapshot.demand_level,
            rationale=snapshot.rationale,
            weather_condition=snapshot.weather_condition,
            demand_factor=snapshot.demand_factor,
            headway_minutes=snapshot.headway_minutes,
            cycle_minutes=snapshot.cycle_minutes,
            coverage=snapshot.coverage,
        )

    # ── Privado ──────────────────────────────────────────────────────────────

    def _plan(self, route_id: str, now: datetime, state) -> Optional[FleetPlan]:
        if self.store is None:
            return None
        try:
            factor = self.weather.travel_factor(state, "bus")
            route = self.store.find_route(route_id)
            if route is not None:
                factor = self.weather.travel_factor(state, route.mode)
            return fleet_plan(self.store, route_id, now, factor)
        except Exception:  # pragma: no cover - base incompleta
            logger.debug("Sin plan de flota para %s", route_id, exc_info=True)
            return None

    @staticmethod
    def _is_peak_hour(now: datetime) -> bool:
        """Respaldo cuando no se conoce la frecuencia de la ruta."""
        return now.hour in _FIXED_PEAK_HOURS

    @staticmethod
    def _is_holiday(now: datetime) -> bool:
        return es_festivo(now)

    @staticmethod
    def _demand_level(factor: float) -> str:
        if factor >= 1.6:
            return "critical"
        if factor >= 1.25:
            return "high"
        if factor >= 0.9:
            return "normal"
        return "low"

    @staticmethod
    def _build_rationale(
        weather_label: str,
        plan: Optional[FleetPlan],
        cover: Optional[float],
        peak_hour: bool,
        holiday: bool,
        weekend_factor: float,
    ) -> str:
        parts = [f"Clima: {weather_label}"]
        if plan is not None:
            if plan.in_service:
                parts.append(
                    f"pasa cada {plan.headway_min:g} min, ciclo de {plan.cycle_min:g} min → "
                    f"{plan.required_vehicles} unidades necesarias"
                )
            else:
                parts.append("sin servicio programado a esta hora")
        if cover is not None:
            parts.append(f"cobertura {round(cover * 100)} %")
        if peak_hour:
            own_peak = plan is not None and plan.is_peak is not None
            parts.append("hora pico de la ruta" if own_peak else "hora pico")
        if holiday:
            parts.append("festivo")
        if weekend_factor < 1.0:
            parts.append("fin de semana")
        return " · ".join(parts)


__all__ = ["DemandAnalyzer", "WeatherCondition"]
