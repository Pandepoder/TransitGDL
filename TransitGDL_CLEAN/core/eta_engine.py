"""
Cálculo del tiempo estimado de llegada (ETA).

Para una ruta, un sentido y una parada se usa, en orden de preferencia:

    1. **Unidad en vivo.** Se toma la unidad del mismo sentido que aún no pasa
       la parada y llega primero. El tiempo es el que el horario asigna al
       tramo entre la unidad y la parada (no una línea recta ni la velocidad
       instantánea), multiplicado por el factor de clima, más la penalización
       de las incidencias que estén en ese tramo.
    2. **Frecuencia.** Si la ruta opera "cada H minutos" y no hay unidad a la
       vista, la espera media es H/2 y puede ir de 0 a H.
    3. **Horario.** Si el servicio tiene hora exacta (o aún no arranca), se usa
       la próxima pasada programada.

Al resultado se le suma la corrección aprendida de las calificaciones y se le
calcula un rango. La confianza se deriva del ancho de ese rango.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Iterable, Optional

from .models import (
    BusPosition,
    ETAResult,
    Incident,
    Route,
    ScheduledDeparture,
    Stop,
)
from .params import ETA, INCIDENCIAS
from .timezone_utils import GDL_TZ
from .weather_client import WeatherClient, WeatherState

if TYPE_CHECKING:
    from .prediction_learner import PredictionLearner
    from .transit import Pattern

logger = logging.getLogger(__name__)

MAX_ROUTE_VARIANTS = 6
MAX_OPTIONS = 4


@dataclass(slots=True)
class Boarding:
    """Dónde se aborda: una parada concreta dentro de un patrón (ruta y sentido)."""

    stop: Stop
    pattern: "Pattern"
    index: int
    place_name: Optional[str] = None
    walk_meters: Optional[float] = None


@dataclass(slots=True)
class LiveCandidate:
    position: BusPosition
    progress_m: float
    eta_s: float


class ETAEngine:
    def __init__(
        self,
        gtfs,
        weather: WeatherClient,
        learner: Optional["PredictionLearner"] = None,
    ):
        self.gtfs = gtfs
        self.weather = weather
        self.learner = learner

    # ── API pública ──────────────────────────────────────────────────────────

    async def get_eta(
        self,
        route: str,
        stop: str,
        live_positions: Optional[list[BusPosition]] = None,
        *,
        incidents: Optional[list[Incident]] = None,
        now: Optional[datetime] = None,
    ) -> ETAResult | None:
        """La mejor opción (la que llega primero) para la ruta en la parada."""
        options = await self.get_eta_options(
            route, stop, live_positions, incidents=incidents, now=now
        )
        return options[0] if options else None

    async def get_eta_options(
        self,
        route: str,
        stop: str,
        live_positions: Optional[list[BusPosition]] = None,
        *,
        incidents: Optional[list[Incident]] = None,
        now: Optional[datetime] = None,
    ) -> list[ETAResult]:
        """Una estimación por sentido (y por ramal) que pase por la parada."""
        state = await self.weather.get_current_state()
        return self.estimate_options(
            route,
            stop,
            live_positions or [],
            state,
            incidents or [],
            now or datetime.now(GDL_TZ),
        )

    def estimate_options(
        self,
        route: str,
        stop: str,
        positions: list[BusPosition],
        state: WeatherState,
        incidents: list[Incident],
        now: datetime,
    ) -> list[ETAResult]:
        """Versión síncrona y sin red de `get_eta_options` (la usa la evaluación)."""
        results: list[ETAResult] = []
        for route_obj in self.candidate_routes(route)[:MAX_ROUTE_VARIANTS]:
            for boarding in self.resolve_boardings(route_obj, stop):
                result = self.estimate(
                    route_obj, boarding, positions, state, incidents, now
                )
                if result is not None:
                    results.append(result)
        results.sort(key=lambda r: (r.service_day_offset, r.estimated_minutes))
        return results[:MAX_OPTIONS]

    # ── Qué ruta y qué parada ────────────────────────────────────────────────

    def candidate_routes(self, route: str) -> list[Route]:
        """Rutas que responden a la consulta; un número antiguo puede tener varios ramales."""
        finder = getattr(self.gtfs, "find_routes", None)
        if callable(finder):
            found = finder(route)
            if isinstance(found, list):
                return found
        single = self.gtfs.find_route(route)
        return [single] if single is not None else []

    def resolve_boardings(self, route: Route, query: str) -> list[Boarding]:
        """
        Paradas de la ruta que responden a `query`, a lo más una por patrón:
        un identificador de parada, un nombre de parada o un lugar conocido
        ("CUCEI"), en ese orden.
        """
        query = (query or "").strip()
        if not query:
            return []
        store = self.gtfs

        exact = store.get_stop(query)
        if exact is not None:
            found = self._boardings_at(route, [exact])
            if found:
                return found

        named = [
            stop
            for stop in store.search_stops(query, limit=40)
            if self._serves(route, stop)
        ]
        if not named:
            fallback = self._search_route_stops(
                store.get_stops_for_route(route.route_id), query
            )
            named = [fallback] if fallback is not None else []
        found = self._boardings_at(route, named)
        if found:
            return found

        landmark = store.find_landmark(query)
        if landmark is None:
            return []
        return [
            Boarding(
                option.stop,
                option.pattern,
                option.index,
                landmark.name,
                option.distance_m,
            )
            for option in store.stops_of_route_near(
                route.route_id, landmark.lat, landmark.lon
            )
        ]

    def _serves(self, route: Route, stop: Stop) -> bool:
        return any(
            pattern.route_id == route.route_id
            for pattern, _ in self.gtfs.patterns_at_stop(stop.stop_id)
        )

    def _boardings_at(self, route: Route, stops: Iterable[Stop]) -> list[Boarding]:
        """Para cada patrón de la ruta, la primera de `stops` donde se puede abordar."""
        boarding: dict[str, Boarding] = {}
        arriving: dict[str, Boarding] = {}
        for stop in stops:
            for pattern, index in self.gtfs.patterns_at_stop(stop.stop_id):
                if pattern.route_id != route.route_id:
                    continue
                # En la última parada del patrón solo se baja.
                target = arriving if index == len(pattern.stop_ids) - 1 else boarding
                target.setdefault(pattern.pattern_id, Boarding(stop, pattern, index))
        chosen = boarding or arriving
        return sorted(
            chosen.values(),
            key=lambda b: (not b.pattern.is_main, b.pattern.direction_id),
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

    # ── Estimación ───────────────────────────────────────────────────────────

    def estimate(
        self,
        route: Route,
        boarding: Boarding,
        positions: list[BusPosition],
        state: WeatherState,
        incidents: list[Incident],
        now: datetime,
    ) -> ETAResult | None:
        pattern, index = boarding.pattern, boarding.index
        stop = boarding.stop
        stop_dist = pattern.dist_m[index]
        stop_offset_s = pattern.offset_s[index]
        factor = self.weather.travel_factor(state, route.mode)
        now_s = now.hour * 3600 + now.minute * 60 + now.second
        headway = self.gtfs.active_headway(pattern, now)

        live = self._live_candidates(pattern, index, positions, factor, incidents)
        arrivals = self.gtfs.pattern_arrivals(pattern, index, now, limit=3)

        vehicle: BusPosition | None = None
        vehicle_distance_m: float | None = None
        next_arrivals: list[float] = []
        next_departures: list[ScheduledDeparture] = []
        service_day_offset = 0
        applied: list[Incident] = []

        if live:
            first = live[0]
            method, source, basis = "vehiculo", "live", "gps_vehicular"
            vehicle = first.position
            vehicle_distance_m = max(0.0, stop_dist - first.progress_m)
            base_minutes = first.eta_s / 60.0
            penalty_s, applied = self._incident_penalty(
                pattern, incidents, first.progress_m, stop_dist
            )
            run_s = first.eta_s - penalty_s
            weather_extra = run_s * (1.0 - 1.0 / factor) / 60.0
            sigma = max(
                float(ETA["sigma_minima_min"]),
                float(ETA["sigma_relativa_vivo"]) * base_minutes,
            )
            low, high = base_minutes - self._half(sigma), base_minutes + self._half(
                sigma
            )
            next_arrivals = [c.eta_s / 60.0 for c in live[1:3]]
        elif arrivals:
            when, window, day_offset = arrivals[0]
            service_day_offset = max(day_offset, 0)
            penalty_s, applied = self._incident_penalty(
                pattern, incidents, pattern.dist_m[0], stop_dist
            )
            penalty_min = penalty_s / 60.0
            # ¿Ya circulan unidades de esta ventana por la parada?
            first_pass = window.start_s + stop_offset_s + day_offset * 86400
            in_flow = window.headway_s > 0 and not window.exact and first_pass <= now_s
            if in_flow:
                method, source, basis = (
                    "frecuencia",
                    "schedule",
                    "frecuencia_programada",
                )
                cycle = window.headway_s / 60.0
                headway = cycle
                base_minutes = cycle / 2.0 + penalty_min
                weather_extra = 0.0
                low, high = penalty_min, cycle * factor + penalty_min
            else:
                method, source = "horario", "schedule"
                # El retraso por clima se acumula con lo recorrido desde la terminal.
                weather_extra = (factor - 1.0) * stop_offset_s / 60.0
                base_minutes = (when - now_s) / 60.0 + weather_extra + penalty_min
                basis = (
                    "horario_oficial_ajustado"
                    if weather_extra or penalty_min
                    else "horario_oficial"
                )
                half = self._half(float(ETA["sigma_horario_min"])) + weather_extra / 2.0
                low, high = base_minutes - half, base_minutes + half
                next_departures = [
                    ScheduledDeparture(
                        departure_time=_clock(item_when),
                        minutes_until=round((item_when - now_s) / 60.0),
                        service_day_offset=max(item_day, 0),
                    )
                    for item_when, _, item_day in arrivals
                ]
        else:
            return None

        # ── Corrección aprendida ─────────────────────────────────────────────
        correction = 0.0
        spread: float | None = None
        if self.learner is not None:
            slot = (route.route_id, stop.stop_id, now.hour, now.weekday())
            correction = self.learner.get_correction(*slot, pattern.direction_id)
            spread = self.learner.get_spread(*slot, pattern.direction_id)
        eta_minutes = max(0.0, base_minutes + correction)
        low, high = max(0.0, low + correction), max(0.0, high + correction)
        if method == "vehiculo" and spread is not None:
            # Hay calificaciones suficientes: se usa la dispersión observada.
            low = max(0.0, eta_minutes - self._half(spread))
            high = eta_minutes + self._half(spread)

        half_width = (high - low) / 2.0
        confidence, label = self._confidence(half_width)

        return ETAResult(
            route_number=route.route_short_name,
            route_id=route.route_id,
            route_label=getattr(route, "label", None),
            stop_id=stop.stop_id,
            stop_name=stop.stop_name,
            estimated_minutes=eta_minutes,
            base_minutes=base_minutes,
            confidence=confidence,
            reliability_label=label,
            source=source,
            method=method,
            basis=basis,
            delay_minutes=round(weather_extra + penalty_s / 60.0, 1),
            vehicle_id=vehicle.vehicle_id if vehicle else None,
            vehicle_lat=vehicle.lat if vehicle else None,
            vehicle_lon=vehicle.lon if vehicle else None,
            vehicle_distance_m=vehicle_distance_m,
            service_day_offset=service_day_offset,
            average_headway_minutes=headway,
            window_start_minutes=low,
            window_end_minutes=high,
            next_departures=next_departures,
            next_arrivals_minutes=next_arrivals,
            learned_correction_minutes=correction,
            weather_condition=state.condition.value,
            weather_label=state.label,
            headsign=pattern.headsign,
            direction_id=pattern.direction_id,
            pattern_id=pattern.pattern_id,
            place_name=boarding.place_name,
            walk_meters=boarding.walk_meters,
            alerts=[incident.summary() for incident in applied],
        )

    @staticmethod
    def _half(sigma_minutes: float) -> float:
        """Media anchura del rango reportado para una desviación dada."""
        return float(ETA["z_ventana"]) * sigma_minutes

    @staticmethod
    def _confidence(half_width_minutes: float) -> tuple[float, str]:
        """La confianza se deriva del ancho del rango: más estrecho, más confiable."""
        if half_width_minutes <= float(ETA["rango_confianza_alta_min"]):
            return 0.9, "alta"
        if half_width_minutes <= float(ETA["rango_confianza_media_min"]):
            return 0.75, "media"
        return 0.55, "baja"

    # ── Unidades en vivo ─────────────────────────────────────────────────────

    def _live_candidates(
        self,
        pattern: "Pattern",
        index: int,
        positions: list[BusPosition],
        factor: float,
        incidents: list[Incident],
    ) -> list[LiveCandidate]:
        """Unidades del patrón que aún no pasan la parada, la más próxima primero."""
        stop_dist = pattern.dist_m[index]
        stop_time = pattern.offset_s[index]
        tolerance = float(ETA["tolerancia_parada_m"])
        candidates: list[LiveCandidate] = []
        for position in positions:
            if position.route_id != pattern.route_id:
                continue
            progress = self._progress_on(pattern, position)
            if progress is None or progress > stop_dist + tolerance:
                continue
            run_s = max(0.0, stop_time - pattern.time_at(progress)) * factor
            penalty_s, _ = self._incident_penalty(
                pattern, incidents, progress, stop_dist
            )
            eta_s = run_s + penalty_s
            if progress <= pattern.dist_m[0] + 5.0 and position.speed_kmh < 1.0:
                # Detenida en la terminal: todavía no sale.
                eta_s += float(ETA["espera_terminal_s"])
            candidates.append(LiveCandidate(position, progress, eta_s))
        candidates.sort(key=lambda candidate: candidate.eta_s)
        return candidates

    def _progress_on(self, pattern: "Pattern", position: BusPosition) -> float | None:
        """
        Metros recorridos por la unidad sobre el patrón, o None si no circula
        por él. Si la posición no declara su patrón se ubica sobre el trazo y
        se descarta cuando va en sentido contrario.
        """
        declared = position.pattern_id or position.trip_id
        if declared:
            if declared == pattern.pattern_id:
                if position.progress_m is not None:
                    return position.progress_m
                return pattern.line.project(position.lat, position.lon)[0]
            if self.gtfs.get_pattern(declared) is not None:
                return None

        dist, offset = pattern.line.project(position.lat, position.lon)
        if offset > float(ETA["radio_vehiculo_m"]):
            return None
        if position.bearing is not None:
            turn = abs(
                (position.bearing - pattern.line.bearing_at(dist) + 180.0) % 360.0
                - 180.0
            )
            if turn > 90.0:
                return None
        return dist

    # ── Incidencias ──────────────────────────────────────────────────────────

    def _incident_penalty(
        self,
        pattern: "Pattern",
        incidents: list[Incident],
        from_m: float,
        to_m: float,
    ) -> tuple[float, list[Incident]]:
        """
        Segundos que suman las incidencias al tramo [from_m, to_m] del patrón y
        cuáles se aplicaron. Una incidencia ubicada solo cuenta si cae en el
        tramo; una sin ubicación cuenta parcialmente en toda la ruta.
        """
        total = 0.0
        applied: list[Incident] = []
        for incident in incidents:
            if (
                incident.affected_routes
                and pattern.route_id not in incident.affected_routes
            ):
                continue
            minutes = float(
                INCIDENCIAS["penalizacion_min"].get(incident.severity.value, 3)
            )
            located, where = self._incident_location(pattern, incident)
            if not located:
                minutes *= float(INCIDENCIAS["fraccion_sin_ubicacion"])
            elif where is None or not from_m <= where <= to_m:
                continue
            total += minutes * 60.0
            applied.append(incident)
        return total, applied

    def _incident_location(
        self, pattern: "Pattern", incident: Incident
    ) -> tuple[bool, float | None]:
        """
        (tiene ubicación, distancia sobre el patrón). La distancia es None
        cuando la incidencia está ubicada pero fuera de este patrón.
        """
        if incident.lat is not None and incident.lon is not None:
            dist, offset = pattern.line.project(incident.lat, incident.lon)
            near = offset <= float(INCIDENCIAS["radio_ubicacion_m"])
            return True, (dist if near else None)
        if incident.stop_ids:
            for stop_id in incident.stop_ids:
                indexes = pattern.stop_indexes(stop_id)
                if indexes:
                    return True, pattern.dist_m[indexes[0]]
            return True, None
        return False, None


def _clock(seconds: float) -> str:
    in_day = int(seconds) % 86400
    return f"{in_day // 3600:02d}:{in_day % 3600 // 60:02d}:{in_day % 60:02d}"
