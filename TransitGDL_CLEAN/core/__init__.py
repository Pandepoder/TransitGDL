from __future__ import annotations

import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Optional

from .config import (
    CONFIG,
    DEFAULT_GTFS_PATH,
    TransitEngineConfig,
    is_demo_simulator_enabled,
)
from .demand_analyzer import DemandAnalyzer
from .eta_engine import ETAEngine
from .expert import Diagnostico, SistemaExperto
from .expert.facts import recolectar_hechos
from .feedback_store import FeedbackStore, PredictionRecord
from .gps_cache import GPSCache
from .incident_store import IncidentStore
from .models import (
    AlternateRoute,
    BusPosition,
    DemandSnapshot,
    ETAResult,
    FleetAction,
    FleetRecommendation,
    Incident,
    Route,
    StopMatch,
)
from .prediction_learner import PredictionLearner
from .timezone_utils import GDL_TZ
from .transit import TransitStore
from .weather_client import WeatherClient

logger = logging.getLogger(__name__)

__all__ = ["TransitEngine"]

# Nivel de demanda del sistema experto → código de la API de flota.
_NIVEL_A_DEMANDA = {
    "critica": "critical",
    "alta": "high",
    "normal": "normal",
    "baja": "low",
}


class TransitEngine:
    def __init__(self, config: Optional[TransitEngineConfig] = None):
        self.config = config or CONFIG
        self._gtfs = TransitStore(
            self.config.gtfs_path,
            self.config.transit_db_path,
            self.config.landmarks_path,
        )
        self._weather = WeatherClient()
        self._feedback_store = FeedbackStore(self.config.feedback_db_path)
        self._learner = PredictionLearner(self._feedback_store)
        self._eta = ETAEngine(self._gtfs, self._weather, learner=self._learner)
        self._demand = DemandAnalyzer(self._weather, self._gtfs)
        self._gps = GPSCache(self.config.redis_url)
        self._incident_store = IncidentStore(self.config.incidents_db_path)
        self._expert = SistemaExperto()
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

    def gps_expected(self, route_id: str) -> bool:
        """
        ¿La ruta debería reportar posiciones? Sí con una fuente GPS real, o en
        la demostración si el simulador la cubre. Sin fuente, que no haya
        unidades en el mapa no indica una falla.
        """
        if self.config.gps_feed_active:
            return True
        if self._simulator is not None:
            return route_id in self._simulator.route_ids
        return False

    def fleet_count_from_gps(self) -> bool:
        """El conteo GPS es la flota en servicio solo con cobertura completa y real."""
        return self.config.gps_full_coverage and self._simulator is None

    async def initialize(self) -> "TransitEngine":
        logger.info("Iniciando TransitEngine...")
        self.config.ensure_paths()
        self._ensure_gtfs_available()
        self._incident_store.initialize()
        self._feedback_store.initialize()
        self._gtfs.load()
        self._eta.gtfs = self._gtfs
        self._eta.learner = self._learner
        self._demand.store = self._gtfs
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
            logger.info(
                "FleetSimulator: desactivado (DEMO_SIMULATOR=false o production)."
            )
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
        close_store = getattr(self._gtfs, "close", None)
        if callable(close_store):
            close_store()

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
        self._demand.store = loader
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

    async def get_eta_options(
        self,
        route: str,
        stop: str,
        live_positions: Optional[list[BusPosition]] = None,
        *,
        save: bool = True,
        stops: Optional[list] = None,
    ) -> list[ETAResult]:
        """
        Una estimación por cada sentido (y ramal) de la ruta que pasa por la
        parada, la que llega primero al inicio. Con `save` las predicciones se
        guardan para poder calificarlas después. Con `stops` se usan esas
        paradas físicas en lugar de buscar `stop` por nombre.
        """
        self._assert_initialized()
        routes = self._eta.candidate_routes(route)[:6]
        if not routes:
            return []
        if live_positions is None:
            live_positions = []
            for route_obj in routes:
                live_positions.extend(
                    await self._gps.get_positions_for_route(route_obj.route_id)
                )
        incidents: list[Incident] = []
        for route_obj in routes:
            for incident in self.get_active_incidents(route_id=route_obj.route_id):
                if incident.incident_id not in {i.incident_id for i in incidents}:
                    incidents.append(incident)

        options = await self._eta.get_eta_options(
            route, stop, live_positions, incidents=incidents, stops=stops
        )
        catalog_note = self._gtfs.catalog_note(route)
        if catalog_note:
            for result in options:
                result.data_notes.insert(0, catalog_note)
        if save:
            now = datetime.now(GDL_TZ)
            for result in options:
                self._save_prediction(result, now)
        return options

    async def get_arrivals(
        self,
        place_query: Optional[str] = None,
        limit: int = 12,
        *,
        stop_id: Optional[str] = None,
    ) -> Optional[dict]:
        """
        Próximas llegadas de todas las rutas en una parada o lugar: la mejor
        opción de cada ruta y sentido, la más próxima primero. Con `stop_id`
        se usa esa parada exacta (y su acera de enfrente), sin importar que
        otras paradas se llamen igual. None si el lugar no existe.
        """
        self._assert_initialized()
        if stop_id:
            place = self._gtfs.place_from_stop(stop_id)
        else:
            place = self._gtfs.resolve_place(place_query or "")
        if place is None:
            return None
        # En una parada se calcula sobre sus paradas físicas; en un lugar
        # ("CUCEI") se buscan las paradas cercanas de cada ruta, con la
        # distancia a pie.
        stops = list(place.stops) if place.kind == "parada" else None
        arrivals: list[ETAResult] = []
        for route in self._gtfs.routes_at_place(place)[:24]:
            options = await self.get_eta_options(
                route.route_id, place_query or place.name, save=False, stops=stops
            )
            arrivals.extend(o for o in options if o.route_id == route.route_id)
        arrivals.sort(key=lambda r: (r.service_day_offset, r.estimated_minutes))
        return {
            "place": {
                "name": place.name,
                "kind": place.kind,
                "lat": place.lat,
                "lon": place.lon,
                "stop_ids": [stop.stop_id for stop in place.stops],
            },
            "arrivals": arrivals[:limit],
        }

    async def get_eta(
        self,
        route: str,
        stop: str,
        live_positions: Optional[list[BusPosition]] = None,
    ) -> Optional[ETAResult]:
        """La opción que llega primero; las demás quedan resumidas en `other_options`."""
        options = await self.get_eta_options(route, stop, live_positions)
        if not options:
            return None
        best = options[0]
        best.other_options = [option.compact_line() for option in options[1:]]
        return best

    def _save_prediction(self, result: ETAResult, now: datetime) -> None:
        if not (result.route_id and result.stop_id):
            return
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
                base_minutes=result.base_minutes,
                learned_correction=result.learned_correction_minutes,
                direction_id=result.direction_id,
                method=result.method,
            )
        )

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

        pred = self._feedback_store.get_prediction(prediction_id)
        if pred is not None:
            self._learner.record_feedback(
                pred.route_id,
                pred.stop_id,
                pred.hour_of_day,
                pred.day_of_week,
                error_minutes,
                direction_id=pred.direction_id,
                applied_correction=pred.learned_correction or 0.0,
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
        self, route_id: str, current_vehicles: Optional[int] = None
    ) -> FleetRecommendation:
        """
        Unidades que la ruta necesita (tiempo de ciclo ÷ frecuencia) y qué
        hacer con su flota. La decisión, la cifra y el motivo salen del
        sistema experto, para que Operación y Experto nunca se contradigan.
        `current_vehicles` es la flota en servicio que informa el operador;
        sin ella no se compara contra ningún conteo.
        """
        self._assert_initialized()
        snapshot = await self._demand.get_demand_snapshot(
            route_id, current_vehicles or 0
        )
        diagnostico = await self.diagnose_route(route_id, vehicles=current_vehicles)
        if diagnostico is None:
            recommendation = self._demand.get_fleet_recommendation(snapshot)
            recommendation.current_vehicles = current_vehicles
            return recommendation

        acciones = {accion.id for accion in diagnostico.acciones}
        if "emitir_alerta_operativa" in acciones:
            action = FleetAction.ALERT
        elif diagnostico.decision_flota == "agregar":
            action = FleetAction.ADD_VEHICLES
        elif diagnostico.decision_flota == "reducir":
            action = FleetAction.REDUCE_VEHICLES
        elif current_vehicles is None and not diagnostico.unidades_necesarias:
            action = FleetAction.MONITOR
        else:
            action = FleetAction.KEEP

        necesarias = diagnostico.unidades_necesarias or 0
        recommended = diagnostico.vehiculos_recomendados
        if recommended is None:
            recommended = necesarias or (current_vehicles or 0)
        partes = []
        if snapshot.headway_minutes and snapshot.cycle_minutes:
            partes.append(
                f"Pasa cada {snapshot.headway_minutes:g} min, ciclo de "
                f"{snapshot.cycle_minutes:g} min → {necesarias} unidades necesarias"
            )
        else:
            partes.append("Sin servicio programado a esta hora")
        if current_vehicles is not None and snapshot.coverage is not None:
            partes.append(f"cobertura {round(snapshot.coverage * 100)} %")
        # Frases sin punto final: la interfaz cierra el texto completo.
        if diagnostico.motivo_flota:
            partes.append(diagnostico.motivo_flota.rstrip("."))
        if diagnostico.nota_flota:
            partes.append(diagnostico.nota_flota.rstrip("."))
        factor = next(
            (
                float(h["valor"])
                for h in diagnostico.hechos_derivados
                if h["hecho"] == "factor_flota"
            ),
            1.0,
        )
        return FleetRecommendation(
            route_id=diagnostico.route_id or route_id,
            current_vehicles=current_vehicles,
            recommended_vehicles=recommended,
            action=action,
            demand_level=_NIVEL_A_DEMANDA.get(diagnostico.nivel_demanda, "normal"),
            rationale=" · ".join(partes),
            weather_condition=snapshot.weather_condition,
            demand_factor=factor,
            headway_minutes=snapshot.headway_minutes,
            cycle_minutes=snapshot.cycle_minutes,
            coverage=snapshot.coverage if current_vehicles is not None else None,
        )

    @property
    def expert(self) -> SistemaExperto:
        return self._expert

    async def diagnose_route(
        self,
        route_id: str,
        vehicles: Optional[int] = None,
        overrides: Optional[dict] = None,
        por_que_no: Optional[str] = None,
    ) -> Optional[Diagnostico]:
        """
        Diagnóstico del sistema experto para una ruta.

        Reúne los hechos actuales de la ruta, aplica `overrides` (escenarios
        "¿qué pasaría si…?") y ejecuta el motor de inferencia.
        Devuelve None si la ruta no existe.
        """
        self._assert_initialized()
        route = self._gtfs.find_route(route_id)
        if route is None:
            return None
        overrides = dict(overrides or {})
        ahora = None
        if isinstance(overrides.get("hora"), int):
            # Escenario a otra hora: frecuencia, hora pico de la ruta y flota
            # necesaria se calculan para esa hora, no para la actual.
            ahora = datetime.now(GDL_TZ).replace(
                hour=overrides["hora"], minute=0, second=0, microsecond=0
            )
        hechos, alternas = await recolectar_hechos(self, route, vehicles, ahora=ahora)
        hechos.update(overrides)
        return self._expert.diagnosticar(
            hechos,
            route_id=route.route_id,
            route_name=route.display_name,
            rutas_alternas=alternas,
            por_que_no=por_que_no,
        )

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
                affected_routes=self._canonical_route_ids(affected_routes or []),
                severity=(
                    IncidentSeverity(severity) if severity else IncidentSeverity.MEDIUM
                ),
                stop_ids=stop_ids or [],
            )
        else:
            incident.affected_routes = self._canonical_route_ids(
                incident.affected_routes
            )
            if stop_ids is not None:
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
        if route_id:
            route_id = self._canonical_route_ids([route_id])[0]
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

    def _canonical_route_ids(self, route_ids: list[str]) -> list[str]:
        """
        Convierte números antiguos y alias a claves oficiales. Un número con
        varios ramales (646) se expande a todos; lo desconocido se conserva.
        """
        canonical: list[str] = []
        finder = getattr(self._gtfs, "find_routes", None)
        for raw in route_ids:
            try:
                found = [route.route_id for route in finder(raw)] if finder else []
            except TypeError:
                found = []
            for route_id in found or [raw]:
                if route_id not in canonical:
                    canonical.append(route_id)
        return canonical

    def get_alternate_routes(self, route_id: str) -> list[AlternateRoute]:
        """Rutas que comparten paradas con una ruta que tiene incidencias activas."""
        self._assert_initialized()
        active = self.get_active_incidents(route_id=route_id)
        if not active:
            return []

        route = self._gtfs.find_route(route_id)
        if route is None:
            return []

        neighbors = self._gtfs.routes_near_route(route.route_id)
        if not neighbors:
            return []

        names = ", ".join(neighbor.label for neighbor in neighbors[:4])
        return [
            AlternateRoute(
                original_route_id=route.route_id,
                alternate_description=(
                    f"Rutas que comparten paradas con la {route.label}: {names}."
                ),
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
        """Rutas que pasan por una parada o cerca de un lugar conocido."""
        self._assert_initialized()
        place = self._gtfs.resolve_place(stop_query)
        if place is None:
            return "No encontré esa parada."
        if place.kind == "parada":
            groups = self._gtfs.place_groups(stop_query, limit=4)
            if len(groups) > 1:
                return self._describe_homonyms(groups)
        routes = self._gtfs.routes_at_place(place)
        if not routes:
            return f"No encontré rutas activas para *{place.name}*."

        shown = [route.label for route in routes[:14]]
        extra = len(routes) - len(shown)
        listing = ", ".join(shown) + (f" y {extra} más" if extra > 0 else "")
        where = (
            f"Cerca de *{place.name}*"
            if place.kind == "lugar"
            else f"En *{place.name}*"
        )
        return f"{where} pasan: {listing}"

    def _describe_homonyms(self, groups: list) -> str:
        """Varias paradas con el mismo nombre en distintos puntos: se listan por sus rutas."""
        name = groups[0].name
        lines = [f"Hay varias paradas llamadas *{name}*. Las rutas de cada una:"]
        example = None
        for place in groups[:3]:
            routes = self._gtfs.routes_at_place(place)
            if not routes:
                continue
            example = example or routes[0].display_name
            shown = ", ".join(route.label for route in routes[:4])
            more = len(routes) - 4
            lines.append(f"· {shown}" + (f" y {more} más" if more > 0 else ""))
        if example:
            lines.append(
                "Para el tiempo de llegada escribe la ruta y la parada, por ejemplo: "
                f"*¿Cuánto falta la {example} en {name}?*"
            )
        return "\n".join(lines)

    async def _handle_weather_query(self) -> str:
        state = await self._weather.get_current_state()
        factor = self._weather.travel_factor(state, "bus")
        if factor > 1.02:
            extra = round((factor - 1.0) * 100)
            return (
                f"Clima en GDL: *{state.label}*.\n"
                f"Los camiones tardan alrededor de {extra} % más en recorrer su ruta; "
                "el tren casi no se afecta. Los tiempos estimados ya lo consideran."
            )
        return (
            f"Clima en GDL: *{state.label}*. "
            "No hay retrasos relevantes por clima en este momento."
        )

    def _parse_route_stop(self, query: str) -> tuple[Optional[str], Optional[str]]:
        route = None
        stop = None

        route_patterns = [
            r"(?:ruta|línea|linea|camión|camion|bus|tren)\s+([\w-]*\d[\w-]*)",
            r"\bla\s+([\w-]*\d[\w-]*)\b",
            r"^([\w-]*\d[\w-]*)\b",
        ]
        for pattern in route_patterns:
            match = re.search(pattern, query, re.IGNORECASE)
            if match:
                route = match.group(1)
                break
        if route is None or self._gtfs.find_route(route) is None:
            # Cualquier palabra con dígitos que sea una ruta conocida (T15, C38-V1, 646).
            for token in re.findall(r"[\w-]*\d[\w-]*", query):
                if self._gtfs.find_route(token) is not None:
                    route = token
                    break

        stop_patterns = [
            r"\b(?:en|parada|hasta|para en)\s+(.+?)(?:\?|$)",
            r"\b(?:por)\s+(.+?)(?:\?|$)",
        ]
        for pattern in stop_patterns:
            match = re.search(pattern, query, re.IGNORECASE)
            if match:
                stop = match.group(1).strip().rstrip("?. ")
                break

        return route, stop

    @staticmethod
    def _parse_stop_only(query: str) -> Optional[str]:
        match = re.search(r"\b(?:por|en|parada)\s+(.+?)(?:\?|$)", query, re.IGNORECASE)
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
            "No se encontró %s. Se genera un GTFS de demostración mínimo; para los "
            "datos reales ejecuta: python scripts/download_gtfs.py",
            gtfs_path,
        )
        from scripts.seed_demo import build_gtfs_zip

        build_gtfs_zip(gtfs_path)
