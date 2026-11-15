"""
tests/conftest.py — Fixtures compartidas para pytest.

Uso:
    pytest tests/ -v

Las pruebas usan `tests/fixtures/gtfs_mini.zip`, un recorte del GTFS oficial
con siete rutas reales (se regenera con `python scripts/make_test_fixture.py`).
No hacen llamadas de red: el clima se simula despejado.

Fixtures disponibles:
    mock_weather_clear  → WeatherClient simulado, sin lluvia
    mock_weather_rain   → WeatherClient simulado, con lluvia
    sample_stop         → Stop de ejemplo (Av. Vallarta)
    sample_route        → Route de ejemplo (Ruta 45)
    bus_near_stop       → BusPosition a 300 m de la parada
    demand_analyzer     → DemandAnalyzer con clima despejado
    gtfs_loader         → TransitStore cargado con el GTFS de prueba
    engine              → TransitEngine completo sobre el GTFS de prueba
"""

import os
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

# Asegurar que el root del proyecto está en el path
sys.path.insert(0, str(Path(__file__).parent.parent))

os.environ.setdefault("DEMO_SIMULATOR", "false")

from core.config import CONFIG  # noqa: E402
from core.demand_analyzer import DemandAnalyzer  # noqa: E402
from core.eta_engine import ETAEngine  # noqa: E402
from core.models import BusPosition, Route, Stop, WeatherCondition  # noqa: E402
from core.transit import TransitStore  # noqa: E402
from core.weather_client import WeatherClient  # noqa: E402

# Sin red durante las pruebas: el clima real se sustituye por "despejado".
CONFIG.use_mock_weather = True

FIXTURE_GTFS = Path("tests/fixtures/gtfs_mini.zip")
LANDMARKS = Path("data/landmarks.json")

# Rutas y paradas reales presentes en el GTFS de prueba.
BRT_ROUTE = "MC-L1"  # Mi Macro Calzada
BRT_STOP_ID = "MM_003"  # Artes Plásticas, sentido Mirador
BRT_STOP_NAME = "Artes Plásticas"
TRAIN_ROUTE = "MT_L3"  # Línea 3 del tren ligero


# ─────────────────────────────────────────
#  FIXTURES DE CLIMA (mocks)
# ─────────────────────────────────────────


@pytest.fixture
def mock_weather_clear():
    w = MagicMock(spec=WeatherClient)
    w.get_current_condition = AsyncMock(return_value=WeatherCondition.CLEAR)
    w.get_delay_minutes = MagicMock(return_value=0.0)
    w.get_speed_factor = MagicMock(return_value=1.0)
    w.get_demand_factor = MagicMock(return_value=1.0)
    w.is_rain_alert = MagicMock(return_value=False)
    return w


@pytest.fixture
def mock_weather_rain():
    w = MagicMock(spec=WeatherClient)
    w.get_current_condition = AsyncMock(return_value=WeatherCondition.RAIN)
    w.get_delay_minutes = MagicMock(return_value=4.0)
    w.get_speed_factor = MagicMock(return_value=0.75)
    w.get_demand_factor = MagicMock(return_value=1.25)
    w.is_rain_alert = MagicMock(return_value=True)
    return w


# ─────────────────────────────────────────
#  FIXTURES DE MODELOS
# ─────────────────────────────────────────


@pytest.fixture
def sample_stop():
    return Stop(
        stop_id="S004",
        stop_name="Av. Vallarta y Chapultepec",
        lat=20.6750,
        lon=-103.3840,
    )


@pytest.fixture
def sample_route():
    return Route(
        route_id="R045",
        route_short_name="45",
        route_long_name="Plaza del Sol - Centro Histórico",
        route_type=3,
    )


@pytest.fixture
def bus_near_stop(sample_stop):
    return BusPosition(
        vehicle_id="bus_001",
        route_id="R045",
        trip_id="T0001",
        lat=sample_stop.lat + 0.003,
        lon=sample_stop.lon,
        speed_kmh=25.0,
    )


# ─────────────────────────────────────────
#  FIXTURES DE ANALIZADORES
# ─────────────────────────────────────────


@pytest.fixture
def demand_analyzer(mock_weather_clear):
    return DemandAnalyzer(mock_weather_clear)


@pytest.fixture
def eta_engine(mock_weather_clear):
    return ETAEngine(MagicMock(), mock_weather_clear)


# ─────────────────────────────────────────
#  FIXTURES CON EL GTFS DE PRUEBA
# ─────────────────────────────────────────


@pytest.fixture(scope="session")
def gtfs_loader(tmp_path_factory):
    """
    Base de transporte construida desde el GTFS de prueba (una vez por sesión).
    Conserva el nombre histórico `gtfs_loader`; hoy es un `TransitStore`.
    """
    db_path = tmp_path_factory.mktemp("transit") / "transit.db"
    store = TransitStore(FIXTURE_GTFS, db_path, LANDMARKS).load()
    yield store
    store.close()


@pytest.fixture(scope="session")
def engine(gtfs_loader):
    """TransitEngine completo sobre el GTFS de prueba, sin Redis ni red."""
    from core import TransitEngine
    from core.feedback_store import FeedbackStore
    from core.gps_cache import GPSCache
    from core.incident_store import IncidentStore
    from core.prediction_learner import PredictionLearner

    e = TransitEngine()
    e._gtfs = gtfs_loader
    e._initialized = True
    # GPSCache en modo memoria (sin Redis)
    cache = GPSCache.__new__(GPSCache)
    cache._use_memory = True
    cache._memory = {}
    cache._redis = None
    e._gps = cache
    e._incident_store = IncidentStore(":memory:")
    e._incident_store.initialize()
    # FeedbackStore + learner en memoria (tablas creadas aquí)
    e._feedback_store = FeedbackStore(":memory:")
    e._feedback_store.initialize()
    e._learner = PredictionLearner(e._feedback_store)
    e._eta = ETAEngine(e._gtfs, e._weather, learner=e._learner)
    return e


# ─────────────────────────────────────────
#  MARKER para tests que requieren internet
# ─────────────────────────────────────────


def pytest_collection_modifyitems(config, items):
    for item in items:
        if "live" in item.keywords:
            item.add_marker(
                pytest.mark.skip(reason="Test 'live' requiere conexión a internet")
            )
