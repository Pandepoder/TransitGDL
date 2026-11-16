"""
Evaluación del ETA en vivo: método anterior contra método nuevo.

Corre el simulador de flota de forma acelerada sobre las rutas de la zona
CUCEI/Tonalá. Cada minuto simulado se hacen consultas de "¿en cuánto llega la
siguiente unidad a esta parada?" y se guarda lo que predice cada método; luego
se deja correr la simulación y se registra cuándo llegó de verdad la primera
unidad de ese sentido. Con eso se mide:

    • error medio absoluto (minutos),
    • sesgo medio (positivo = la unidad llegó más tarde de lo predicho),
    • cobertura: en cuántos casos la llegada real cayó dentro del rango reportado.

Método anterior (reproducido tal como estaba): la unidad de la ruta más cercana
en línea recta, sin importar el sentido, con su velocidad instantánea.
Método nuevo: `core.eta_engine.ETAEngine`.

Advertencia: el simulador sigue el perfil de tiempos del horario con variación
aleatoria de ritmo; eso favorece al método nuevo, que usa ese mismo perfil.
La comparación mide sobre todo los errores de geometría y de sentido del
método anterior, no la calidad del horario frente a la operación real.

Uso:
    python scripts/evaluate_eta.py
    python scripts/evaluate_eta.py --horas 2 --semilla 7 --salida docs/evaluacion_eta.md
"""

from __future__ import annotations

import argparse
import random
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from statistics import mean

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.config import CONFIG  # noqa: E402
from core.demo_simulator import (
    build_fleet,
    build_turnaround,
    simulated_routes,
)  # noqa: E402
from core.eta_engine import Boarding, ETAEngine  # noqa: E402
from core.models import WeatherCondition, haversine_km  # noqa: E402
from core.timezone_utils import GDL_TZ  # noqa: E402
from core.transit import TransitStore  # noqa: E402
from core.weather_client import WeatherClient, WeatherState  # noqa: E402

STEP_S = 10.0
QUERY_EVERY_S = 60.0
QUERIES_PER_ROUND = 12
PACE_DRIFT = 0.015  # variación del ritmo de cada unidad por paso
PACE_BOUNDS = (0.85, 1.30)
MAX_WAIT_S = 3 * 3600.0


@dataclass
class Query:
    pattern_id: str
    stop_index: int
    asked_at: float
    legacy: tuple[float, float, float] | None  # (eta, rango bajo, rango alto) en min
    new: tuple[float, float, float] | None
    actual: float | None = None


@dataclass
class Scenario:
    name: str
    weather: WeatherState
    queries: list[Query] = field(default_factory=list)


def legacy_eta(store, weather, state, pattern, index, positions):
    """Reproducción del cálculo anterior (línea recta, cualquier sentido)."""
    stop = store.get_stop(pattern.stop_ids[index])
    candidates = [p for p in positions if p.route_id == pattern.route_id]
    if not candidates:
        return None
    best = min(candidates, key=lambda p: haversine_km(p.lat, p.lon, stop.lat, stop.lon))
    distance = haversine_km(best.lat, best.lon, stop.lat, stop.lon)
    condition = state.condition
    speed = max(best.speed_kmh * max(weather.get_speed_factor(condition), 0.4), 5.0)
    eta = distance / speed * 60.0 + weather.get_delay_minutes(condition)
    confidence = (
        0.92
        if distance <= 1
        else 0.84 if distance <= 3 else 0.74 if distance <= 8 else 0.62
    )
    padding = 2.0 if confidence >= 0.88 else 4.0 if confidence >= 0.75 else 6.0
    padding += min(4.0, weather.get_delay_minutes(condition) / 2)
    return eta, max(0.0, eta - padding), eta + padding


def run_scenario(
    store, scenario: Scenario, hours: float, seed: int, start: datetime
) -> None:
    rng = random.Random(seed)
    weather = WeatherClient()
    weather._demo_override = scenario.weather
    engine = ETAEngine(store, weather, learner=None)

    region = CONFIG.demo_region
    vehicles = build_fleet(store, region, rng)
    turnaround = build_turnaround(store, simulated_routes(store, region))
    factor_by_route = {
        v.pattern.route_id: weather.travel_factor(
            scenario.weather, store.get_route(v.pattern.route_id).mode
        )
        for v in vehicles
    }
    patterns = sorted(
        {v.pattern.pattern_id for v in vehicles}
        | {p.pattern_id for p in turnaround.values()}
    )

    def step() -> None:
        for vehicle in vehicles:
            vehicle.pace = min(
                max(
                    vehicle.pace + rng.uniform(-PACE_DRIFT, PACE_DRIFT), PACE_BOUNDS[0]
                ),
                PACE_BOUNDS[1],
            )
            vehicle.advance(
                STEP_S, factor_by_route[vehicle.pattern.route_id], rng, turnaround
            )

    # Calentamiento: que las unidades se repartan y algunas den la vuelta.
    for _ in range(int(1800 / STEP_S)):
        step()

    pending: list[Query] = []
    elapsed = 0.0
    total = hours * 3600.0
    while elapsed < total + MAX_WAIT_S and (elapsed < total or pending):
        if elapsed < total and elapsed % QUERY_EVERY_S < STEP_S / 2:
            now = start + timedelta(seconds=elapsed)
            positions = [v.to_position() for v in vehicles]
            for _ in range(QUERIES_PER_ROUND):
                pattern = store.get_pattern(rng.choice(patterns))
                index = rng.randrange(1, len(pattern.stop_ids) - 1)
                boarding = Boarding(
                    store.get_stop(pattern.stop_ids[index]), pattern, index
                )
                result = engine.estimate(
                    store.get_route(pattern.route_id),
                    boarding,
                    positions,
                    scenario.weather,
                    [],
                    now,
                )
                new = None
                if result is not None and result.method == "vehiculo":
                    new = (
                        result.estimated_minutes,
                        result.window_start_minutes,
                        result.window_end_minutes,
                    )
                query = Query(
                    pattern.pattern_id,
                    index,
                    elapsed,
                    legacy_eta(
                        store, weather, scenario.weather, pattern, index, positions
                    ),
                    new,
                )
                pending.append(query)
                scenario.queries.append(query)

        before = {v.vehicle_id: (v.pattern.pattern_id, v.dist_m) for v in vehicles}
        step()
        elapsed += STEP_S
        for vehicle in vehicles:
            pattern_before, dist_before = before[vehicle.vehicle_id]
            if pattern_before != vehicle.pattern.pattern_id:
                continue  # dio la vuelta en este paso
            for query in list(pending):
                if query.pattern_id != pattern_before:
                    continue
                stop_dist = vehicle.pattern.dist_m[query.stop_index]
                if dist_before < stop_dist <= vehicle.dist_m:
                    share = (stop_dist - dist_before) / max(
                        vehicle.dist_m - dist_before, 1e-6
                    )
                    arrived = elapsed - STEP_S + share * STEP_S
                    query.actual = (arrived - query.asked_at) / 60.0
                    pending.remove(query)
        pending = [q for q in pending if elapsed - q.asked_at < MAX_WAIT_S]


def metrics(pairs: list[tuple[tuple[float, float, float], float]]) -> dict[str, float]:
    errors = [actual - predicted[0] for predicted, actual in pairs]
    inside = [predicted[1] <= actual <= predicted[2] for predicted, actual in pairs]
    return {
        "casos": len(pairs),
        "error_medio_abs": mean(abs(e) for e in errors),
        "sesgo": mean(errors),
        "error_p90": sorted(abs(e) for e in errors)[int(len(errors) * 0.9)],
        "cobertura": 100.0 * sum(inside) / len(inside),
    }


def report(scenarios: list[Scenario], hours: float, seed: int) -> str:
    lines = [
        "# Evaluación del ETA en vivo",
        "",
        f"Simulación de {hours:g} h por escenario sobre las rutas de la zona CUCEI/Tonalá "
        f"(semilla {seed}). Solo se cuentan las consultas con unidad a la vista para ambos "
        "métodos y con llegada observada.",
        "",
        "| Escenario | Método | Casos | Error medio | Error p90 | Sesgo | Llegada dentro del rango |",
        "|---|---|---|---|---|---|---|",
    ]
    for scenario in scenarios:
        pairs_legacy, pairs_new = [], []
        for query in scenario.queries:
            if query.actual is None or query.legacy is None or query.new is None:
                continue
            pairs_legacy.append((query.legacy, query.actual))
            pairs_new.append((query.new, query.actual))
        if not pairs_new:
            continue
        for name, pairs in (
            ("Anterior (línea recta)", pairs_legacy),
            ("Nuevo (sobre la ruta)", pairs_new),
        ):
            m = metrics(pairs)
            lines.append(
                f"| {scenario.name} | {name} | {m['casos']} | {m['error_medio_abs']:.1f} min | "
                f"{m['error_p90']:.1f} min | {m['sesgo']:+.1f} min | {m['cobertura']:.0f} % |"
            )
    lines += [
        "",
        "El simulador sigue el perfil de tiempos del horario con variación aleatoria de ritmo por "
        "unidad, así que favorece al método nuevo; la diferencia mide sobre todo los errores de "
        "geometría (línea recta) y de sentido del método anterior.",
    ]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Evalúa el ETA en vivo con el simulador."
    )
    parser.add_argument("--gtfs", default=CONFIG.gtfs_path)
    parser.add_argument("--db", default=CONFIG.transit_db_path)
    parser.add_argument("--horas", type=float, default=2.0)
    parser.add_argument("--semilla", type=int, default=7)
    parser.add_argument("--salida", help="Archivo Markdown donde guardar la tabla")
    args = parser.parse_args(argv)

    store = TransitStore(args.gtfs, args.db, CONFIG.landmarks_path).load()
    start = datetime(2026, 11, 10, 7, 0, tzinfo=GDL_TZ)  # martes, 7:00
    scenarios = [
        Scenario("Despejado", WeatherState(WeatherCondition.CLEAR)),
        Scenario("Lluvia fuerte", WeatherState(WeatherCondition.RAIN, 10.0)),
    ]
    for offset, scenario in enumerate(scenarios):
        run_scenario(store, scenario, args.horas, args.semilla + offset, start)
    store.close()

    text = report(scenarios, args.horas, args.semilla)
    print(text)
    if args.salida:
        Path(args.salida).parent.mkdir(parents=True, exist_ok=True)
        Path(args.salida).write_text(text, encoding="utf-8")
        print(f"Tabla guardada en {args.salida}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
