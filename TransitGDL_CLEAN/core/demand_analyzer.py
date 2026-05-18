from __future__ import annotations

import logging
from datetime import datetime
from math import ceil

try:
    import holidays  # type: ignore[import-untyped]
except Exception:  # pragma: no cover
    holidays = None

from .models import DemandSnapshot, FleetAction, FleetRecommendation, WeatherCondition
from .timezone_utils import GDL_TZ
from .weather_client import WeatherClient

logger = logging.getLogger(__name__)


class DemandAnalyzer:
    """
    Estima demanda relativa y recomienda flota.
    """

    def __init__(self, weather_client: WeatherClient):
        self.weather = weather_client

    async def get_demand_snapshot(
        self, route_id: str, current_vehicles: int
    ) -> DemandSnapshot:
        now = datetime.now(GDL_TZ)
        weather_condition = await self.weather.get_current_condition()
        weather_factor = self.weather.get_demand_factor(weather_condition)

        peak_hour = self._is_peak_hour(now)
        holiday = self._is_holiday(now)
        day_factor = 1.15 if peak_hour else 1.0
        day_factor *= 0.9 if holiday else 1.0
        weekend_factor = 0.88 if now.weekday() >= 5 else 1.0

        demand_factor = weather_factor * day_factor * weekend_factor
        base_target = max(1, current_vehicles)
        recommended = max(1, ceil(base_target * demand_factor))

        demand_level = self._demand_level(demand_factor)
        rationale = self._build_rationale(
            weather_condition, peak_hour, holiday, weekend_factor, demand_factor
        )

        return DemandSnapshot(
            route_id=route_id,
            current_vehicles=current_vehicles,
            weather_condition=weather_condition,
            demand_factor=round(demand_factor, 2),
            recommended_vehicles=recommended,
            demand_level=demand_level,
            peak_hour=peak_hour,
            holiday=holiday,
            rationale=rationale,
        )

    def get_fleet_recommendation(self, snapshot: DemandSnapshot) -> FleetRecommendation:
        if snapshot.recommended_vehicles > snapshot.current_vehicles:
            action = FleetAction.ADD_VEHICLES
        elif snapshot.recommended_vehicles < snapshot.current_vehicles:
            action = FleetAction.REDUCE_VEHICLES
        else:
            action = FleetAction.KEEP

        if snapshot.demand_level == "critical":
            action = FleetAction.ALERT

        return FleetRecommendation(
            route_id=snapshot.route_id,
            current_vehicles=snapshot.current_vehicles,
            recommended_vehicles=snapshot.recommended_vehicles,
            action=action,
            demand_level=snapshot.demand_level,
            rationale=snapshot.rationale,
            weather_condition=snapshot.weather_condition,
            demand_factor=snapshot.demand_factor,
        )

    @staticmethod
    def _is_peak_hour(now: datetime) -> bool:
        hour = now.hour
        return hour in {7, 8, 9, 13, 14, 15, 18, 19, 20}

    @staticmethod
    def _is_holiday(now: datetime) -> bool:
        if holidays is None:
            return False
        try:
            mx = holidays.country_holidays("MX", subdiv="JAL")
            return now.date() in mx
        except Exception:  # pragma: no cover
            return False

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
        condition: WeatherCondition,
        peak_hour: bool,
        holiday: bool,
        weekend_factor: float,
        demand_factor: float,
    ) -> str:
        parts = [f"Clima: {condition.value}"]
        if peak_hour:
            parts.append("hora pico")
        if holiday:
            parts.append("festivo")
        if weekend_factor < 1.0:
            parts.append("fin de semana")
        parts.append(f"factor final {round(demand_factor, 2)}")
        return " · ".join(parts)
