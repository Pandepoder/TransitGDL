from __future__ import annotations

import logging
from datetime import datetime
from typing import TYPE_CHECKING, Iterable, Optional

from .gtfs_loader import GTFSLoader
from .models import (
    BusPosition,
    ETAResult,
    Route,
    ScheduledDeparture,
    Stop,
    haversine_km,
)
from .timezone_utils import GDL_TZ
from .weather_client import WeatherClient

if TYPE_CHECKING:
    from .prediction_learner import PredictionLearner

logger = logging.getLogger(__name__)


class ETAEngine:
    """
    Calcula ETA usando GTFS estático, con posiciones live opcionales.
    Si se inyecta un PredictionLearner, aplica correcciones aprendidas
    de calificaciones históricas de usuarios.
    """

    def __init__(
        self,
        gtfs: GTFSLoader,
        weather: WeatherClient,
        learner: Optional["PredictionLearner"] = None,
    ):
        self.gtfs = gtfs
        self.weather = weather
        self.learner = learner

    async def get_eta(
        self,
        route: str,
        stop: str,
        live_positions: Optional[list[BusPosition]] = None,
    ) -> ETAResult | None:
        route_obj, stop_obj, place = self._resolve_route_and_stop(route, stop)
        if route_obj is None or stop_obj is None:
            return None

        condition = await self.weather.get_current_condition()
        weather_delay = self.weather.get_delay_minutes(condition)
        speed_factor = self.weather.get_speed_factor(condition)
        schedule_result = self._estimate_schedule(route_obj, stop_obj, weather_delay)

        if live_positions:
            live_result = self._estimate_live(
                route_obj,
                stop_obj,
                live_positions,
                weather_delay,
                speed_factor,
                schedule_result,
            )
            if live_result is not None:
                live_result.weather_condition = condition.value
                if schedule_result is not None:
                    live_result.headsign = schedule_result.headsign
                    live_result.direction_id = schedule_result.direction_id
                    live_result.pattern_id = schedule_result.pattern_id
                return self._decorate(live_result, route_obj, place)

        if schedule_result is not None:
            schedule_result.weather_condition = condition.value
            self._decorate(schedule_result, route_obj, place)
        return schedule_result

    @staticmethod
    def _decorate(
        result: ETAResult, route: Route, place: tuple[str, float] | None
    ) -> ETAResult:
        label = getattr(route, "label", None)
        if isinstance(label, str):
            result.route_label = label
        if place is not None:
            result.place_name, result.walk_meters = place
        return result

    def _candidate_routes(self, route: str) -> list[Route]:
        """Rutas que responden a la consulta; un número antiguo puede tener varios ramales."""
        finder = getattr(self.gtfs, "find_routes", None)
        if callable(finder):
            found = finder(route)
            if isinstance(found, list):
                return found
        single = self.gtfs.find_route(route)
        return [single] if single is not None else []

    def _resolve_route_and_stop(
        self, route: str, stop: str
    ) -> tuple[Route | None, Stop | None, tuple[str, float] | None]:
        """
        Elige el ramal de la ruta que pasa por la parada pedida. Si el texto no
        es una parada de la ruta pero sí un lugar conocido ("CUCEI"), usa la
        parada de la ruta más cercana a ese lugar.
        """
        candidates = self._candidate_routes(route)[:6]
        for route_obj in candidates:
            stop_obj = self._resolve_stop(route_obj, stop)
            if stop_obj is not None:
                return route_obj, stop_obj, None
        for route_obj in candidates:
            near = self._resolve_stop_by_place(route_obj, stop)
            if near is not None:
                return route_obj, near[0], (near[1], near[2])
        return (candidates[0] if candidates else None), None, None

    def _resolve_stop_by_place(
        self, route: Route, stop_query: str
    ) -> tuple[Stop, str, float] | None:
        find_landmark = getattr(self.gtfs, "find_landmark", None)
        near_stops = getattr(self.gtfs, "stops_of_route_near", None)
        if not callable(find_landmark) or not callable(near_stops):
            return None
        landmark = find_landmark(stop_query)
        if landmark is None or not hasattr(landmark, "lat"):
            return None
        options = near_stops(route.route_id, landmark.lat, landmark.lon)
        if not isinstance(options, list) or not options:
            return None
        best = min(options, key=lambda option: option.distance_m)
        return best.stop, landmark.name, best.distance_m

    def _resolve_stop(self, route: Route, stop_query: str) -> Stop | None:
        if not stop_query or not stop_query.strip():
            return None

        stop = self.gtfs.get_stop(stop_query)
        if stop and self._stop_served_by_route(stop.stop_id, route.route_id):
            return stop

        matches = self.gtfs.search_stops(stop_query, limit=10)
        route_matches = [
            candidate
            for candidate in matches
            if self._stop_served_by_route(candidate.stop_id, route.route_id)
        ]
        if route_matches:
            return route_matches[0]

        return self._search_route_stops(
            self.gtfs.get_stops_for_route(route.route_id), stop_query
        )

    def _search_route_stops(
        self, route_stops: Iterable[Stop], stop_query: str
    ) -> Stop | None:
        """
        Busca entre las paradas de la ruta por nombre: coincidencia exacta,
        frase contenida o todas las palabras significativas presentes. En caso
        de empate gana el nombre más corto y, después, el orden del recorrido.
        """
        normalized_query = self.gtfs._normalize(stop_query)
        if not normalized_query:
            return None

        tokens = [token for token in normalized_query.split() if len(token) >= 3]
        best_key: tuple[int, int] | None = None
        best_stop: Stop | None = None
        for stop in route_stops:
            name_norm = self.gtfs._normalize(stop.stop_name)
            if normalized_query == name_norm:
                score = 100
            elif normalized_query in name_norm:
                score = 80
            elif tokens and all(token in name_norm for token in tokens):
                score = 60
            else:
                continue
            key = (-score, len(stop.stop_name))
            if best_key is None or key < best_key:
                best_key, best_stop = key, stop
        return best_stop

    def _stop_served_by_route(self, stop_id: str, route_id: str) -> bool:
        routes = self.gtfs.get_routes_at_stop(stop_id)
        return any(route.route_id == route_id for route in routes)

    def _estimate_live(
        self,
        route: Route,
        stop: Stop,
        live_positions: list[BusPosition],
        weather_delay: float,
        speed_factor: float,
        schedule_result: ETAResult | None,
    ) -> ETAResult | None:
        candidates = [
            position
            for position in live_positions
            if self._route_matches(position.route_id, route)
        ]
        if not candidates:
            return None

        best = min(
            candidates,
            key=lambda position: haversine_km(
                position.lat, position.lon, stop.lat, stop.lon
            ),
        )
        distance_km = haversine_km(best.lat, best.lon, stop.lat, stop.lon)
        speed = max(best.speed_kmh * max(speed_factor, 0.4), 5.0)
        eta_minutes = (distance_km / speed) * 60.0 + weather_delay

        confidence = 0.92
        if distance_km > 8:
            confidence = 0.62
        elif distance_km > 3:
            confidence = 0.74
        elif distance_km > 1:
            confidence = 0.84

        # ── Corrección aprendida (igual que en schedule) ───────────────────────
        now = datetime.now(GDL_TZ)
        learned_correction = 0.0
        if self.learner is not None:
            learned_correction = self.learner.get_correction(
                route.route_id, stop.stop_id, now.hour, now.weekday()
            )
            confidence_adj = self.learner.get_confidence_adjustment(
                route.route_id, stop.stop_id, now.hour, now.weekday()
            )
            eta_minutes = max(0.0, eta_minutes + learned_correction)
            confidence = max(0.40, min(0.97, confidence + confidence_adj))

        window_padding = self._estimate_window_padding(
            confidence, weather_delay, schedule_result
        )
        return ETAResult(
            route_number=route.route_short_name,
            route_id=route.route_id,
            stop_id=stop.stop_id,
            stop_name=stop.stop_name,
            estimated_minutes=max(0.0, eta_minutes),
            confidence=confidence,
            source="live",
            delay_minutes=weather_delay,
            vehicle_id=best.vehicle_id,
            vehicle_lat=best.lat,
            vehicle_lon=best.lon,
            service_day_offset=0,
            basis="gps_vehicular",
            reliability_label=self._reliability_label(confidence),
            average_headway_minutes=(
                schedule_result.average_headway_minutes if schedule_result else None
            ),
            window_start_minutes=max(0.0, eta_minutes - window_padding),
            window_end_minutes=max(0.0, eta_minutes + window_padding),
            next_departures=schedule_result.next_departures if schedule_result else [],
            learned_correction_minutes=learned_correction,
        )

    def _estimate_schedule(
        self, route: Route, stop: Stop, weather_delay: float
    ) -> ETAResult | None:
        now = datetime.now(GDL_TZ)
        current_minutes = now.hour * 60 + now.minute
        departures = self.gtfs.get_next_departures(
            route.route_id, stop.stop_id, current_minutes, limit=4
        )
        if not departures:
            return None

        first = departures[0]
        # En una estación que sirven ambos sentidos, el resultado describe un
        # solo sentido: el de la primera pasada.
        pattern_id = first.get("pattern_id")
        if pattern_id:
            departures = self.gtfs.get_next_departures(
                route.route_id,
                stop.stop_id,
                current_minutes,
                limit=4,
                pattern_id=pattern_id,
            )
            first = departures[0]
        minutes_until = float(first["minutes_until"])
        service_day_offset = int(first.get("service_day_offset", 0))
        eta_minutes = minutes_until + weather_delay
        headway = first.get("headway_minutes") or self.gtfs.get_average_headway(
            route.route_id, stop.stop_id, current_minutes=current_minutes
        )

        confidence = (
            0.9 if minutes_until <= 30 else 0.82 if minutes_until <= 60 else 0.72
        )
        # Valores de confianza iniciales basados en heurística de horizonte temporal.
        # Calibrar contra datos reales una vez que haya suficientes muestras de feedback:
        #   confidence = true_positives_within_window / total_predictions_in_bucket
        # El PredictionLearner ajustará estos valores ±MAX_CONFIDENCE_BOOST/PENALTY
        # automáticamente conforme se acumulen calificaciones por franja horaria.
        if headway and headway >= 20:
            confidence -= 0.05
        if service_day_offset > 0:
            confidence = min(confidence, 0.68)
        confidence = max(0.45, confidence)

        # ── Corrección aprendida ───────────────────────────────────────────────
        learned_correction = 0.0
        if self.learner is not None:
            learned_correction = self.learner.get_correction(
                route.route_id, stop.stop_id, now.hour, now.weekday()
            )
            confidence_adj = self.learner.get_confidence_adjustment(
                route.route_id, stop.stop_id, now.hour, now.weekday()
            )
            eta_minutes = max(0.0, eta_minutes + learned_correction)
            confidence = max(0.40, min(0.97, confidence + confidence_adj))

        window_padding = self._estimate_window_padding(
            confidence, weather_delay, None, headway
        )
        return ETAResult(
            route_number=route.route_short_name,
            route_id=route.route_id,
            stop_id=stop.stop_id,
            stop_name=stop.stop_name,
            estimated_minutes=max(0.0, eta_minutes),
            confidence=confidence,
            source="schedule",
            delay_minutes=weather_delay,
            service_day_offset=service_day_offset,
            basis="horario_oficial_ajustado" if weather_delay else "horario_oficial",
            reliability_label=self._reliability_label(confidence),
            average_headway_minutes=headway,
            window_start_minutes=max(0.0, eta_minutes - window_padding),
            window_end_minutes=max(0.0, eta_minutes + window_padding),
            learned_correction_minutes=learned_correction,
            headsign=first.get("headsign"),
            direction_id=first.get("direction_id"),
            pattern_id=pattern_id,
            next_departures=[
                ScheduledDeparture(
                    departure_time=str(item["departure_time"]),
                    minutes_until=int(item["minutes_until"]),
                    service_day_offset=int(item.get("service_day_offset", 0)),
                )
                for item in departures[:3]
            ],
        )

    @staticmethod
    def _estimate_window_padding(
        confidence: float,
        weather_delay: float,
        schedule_result: ETAResult | None = None,
        headway: float | None = None,
    ) -> float:
        baseline = 2.0 if confidence >= 0.88 else 4.0 if confidence >= 0.75 else 6.0
        if weather_delay:
            baseline += min(4.0, weather_delay / 2)
        effective_headway = (
            headway
            if headway is not None
            else schedule_result.average_headway_minutes if schedule_result else None
        )
        if effective_headway and effective_headway >= 20:
            baseline += 2.0
        if schedule_result and schedule_result.service_day_offset > 0:
            baseline = max(baseline, 8.0)
        return baseline

    @staticmethod
    def _reliability_label(confidence: float) -> str:
        if confidence >= 0.85:
            return "alta"
        if confidence >= 0.7:
            return "media"
        return "baja"

    @staticmethod
    def _route_matches(position_route_id: str, route: Route) -> bool:
        position = position_route_id.strip().lower()
        return (
            position == route.route_id.strip().lower()
            or position == route.route_short_name.strip().lower()
        )
