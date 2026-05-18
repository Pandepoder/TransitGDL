"""
tests/conftest.py — Fixtures compartidas para pytest.

Uso:
    pytest tests/ -v

Fixtures disponibles:
    mock_weather_clear  → WeatherClient simulado, sin lluvia
    mock_weather_rain   → WeatherClient simulado, con lluvia
    sample_stop         → Stop de ejemplo (Av. Vallarta)
    sample_route        → Route de ejemplo (Ruta 45)
    bus_near_stop       → BusPosition a 300m de la parada
    demand_analyzer     → DemandAnalyzer con clima despejado
    gtfs_loader         → GTFSLoader cargado con GTFS de demo
    engine              → TransitEngine completo (con GTFS demo)
"""

import subprocess
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

# Asegurar que el root del proyecto está en el path
sys.path.insert(0, str(Path(__file__).parent.parent))

from core.demand_analyzer import DemandAnalyzer
from core.eta_engine import ETAEngine
from core.gtfs_loader import GTFSLoader
from core.models import BusPosition, Route, Stop, WeatherCondition
from core.weather_client import WeatherClient

DEMO_GTFS = Path("data/gtfs_guadalajara.zip")


# ─────────────────────────────────────────
#  ASEGURAR GTFS DE DEMO
# ─────────────────────────────────────────


def pytest_configure(config):
    """Genera el GTFS de demo si no existe, antes de correr los tests."""
    import os

    os.environ.setdefault("DEMO_SIMULATOR", "false")
    if not DEMO_GTFS.exists():
        print("\n[conftest] Generando GTFS de demo...")
        subprocess.run(
            [sys.executable, "scripts/seed_demo.py"],
            input=b"s\n",
            check=True,
        )
        print("[conftest] GTFS de demo listo.")


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
#  FIXTURES CON GTFS REAL (demo)
# ─────────────────────────────────────────


@pytest.fixture(scope="session")
def gtfs_loader():
    """GTFSLoader cargado con el GTFS de demo. Scope session = carga una sola vez."""
    loader = GTFSLoader(str(DEMO_GTFS))
    loader.load()
    return loader


@pytest.fixture(scope="session")
def engine(gtfs_loader):
    """TransitEngine completo con GTFS demo, sin Redis (modo memoria)."""
    from core import TransitEngine
    from core.eta_engine import ETAEngine
    from core.feedback_store import FeedbackStore
    from core.incident_store import IncidentStore
    from core.prediction_learner import PredictionLearner
    from core.weather_client import WeatherClient

    e = TransitEngine()
    e._gtfs = gtfs_loader
    e._initialized = True
    # GPSCache en modo memoria (sin Redis)
    from core.gps_cache import GPSCache

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
    e._eta = ETAEngine(e._gtfs, WeatherClient(), learner=e._learner)
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
