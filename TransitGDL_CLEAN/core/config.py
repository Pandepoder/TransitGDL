from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

try:
    from dotenv import load_dotenv

    load_dotenv(PROJECT_ROOT / ".env", override=True)
except ImportError:
    pass

DEFAULT_GTFS_PATH = "data/gtfs_amg.zip"
DEFAULT_TRANSIT_DB_PATH = "data/transit.db"
DEFAULT_LANDMARKS_PATH = "data/landmarks.json"
DEFAULT_INCIDENTS_DB_PATH = "data/incidents.db"
DEFAULT_FEEDBACK_DB_PATH = "data/feedback.db"


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None or not str(value).strip():
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def is_demo_simulator_enabled() -> bool:
    """On by default in development; set DEMO_SIMULATOR=false to disable."""
    raw = os.getenv("DEMO_SIMULATOR")
    if raw is not None and str(raw).strip():
        return _env_bool("DEMO_SIMULATOR", True)
    return os.getenv("ENVIRONMENT", "development").strip().lower() != "production"


def _default_use_memory_cache() -> bool:
    raw = os.getenv("USE_MEMORY_CACHE")
    if raw is not None and str(raw).strip():
        return _env_bool("USE_MEMORY_CACHE", False)
    return os.getenv("ENVIRONMENT", "development").strip().lower() != "production"


@dataclass(slots=True)
class TransitEngineConfig:
    gtfs_path: str = os.getenv("GTFS_PATH", DEFAULT_GTFS_PATH)
    incidents_db_path: str = os.getenv("INCIDENTS_DB_PATH", DEFAULT_INCIDENTS_DB_PATH)
    feedback_db_path: str = os.getenv("FEEDBACK_DB_PATH", DEFAULT_FEEDBACK_DB_PATH)
    redis_url: str = os.getenv("REDIS_URL", "redis://localhost:6379/0")
    environment: str = os.getenv("ENVIRONMENT", "development")
    log_level: str = os.getenv("LOG_LEVEL", "INFO")
    weather_latitude: float = float(os.getenv("WEATHER_LATITUDE", "20.6736"))
    weather_longitude: float = float(os.getenv("WEATHER_LONGITUDE", "-103.3438"))
    weather_timeout_seconds: float = float(os.getenv("WEATHER_TIMEOUT_SECONDS", "5"))
    gps_position_ttl_seconds: int = int(os.getenv("GPS_POSITION_TTL_SECONDS", "30"))
    session_timeout_minutes: int = int(os.getenv("SESSION_TIMEOUT_MINUTES", "10"))
    use_mock_weather: bool = _env_bool("USE_MOCK_WEATHER", False)
    use_memory_cache: bool = field(default_factory=_default_use_memory_cache)
    demo_simulator_enabled: bool = field(default_factory=is_demo_simulator_enabled)
    demo_simulator_interval_seconds: float = float(
        os.getenv("DEMO_SIMULATOR_INTERVAL", "2.5")
    )
    twilio_auth_token: str = os.getenv("TWILIO_AUTH_TOKEN", "")
    require_twilio_signature: bool = _env_bool("REQUIRE_TWILIO_SIGNATURE", False)
    default_locale: str = os.getenv("DEFAULT_LOCALE", "es")
    # Base de transporte derivada del GTFS (se reconstruye sola si falta).
    transit_db_path: str = os.getenv("TRANSIT_DB_PATH", DEFAULT_TRANSIT_DB_PATH)
    landmarks_path: str = os.getenv("LANDMARKS_PATH", DEFAULT_LANDMARKS_PATH)
    # Región cuyas rutas simula la flota de demostración ("" = toda la red).
    demo_region: str = os.getenv("DEMO_REGION", "tonala_cucei")

    def ensure_paths(self) -> None:
        Path(self.gtfs_path).parent.mkdir(parents=True, exist_ok=True)
        Path(self.incidents_db_path).parent.mkdir(parents=True, exist_ok=True)
        Path(self.feedback_db_path).parent.mkdir(parents=True, exist_ok=True)


CONFIG = TransitEngineConfig()
