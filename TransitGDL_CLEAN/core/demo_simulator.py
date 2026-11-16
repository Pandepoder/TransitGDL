"""
Simulador de flota para demostraciones.

Cada unidad recorre un patrón (ruta y sentido) siguiendo el perfil de tiempos
del horario: va más lento donde el horario dice que el tramo tarda más. Al
llegar a la terminal descansa unos minutos y vuelve a salir. Hay unidades en
ambos sentidos y su número se deriva de la frecuencia de paso programada.

El simulador publica la posición, el rumbo y el progreso sobre el trazo de
cada unidad, igual que lo haría un sistema de GPS real.
"""

from __future__ import annotations

import asyncio
import logging
import random
from dataclasses import dataclass
from statistics import median

from .config import CONFIG
from .models import BusPosition
from .transit import Pattern, TransitStore

logger = logging.getLogger(__name__)

# Tope de unidades simuladas por patrón, para no saturar el mapa.
MAX_VEHICLES_PER_PATTERN = 3
LAYOVER_SECONDS = (60.0, 180.0)
# Variación de ritmo entre unidades (1.0 = exactamente el horario).
PACE_RANGE = (0.92, 1.12)


@dataclass
class SimVehicle:
    vehicle_id: str
    pattern: Pattern
    # Segundos de viaje programado ya recorridos (posición dentro del horario).
    schedule_s: float
    pace: float
    layover_left: float = 0.0
    dist_m: float = 0.0
    speed_kmh: float = 0.0

    def advance(
        self,
        dt_seconds: float,
        slowdown: float,
        rng: random.Random,
        turnaround: dict[str, Pattern] | None = None,
    ) -> None:
        """
        Avanza `dt_seconds` de reloj; `slowdown` > 1 frena a toda la flota.
        Al terminar el recorrido la unidad pasa al sentido contrario
        (`turnaround`) y descansa en esa terminal antes de volver a salir.
        """
        pattern = self.pattern
        if self.layover_left > 0.0:
            self.layover_left = max(0.0, self.layover_left - dt_seconds)
            self.speed_kmh = 0.0
            return

        previous = self.dist_m
        self.schedule_s += dt_seconds / (self.pace * slowdown)
        if self.schedule_s >= pattern.run_time_s:
            # Llegó a la terminal: toma el sentido contrario y descansa ahí.
            self.pattern = (turnaround or {}).get(pattern.pattern_id, pattern)
            self.schedule_s = 0.0
            self.layover_left = rng.uniform(*LAYOVER_SECONDS)
            self.dist_m = self.pattern.dist_m[0] if self.pattern.dist_m else 0.0
            self.speed_kmh = 0.0
            return
        self.dist_m = pattern.dist_at_time(self.schedule_s)
        self.speed_kmh = max(0.0, (self.dist_m - previous) / dt_seconds * 3.6)

    def to_position(self) -> BusPosition:
        lat, lon = self.pattern.line.point_at(self.dist_m)
        return BusPosition(
            vehicle_id=self.vehicle_id,
            route_id=self.pattern.route_id,
            trip_id=self.pattern.pattern_id,
            pattern_id=self.pattern.pattern_id,
            lat=lat,
            lon=lon,
            speed_kmh=round(self.speed_kmh, 1),
            bearing=round(self.pattern.line.bearing_at(self.dist_m), 1),
            progress_m=round(self.dist_m, 1),
        )


def vehicles_for_pattern(pattern: Pattern) -> int:
    """Unidades necesarias para sostener la frecuencia programada del patrón."""
    headways = [w.headway_s for w in pattern.windows if w.headway_s > 0]
    if not headways or pattern.run_time_s <= 0:
        return 1
    needed = round(pattern.run_time_s / median(headways))
    return max(1, min(MAX_VEHICLES_PER_PATTERN, needed))


def _usable(pattern: Pattern) -> bool:
    return len(pattern.line) >= 2 and pattern.run_time_s > 0


def build_turnaround(store: TransitStore, routes: list) -> dict[str, Pattern]:
    """Para cada patrón principal, el del sentido contrario de su misma ruta."""
    turnaround: dict[str, Pattern] = {}
    for route in routes:
        mains = [
            p for p in store.get_patterns(route.route_id, main_only=True) if _usable(p)
        ]
        for pattern in mains:
            others = [p for p in mains if p.direction_id != pattern.direction_id]
            if others:
                turnaround[pattern.pattern_id] = others[0]
    return turnaround


def simulated_routes(store: TransitStore, region: str | None) -> list:
    routes = store.get_routes(region) if region else []
    return routes or store.get_routes()


def build_fleet(
    store: TransitStore, region: str | None, rng: random.Random
) -> list[SimVehicle]:
    fleet: list[SimVehicle] = []
    for route in simulated_routes(store, region):
        number = 0
        for pattern in store.get_patterns(route.route_id, main_only=True):
            if not _usable(pattern):
                continue
            count = vehicles_for_pattern(pattern)
            for n in range(count):
                # Reparto uniforme a lo largo del recorrido, con algo de ruido.
                phase = (n + rng.uniform(0.2, 0.8)) / count
                schedule_s = phase * pattern.run_time_s
                number += 1
                fleet.append(
                    SimVehicle(
                        vehicle_id=f"SIM_{route.route_id}_{number}",
                        pattern=pattern,
                        schedule_s=schedule_s,
                        pace=rng.uniform(*PACE_RANGE),
                        dist_m=pattern.dist_at_time(schedule_s),
                    )
                )
    return fleet


class FleetSimulator:
    """Simula la flota en tiempo real sobre los patrones de la base de transporte."""

    def __init__(self, engine, *, seed: int | None = None) -> None:
        self._engine = engine
        self._task: asyncio.Task | None = None
        self._vehicles: list[SimVehicle] = []
        self._turnaround: dict[str, Pattern] = {}
        self._interval = float(CONFIG.demo_simulator_interval_seconds)
        self._rng = random.Random(seed)

    @property
    def vehicles(self) -> list[SimVehicle]:
        return self._vehicles

    @property
    def vehicle_count(self) -> int:
        return len(self._vehicles)

    def slowdown_by_mode(self) -> dict[str, float]:
        """Cuánto frena el clima a cada modo (1.0 = sin efecto)."""
        weather = getattr(self._engine, "_weather", None)
        if weather is None:
            return {}
        state = weather.cached_state()
        return {
            mode: weather.travel_factor(state, mode)
            for mode in ("tren", "brt", "trolebus", "bus")
        }

    async def start(self) -> None:
        if self._task is not None:
            return
        store = self._engine._gtfs
        if not store.loaded:
            store.load()
        self._vehicles = build_fleet(store, CONFIG.demo_region, self._rng)
        self._turnaround = build_turnaround(
            store, simulated_routes(store, CONFIG.demo_region)
        )
        logger.info(
            "FleetSimulator: %d vehículos simulados en %d rutas.",
            len(self._vehicles),
            len({v.pattern.route_id for v in self._vehicles}),
        )
        await self._publish()
        self._task = asyncio.create_task(self._run_loop())

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        self._task = None

    async def _run_loop(self) -> None:
        while True:
            await asyncio.sleep(self._interval)
            await self._tick(self._interval)

    def step(self, dt_seconds: float) -> None:
        """Avanza la simulación sin publicar (útil para pruebas y evaluación)."""
        slowdown = self.slowdown_by_mode()
        store = self._engine._gtfs
        for vehicle in self._vehicles:
            route = store.get_route(vehicle.pattern.route_id)
            factor = slowdown.get(route.mode if route else "bus", 1.0)
            vehicle.advance(dt_seconds, factor, self._rng, self._turnaround)

    async def _tick(self, dt_seconds: float) -> None:
        self.step(dt_seconds)
        await self._publish()

    async def _publish(self) -> None:
        await self._engine._gps.update_bulk([v.to_position() for v in self._vehicles])
