from __future__ import annotations

import logging
import time
from dataclasses import dataclass

import httpx

from .config import CONFIG
from .models import WeatherCondition
from .params import CLIMA

logger = logging.getLogger(__name__)

# Intensidad nominal (mm/h) cuando el clima se simula y no hay medición.
_NOMINAL_INTENSITY = {
    WeatherCondition.RAIN: 5.0,
    WeatherCondition.THUNDERSTORM: 12.0,
}


@dataclass(frozen=True, slots=True)
class WeatherState:
    """Clima actual: condición y, si llueve, intensidad en mm por hora."""

    condition: WeatherCondition = WeatherCondition.CLEAR
    precipitation_mm_h: float = 0.0

    @property
    def key(self) -> str:
        """Clave de `params.yaml` que corresponde a este clima."""
        if self.condition != WeatherCondition.RAIN:
            return self.condition.value
        if self.precipitation_mm_h >= CLIMA["umbral_lluvia_fuerte_mm_h"]:
            return "rain_fuerte"
        if self.precipitation_mm_h >= CLIMA["umbral_lluvia_moderada_mm_h"]:
            return "rain_moderada"
        return "rain_ligera"

    @property
    def label(self) -> str:
        return {
            "clear": "despejado",
            "cloudy": "nublado",
            "fog": "niebla",
            "rain_ligera": "lluvia ligera",
            "rain_moderada": "lluvia moderada",
            "rain_fuerte": "lluvia fuerte",
            "thunderstorm": "tormenta",
            "snow": "nieve",
        }.get(self.key, "sin datos")


class WeatherClient:
    """
    Cliente simple para Open-Meteo. Si la red falla, cae a despejado.
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
        self._cache_value: WeatherState | None = None
        self._cache_ts: float = 0.0
        self._demo_override: WeatherState | None = None

    # ── Clima simulado para demostraciones ───────────────────────────────────

    def set_demo_override(
        self,
        condition: WeatherCondition | str | None,
        precipitation_mm_h: float | None = None,
    ) -> None:
        """Presenter override (development demos). Pass None to clear."""
        if condition is None:
            self._demo_override = None
            return
        parsed = self._coerce_condition(condition)
        intensity = (
            precipitation_mm_h
            if precipitation_mm_h is not None
            else _NOMINAL_INTENSITY.get(parsed, 0.0)
        )
        self._demo_override = WeatherState(parsed, float(intensity))

    def clear_demo_override(self) -> None:
        self._demo_override = None

    def state_for(self, condition: WeatherCondition | str) -> WeatherState:
        """Clima de un escenario ("¿y si llueve?"), con la intensidad típica."""
        parsed = self._coerce_condition(condition)
        return WeatherState(parsed, float(_NOMINAL_INTENSITY.get(parsed, 0.0)))

    @property
    def is_simulated(self) -> bool:
        return self._demo_override is not None

    # ── Lectura ──────────────────────────────────────────────────────────────

    async def get_current_state(self) -> WeatherState:
        """Condición e intensidad de lluvia actuales (con caché de 5 minutos)."""
        if self._demo_override is not None:
            return self._demo_override
        if CONFIG.use_mock_weather:
            return WeatherState()

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
            "current": "weather_code,precipitation",
            "timezone": "America/Mexico_City",
        }

        try:
            async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
                response = await client.get(url, params=params)
                response.raise_for_status()
                current = response.json().get("current", {})
                condition = self._map_weather_code(int(current.get("weather_code", -1)))
                # La precipitación viene acumulada en el intervalo (segundos).
                interval = float(current.get("interval") or 3600.0)
                amount = float(current.get("precipitation") or 0.0)
                state = WeatherState(condition, amount * 3600.0 / max(interval, 60.0))
        except Exception as exc:  # pragma: no cover - live network failure path
            logger.warning("WeatherClient: sin datos de clima (%s).", exc)
            # Se conserva el último valor conocido y no se actualiza la marca
            # de tiempo, para reintentar en la siguiente llamada.
            return (
                self._cache_value if self._cache_value is not None else WeatherState()
            )

        self._cache_value = state
        self._cache_ts = now
        return state

    def cached_state(self) -> WeatherState:
        """Último clima conocido sin consultar la red (para el simulador)."""
        if self._demo_override is not None:
            return self._demo_override
        return self._cache_value or WeatherState()

    async def get_current_condition(self) -> WeatherCondition:
        return (await self.get_current_state()).condition

    # ── Efecto del clima ─────────────────────────────────────────────────────

    def travel_factor(
        self, state: WeatherState | WeatherCondition | str | None, mode: str = "bus"
    ) -> float:
        """
        Factor multiplicativo sobre el tiempo de recorrido. Los modos con vía
        propia (tren, Macrobús) reciben solo una parte del efecto.
        """
        if not isinstance(state, WeatherState):
            condition = self._coerce_condition(state)
            state = WeatherState(condition, _NOMINAL_INTENSITY.get(condition, 0.0))
        base = float(CLIMA["factor_recorrido"].get(state.key, 1.0))
        sensitivity = float(CLIMA["sensibilidad"].get(mode, 1.0))
        return 1.0 + (base - 1.0) * sensitivity

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
        if code in {56, 57, 66, 67, 71, 73, 75, 77, 85, 86}:
            return WeatherCondition.SNOW
        if code in {95, 96, 97, 99}:
            return WeatherCondition.THUNDERSTORM
        return WeatherCondition.UNKNOWN
