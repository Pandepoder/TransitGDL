from __future__ import annotations

import logging
import time

import httpx

from .config import CONFIG
from .models import WeatherCondition

logger = logging.getLogger(__name__)


class WeatherClient:
    """
    Cliente simple para Open-Meteo. Si la red falla, cae a CLEAR.
    El resultado se cachea 5 minutos para evitar una petición HTTP por
    cada consulta de ETA y no agotar el rate-limit gratuito de Open-Meteo.
    """

    _CACHE_TTL_SECONDS: float = 300.0  # 5 minutos

    def __init__(
        self,
        latitude: float | None = None,
        longitude: float | None = None,
        timeout_seconds: float | None = None,
    ) -> None:
        self.latitude = latitude if latitude is not None else CONFIG.weather_latitude
        self.longitude = (
            longitude if longitude is not None else CONFIG.weather_longitude
        )
        self.timeout_seconds = (
            timeout_seconds
            if timeout_seconds is not None
            else CONFIG.weather_timeout_seconds
        )
        # Caché en memoria: no se comparte entre workers, pero es suficiente
        # para el volumen de una instancia única (el caso de producción previsto).
        self._cache_value: WeatherCondition | None = None
        self._cache_ts: float = 0.0
        self._demo_override: WeatherCondition | None = None

    def set_demo_override(self, condition: WeatherCondition | str | None) -> None:
        """Presenter override (development demos). Pass None to clear."""
        if condition is None:
            self._demo_override = None
            return
        self._demo_override = self._coerce_condition(condition)

    def clear_demo_override(self) -> None:
        self._demo_override = None

    async def get_current_condition(self) -> WeatherCondition:
        if self._demo_override is not None:
            return self._demo_override
        if CONFIG.use_mock_weather:
            return WeatherCondition.CLEAR

        # Devolver valor cacheado si todavía es reciente
        now = time.monotonic()
        if (
            self._cache_value is not None
            and (now - self._cache_ts) < self._CACHE_TTL_SECONDS
        ):
            return self._cache_value

        url = "https://api.open-meteo.com/v1/forecast"
        params = {
            "latitude": self.latitude,
            "longitude": self.longitude,
            "current_weather": "true",
            "timezone": "America/Mexico_City",
        }

        try:
            async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
                response = await client.get(url, params=params)
                response.raise_for_status()
                data = response.json()
                code = int(data.get("current_weather", {}).get("weathercode", -1))
                result = self._map_weather_code(code)
        except Exception as exc:  # pragma: no cover - live network failure path
            logger.warning("WeatherClient: fallback a CLEAR por error: %s", exc)
            # En caso de error conservamos el último valor conocido si existe,
            # o caemos a CLEAR. No actualizamos el timestamp para reintentar
            # en la próxima llamada en lugar de esperar 5 minutos.
            return (
                self._cache_value
                if self._cache_value is not None
                else WeatherCondition.CLEAR
            )

        self._cache_value = result
        self._cache_ts = now
        return result

    def get_delay_minutes(
        self, condition: WeatherCondition | str | None = None
    ) -> float:
        condition = self._coerce_condition(condition)
        delays = {
            WeatherCondition.CLEAR: 0.0,
            WeatherCondition.CLOUDY: 0.5,
            WeatherCondition.FOG: 2.0,
            WeatherCondition.RAIN: 4.0,
            WeatherCondition.THUNDERSTORM: 8.0,
            WeatherCondition.SNOW: 10.0,
            WeatherCondition.UNKNOWN: 0.0,
        }
        return delays.get(condition, 0.0)

    def get_speed_factor(
        self, condition: WeatherCondition | str | None = None
    ) -> float:
        condition = self._coerce_condition(condition)
        factors = {
            WeatherCondition.CLEAR: 1.0,
            WeatherCondition.CLOUDY: 0.98,
            WeatherCondition.FOG: 0.9,
            WeatherCondition.RAIN: 0.78,
            WeatherCondition.THUNDERSTORM: 0.65,
            WeatherCondition.SNOW: 0.6,
            WeatherCondition.UNKNOWN: 1.0,
        }
        return factors.get(condition, 1.0)

    def get_demand_factor(
        self, condition: WeatherCondition | str | None = None
    ) -> float:
        condition = self._coerce_condition(condition)
        factors = {
            WeatherCondition.CLEAR: 1.0,
            WeatherCondition.CLOUDY: 1.02,
            WeatherCondition.FOG: 1.05,
            WeatherCondition.RAIN: 1.25,
            WeatherCondition.THUNDERSTORM: 1.45,
            WeatherCondition.SNOW: 1.35,
            WeatherCondition.UNKNOWN: 1.0,
        }
        return factors.get(condition, 1.0)

    def is_rain_alert(self, condition: WeatherCondition | str | None = None) -> bool:
        condition = self._coerce_condition(condition)
        return condition in {
            WeatherCondition.RAIN,
            WeatherCondition.THUNDERSTORM,
            WeatherCondition.SNOW,
        }

    @staticmethod
    def _coerce_condition(condition: WeatherCondition | str | None) -> WeatherCondition:
        if condition is None:
            return WeatherCondition.UNKNOWN
        if isinstance(condition, WeatherCondition):
            return condition
        try:
            return WeatherCondition(condition)
        except Exception:
            name = str(condition).strip().lower()
            mapping = {
                "clear": WeatherCondition.CLEAR,
                "sunny": WeatherCondition.CLEAR,
                "cloudy": WeatherCondition.CLOUDY,
                "rain": WeatherCondition.RAIN,
                "lluvia": WeatherCondition.RAIN,
                "thunderstorm": WeatherCondition.THUNDERSTORM,
                "storm": WeatherCondition.THUNDERSTORM,
                "fog": WeatherCondition.FOG,
                "niebla": WeatherCondition.FOG,
                "snow": WeatherCondition.SNOW,
            }
            return mapping.get(name, WeatherCondition.UNKNOWN)

    @staticmethod
    def _map_weather_code(code: int) -> WeatherCondition:
        if code == 0:
            return WeatherCondition.CLEAR
        if code in {1, 2, 3}:
            return WeatherCondition.CLOUDY
        if code in {45, 48}:
            return WeatherCondition.FOG
        if code in {51, 53, 55, 61, 63, 65, 80, 81, 82}:
            return WeatherCondition.RAIN
        if code in {56, 57, 66, 67, 71, 73, 75, 77}:
            return WeatherCondition.SNOW
        if code in {95, 96, 99}:
            return WeatherCondition.THUNDERSTORM
        return WeatherCondition.UNKNOWN
