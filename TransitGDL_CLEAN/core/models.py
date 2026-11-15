from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from math import asin, cos, radians, sin, sqrt
from typing import Any, Optional
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator


class FlexibleModel(BaseModel):
    model_config = ConfigDict(extra="ignore")

    def __init__(self, *args, **kwargs):
        if args:
            field_names = list(self.__class__.model_fields.keys())
            for index, value in enumerate(args):
                if index < len(field_names) and field_names[index] not in kwargs:
                    kwargs[field_names[index]] = value
        super().__init__(**kwargs)


class WeatherCondition(str, Enum):
    CLEAR = "clear"
    CLOUDY = "cloudy"
    RAIN = "rain"
    THUNDERSTORM = "thunderstorm"
    FOG = "fog"
    SNOW = "snow"
    UNKNOWN = "unknown"


class FleetAction(str, Enum):
    KEEP = "keep"
    ADD_VEHICLES = "add_vehicles"
    REDUCE_VEHICLES = "reduce_vehicles"
    ALERT = "alert"
    MONITOR = "monitor"


class IncidentSeverity(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class Route(FlexibleModel):
    model_config = ConfigDict(extra="ignore")

    route_id: str
    route_short_name: str
    route_long_name: str = ""
    route_type: int = 3
    agency_id: Optional[str] = None
    # Datos derivados al construir la base de transporte.
    mode: str = "bus"  # tren | brt | bus | trolebus
    group: str = "bus"  # tren | macro | sitren | troncal | complementaria | ...
    legacy_number: Optional[str] = None  # número antiguo de la ruta (p. ej. 646)
    branch_name: str = ""  # nombre del ramal (p. ej. "Rio Nilo - Guadalupe")
    color: Optional[str] = None  # color para mostrar, #RRGGBB
    has_frequency: bool = True

    @property
    def display_name(self) -> str:
        return self.route_short_name or self.route_id

    @property
    def label(self) -> str:
        """Clave oficial con el número antiguo, p. ej. "T15 (antes 231)"."""
        if self.legacy_number and self.legacy_number != self.route_short_name:
            return f"{self.display_name} (antes {self.legacy_number})"
        return self.display_name


class Stop(FlexibleModel):
    model_config = ConfigDict(extra="ignore")

    stop_id: str
    stop_name: str
    lat: float
    lon: float

    def distance_km_to(self, lat: float, lon: float) -> float:
        return haversine_km(self.lat, self.lon, lat, lon)


class StopMatch(FlexibleModel):
    model_config = ConfigDict(extra="ignore")

    stop_id: str
    stop_name: str
    lat: float
    lon: float
    route_ids: list[str] = Field(default_factory=list)
    route_numbers: list[str] = Field(default_factory=list)


class BusPosition(FlexibleModel):
    model_config = ConfigDict(extra="ignore")

    vehicle_id: str
    route_id: str
    trip_id: Optional[str] = None
    lat: float
    lon: float
    speed_kmh: float = 0.0
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    # Datos opcionales que enriquecen la posición (los publica el simulador;
    # un GPS real puede omitirlos y el motor los deduce).
    pattern_id: Optional[str] = None  # patrón (ruta y sentido) que recorre
    bearing: Optional[float] = None  # rumbo en grados, 0 = norte
    progress_m: Optional[float] = None  # metros recorridos sobre el trazo

    @field_validator("timestamp", mode="before")
    @classmethod
    def _ensure_timestamp(cls, value: Any) -> datetime:
        if value is None:
            return datetime.now(timezone.utc)
        if isinstance(value, datetime):
            return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        if isinstance(value, (int, float)):
            return datetime.fromtimestamp(value, tz=timezone.utc)
        if isinstance(value, str):
            try:
                dt_obj = datetime.fromisoformat(value)
            except ValueError:
                return datetime.now(timezone.utc)
            return dt_obj if dt_obj.tzinfo else dt_obj.replace(tzinfo=timezone.utc)
        return datetime.now(timezone.utc)

    @property
    def age_seconds(self) -> float:
        now = datetime.now(timezone.utc)
        ts = (
            self.timestamp
            if self.timestamp.tzinfo
            else self.timestamp.replace(tzinfo=timezone.utc)
        )
        return max(0.0, (now - ts).total_seconds())

    @property
    def is_stale(self) -> bool:
        return self.age_seconds > 120.0

    def distance_km_to(self, lat: float, lon: float) -> float:
        return haversine_km(self.lat, self.lon, lat, lon)


class ScheduledDeparture(FlexibleModel):
    model_config = ConfigDict(extra="ignore")

    departure_time: str
    minutes_until: int
    service_day_offset: int = 0

    def display_time(self) -> str:
        clock = self.departure_time[:5]
        if self.service_day_offset > 0:
            return f"mañana {clock}"
        return clock


class ETAResult(FlexibleModel):
    model_config = ConfigDict(extra="ignore")

    route_number: str
    stop_id: str
    stop_name: str
    estimated_minutes: float
    confidence: float = 0.85
    source: str = "schedule"
    delay_minutes: float = 0.0
    vehicle_id: Optional[str] = None
    vehicle_lat: Optional[float] = None
    vehicle_lon: Optional[float] = None
    route_id: Optional[str] = None
    service_day_offset: int = 0
    basis: str = "horario_oficial"
    reliability_label: str = "media"
    average_headway_minutes: Optional[float] = None
    window_start_minutes: Optional[float] = None
    window_end_minutes: Optional[float] = None
    next_departures: list[ScheduledDeparture] = Field(default_factory=list)
    alerts: list[str] = Field(default_factory=list)
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    # ID único para correlacionar predicción con feedback posterior
    prediction_id: str = Field(default_factory=lambda: uuid4().hex)
    # Corrección aprendida aplicada (en minutos); 0 si no hay datos históricos aún
    learned_correction_minutes: float = 0.0
    # Condición climática en el momento de la predicción (para PredictionRecord)
    weather_condition: str = "unknown"
    # Sentido del viaje: destino que anuncia la unidad y patrón de la base.
    headsign: Optional[str] = None
    direction_id: Optional[int] = None
    pattern_id: Optional[str] = None
    # Clave con el número antiguo, p. ej. "T15 (antes 231)".
    route_label: Optional[str] = None
    # Si la parada se eligió por un lugar ("CUCEI"): nombre y distancia a pie.
    place_name: Optional[str] = None
    walk_meters: Optional[float] = None

    def to_whatsapp_message(self) -> str:
        minutes = max(0, round(self.estimated_minutes))
        confidence_pct = round(max(0.0, min(1.0, self.confidence)) * 100)
        source_label = "tiempo real" if self.source == "live" else "horario oficial"
        eta_label = f"Llegada estimada: *{self._format_eta(minutes)}*"
        if self.service_day_offset > 0:
            eta_label = f"Próxima salida: *mañana en {self._format_eta(minutes)}*"

        header = f"*Ruta {self.route_label or self.route_number}*"
        if self.headsign:
            header += f" hacia *{self.headsign}*"
        header += f" en *{self.stop_name}*"
        if self.place_name and self.walk_meters is not None:
            header += f" (a {round(self.walk_meters / 10) * 10} m de {self.place_name})"

        lines = [
            header,
            eta_label,
            f"Base: {source_label} | Confianza: {self.reliability_label} ({confidence_pct}%)",
        ]

        if self.average_headway_minutes:
            lines.append(
                f"Frecuencia programada: cada ~{round(self.average_headway_minutes)} min"
            )

        if (
            self.window_start_minutes is not None
            and self.window_end_minutes is not None
        ):
            lines.append(
                "Ventana esperada: "
                f"*{self._format_eta(round(self.window_start_minutes))} a {self._format_eta(round(self.window_end_minutes))}*"
            )

        if self.delay_minutes:
            lines.append(
                f"Ajuste por clima/incidencias: +{round(self.delay_minutes)} min"
            )

        if abs(self.learned_correction_minutes) >= 1.0:
            sign = "+" if self.learned_correction_minutes > 0 else ""
            lines.append(
                f"Ajuste por historial: {sign}{round(self.learned_correction_minutes)} min 📊"
            )

        if self.next_departures:
            departures_label = ", ".join(
                dep.display_time() for dep in self.next_departures[:3]
            )
            lines.append(f"Siguientes salidas: {departures_label}")

        if self.alerts:
            lines.append("Avisos: " + "; ".join(self.alerts[:2]))

        return "\n".join(lines)

    @staticmethod
    def _format_eta(minutes: int) -> str:
        if minutes < 60:
            return f"{minutes} min"
        hours, remaining = divmod(minutes, 60)
        if remaining == 0:
            return f"{hours} h"
        return f"{hours} h {remaining} min"


class DemandSnapshot(FlexibleModel):
    model_config = ConfigDict(extra="ignore")

    route_id: str
    current_vehicles: int
    weather_condition: WeatherCondition = WeatherCondition.UNKNOWN
    demand_factor: float = 1.0
    recommended_vehicles: int = 0
    demand_level: str = "normal"
    peak_hour: bool = False
    holiday: bool = False
    rationale: str = ""


class FleetRecommendation(FlexibleModel):
    model_config = ConfigDict(extra="ignore")

    route_id: str
    current_vehicles: int
    recommended_vehicles: int
    action: FleetAction = FleetAction.KEEP
    demand_level: str = "normal"
    rationale: str = ""
    weather_condition: WeatherCondition = WeatherCondition.UNKNOWN
    demand_factor: float = 1.0

    def summary(self) -> str:
        if self.action == FleetAction.ADD_VEHICLES:
            return (
                f"Agregar {self.recommended_vehicles - self.current_vehicles} unidades"
            )
        if self.action == FleetAction.REDUCE_VEHICLES:
            return (
                f"Reducir {self.current_vehicles - self.recommended_vehicles} unidades"
            )
        if self.action == FleetAction.ALERT:
            return "Enviar alerta operativa"
        return "Mantener operacion actual"


class Incident(FlexibleModel):
    model_config = ConfigDict(extra="ignore")

    incident_id: str = Field(default_factory=lambda: uuid4().hex)
    description: str
    affected_routes: list[str] = Field(default_factory=list)
    severity: IncidentSeverity = IncidentSeverity.MEDIUM
    active: bool = True
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    resolved_at: Optional[datetime] = None
    stop_ids: list[str] = Field(default_factory=list)

    @field_validator("created_at", "resolved_at", mode="before")
    @classmethod
    def _ensure_datetime(cls, value: Any) -> datetime | None:
        if value is None:
            return None
        if isinstance(value, datetime):
            return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        if isinstance(value, str):
            try:
                dt_obj = datetime.fromisoformat(value)
            except ValueError:
                return None
            return dt_obj if dt_obj.tzinfo else dt_obj.replace(tzinfo=timezone.utc)
        return None

    def summary(self) -> str:
        scope = []
        if self.affected_routes:
            scope.append("rutas " + ", ".join(self.affected_routes[:3]))
        if self.stop_ids:
            scope.append("paradas afectadas")
        if scope:
            return f"{self.description} ({'; '.join(scope)})"
        return self.description


class AlternateRoute(FlexibleModel):
    model_config = ConfigDict(extra="ignore")

    original_route_id: str
    alternate_description: str
    extra_time_minutes: float = 0.0
    affected_stops: list[str] = Field(default_factory=list)
    reason: str = ""


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Distancia geodesica aproximada entre dos puntos en kilometros."""
    radius_km = 6371.0
    dlat = radians(lat2 - lat1)
    dlon = radians(lon2 - lon1)
    a = (
        sin(dlat / 2) ** 2
        + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlon / 2) ** 2
    )
    return 2 * radius_km * asin(sqrt(a))
