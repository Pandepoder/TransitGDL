from __future__ import annotations

import csv
import logging
import unicodedata
import zipfile
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Optional

from .models import Route, Stop, haversine_km
from .timezone_utils import GDL_TZ

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class _Departure:
    trip_id: str
    route_id: str
    service_id: str
    stop_id: str
    departure_minutes: int  # minutos enteros desde medianoche (compatibilidad)
    departure_seconds: float  # segundos exactos desde medianoche (para frecuencias)
    departure_time: str
    stop_sequence: int


@dataclass(slots=True)
class _FreqWindow:
    """Ventana horaria de un servicio de frecuencia (frequencies.txt)."""

    start_secs: float  # inicio de la ventana en segundos desde medianoche
    end_secs: float  # fin de la ventana en segundos desde medianoche
    headway_secs: float  # frecuencia en segundos (headway_secs del GTFS)


class GTFSLoader:
    """
    Cargador compacto de GTFS.
    Se enfoca en consultas rapidas por ruta/parada y proximas salidas.
    """

    def __init__(self, gtfs_path: str):
        self.gtfs_path = str(gtfs_path)
        self._loaded = False
        self._routes: dict[str, Route] = {}
        self._routes_by_short: dict[str, Route] = {}
        self._stops: dict[str, Stop] = {}
        self._route_stops: dict[str, list[str]] = {}
        self._stop_routes: dict[str, set[str]] = {}
        self._departures_by_route_stop: dict[tuple[str, str], list[_Departure]] = {}
        self._trips_by_route: dict[str, set[str]] = {}
        self._service_calendars: dict[str, dict[str, str]] = {}
        # Excepciones de calendar_dates.txt:
        # {date_str: {service_id: exception_type}}
        # exception_type=1 → servicio añadido ese día
        # exception_type=2 → servicio eliminado ese día
        self._service_date_exceptions: dict[str, dict[str, int]] = {}
        # Frecuencias (frequencies.txt):
        # trip_id → lista de ventanas horarias con su headway
        self._trip_frequencies: dict[str, list[_FreqWindow]] = {}
        # trip_id → tiempo base del viaje en segundos (primera parada del template)
        self._trip_base_secs: dict[str, float] = {}
        # Conjunto de trip_ids que usan frequencies.txt en lugar de stop_times explícitos
        self._frequency_trip_ids: set[str] = set()
        # Índice invertido para búsqueda rápida de paradas O(tokens) en lugar de O(n)
        # token (>=3 chars) → [stop_id, ...]
        self._stop_token_index: dict[str, list[str]] = {}
        # shapes.txt: shape_id → [(lat, lon), ...] ordenado por shape_pt_sequence
        self._shape_points: dict[str, list[tuple[float, float]]] = {}
        # route_id → shape_id principal (el más usado en trips.txt)
        self._route_shape_id: dict[str, str] = {}
        # Última fecha de vigencia declarada por el feed (máximo end_date de
        # calendar.txt). Pasada esa fecha, sus servicios se tratan como patrón
        # semanal recurrente para que un feed vencido siga dando horarios.
        self._feed_last_date: date | None = None

    @property
    def loaded(self) -> bool:
        return self._loaded

    def load(self) -> "GTFSLoader":
        path = Path(self.gtfs_path)
        if not path.exists():
            raise FileNotFoundError(f"No existe GTFS: {self.gtfs_path}")

        logger.info("Cargando GTFS desde %s ...", self.gtfs_path)
        with zipfile.ZipFile(path) as archive:
            routes_rows = self._read_csv_from_zip(archive, "routes.txt")
            stops_rows = self._read_csv_from_zip(archive, "stops.txt")
            calendar_rows = self._read_csv_from_zip(archive, "calendar.txt")
            calendar_dates_rows = self._read_csv_from_zip(archive, "calendar_dates.txt")
            trips_rows = self._read_csv_from_zip(archive, "trips.txt")
            stop_times_rows = self._read_csv_from_zip(archive, "stop_times.txt")
            frequencies_rows = self._read_csv_from_zip(archive, "frequencies.txt")
            shapes_rows = self._read_csv_from_zip(archive, "shapes.txt")

        self._routes.clear()
        self._routes_by_short.clear()
        self._stops.clear()
        self._route_stops.clear()
        self._stop_routes.clear()
        self._departures_by_route_stop.clear()
        self._trips_by_route.clear()
        self._service_calendars.clear()
        self._service_date_exceptions.clear()
        self._trip_frequencies.clear()
        self._trip_base_secs.clear()
        self._frequency_trip_ids.clear()
        self._stop_token_index.clear()
        self._shape_points.clear()
        self._route_shape_id.clear()

        for row in routes_rows:
            try:
                rt_raw = row.get("route_type") or "3"
                route_type = int(rt_raw) if rt_raw.strip().isdigit() else 3
            except (ValueError, TypeError):
                route_type = 3
            route = Route(
                route_id=row.get("route_id", "").strip(),
                route_short_name=row.get("route_short_name", "").strip()
                or row.get("route_id", "").strip(),
                route_long_name=row.get("route_long_name", "").strip(),
                route_type=route_type,
                agency_id=row.get("agency_id") or None,
            )
            self._routes[route.route_id] = route
            self._routes_by_short[self._normalize(route.route_short_name)] = route
            self._routes_by_short[self._normalize(route.route_id)] = route

        for row in stops_rows:
            try:
                stop = Stop(
                    stop_id=row.get("stop_id", "").strip(),
                    stop_name=row.get("stop_name", "").strip(),
                    lat=float(row.get("stop_lat") or 0.0),
                    lon=float(row.get("stop_lon") or 0.0),
                )
            except ValueError:
                continue
            self._stops[stop.stop_id] = stop
            # Índice invertido: cada token del nombre apunta a esta parada
            for token in self._normalize(stop.stop_name).split():
                if len(token) >= 3:
                    self._stop_token_index.setdefault(token, []).append(stop.stop_id)

        trip_to_route: dict[str, str] = {}
        trip_to_service: dict[str, str] = {}
        route_shape_votes: dict[str, dict[str, int]] = {}

        shape_buckets: dict[str, list[tuple[int, float, float]]] = {}
        for row in shapes_rows:
            shape_id = (row.get("shape_id") or "").strip()
            if not shape_id:
                continue
            try:
                seq = int(row.get("shape_pt_sequence") or 0)
                lat = float(row.get("shape_pt_lat") or 0.0)
                lon = float(row.get("shape_pt_lon") or 0.0)
            except (TypeError, ValueError):
                continue
            shape_buckets.setdefault(shape_id, []).append((seq, lat, lon))

        for shape_id, points in shape_buckets.items():
            points.sort(key=lambda item: item[0])
            self._shape_points[shape_id] = [(lat, lon) for _, lat, lon in points]

        self._feed_last_date = None
        for row in calendar_rows:
            service_id = row.get("service_id", "").strip()
            if service_id:
                self._service_calendars[service_id] = dict(row)
                end = (row.get("end_date") or "").strip()
                try:
                    end_date = self._parse_service_date(end) if end else None
                except ValueError:
                    end_date = None
                if end_date and (
                    self._feed_last_date is None or end_date > self._feed_last_date
                ):
                    self._feed_last_date = end_date

        for row in calendar_dates_rows:
            service_id = row.get("service_id", "").strip()
            date_str = row.get("date", "").strip()
            try:
                exc_type = int(row.get("exception_type", "0"))
            except ValueError:
                continue
            if service_id and date_str and exc_type in {1, 2}:
                self._service_date_exceptions.setdefault(date_str, {})[
                    service_id
                ] = exc_type

        for row in trips_rows:
            trip_id = row.get("trip_id", "").strip()
            route_id = row.get("route_id", "").strip()
            service_id = row.get("service_id", "").strip()
            shape_id = (row.get("shape_id") or "").strip()
            if trip_id and route_id:
                trip_to_route[trip_id] = route_id
                trip_to_service[trip_id] = service_id
                self._trips_by_route.setdefault(route_id, set()).add(trip_id)
                if shape_id:
                    route_shape_votes.setdefault(route_id, {})
                    route_shape_votes[route_id][shape_id] = (
                        route_shape_votes[route_id].get(shape_id, 0) + 1
                    )

        for route_id, votes in route_shape_votes.items():
            self._route_shape_id[route_id] = max(votes, key=votes.get)

        for row in stop_times_rows:
            trip_id = row.get("trip_id", "").strip()
            stop_id = row.get("stop_id", "").strip()
            if not trip_id or not stop_id:
                continue
            route_id = trip_to_route.get(trip_id)
            if not route_id:
                continue

            departure_time = (
                row.get("departure_time") or row.get("arrival_time") or "00:00:00"
            ).strip()
            departure = _Departure(
                trip_id=trip_id,
                route_id=route_id,
                service_id=trip_to_service.get(trip_id, ""),
                stop_id=stop_id,
                departure_minutes=self._time_to_minutes(departure_time),
                departure_seconds=self._time_to_seconds(departure_time),
                departure_time=departure_time,
                stop_sequence=int(row.get("stop_sequence") or 0),
            )
            self._departures_by_route_stop.setdefault((route_id, stop_id), []).append(
                departure
            )
            self._route_stops.setdefault(route_id, [])
            if stop_id not in self._route_stops[route_id]:
                self._route_stops[route_id].append(stop_id)
            self._stop_routes.setdefault(stop_id, set()).add(route_id)

        for items in self._departures_by_route_stop.values():
            items.sort(key=lambda dep: (dep.departure_minutes, dep.stop_sequence))

        # ── Procesar frequencies.txt ──────────────────────────────────────────
        # Construir ventanas de frecuencia por trip_id
        for row in frequencies_rows:
            trip_id = row.get("trip_id", "").strip()
            start_time = row.get("start_time", "").strip()
            end_time = row.get("end_time", "").strip()
            headway_str = row.get("headway_secs", "").strip()
            if not trip_id or not start_time or not end_time or not headway_str:
                continue
            try:
                headway_secs = float(headway_str)
            except ValueError:
                continue
            if headway_secs <= 0:
                continue
            self._trip_frequencies.setdefault(trip_id, []).append(
                _FreqWindow(
                    start_secs=self._time_to_seconds(start_time),
                    end_secs=self._time_to_seconds(end_time),
                    headway_secs=headway_secs,
                )
            )

        # Para cada trip con frecuencias, determinar el tiempo base (primera parada)
        # y registrar el trip_id en el conjunto de trips de frecuencia.
        # El tiempo base es el mínimo departure_seconds de todos los templates del trip.
        trip_min_secs: dict[str, float] = {}
        for (_, _stop_id), dep_list in self._departures_by_route_stop.items():
            for dep in dep_list:
                if dep.trip_id in self._trip_frequencies:
                    current = trip_min_secs.get(dep.trip_id)
                    if current is None or dep.departure_seconds < current:
                        trip_min_secs[dep.trip_id] = dep.departure_seconds

        self._trip_base_secs.update(trip_min_secs)
        self._frequency_trip_ids.update(self._trip_frequencies.keys())

        self._loaded = True
        logger.info(
            "GTFS cargado: %s rutas, %s paradas, %s viajes (%s con frecuencia)",
            len(self._routes),
            len(self._stops),
            sum(len(v) for v in self._trips_by_route.values()),
            len(self._frequency_trip_ids),
        )
        return self

    def get_routes(self) -> list[Route]:
        self._assert_loaded()
        return sorted(
            self._routes.values(),
            key=lambda route: (route.route_short_name, route.route_id),
        )

    def get_stops(self) -> list[Stop]:
        self._assert_loaded()
        return sorted(self._stops.values(), key=lambda stop: stop.stop_name)

    def get_stop(self, stop_id: str) -> Optional[Stop]:
        self._assert_loaded()
        return self._stops.get(stop_id)

    def find_route(self, query: str) -> Optional[Route]:
        self._assert_loaded()
        normalized = self._normalize(query)
        if not normalized:
            return None

        if query in self._routes:
            return self._routes[query]

        if normalized in self._routes_by_short:
            return self._routes_by_short[normalized]

        for route in self._routes.values():
            short_name = self._normalize(route.route_short_name)
            long_name = self._normalize(route.route_long_name)
            route_id = self._normalize(route.route_id)
            if normalized in {short_name, long_name, route_id}:
                return route

        # Números de línea (55, 646): solo coincidencia exacta de short_name o RG_<n>.
        if normalized.isdigit():
            exact = self._routes_by_short.get(normalized)
            if exact is not None:
                return exact
            prefixed = f"RG_{normalized}"
            if prefixed in self._routes:
                return self._routes[prefixed]
            return None

        # Sin coincidencia ambigua por subcadena (evita que "CUCEI" resuelva a ruta 55).
        return None

    def search_stops(self, query: str, limit: int = 10) -> list[Stop]:
        """
        Búsqueda de paradas por nombre.
        Usa índice invertido precalculado → O(tokens) en lugar de O(n).
        Score: 100=exacto, 80=contiene frase completa, 60=token parcial.
        """
        self._assert_loaded()
        normalized = self._normalize(query)
        if not normalized:
            return []

        # Candidatos por tokens del índice invertido
        token_hits: dict[str, int] = {}
        query_tokens = [t for t in normalized.split() if len(t) >= 3]
        for token in query_tokens:
            for stop_id in self._stop_token_index.get(token, []):
                token_hits[stop_id] = token_hits.get(stop_id, 0) + 1

        # Si la query completa (sin espacios) también está en el índice, incluirla
        if normalized in self._stop_token_index:
            for stop_id in self._stop_token_index[normalized]:
                token_hits[stop_id] = token_hits.get(stop_id, 0) + 10

        if not token_hits:
            return []

        scored: list[tuple[int, int, Stop]] = []
        for stop_id, hits in token_hits.items():
            stop = self._stops.get(stop_id)
            if stop is None:
                continue
            name_norm = self._normalize(stop.stop_name)
            if normalized == name_norm:
                score = 100
            elif normalized in name_norm:
                score = 80
            else:
                score = 40 + hits * 20
            scored.append((score, len(stop.stop_name), stop))

        scored.sort(key=lambda item: (-item[0], item[1], item[2].stop_name))
        return [item[2] for item in scored[:limit]]

    def get_routes_at_stop(self, stop_id: str) -> list[Route]:
        self._assert_loaded()
        if stop_id in self._stop_routes:
            return [
                self._routes[route_id]
                for route_id in sorted(self._stop_routes[stop_id])
                if route_id in self._routes
            ]

        matches = self.search_stops(stop_id, limit=1)
        if matches:
            return self.get_routes_at_stop(matches[0].stop_id)
        return []

    def get_ordered_stops_for_route(self, route_id: str) -> list[Stop]:
        """Paradas en orden de recorrido (stop_sequence del viaje más completo)."""
        self._assert_loaded()
        route = self.find_route(route_id)
        if route is None:
            return []

        trip_stops: dict[str, dict[int, str]] = {}
        for (rid, stop_id), departures in self._departures_by_route_stop.items():
            if rid != route.route_id:
                continue
            for dep in departures:
                bucket = trip_stops.setdefault(dep.trip_id, {})
                bucket[dep.stop_sequence] = stop_id

        best_ids: list[str] = []
        best_len = 0
        for stop_map in trip_stops.values():
            ordered = [stop_map[k] for k in sorted(stop_map) if k in stop_map]
            if len(ordered) > best_len:
                best_len = len(ordered)
                best_ids = ordered

        if best_ids:
            return [self._stops[sid] for sid in best_ids if sid in self._stops]

        stop_ids = self._route_stops.get(route.route_id, [])
        return [self._stops[sid] for sid in stop_ids if sid in self._stops]

    def get_route_path(self, route_id: str) -> list[tuple[float, float]]:
        """Trazado real (shapes.txt) o paradas ordenadas como polyline."""
        self._assert_loaded()
        route = self.find_route(route_id)
        if route is None:
            return []

        shape_id = self._route_shape_id.get(route.route_id)
        if shape_id and shape_id in self._shape_points:
            return list(self._shape_points[shape_id])

        return [
            (s.lat, s.lon) for s in self.get_ordered_stops_for_route(route.route_id)
        ]

    def get_stops_for_route(self, route_id: str) -> list[Stop]:
        return self.get_ordered_stops_for_route(route_id)

    def get_next_departures(
        self,
        route_id: str,
        stop_id: str,
        current_minutes: int | None = None,
        limit: int = 3,
    ) -> list[dict[str, Any]]:
        """
        Retorna proximas salidas a partir de current_minutes (minutos desde medianoche).
        Soporta tanto stop_times explícitos como trips de frecuencia (frequencies.txt).
        Puede envolver al dia siguiente si ya no quedan salidas hoy.
        """
        self._assert_loaded()
        route = self.find_route(route_id)
        if route is None:
            return []

        stop = self._resolve_stop(stop_id)
        if stop is None:
            return []

        departures = self._departures_by_route_stop.get(
            (route.route_id, stop.stop_id), []
        )
        if not departures:
            return []

        if current_minutes is None:
            now = datetime.now(GDL_TZ)
            current_minutes = now.hour * 60 + now.minute

        upcoming: list[dict[str, Any]] = []
        base_date = datetime.now(GDL_TZ).date()
        for day_offset in range(3):
            active_services = self._active_service_ids_for_date(
                base_date + timedelta(days=day_offset)
            )
            if self._service_calendars and not active_services:
                active_services = self._active_service_ids_for_date(
                    base_date + timedelta(days=day_offset),
                    ignore_date_range=True,
                )
            for departure in departures:
                if active_services and departure.service_id not in active_services:
                    continue

                if departure.trip_id in self._frequency_trip_ids:
                    # Viaje de frecuencia: expandir ventanas en próximas N salidas
                    freq_deps = self._next_freq_departures(
                        departure, current_minutes, day_offset, limit - len(upcoming)
                    )
                    upcoming.extend(freq_deps)
                else:
                    # Stop_times explícito: lógica original
                    absolute_minutes = (
                        departure.departure_minutes + day_offset * 24 * 60
                    )
                    delta = absolute_minutes - current_minutes
                    if delta < 0:
                        continue
                    upcoming.append(
                        {
                            "trip_id": departure.trip_id,
                            "route_id": departure.route_id,
                            "stop_id": departure.stop_id,
                            "departure_time": departure.departure_time,
                            "departure_minutes": departure.departure_minutes,
                            "minutes_until": delta,
                            "service_day_offset": day_offset,
                            "absolute_minutes": float(absolute_minutes),
                        }
                    )

                if len(upcoming) >= limit:
                    upcoming.sort(key=lambda x: x["absolute_minutes"])
                    return upcoming[:limit]

        upcoming.sort(key=lambda x: x["absolute_minutes"])
        return upcoming[:limit]

    def _next_freq_departures(
        self,
        departure: _Departure,
        current_minutes: float,
        day_offset: int,
        limit: int,
    ) -> list[dict[str, Any]]:
        """
        Genera las próximas `limit` salidas para un viaje de frecuencia,
        calculando cuándo toca el siguiente bus en esta parada específica
        según las ventanas horarias del trip.
        """
        if limit <= 0:
            return []

        base_secs = self._trip_base_secs.get(departure.trip_id)
        windows = self._trip_frequencies.get(departure.trip_id, [])
        if base_secs is None or not windows:
            return []

        # Offset de esta parada respecto al inicio del viaje template (en segundos)
        stop_offset_secs = departure.departure_seconds - base_secs
        current_secs = current_minutes * 60.0
        day_offset_secs = day_offset * 24.0 * 3600.0
        results: list[dict[str, Any]] = []

        for window in windows:
            # Tiempo absoluto de esta parada en la primera salida de la ventana
            first_stop_secs = window.start_secs + stop_offset_secs + day_offset_secs
            # La ventana termina cuando el bus sale del primer stop por última vez + offset
            last_stop_secs = window.end_secs + stop_offset_secs + day_offset_secs

            # Encontrar la primera salida >= current_secs en esta ventana
            if first_stop_secs < current_secs:
                elapsed = current_secs - first_stop_secs
                n = int(elapsed / window.headway_secs) + 1
                next_secs = first_stop_secs + n * window.headway_secs
            else:
                next_secs = first_stop_secs

            while next_secs <= last_stop_secs and len(results) < limit:
                delta_secs = next_secs - current_secs
                if delta_secs < 0:
                    next_secs += window.headway_secs
                    continue

                # Formatear hora de salida como HH:MM:SS (puede pasar de 24h en GTFS)
                dep_in_day = int(next_secs) % (24 * 3600)
                dep_h = dep_in_day // 3600
                dep_m = (dep_in_day % 3600) // 60
                dep_s = dep_in_day % 60
                dep_time_str = f"{dep_h:02d}:{dep_m:02d}:{dep_s:02d}"

                results.append(
                    {
                        "trip_id": departure.trip_id,
                        "route_id": departure.route_id,
                        "stop_id": departure.stop_id,
                        "departure_time": dep_time_str,
                        "departure_minutes": dep_in_day // 60,
                        "minutes_until": delta_secs / 60.0,
                        "service_day_offset": day_offset,
                        "absolute_minutes": next_secs / 60.0,
                        "headway_minutes": window.headway_secs / 60.0,
                    }
                )
                next_secs += window.headway_secs

        results.sort(key=lambda x: x["absolute_minutes"])
        return results[:limit]

    def get_active_headway(
        self, route_id: str, current_minutes: int | None = None
    ) -> float | None:
        """
        Devuelve el headway activo (en minutos) para una ruta de frecuencia
        en el momento actual. Útil para mostrar "pasa cada X min" en la UI.
        Retorna None si la ruta no usa frequencies.txt o no hay ventana activa.
        """
        self._assert_loaded()
        route = self.find_route(route_id)
        if route is None:
            return None

        if current_minutes is None:
            now = datetime.now(GDL_TZ)
            current_minutes = now.hour * 60 + now.minute
        current_secs = current_minutes * 60.0

        trip_ids = self._trips_by_route.get(route.route_id, set())
        for trip_id in trip_ids:
            windows = self._trip_frequencies.get(trip_id)
            if not windows:
                continue
            for window in windows:
                if window.start_secs <= current_secs < window.end_secs:
                    return window.headway_secs / 60.0
        return None

    def _active_service_ids_for_date(
        self, service_date: date, ignore_date_range: bool = False
    ) -> set[str]:
        if not self._service_calendars:
            return set()

        weekday_column = [
            "monday",
            "tuesday",
            "wednesday",
            "thursday",
            "friday",
            "saturday",
            "sunday",
        ][service_date.weekday()]
        # Feed vencido: sus servicios valen como patrón semanal. La decisión se
        # toma con la vigencia del propio feed, no según haya otros servicios
        # activos (un servicio añadido después no debe apagar a los demás).
        feed_last = self._feed_last_date
        feed_expired = feed_last is not None and service_date > feed_last

        active: set[str] = set()
        for service_id, row in self._service_calendars.items():
            if row.get(weekday_column, "0") != "1":
                continue
            if not ignore_date_range:
                start = row.get("start_date", "")
                end = row.get("end_date", "")
                end_date = self._parse_service_date(end) if end else None
                from_expired_feed = feed_expired and (
                    end_date is None or end_date <= feed_last
                )
                if not from_expired_feed:
                    if start and service_date < self._parse_service_date(start):
                        continue
                    if end_date and service_date > end_date:
                        continue
            active.add(service_id)

        # Aplicar excepciones de calendar_dates.txt:
        # exception_type=1 → añadir servicio aunque el calendar.txt no lo marque
        # exception_type=2 → eliminar servicio aunque el calendar.txt sí lo marque
        date_key = service_date.strftime("%Y%m%d")
        for service_id, exc_type in self._service_date_exceptions.get(
            date_key, {}
        ).items():
            if exc_type == 1:
                active.add(service_id)
            elif exc_type == 2:
                active.discard(service_id)

        return active

    def get_average_headway(
        self,
        route_id: str,
        stop_id: str,
        current_minutes: int | None = None,
        lookahead: int = 4,
    ) -> float | None:
        departures = self.get_next_departures(
            route_id, stop_id, current_minutes=current_minutes, limit=lookahead
        )
        if len(departures) < 2:
            return None

        gaps: list[float] = []
        for previous, current in zip(departures, departures[1:], strict=False):
            gap = float(current["absolute_minutes"]) - float(
                previous["absolute_minutes"]
            )
            if gap > 0:
                gaps.append(gap)
        if not gaps:
            return None
        return round(sum(gaps) / len(gaps), 1)

    def nearest_stop(
        self, lat: float, lon: float, route_id: str | None = None
    ) -> Optional[Stop]:
        self._assert_loaded()
        stops = self.get_stops_for_route(route_id) if route_id else self.get_stops()
        if not stops:
            return None
        return min(stops, key=lambda stop: haversine_km(stop.lat, stop.lon, lat, lon))

    def stops_near(
        self, lat: float, lon: float, radius_km: float = 0.5, limit: int = 10
    ) -> list[tuple[Stop, float]]:
        self._assert_loaded()
        matches: list[tuple[Stop, float]] = []
        for stop in self._stops.values():
            distance = haversine_km(lat, lon, stop.lat, stop.lon)
            if distance <= radius_km:
                matches.append((stop, distance))
        matches.sort(key=lambda item: item[1])
        return matches[:limit]

    def _resolve_stop(self, stop_id_or_query: str) -> Stop | None:
        stop = self._stops.get(stop_id_or_query)
        if stop is not None:
            return stop
        matches = self.search_stops(stop_id_or_query, limit=1)
        return matches[0] if matches else None

    def _assert_loaded(self) -> None:
        if not self._loaded:
            self.load()

    @staticmethod
    def _normalize(value: str) -> str:
        # NFD descompone cada carácter acentuado en base + diacrítico separado.
        # Al filtrar la categoría "Mn" (diacríticos), á→a, é→e, ñ→n, ü→u, etc.
        # sin necesidad de una tabla de reemplazos manual.
        nfd = unicodedata.normalize("NFD", value.strip().lower())
        stripped = "".join(c for c in nfd if unicodedata.category(c) != "Mn")
        return " ".join(stripped.split())

    @staticmethod
    def _time_to_minutes(value: str) -> int:
        try:
            hours, minutes, _seconds = value.split(":")
            return int(hours) * 60 + int(minutes)
        except Exception:
            return 0

    @staticmethod
    def _time_to_seconds(value: str) -> float:
        """Convierte HH:MM:SS a segundos. GTFS permite horas > 24 (ej. 25:30:00)."""
        try:
            parts = value.split(":")
            return int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
        except Exception:
            return 0.0

    @staticmethod
    def _read_csv_from_zip(
        archive: zipfile.ZipFile, filename: str
    ) -> list[dict[str, str]]:
        try:
            with archive.open(filename) as file_pointer:
                text = file_pointer.read().decode("utf-8-sig")
            reader = csv.DictReader(text.splitlines())
            return [dict(row) for row in reader]
        except KeyError:
            logger.warning("GTFS: no se encontro %s", filename)
            return []

    @staticmethod
    def _parse_service_date(value: str) -> date:
        return datetime.strptime(value, "%Y%m%d").date()
