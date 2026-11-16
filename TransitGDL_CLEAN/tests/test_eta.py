"""
Pruebas del cálculo (Fase 2): ETA por unidad en vivo, frecuencia y horario;
clima, incidencias, aprendizaje, flota, simulador y migraciones.

Todas usan horas fijas para no depender de cuándo se corren.
"""

from __future__ import annotations

import asyncio
import json
import random
import sqlite3
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from core import TransitEngine
from core.config import TransitEngineConfig
from core.demand_analyzer import DemandAnalyzer
from core.demo_simulator import FleetSimulator, build_fleet
from core.expert import SistemaExperto
from core.feedback_store import FeedbackStore, PredictionRecord
from core.fleet import fleet_plan
from core.incident_store import IncidentStore
from core.models import (
    BusPosition,
    FleetAction,
    Incident,
    IncidentSeverity,
    WeatherCondition,
)
from core.prediction_learner import PredictionLearner, period_of
from core.timezone_utils import GDL_TZ
from core.weather_client import WeatherClient, WeatherState

TUESDAY_8AM = datetime(2026, 11, 10, 8, 0, tzinfo=GDL_TZ)
TUESDAY_3AM = datetime(2026, 11, 10, 3, 0, tzinfo=GDL_TZ)
CLEAR = WeatherState(WeatherCondition.CLEAR)
HEAVY_RAIN = WeatherState(WeatherCondition.RAIN, 10.0)

# T15 sentido 1 (hacia La Coronilla) pasa por la parada "Cucei".
T15_TO_CORONILLA = "T15:1"


@pytest.fixture
def eta(engine):
    """Motor de ETA con un aprendizaje vacío propio de cada prueba."""
    from core.eta_engine import ETAEngine

    store = FeedbackStore(":memory:")
    store.initialize()
    return ETAEngine(engine._gtfs, WeatherClient(), learner=PredictionLearner(store))


def _cucei(gtfs):
    pattern = gtfs.get_pattern(T15_TO_CORONILLA)
    index = next(
        i
        for i, sid in enumerate(pattern.stop_ids)
        if gtfs.get_stop(sid).stop_name == "Cucei"
    )
    return pattern, index


def _bus_at(
    pattern, progress_m, vehicle_id="bus", *, declare=True, bearing=None, speed=20.0
):
    lat, lon = pattern.line.point_at(progress_m)
    return BusPosition(
        vehicle_id=vehicle_id,
        route_id=pattern.route_id,
        lat=lat,
        lon=lon,
        speed_kmh=speed,
        pattern_id=pattern.pattern_id if declare else None,
        progress_m=progress_m if declare else None,
        bearing=bearing,
    )


def _option_for(results, pattern_id):
    return next(r for r in results if r.pattern_id == pattern_id)


# ── Unidad en vivo ───────────────────────────────────────────────────────────


def test_live_eta_uses_schedule_profile_along_the_route(eta, engine):
    pattern, index = _cucei(engine._gtfs)
    start = pattern.dist_m[index] - 1500.0
    bus = _bus_at(pattern, start)

    results = eta.estimate_options("231", "Cucei", [bus], CLEAR, [], TUESDAY_8AM)
    result = _option_for(results, T15_TO_CORONILLA)

    expected = (pattern.offset_s[index] - pattern.time_at(start)) / 60.0
    assert result.method == "vehiculo"
    assert result.source == "live"
    assert result.vehicle_id == "bus"
    assert result.estimated_minutes == pytest.approx(expected, abs=0.01)
    assert result.vehicle_distance_m == pytest.approx(1500.0)
    assert (
        result.window_start_minutes
        < result.estimated_minutes
        < result.window_end_minutes
    )
    assert result.headsign == "La Coronilla"


def test_live_eta_ignores_opposite_direction_and_buses_that_already_passed(eta, engine):
    gtfs = engine._gtfs
    pattern, index = _cucei(gtfs)
    stop = gtfs.get_stop(pattern.stop_ids[index])
    opposite = gtfs.get_pattern("T15:0")
    # Una unidad del sentido contrario justo frente a la parada (la más cercana en
    # línea recta) y otra del sentido correcto que ya pasó.
    near_dist, _ = opposite.line.project(stop.lat, stop.lon)
    wrong_way = _bus_at(opposite, near_dist, "contrario")
    passed = _bus_at(pattern, pattern.dist_m[index] + 400.0, "ya_paso")
    coming = _bus_at(pattern, pattern.dist_m[index] - 1500.0, "viene")

    results = eta.estimate_options(
        "T15", "Cucei", [wrong_way, passed, coming], CLEAR, [], TUESDAY_8AM
    )

    assert _option_for(results, T15_TO_CORONILLA).vehicle_id == "viene"


def test_live_eta_picks_first_to_arrive_not_nearest_in_straight_line(eta, engine):
    pattern, index = _cucei(engine._gtfs)
    far = _bus_at(pattern, pattern.dist_m[index] - 6000.0, "lejos")
    close = _bus_at(pattern, pattern.dist_m[index] - 1500.0, "cerca")

    result = _option_for(
        eta.estimate_options("T15", "Cucei", [far, close], CLEAR, [], TUESDAY_8AM),
        T15_TO_CORONILLA,
    )

    assert result.vehicle_id == "cerca"
    assert (
        result.next_arrivals_minutes
        and result.next_arrivals_minutes[0] > result.estimated_minutes
    )


def test_undeclared_gps_is_matched_by_position_and_heading(eta, engine):
    pattern, index = _cucei(engine._gtfs)
    where = pattern.dist_m[index] - 1200.0
    heading = pattern.line.bearing_at(where)
    same_way = _bus_at(pattern, where, "mismo", declare=False, bearing=heading)
    other_way = _bus_at(
        pattern, where, "contra", declare=False, bearing=(heading + 180) % 360
    )

    with_same = eta.estimate_options("T15", "Cucei", [same_way], CLEAR, [], TUESDAY_8AM)
    with_other = eta.estimate_options(
        "T15", "Cucei", [other_way], CLEAR, [], TUESDAY_8AM
    )

    assert _option_for(with_same, T15_TO_CORONILLA).vehicle_id == "mismo"
    assert _option_for(with_other, T15_TO_CORONILLA).method == "frecuencia"


def test_far_tracked_bus_yields_to_frequency_with_partial_coverage(eta, engine):
    """Con rastreo parcial, una unidad a varias frecuencias no es la siguiente en pasar."""
    pattern, index = _cucei(engine._gtfs)
    start = pattern.dist_m[index] - 6000.0
    far = _bus_at(pattern, start, "lejos")

    result = _option_for(
        eta.estimate_options("T15", "Cucei", [far], CLEAR, [], TUESDAY_8AM),
        T15_TO_CORONILLA,
    )

    expected = (pattern.offset_s[index] - pattern.time_at(start)) / 60.0
    assert result.method == "frecuencia"
    assert result.estimated_minutes == pytest.approx(2.5)
    assert result.tracked_vehicle_minutes == pytest.approx(expected, abs=0.01)


# ── Sin unidades: frecuencia y horario ───────────────────────────────────────


def test_frequency_wait_is_half_the_headway(eta):
    result = _option_for(
        eta.estimate_options("T15", "Cucei", [], CLEAR, [], TUESDAY_8AM),
        T15_TO_CORONILLA,
    )

    assert result.method == "frecuencia"
    assert result.average_headway_minutes == 5
    assert result.estimated_minutes == pytest.approx(2.5)
    assert (result.window_start_minutes, result.window_end_minutes) == pytest.approx(
        (0, 5)
    )
    assert result.next_departures == []


def test_before_service_starts_uses_first_scheduled_pass(eta, engine):
    pattern, index = _cucei(engine._gtfs)
    window = next(w for w in pattern.windows if w.service_id == "LS")
    first_pass_s = window.start_s + pattern.offset_s[index]

    result = _option_for(
        eta.estimate_options("T15", "Cucei", [], CLEAR, [], TUESDAY_3AM),
        T15_TO_CORONILLA,
    )

    assert result.method == "horario"
    assert result.estimated_minutes == pytest.approx(
        (first_pass_s - 3 * 3600) / 60, abs=0.02
    )
    assert result.next_departures


def test_options_list_every_direction_soonest_first(eta):
    results = eta.estimate_options("MT_L3", "MT-L3-113", [], CLEAR, [], TUESDAY_8AM)

    assert {r.headsign for r in results} == {"Arcos de Zapopan", "Central de Autobuses"}
    assert [r.estimated_minutes for r in results] == sorted(
        r.estimated_minutes for r in results
    )


def test_place_query_reports_walking_distance(eta):
    results = eta.estimate_options("615", "CUCEI", [], CLEAR, [], TUESDAY_8AM)

    assert results
    assert all(r.place_name == "CUCEI" and r.walk_meters < 600 for r in results)


# ── Clima ────────────────────────────────────────────────────────────────────


def test_weather_factor_depends_on_intensity_and_mode():
    weather = WeatherClient()
    assert weather.travel_factor(CLEAR, "bus") == 1.0
    assert weather.travel_factor(
        WeatherState(WeatherCondition.RAIN, 1.0), "bus"
    ) == pytest.approx(1.10)
    assert weather.travel_factor(
        WeatherState(WeatherCondition.RAIN, 4.0), "bus"
    ) == pytest.approx(1.20)
    assert weather.travel_factor(HEAVY_RAIN, "bus") == pytest.approx(1.35)
    # El tren corre en vía propia: recibe una fracción del efecto.
    assert weather.travel_factor(HEAVY_RAIN, "tren") == pytest.approx(1.07)
    assert HEAVY_RAIN.label == "lluvia fuerte"


def test_rain_stretches_live_eta_by_travel_factor(eta, engine):
    pattern, index = _cucei(engine._gtfs)
    bus = _bus_at(pattern, pattern.dist_m[index] - 1200.0)

    dry = _option_for(
        eta.estimate_options("T15", "Cucei", [bus], CLEAR, [], TUESDAY_8AM),
        T15_TO_CORONILLA,
    )
    wet = _option_for(
        eta.estimate_options("T15", "Cucei", [bus], HEAVY_RAIN, [], TUESDAY_8AM),
        T15_TO_CORONILLA,
    )

    assert wet.estimated_minutes == pytest.approx(
        dry.estimated_minutes * 1.35, rel=1e-6
    )
    assert wet.delay_minutes == pytest.approx(
        wet.estimated_minutes - dry.estimated_minutes, abs=0.1
    )
    assert wet.weather_label == "lluvia fuerte"


def test_weather_reads_intensity_from_open_meteo(monkeypatch):
    """La precipitación del intervalo se convierte a mm por hora."""
    from core.config import CONFIG

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "current": {"interval": 900, "weather_code": 63, "precipitation": 1.5}
            }

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def get(self, url, params):
            assert "precipitation" in params["current"]
            return FakeResponse()

    monkeypatch.setattr(CONFIG, "use_mock_weather", False)
    monkeypatch.setattr("core.weather_client.httpx.AsyncClient", FakeClient)

    state = asyncio.run(WeatherClient().get_current_state())

    assert state.condition == WeatherCondition.RAIN
    assert state.precipitation_mm_h == pytest.approx(6.0)
    assert state.key == "rain_moderada"


# ── Incidencias ──────────────────────────────────────────────────────────────


def _incident(severity, *, routes=("T15",), at=None):
    lat, lon = at if at else (None, None)
    return Incident(
        description="Prueba",
        affected_routes=list(routes),
        severity=severity,
        lat=lat,
        lon=lon,
    )


def test_incident_only_counts_between_bus_and_stop(eta, engine):
    pattern, index = _cucei(engine._gtfs)
    stop_dist = pattern.dist_m[index]
    bus = _bus_at(pattern, stop_dist - 1500.0)
    between = pattern.line.point_at(stop_dist - 700.0)
    ahead = _incident(IncidentSeverity.HIGH, at=between)
    behind_stop = _incident(
        IncidentSeverity.HIGH, at=pattern.line.point_at(stop_dist + 1500)
    )
    other_route = _incident(IncidentSeverity.CRITICAL, routes=("MT_L3",), at=between)

    base = _option_for(
        eta.estimate_options("T15", "Cucei", [bus], CLEAR, [], TUESDAY_8AM),
        T15_TO_CORONILLA,
    )
    hit = _option_for(
        eta.estimate_options("T15", "Cucei", [bus], CLEAR, [ahead], TUESDAY_8AM),
        T15_TO_CORONILLA,
    )
    miss = _option_for(
        eta.estimate_options(
            "T15", "Cucei", [bus], CLEAR, [behind_stop, other_route], TUESDAY_8AM
        ),
        T15_TO_CORONILLA,
    )

    assert base.method == hit.method == "vehiculo"
    assert hit.estimated_minutes == pytest.approx(base.estimated_minutes + 8)
    assert hit.alerts == ["Prueba (rutas T15)"]
    assert miss.estimated_minutes == pytest.approx(base.estimated_minutes)
    assert miss.alerts == []


def test_incident_without_location_counts_partially(eta):
    unlocated = _incident(IncidentSeverity.MEDIUM)

    base = _option_for(
        eta.estimate_options("T15", "Cucei", [], CLEAR, [], TUESDAY_8AM),
        T15_TO_CORONILLA,
    )
    hit = _option_for(
        eta.estimate_options("T15", "Cucei", [], CLEAR, [unlocated], TUESDAY_8AM),
        T15_TO_CORONILLA,
    )

    assert hit.estimated_minutes == pytest.approx(base.estimated_minutes + 1.5)


def test_incidents_expire_by_severity(tmp_path):
    store = IncidentStore(str(tmp_path / "incidents.db"))
    created = datetime(2026, 11, 10, 8, 0, tzinfo=timezone.utc)
    store.add_incident(
        Incident(
            description="Choque",
            affected_routes=["T15"],
            severity="high",
            created_at=created,
        )
    )

    assert (
        len(store.list_incidents(route_id="T15", now=created + timedelta(hours=3))) == 1
    )
    assert store.list_incidents(route_id="T15", now=created + timedelta(hours=5)) == []
    assert store.count_active(now=created + timedelta(hours=5)) == 0
    # Sin `active_only` se siguen viendo en el historial.
    assert len(store.list_incidents(active_only=False)) == 1
    store.close()


# ── Aprendizaje ──────────────────────────────────────────────────────────────


def test_learner_converges_to_the_real_bias():
    """Regresión: antes se estancaba en la mitad del retraso real."""
    store = FeedbackStore(":memory:")
    store.initialize()
    learner = PredictionLearner(store)
    base, delay = 5.0, 10.0
    for _ in range(40):
        correction = learner.get_correction("R", "S", 8, 1, 0)
        shown = base + correction
        learner.record_feedback("R", "S", 8, 1, (base + delay) - shown, 0, correction)

    assert learner.get_correction("R", "S", 8, 1, 0) == pytest.approx(delay, abs=0.1)


def test_learner_falls_back_to_route_average_for_new_stops():
    store = FeedbackStore(":memory:")
    store.initialize()
    learner = PredictionLearner(store)
    for _ in range(6):
        learner.record_feedback("R", "A", 8, 1, 8.0, 0, 0.0)

    assert learner.get_correction("R", "B", 8, 1, 0) == pytest.approx(8.0)
    # Otro sentido y otra franja no heredan el sesgo.
    assert learner.get_correction("R", "B", 8, 1, 1) == 0.0
    assert learner.get_correction("R", "B", 23, 1, 0) == 0.0


def test_learning_periods():
    assert [period_of(h) for h in (3, 7, 11, 14, 16, 19, 22)] == [
        "madrugada",
        "pico_manana",
        "valle_manana",
        "pico_mediodia",
        "valle_tarde",
        "pico_tarde",
        "noche",
    ]


def test_engine_feedback_loop_learns_a_constant_delay(gtfs_loader, tmp_path):
    engine = TransitEngine(
        TransitEngineConfig(
            gtfs_path=str(tmp_path / "x.zip"),
            incidents_db_path=str(tmp_path / "incidents.db"),
            feedback_db_path=str(tmp_path / "feedback.db"),
        )
    )
    engine.initialize_with_loader(gtfs_loader)

    async def run():
        # Una unidad en vivo poco antes de la parada: el aprendizaje corrige las
        # estimaciones con unidad (las de frecuencia no tienen un sesgo que aprender).
        await _place_bus_before(engine, "T15", "Cucei", meters=1200)
        for _ in range(30):
            result = await engine.get_eta("T15", "Cucei")
            assert result.method == "vehiculo"
            # La unidad siempre tarda 6 minutos más de lo que dice el cálculo base.
            assert engine.record_feedback(
                result.prediction_id, actual_minutes=result.base_minutes + 6
            )
        return await engine.get_eta("T15", "Cucei")

    try:
        final = asyncio.run(run())
        assert final.learned_correction_minutes == pytest.approx(6, abs=0.2)
        assert final.estimated_minutes == pytest.approx(final.base_minutes + 6, abs=0.2)
    finally:
        engine._gps._memory.clear()
        engine._feedback_store.close()
        engine._incident_store.close()


def test_frequency_feedback_is_saved_but_not_learned(gtfs_loader, tmp_path):
    """Con "pasa cada H min" la espera es aleatoria entre 0 y H: no es un sesgo."""
    engine = TransitEngine(
        TransitEngineConfig(
            gtfs_path=str(tmp_path / "x.zip"),
            incidents_db_path=str(tmp_path / "incidents.db"),
            feedback_db_path=str(tmp_path / "feedback.db"),
        )
    )
    engine.initialize_with_loader(gtfs_loader)

    async def run():
        for _ in range(10):
            result = await engine.get_eta("T15", "Cucei")
            assert result.method == "frecuencia"
            assert engine.record_feedback(
                result.prediction_id, actual_minutes=result.base_minutes + 6
            )
        return await engine.get_eta("T15", "Cucei")

    try:
        final = asyncio.run(run())
        assert final.learned_correction_minutes == 0.0
        assert engine._feedback_store.get_global_stats()["total_feedback"] >= 10
    finally:
        engine._feedback_store.close()
        engine._incident_store.close()


async def _place_bus_before(engine, route_id: str, stop_query: str, meters: float):
    """Pone una unidad en vivo `meters` antes de la parada, en el sentido que la sirve."""
    route = engine._gtfs.find_route(route_id)
    boarding = engine._eta.resolve_boardings(route, stop_query)[0]
    pattern = boarding.pattern
    progress = max(0.0, pattern.dist_m[boarding.index] - meters)
    lat, lon = pattern.line.point_at(progress)
    await engine._gps.update(
        BusPosition(
            vehicle_id="vivo-1",
            route_id=route.route_id,
            pattern_id=pattern.pattern_id,
            progress_m=progress,
            bearing=pattern.line.bearing_at(progress),
            lat=lat,
            lon=lon,
            speed_kmh=18,
        )
    )


# ── Migraciones ──────────────────────────────────────────────────────────────


def test_feedback_db_v1_is_upgraded_in_place(tmp_path):
    from core.feedback_store import _SCHEMA_V1

    path = tmp_path / "feedback.db"
    conn = sqlite3.connect(path)
    conn.executescript(_SCHEMA_V1)
    conn.execute(
        "INSERT INTO predictions VALUES ('p1', 'T15', 'S', 5.0, 'clear', 8, 1, 'schedule', ?)",
        (datetime.now(timezone.utc).isoformat(),),
    )
    conn.commit()
    conn.close()

    store = FeedbackStore(str(path))
    store.initialize()

    record = store.get_prediction("p1")
    assert record.predicted_minutes == 5.0 and record.learned_correction == 0.0
    assert store._connect().execute("PRAGMA user_version").fetchone()[0] == 2
    store.close()


def test_incident_db_v1_is_upgraded_and_backfilled(tmp_path):
    from core.incident_store import _SCHEMA_V1

    path = tmp_path / "incidents.db"
    conn = sqlite3.connect(path)
    conn.executescript(_SCHEMA_V1)
    conn.execute(
        "INSERT INTO incidents VALUES ('i1', 'Viejo', ?, 'medium', 1, ?, NULL, '[]')",
        (json.dumps(["T15"]), datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()
    conn.close()

    store = IncidentStore(str(path))
    store.initialize()

    assert [i.incident_id for i in store.list_incidents(route_id="T15")] == ["i1"]
    store.close()


def test_old_unrated_predictions_are_purged():
    store = FeedbackStore(":memory:")
    store.initialize()
    old = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
    for prediction_id in ("vieja", "calificada"):
        store.save_prediction(
            PredictionRecord(
                prediction_id, "T15", "S", 5.0, "clear", 8, 1, "schedule", old
            )
        )
    store._connect().execute(
        "INSERT INTO feedback VALUES ('f', 'calificada', 'T15', 'S', 5, 6, 1, NULL, 8, 1, ?)",
        (old,),
    )

    assert store.purge_old_predictions() == 1
    assert store.get_prediction("vieja") is None
    assert store.get_prediction("calificada") is not None


# ── Flota ────────────────────────────────────────────────────────────────────


def test_fleet_need_is_cycle_time_over_headway(gtfs_loader):
    plan = fleet_plan(gtfs_loader, "231", TUESDAY_8AM)
    patterns = gtfs_loader.get_patterns("T15", main_only=True)
    cycle_s = sum(p.run_time_s + max(240, 0.1 * p.run_time_s) for p in patterns)

    assert plan.headway_min == 5
    assert plan.cycle_min == pytest.approx(cycle_s / 60, abs=0.1)
    assert plan.required_vehicles == -(-int(cycle_s) // 300)
    # La T15 tiene la misma frecuencia todo el día: no se puede deducir su pico.
    assert plan.is_peak is None
    assert fleet_plan(gtfs_loader, "T15", TUESDAY_3AM).required_vehicles == 0


def test_rain_raises_fleet_need(gtfs_loader):
    dry = fleet_plan(gtfs_loader, "T15", TUESDAY_8AM, 1.0)
    wet = fleet_plan(gtfs_loader, "T15", TUESDAY_8AM, 1.35)
    assert wet.required_vehicles > dry.required_vehicles


def test_train_peak_comes_from_its_own_frequencies(gtfs_loader):
    assert fleet_plan(gtfs_loader, "MT_L3", TUESDAY_8AM).is_peak is True
    assert (
        fleet_plan(
            gtfs_loader, "MT_L3", datetime(2026, 11, 10, 11, 30, tzinfo=GDL_TZ)
        ).is_peak
        is False
    )


@pytest.mark.parametrize(
    ("vehicles", "action"),
    [
        (10, FleetAction.ADD_VEHICLES),
        (48, FleetAction.KEEP),
        (80, FleetAction.REDUCE_VEHICLES),
    ],
)
def test_fleet_recommendation_compares_with_need(gtfs_loader, vehicles, action):
    analyzer = DemandAnalyzer(WeatherClient(), gtfs_loader)

    snapshot = asyncio.run(
        analyzer.get_demand_snapshot("231", vehicles, now=TUESDAY_8AM)
    )
    recommendation = analyzer.get_fleet_recommendation(snapshot)

    assert snapshot.route_id == "T15"
    assert snapshot.required_vehicles == recommendation.recommended_vehicles
    assert recommendation.action == action
    assert "unidades necesarias" in snapshot.rationale


def test_expert_uses_fleet_coverage():
    experto = SistemaExperto()
    hechos = {
        "clima": "clear",
        "hora": 11,
        "es_fin_de_semana": False,
        "es_festivo": False,
        "tipo_ruta": 3,
        "vehiculos_en_ruta": 24,
        "frecuencia_min": 5,
        "incidentes_activos": 0,
        "hay_ruta_alterna": True,
        "muestras_feedback": 0,
        "unidades_necesarias": 48,
        "cobertura_flota": 0.5,
    }

    d = experto.diagnosticar(hechos)
    ids = [a.id for a in d.acciones]

    assert "agregar_unidades" in ids
    assert d.vehiculos_recomendados == 48
    plan = next(a for a in d.acciones if a.id == "agregar_unidades")
    assert "R61" in plan.reglas


# ── Simulador ────────────────────────────────────────────────────────────────


def test_rain_slows_buses_more_than_trains(gtfs_loader):
    def run(weather_state):
        weather = WeatherClient()
        weather._demo_override = weather_state
        sim = FleetSimulator(
            SimpleNamespace(_gtfs=gtfs_loader, _weather=weather), seed=3
        )
        sim._vehicles = build_fleet(gtfs_loader, "tonala_cucei", random.Random(3))
        for vehicle in sim._vehicles:
            vehicle.schedule_s = 0.0
        sim.step(600.0)
        return {v.vehicle_id: v.schedule_s for v in sim.vehicles}

    dry, wet = run(CLEAR), run(HEAVY_RAIN)
    bus = next(k for k in dry if k.startswith("SIM_T15_"))
    train = next(k for k in dry if k.startswith("SIM_MT_L3_"))

    assert dry[bus] / wet[bus] == pytest.approx(1.35)
    assert dry[train] / wet[train] == pytest.approx(1.07)
