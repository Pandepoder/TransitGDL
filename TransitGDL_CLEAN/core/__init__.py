from __future__ import annotations

import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Optional

from .config import CONFIG, DEFAULT_GTFS_PATH, TransitEngineConfig, is_demo_simulator_enabled
from .demand_analyzer import DemandAnalyzer
from .eta_engine import ETAEngine
from .feedback_store import FeedbackStore, PredictionRecord
from .gps_cache import GPSCache
from .gtfs_loader import GTFSLoader
from .incident_store import IncidentStore
from .models import (
    AlternateRoute,
    BusPosition,
    DemandSnapshot,
    ETAResult,
    FleetRecommendation,
    Incident,
    Route,
    StopMatch,
)
from .prediction_learner import PredictionLearner
from .timezone_utils import GDL_TZ
from .weather_client import WeatherClient

logger = logging.getLogger(__name__)

__all__ = ["TransitEngine"]


class TransitEngine:
    def __init__(self, config: Optional[TransitEngineConfig] = None):
        self.config = config or CONFIG
        self._gtfs = GTFSLoader(self.config.gtfs_path)
        self._weather = WeatherClient()
        self._feedback_store = FeedbackStore(self.config.feedback_db_path)
        self._learner = PredictionLearner(self._feedback_store)
        self._eta = ETAEngine(self._gtfs, self._weather, learner=self._learner)
        self._demand = DemandAnalyzer(self._weather)
        self._gps = GPSCache(self.config.redis_url)
        self._incident_store = IncidentStore(self.config.incidents_db_path)
        self._simulator = None
        self._initialized = False

    @property
    def simulator_active(self) -> bool:
        return self._simulator is not None

    @property
    def live_vehicle_count(self) -> int:
        if self._simulator is None:
            return 0
        return self._simulator.vehicle_count

    async def initialize(self) -> "TransitEngine":
        logger.info("Iniciando TransitEngine...")
        self.config.ensure_paths()
        self._ensure_gtfs_available()
        self._incident_store.initialize()
        self._feedback_store.initialize()
        self._gtfs.load()
        from .regional_overlay import apply_regional_overlay

        apply_regional_overlay(self._gtfs)
        self._eta.gtfs = self._gtfs
        self._eta.learner = self._learner
        await self._gps.connect()
        self._initialized = True
        await self.ensure_simulator()
        logger.info("TransitEngine listo.")
        return self

    async def ensure_simulator(self) -> bool:
        """Start the demo fleet if enabled and not already running."""
        if self._simulator is not None:
            return True
        if not is_demo_simulator_enabled():
            logger.info("FleetSimulator: desactivado (DEMO_SIMULATOR=false o production).")
            return False
        if not self._initialized:
            return False
        from .demo_simulator import FleetSimulator

        self._simulator = FleetSimulator(self)
        await self._simulator.start()
        logger.info(
            "FleetSimulator: activo con %d vehículos.",
            self._simulator.vehicle_count,
        )
        return True

    async def close(self) -> None:
        if self._simulator is not None:
            await self._simulator.stop()
            self._simulator = None
        await self._gps.close()

    @property
    def cache_backend(self) -> str:
        return (
            "memory" if self._gps._use_memory or self._gps._redis is None else "redis"
        )

    @property
    def active_incidents_count(self) -> int:
        return self._incident_store.count_active()

    def initialize_with_loader(self, loader) -> "TransitEngine":
        self._gtfs = loader
        self._eta.gtfs = loader
        self._eta.learner = self._learner
        self._incident_store.initialize()
        self._feedback_store.initialize()
        self._initialized = True
        return self

    async def ask(
        self, query: str, live_positions: Optional[list[BusPosition]] = None
    ) -> str:
        self._assert_initialized()
        query_lower = query.lower()

        if any(
            word in query_lower
            for word in ["lluvia", "clima", "retraso", "tráfico", "trafico", "bloqu"]
        ):
            return await self._handle_weather_query()

        if any(
            word in query_lower
            for word in ["qué rutas", "que rutas", "pasan por", "rutas pasan"]
        ):
            stop = self._parse_stop_only(query)
            if stop:
                return self.describe_routes_at_stop(stop)

        route, stop = self._parse_route_stop(query)
        if route:
            if not stop:
                return (
                    f"Encontré la ruta *{route}*, pero necesito también la parada o referencia.\n"
                    "Ejemplo: *¿Cuánto falta la MACRO01_R en Artes Plásticas?*"
                )
            result = await self.get_eta(route, stop, live_positions)
            if result:
                return result.to_whatsapp_message()
            return (
                f"No encontré información para la ruta *{route}*"
                + (f" en *{stop}*" if stop else "")
                + ".\nPrueba con el número exacto de la ruta y una parada más específica."
            )

        return (
            "Puedo ayudarte con:\n"
            "- *¿Cuánto falta la [ruta] en [parada]?*\n"
            "- *¿Hay retrasos por lluvia?*\n"
            "- *¿Qué rutas pasan por [parada]?*\n\n"
            "Por ahora la estimación es por *horario oficial* ajustado por clima e incidencias."
        )

    async def get_eta(
        self,
        route: str,
        stop: str,
        live_positions: Optional[list[BusPosition]] = None,
    ) -> Optional[ETAResult]:
        self._assert_initialized()
        found = self._gtfs.find_route(route)
        if live_positions is None and found:
            live_positions = await self._gps.get_positions_for_route(found.route_id)

        result = await self._eta.get_eta(route, stop, live_positions or [])
        if result is None:
            return None

        alerts = self.get_active_incidents(
            route_id=result.route_id, stop_id=result.stop_id
        )
        result.alerts = [incident.summary() for incident in alerts]
        if alerts and result.source == "schedule":
            incident_delay = 2.0
            result.estimated_minutes += incident_delay
            result.delay_minutes += incident_delay
            if result.window_start_minutes is not None:
                result.window_start_minutes += incident_delay
            result.window_end_minutes = (
                result.window_end_minutes or result.estimated_minutes
            ) + 3.0
            result.confidence = max(0.5, result.confidence - 0.08)
            result.reliability_label = "media" if result.confidence >= 0.7 else "baja"
            result.basis = "horario_oficial_ajustado_incidencias"

        # Guardar predicción para correlacionar con feedback futuro.
        # La condición climática ya viene en result.weather_condition (set por ETAEngine),
        # evitando una segunda llamada HTTP a Open-Meteo.
        if result.route_id and result.stop_id:
            now = datetime.now(GDL_TZ)
            self._feedback_store.save_prediction(
                PredictionRecord(
                    prediction_id=result.prediction_id,
                    route_id=result.route_id,
                    stop_id=result.stop_id,
                    predicted_minutes=result.estimated_minutes,
                    weather_condition=result.weather_condition,
                    hour_of_day=now.hour,
                    day_of_week=now.weekday(),
                    source=result.source,
                    created_at=result.updated_at.isoformat(),
                )
            )

        return result

    def record_feedback(
        self,
        prediction_id: str,
        *,
        actual_minutes: Optional[float] = None,
        rating: Optional[int] = None,
    ) -> bool:
        """
        Registra la calificación o tiempo real del usuario y actualiza el modelo.

        rating: 1=llegó antes, 2=puntual, 3=poco tarde, 4=tarde, 5=muy tarde
        actual_minutes: tiempo real de espera en minutos (más preciso que el rating)

        Retorna True si el feedback fue aceptado y el modelo actualizado.
        """
        self._assert_initialized()
        error_minutes = self._feedback_store.save_feedback(
            prediction_id,
            actual_minutes=actual_minutes,
            rating=rating,
        )
        if error_minutes is None:
            return False

        # Obtener contexto temporal de la predicción original para actualizar el slot correcto
        pred = self._feedback_store.get_prediction(prediction_id)
        if pred is not None:
            self._learner.record_feedback(
                pred.route_id,
                pred.stop_id,
                pred.hour_of_day,
                pred.day_of_week,
                error_minutes,
            )
            logger.info(
                "Feedback registrado: pred=%s route=%s stop=%s error=%.1f min",
                prediction_id[:8],
                pred.route_id,
                pred.stop_id,
                error_minutes,
            )
        return True

    def get_feedback_stats(self, route_id: Optional[str] = None) -> dict:
        """Retorna estadísticas del sistema de aprendizaje."""
        self._assert_initialized()
        global_stats = self._feedback_store.get_global_stats()
        if route_id:
            route_detail = self._feedback_store.get_route_stats_summary(route_id)
            return {**global_stats, "route_id": route_id, "slots": route_detail}
        return global_stats

    async def get_demand_snapshot(
        self, route_id: str, current_vehicles: int
    ) -> DemandSnapshot:
        self._assert_initialized()
        return await self._demand.get_demand_snapshot(route_id, current_vehicles)

    async def get_fleet_recommendation(
        self, route_id: str, current_vehicles: int
    ) -> FleetRecommendation:
        self._assert_initialized()
        snapshot = await self._demand.get_demand_snapshot(route_id, current_vehicles)
        return self._demand.get_fleet_recommendation(snapshot)

    def report_incident(
        self,
        incident: Incident | str,
        affected_routes: Optional[list[str]] = None,
        severity=None,
        stop_ids: Optional[list[str]] = None,
    ) -> Incident:
        self._assert_initialized()
        if isinstance(incident, str):
            from .models import IncidentSeverity

            incident = Incident(
                description=incident,
                affected_routes=affected_routes or [],
                severity=(
                    IncidentSeverity(severity) if severity else IncidentSeverity.MEDIUM
                ),
                stop_ids=stop_ids or [],
            )
        elif stop_ids is not None:
            incident.stop_ids = stop_ids

        saved = self._incident_store.add_incident(incident)
        logger.info("Incidencia registrada: %s", saved.description)
        return saved

    def list_incidents(
        self,
        *,
        active_only: bool = True,
        route_id: str | None = None,
        stop_id: str | None = None,
        limit: int = 50,
    ) -> list[Incident]:
        self._assert_initialized()
        return self._incident_store.list_incidents(
            active_only=active_only,
            route_id=route_id,
            stop_id=stop_id,
            limit=limit,
        )

    def get_active_incidents(
        self, route_id: str | None = None, stop_id: str | None = None
    ) -> list[Incident]:
        return self.list_incidents(
            active_only=True, route_id=route_id, stop_id=stop_id, limit=100
        )

    def resolve_incident(self, incident_id: str) -> Incident | None:
        self._assert_initialized()
        return self._incident_store.resolve_incident(incident_id)

    def get_alternate_routes(self, route_id: str) -> list[AlternateRoute]:
        self._assert_initialized()
        active = self.get_active_incidents(route_id=route_id)
        if not active:
            return []

        route = self._gtfs.find_route(route_id)
        if route is None:
            return []

        alternatives = [
            candidate
            for candidate in self._gtfs.get_routes()
            if candidate.route_id != route.route_id
            and candidate.route_type == route.route_type
        ]
        if not alternatives:
            return []

        return [
            AlternateRoute(
                original_route_id=route.route_id,
                alternate_description=f"Considera {alternatives[0].display_name} como alternativa cercana.",
                extra_time_minutes=10.0,
                affected_stops=[],
                reason=active[0].description,
            )
        ]

    def search_stops(self, query: str, limit: int = 10) -> list:
        self._assert_initialized()
        return self._gtfs.search_stops(query, limit=limit)

    def search_stops_with_routes(self, query: str, limit: int = 5) -> list[StopMatch]:
        self._assert_initialized()
        matches = self._gtfs.search_stops(query, limit=limit)
        results: list[StopMatch] = []
        for stop in matches:
            routes = self._gtfs.get_routes_at_stop(stop.stop_id)
            results.append(
                StopMatch(
                    stop_id=stop.stop_id,
                    stop_name=stop.stop_name,
                    lat=stop.lat,
                    lon=stop.lon,
                    route_ids=[route.route_id for route in routes],
                    route_numbers=[route.route_short_name for route in routes],
                )
            )
        return results

    def get_routes_at_stop(self, stop_id: str) -> list[Route]:
        self._assert_initialized()
        return self._gtfs.get_routes_at_stop(stop_id)

    def get_active_headway(self, route_id: str) -> float | None:
        """
        Devuelve el headway actual en minutos para rutas de frecuencia (frequencies.txt).
        Retorna None si la ruta usa stop_times explícitos o no hay ventana activa.
        """
        self._assert_initialized()
        return self._gtfs.get_active_headway(route_id)

    def describe_routes_at_stop(self, stop_query: str) -> str:
        self._assert_initialized()
        from .regional_overlay import CURRENT_OVERLAY_IDS

        normalized = stop_query.strip().lower()
        matches = self.search_stops_with_routes(stop_query, limit=15)
        if not matches:
            return "No encontré esa parada."

        relevant = [
            m
            for m in matches
            if normalized in m.stop_name.lower()
            or m.stop_name.lower() in normalized
        ]
        if not relevant:
            relevant = [self._pick_stop_match(matches, stop_query)]

        short_names: set[str] = set()
        display_name = relevant[0].stop_name
        for match in relevant:
            for route_id, short_name in zip(match.route_ids, match.route_numbers):
                if route_id in CURRENT_OVERLAY_IDS:
                    short_names.add(short_name)
            if "cucei" in match.stop_name.lower():
                display_name = "CUCEI"

        if not short_names:
            stop = self._pick_stop_match(matches, stop_query)
            routes = self._gtfs.get_routes_at_stop(stop.stop_id)
            regional = [r for r in routes if r.route_id in CURRENT_OVERLAY_IDS]
            if regional:
                routes = regional
            if not routes:
                return f"No encontré rutas activas para *{stop.stop_name}*."
            short_names = {r.route_short_name for r in routes}
            display_name = stop.stop_name

        route_names = ", ".join(
            sorted(short_names, key=lambda n: (len(n), n))
        )
        return f"En *{display_name}* pasan: {route_names}"

    @staticmethod
    def _pick_stop_match(matches: list[StopMatch], query: str):
        """Prioriza paradas del overlay regional (RG_*) sobre homónimos del GTFS."""
        normalized = query.strip().lower()

        def score(match: StopMatch) -> tuple[int, int, int]:
            overlay = 1 if match.stop_id.startswith(("R55_", "R231_", "R646_", "R615_")) else 0
            exact = 1 if match.stop_name.lower() == normalized else 0
            partial = 1 if normalized in match.stop_name.lower() else 0
            rg_routes = sum(1 for rid in match.route_ids if rid.startswith("RG_"))
            return (overlay, exact, partial, rg_routes, -len(match.stop_name))

        return sorted(matches, key=score, reverse=True)[0]

    async def _handle_weather_query(self) -> str:
        condition = await self._weather.get_current_condition()
        delay = self._weather.get_delay_minutes(condition)
        if self._weather.is_rain_alert(condition):
            return (
                f"Alerta de clima en GDL: *{condition.value.replace('_', ' ')}*.\n"
                f"Retrasos estimados: +{round(delay)} min en rutas con horario oficial."
            )
        return "Clima estable en GDL. No hay retrasos relevantes por clima en este momento."

    def _parse_route_stop(self, query: str) -> tuple[Optional[str], Optional[str]]:
        route = None
        stop = None

        route_patterns = [
            r"(?:ruta|línea|linea|camión|camion|bus)\s*(\d+\w*)",
            r"\bla\s+(\d+\w*)\b",
            r"^(\d+\w*)\b",
        ]
        for pattern in route_patterns:
            match = re.search(pattern, query, re.IGNORECASE)
            if match:
                route = match.group(1)
                break

        stop_patterns = [
            r"(?:en|parada|hasta|para en)\s+(.+?)(?:\?|$)",
            r"(?:por)\s+(.+?)(?:\?|$)",
        ]
        for pattern in stop_patterns:
            match = re.search(pattern, query, re.IGNORECASE)
            if match:
                stop = match.group(1).strip().rstrip("?. ")
                break

        return route, stop

    @staticmethod
    def _parse_stop_only(query: str) -> Optional[str]:
        match = re.search(r"(?:por|en|parada)\s+(.+?)(?:\?|$)", query, re.IGNORECASE)
        if match:
            return match.group(1).strip().rstrip("?. ")
        return None

    def _assert_initialized(self) -> None:
        if not self._initialized:
            raise RuntimeError(
                "Llama a `await engine.initialize()` antes de usar el motor."
            )

    def _ensure_gtfs_available(self) -> None:
        gtfs_path = Path(self.config.gtfs_path)
        if gtfs_path.exists():
            return

        if self.config.gtfs_path != DEFAULT_GTFS_PATH:
            raise FileNotFoundError(f"No existe GTFS configurado: {gtfs_path}")

        logger.warning(
            "No se encontro %s. Generando GTFS de demostracion para facilitar el arranque local.",
            gtfs_path,
        )
        from scripts.seed_demo import build_gtfs_zip

        build_gtfs_zip(gtfs_path)
