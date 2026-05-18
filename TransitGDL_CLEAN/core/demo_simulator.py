from __future__ import annotations

import asyncio
import logging
import random
from dataclasses import dataclass, field

from .config import CONFIG
from .gtfs_loader import GTFSLoader
from .models import BusPosition, haversine_km

logger = logging.getLogger(__name__)

# Rutas destacadas con estilo y velocidad creíble (km/h).
ROUTE_PROFILES: dict[str, dict] = {
    "RG_55": {"mode": "bus", "speed_kmh": 28, "vehicles": 2},
    "RG_231": {"mode": "bus", "speed_kmh": 30, "vehicles": 2},
    "RG_646": {"mode": "bus", "speed_kmh": 26, "vehicles": 3},
    "RG_615": {"mode": "bus", "speed_kmh": 24, "vehicles": 2},
    "MP-A04": {"mode": "bus", "speed_kmh": 26, "vehicles": 2},
    "MP-A05-2": {"mode": "bus", "speed_kmh": 26, "vehicles": 2},
    "MC-L1": {"mode": "brt", "speed_kmh": 30, "vehicles": 2},
    "MACRO01_R": {"mode": "brt", "speed_kmh": 32, "vehicles": 5},
    "MACRO02_E": {"mode": "brt", "speed_kmh": 38, "vehicles": 2},
    "MT_L1": {"mode": "train", "speed_kmh": 48, "vehicles": 4},
    "MT_L2": {"mode": "train", "speed_kmh": 46, "vehicles": 3},
    "MT_L3": {"mode": "train", "speed_kmh": 44, "vehicles": 3},
    "ST_L1": {"mode": "tram", "speed_kmh": 34, "vehicles": 3},
    "ST_L1B": {"mode": "tram", "speed_kmh": 32, "vehicles": 2},
    "ST_L2": {"mode": "tram", "speed_kmh": 34, "vehicles": 3},
    "ST_L3": {"mode": "tram", "speed_kmh": 33, "vehicles": 2},
    "ST_L4": {"mode": "tram", "speed_kmh": 33, "vehicles": 3},
}

DEFAULT_BUS_PROFILE = {"mode": "bus", "speed_kmh": 24, "vehicles": 1}


@dataclass
class _VehicleState:
    vehicle_id: str
    route_id: str
    mode: str
    path: list[tuple[float, float]]
    seg_index: int = 0
    seg_progress: float = 0.0
    speed_kmh: float = 25.0
    forward: bool = True
    phase_offset: float = field(default_factory=random.random)

    def position_at(self) -> tuple[float, float]:
        if len(self.path) < 2:
            if self.path:
                return self.path[0]
            return 20.6736, -103.3438

        idx = min(self.seg_index, len(self.path) - 2)
        a = self.path[idx]
        b = self.path[idx + 1]
        t = max(0.0, min(1.0, self.seg_progress))
        return a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t

    def advance(self, dt_seconds: float) -> None:
        if len(self.path) < 2:
            return

        idx = min(self.seg_index, len(self.path) - 2)
        a, b = self.path[idx], self.path[idx + 1]
        dist_km = max(haversine_km(a[0], a[1], b[0], b[1]), 0.0005)
        travel_km = (self.speed_kmh / 3600.0) * dt_seconds
        self.seg_progress += travel_km / dist_km

        while self.seg_progress >= 1.0 and self.seg_index < len(self.path) - 2:
            self.seg_progress -= 1.0
            self.seg_index += 1

        if self.seg_index >= len(self.path) - 2:
            self.seg_index = 0
            self.seg_progress = self.phase_offset * 0.35
            self.forward = not self.forward
            if not self.forward:
                self.path = list(reversed(self.path))


class FleetSimulator:
    """Simula flota en tiempo real sobre paradas oficiales del GTFS."""

    def __init__(self, engine) -> None:
        self._engine = engine
        self._task: asyncio.Task | None = None
        self._vehicles: list[_VehicleState] = []
        self._interval = float(CONFIG.demo_simulator_interval_seconds)

    def _build_vehicles(self, gtfs: GTFSLoader) -> list[_VehicleState]:
        vehicles: list[_VehicleState] = []
        for route in gtfs.get_routes():
            profile = ROUTE_PROFILES.get(route.route_id, DEFAULT_BUS_PROFILE)
            path = gtfs.get_route_path(route.route_id)
            if len(path) < 2:
                continue
            count = profile.get("vehicles", 1)
            if route.route_id not in ROUTE_PROFILES and len(path) > 30:
                count = 2

            for n in range(count):
                phase = (n + 1) / (count + 1)
                seg_index = int(phase * (len(path) - 2))
                vehicles.append(
                    _VehicleState(
                        vehicle_id=f"SIM_{route.route_id}_{n+1}",
                        route_id=route.route_id,
                        mode=profile["mode"],
                        path=path,
                        seg_index=seg_index,
                        seg_progress=random.uniform(0.05, 0.75),
                        speed_kmh=profile["speed_kmh"]
                        * random.uniform(0.88, 1.08),
                        phase_offset=phase,
                    )
                )
        return vehicles

    async def start(self) -> None:
        if self._task is not None:
            return
        gtfs = self._engine._gtfs
        if not gtfs.loaded:
            gtfs.load()
        self._vehicles = self._build_vehicles(gtfs)
        logger.info(
            "FleetSimulator: %d vehículos simulados en %d rutas.",
            len(self._vehicles),
            len({v.route_id for v in self._vehicles}),
        )
        await self._tick(self._interval)
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
        try:
            while True:
                await self._tick(self._interval)
                await asyncio.sleep(self._interval)
        except asyncio.CancelledError:
            raise

    async def _tick(self, dt_seconds: float) -> None:
        positions: list[BusPosition] = []
        for state in self._vehicles:
            state.advance(dt_seconds)
            lat, lon = state.position_at()
            positions.append(
                BusPosition(
                    vehicle_id=state.vehicle_id,
                    route_id=state.route_id,
                    lat=lat,
                    lon=lon,
                    speed_kmh=round(state.speed_kmh, 1),
                )
            )
        await self._engine._gps.update_bulk(positions)

    @property
    def vehicle_count(self) -> int:
        return len(self._vehicles)
