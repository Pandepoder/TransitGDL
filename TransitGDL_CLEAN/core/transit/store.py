"""
Acceso en memoria a la base de transporte (`transit.db`).

`TransitStore` lee la base en bloque al arrancar y resuelve en memoria las
consultas frecuentes (rutas, patrones, próximas pasadas). Usa SQL directo solo
para la búsqueda de texto (FTS5) y la búsqueda por cercanía (R-tree).

Ofrece la misma interfaz que usaba el cargador GTFS anterior (`find_route`,
`get_stops_for_route`, `get_next_departures`, `search_stops`, `get_route_path`…)
y añade consultas por sentido, alias de rutas y puntos de interés.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from array import array
from bisect import bisect_right
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from math import ceil, cos, radians
from pathlib import Path
from typing import Any, Optional

from ..calendar_utils import es_festivo
from ..geometry import Polyline, decode_polyline
from ..gtfs_loader import GTFSLoader
from ..models import Route, Stop, haversine_km
from ..timezone_utils import GDL_TZ
from .builder import (
    CATALOG_ALIAS_RANK,
    ISSUE_APPROXIMATE_SHAPE,
    ISSUE_DOUBTFUL,
    ISSUE_ESTIMATED_TIMES,
    ISSUE_ONLY_TERMINALS,
    ISSUE_STOPS_OFF_SHAPE,
    BuildReport,
    ensure_transit_db,
)

logger = logging.getLogger(__name__)

# Avisos por defecto del feed oficial, en el orden en que se muestran.
PATTERN_NOTES = {
    ISSUE_ONLY_TERMINALS: (
        "El feed oficial solo trae las terminales de este recorrido; "
        "no se conocen sus paradas intermedias."
    ),
    ISSUE_ESTIMATED_TIMES: (
        "Los tiempos del feed para este recorrido son imposibles; "
        "se estiman con una velocidad típica."
    ),
    ISSUE_APPROXIMATE_SHAPE: (
        "El trazo de este recorrido es aproximado: une las paradas en línea recta."
    ),
    ISSUE_STOPS_OFF_SHAPE: "Algunas paradas quedan lejos del trazo oficial.",
    ISSUE_DOUBTFUL: (
        "El feed repite muchas paradas en este recorrido; " "puede estar mal capturado."
    ),
}

_MODE_PRIORITY = {"tren": 0, "brt": 1, "trolebus": 2, "bus": 3}
_TOKEN_RE = re.compile(r"[a-z0-9]+")
_KM_PER_DEGREE = 111.195


@dataclass(slots=True, frozen=True)
class ServiceWindow:
    """Ventana de servicio de un patrón. `headway_s == 0` es una salida única."""

    service_id: str
    start_s: int
    end_s: int
    headway_s: int
    exact: bool


@dataclass(slots=True)
class Pattern:
    """Un recorrido concreto: ruta, sentido y secuencia de paradas."""

    pattern_id: str
    route_id: str
    direction_id: int
    headsign: str
    length_m: float
    run_time_s: float
    is_main: bool
    shape_source: str
    stop_ids: tuple[str, ...]
    dist_m: tuple[float, ...]
    offset_s: tuple[float, ...]
    line: Polyline
    map_polyline: str
    windows: tuple[ServiceWindow, ...]
    # Defectos del feed detectados al construir la base (ver PATTERN_NOTES).
    issues: tuple[str, ...] = ()

    @property
    def notes(self) -> list[str]:
        """Avisos para el usuario sobre la calidad de los datos del recorrido."""
        return [PATTERN_NOTES[code] for code in self.issues if code in PATTERN_NOTES]

    def time_at(self, dist_m: float) -> float:
        """Tiempo programado (s desde la terminal) al pasar por `dist_m`."""
        dists, offsets = self.dist_m, self.offset_s
        if not dists:
            return 0.0
        if dist_m <= dists[0]:
            return offsets[0]
        if dist_m >= dists[-1]:
            return offsets[-1]
        i = bisect_right(dists, dist_m) - 1
        span = dists[i + 1] - dists[i]
        if span <= 0.0:
            return offsets[i]
        return offsets[i] + (offsets[i + 1] - offsets[i]) * (dist_m - dists[i]) / span

    def dist_at_time(self, seconds: float) -> float:
        """Inversa de `time_at`: distancia recorrida tras `seconds` de viaje."""
        dists, offsets = self.dist_m, self.offset_s
        if not dists:
            return 0.0
        if seconds <= offsets[0]:
            return dists[0]
        if seconds >= offsets[-1]:
            return dists[-1]
        i = bisect_right(offsets, seconds) - 1
        span = offsets[i + 1] - offsets[i]
        if span <= 0.0:
            return dists[i]
        return dists[i] + (dists[i + 1] - dists[i]) * (seconds - offsets[i]) / span

    def stop_indexes(self, stop_id: str) -> list[int]:
        return [i for i, sid in enumerate(self.stop_ids) if sid == stop_id]


@dataclass(slots=True, frozen=True)
class Landmark:
    landmark_id: str
    name: str
    kind: str
    lat: float
    lon: float
    aliases: tuple[str, ...]


@dataclass(slots=True, frozen=True)
class StopOnPattern:
    """Una parada concreta dentro de un patrón, con su distancia a un punto."""

    stop: Stop
    pattern: Pattern
    index: int
    distance_m: float


@dataclass(slots=True, frozen=True)
class Place:
    """Un lugar o una parada con nombre, y las paradas físicas que lo sirven."""

    name: str
    kind: str  # "lugar" | "parada"
    lat: float
    lon: float
    stops: tuple[Stop, ...]


class TransitStore:
    def __init__(
        self,
        gtfs_path: str | Path,
        db_path: str | Path | None = None,
        landmarks_path: str | Path | None = None,
    ):
        self.gtfs_path = str(gtfs_path)
        self.db_path = (
            str(db_path) if db_path else str(Path(gtfs_path).with_name("transit.db"))
        )
        self.landmarks_path = str(landmarks_path) if landmarks_path else None
        self.build_report: BuildReport | None = None
        self.meta: dict[str, str] = {}
        self._loaded = False
        self._conn: sqlite3.Connection | None = None
        self._routes: dict[str, Route] = {}
        self._route_order: list[str] = []
        self._stops: dict[str, Stop] = {}
        self._patterns: dict[str, Pattern] = {}
        self._route_patterns: dict[str, list[Pattern]] = {}
        self._stop_patterns: dict[str, list[tuple[Pattern, int]]] = {}
        self._stop_routes: dict[str, list[str]] = {}
        self._alias: dict[str, list[str]] = {}
        self._catalog: dict[str, list[tuple[str, str]]] = {}
        self._regions: dict[str, set[str]] = {}
        self._landmarks: list[Landmark] = []
        self._calendar: dict[str, tuple[str, Optional[date], Optional[date]]] = {}
        self._calendar_dates: dict[str, dict[str, int]] = {}
        self._feed_end: Optional[date] = None
        self._services_cache: dict[date, Optional[frozenset[str]]] = {}
        self._map_coords: dict[str, list[list[float]]] = {}
        self._neighbors: dict[tuple[str, float], list[Route]] = {}

    # ── Carga ────────────────────────────────────────────────────────────────

    @property
    def loaded(self) -> bool:
        return self._loaded

    def load(self) -> "TransitStore":
        if not Path(self.gtfs_path).exists():
            raise FileNotFoundError(f"No existe GTFS: {self.gtfs_path}")
        self.close()
        self.build_report = ensure_transit_db(
            self.gtfs_path, self.db_path, self.landmarks_path
        )
        self._read()
        self._loaded = True
        logger.info(
            "Base de transporte cargada: %d rutas, %d paradas, %d patrones.",
            len(self._routes),
            len(self._stops),
            len(self._patterns),
        )
        return self

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None
        self._loaded = False

    def _assert_loaded(self) -> None:
        if not self._loaded:
            self.load()

    def _read(self) -> None:
        uri = f"{Path(self.db_path).resolve().as_uri()}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        self._conn = conn
        self.meta = dict(conn.execute("SELECT key, value FROM meta").fetchall())

        self._routes = {}
        for row in conn.execute("SELECT * FROM routes ORDER BY sort_key"):
            self._routes[row["route_id"]] = Route(
                route_id=row["route_id"],
                route_short_name=row["short_name"],
                route_long_name=row["long_name"],
                route_type=row["route_type"],
                agency_id=row["agency_id"],
                mode=row["mode"],
                group=row["grp"],
                legacy_number=row["legacy_number"],
                branch_name=row["branch_name"],
                color=row["display_color"],
                has_frequency=bool(row["has_frequency"]),
            )
        self._route_order = list(self._routes)

        self._stops = {
            row["stop_id"]: Stop(
                stop_id=row["stop_id"],
                stop_name=row["name"],
                lat=row["lat"],
                lon=row["lon"],
            )
            for row in conn.execute("SELECT * FROM stops")
        }

        stops_by_pattern: dict[str, list[tuple[str, float, float]]] = {}
        for row in conn.execute(
            "SELECT pattern_id, stop_id, dist_m, offset_s FROM pattern_stops "
            "ORDER BY pattern_id, seq"
        ):
            stops_by_pattern.setdefault(row[0], []).append((row[1], row[2], row[3]))

        windows_by_pattern: dict[str, list[ServiceWindow]] = {}
        for row in conn.execute(
            "SELECT pattern_id, service_id, start_s, end_s, headway_s, exact_times "
            "FROM pattern_service ORDER BY pattern_id, service_id, start_s"
        ):
            windows_by_pattern.setdefault(row[0], []).append(
                ServiceWindow(row[1], row[2], row[3], row[4], bool(row[5]))
            )

        self._patterns = {}
        self._route_patterns = {rid: [] for rid in self._routes}
        self._stop_patterns = {}
        for row in conn.execute("SELECT * FROM patterns ORDER BY rowid"):
            flat = array("d")
            flat.frombytes(row["shape"])
            points = list(zip(flat[0::2], flat[1::2], strict=True))
            triples = stops_by_pattern.get(row["pattern_id"], [])
            pattern = Pattern(
                pattern_id=row["pattern_id"],
                route_id=row["route_id"],
                direction_id=row["direction_id"],
                headsign=row["headsign"],
                length_m=row["length_m"],
                run_time_s=row["run_time_s"],
                is_main=bool(row["is_main"]),
                shape_source=row["shape_source"],
                stop_ids=tuple(t[0] for t in triples),
                dist_m=tuple(t[1] for t in triples),
                offset_s=tuple(t[2] for t in triples),
                line=Polyline(points),
                map_polyline=row["map_polyline"],
                windows=tuple(windows_by_pattern.get(row["pattern_id"], ())),
                issues=tuple(code for code in row["issues"].split(",") if code),
            )
            self._patterns[pattern.pattern_id] = pattern
            self._route_patterns.setdefault(pattern.route_id, []).append(pattern)
            for index, stop_id in enumerate(pattern.stop_ids):
                self._stop_patterns.setdefault(stop_id, []).append((pattern, index))

        self._stop_routes = {}
        for stop_id, items in self._stop_patterns.items():
            seen: list[str] = []
            for pattern, _ in items:
                if pattern.route_id not in seen:
                    seen.append(pattern.route_id)
            seen.sort(key=self._route_order.index)
            self._stop_routes[stop_id] = seen

        def alias_priority(route_id: str) -> tuple:
            route = self._routes[route_id]
            return (
                _MODE_PRIORITY.get(route.mode, 9),
                not route.has_frequency,
                self._route_order.index(route_id),
            )

        buckets: dict[str, list[tuple[int, str]]] = {}
        for alias, route_id, rank in conn.execute(
            "SELECT alias, route_id, rank FROM route_alias"
        ):
            if route_id in self._routes:
                buckets.setdefault(alias, []).append((rank, route_id))
        self._alias = {
            alias: [
                rid
                for _, rid in sorted(
                    items, key=lambda item: (item[0], alias_priority(item[1]))
                )
            ]
            for alias, items in buckets.items()
        }
        # Alias que solo existen por una ruta del catálogo sin viajes.
        self._catalog = {}
        for alias, short_name, long_name in conn.execute(
            "SELECT alias, short_name, long_name FROM catalog_routes ORDER BY short_name"
        ):
            items = buckets.get(alias)
            if not items or min(rank for rank, _ in items) >= CATALOG_ALIAS_RANK:
                self._catalog.setdefault(alias, []).append((short_name, long_name))

        self._regions = {}
        for route_id, region in conn.execute(
            "SELECT route_id, region FROM route_region"
        ):
            self._regions.setdefault(region, set()).add(route_id)

        self._landmarks = [
            Landmark(
                landmark_id=row["landmark_id"],
                name=row["name"],
                kind=row["kind"],
                lat=row["lat"],
                lon=row["lon"],
                aliases=tuple(json.loads(row["aliases"])),
            )
            for row in conn.execute("SELECT * FROM landmarks ORDER BY name")
        ]

        self._calendar = {}
        self._feed_end = None
        for service_id, days, start, end in conn.execute(
            "SELECT service_id, days, start_date, end_date FROM calendar"
        ):
            start_date, end_date = _parse_date(start), _parse_date(end)
            self._calendar[service_id] = (days, start_date, end_date)
            if end_date and (self._feed_end is None or end_date > self._feed_end):
                self._feed_end = end_date
        self._calendar_dates = {}
        for service_id, day, kind in conn.execute(
            "SELECT service_id, date, exception_type FROM calendar_dates"
        ):
            self._calendar_dates.setdefault(day, {})[service_id] = kind

        self._services_cache.clear()
        self._map_coords.clear()
        self._neighbors.clear()

    # ── Rutas ────────────────────────────────────────────────────────────────

    _normalize = staticmethod(GTFSLoader._normalize)

    def get_routes(self, region: str | None = None) -> list[Route]:
        self._assert_loaded()
        if region:
            members = self._regions.get(region, set())
            return [self._routes[rid] for rid in self._route_order if rid in members]
        return [self._routes[rid] for rid in self._route_order]

    def get_route(self, route_id: str) -> Optional[Route]:
        self._assert_loaded()
        return self._routes.get(route_id)

    def find_routes(self, query: str) -> list[Route]:
        """
        Todas las rutas que responden a `query`: clave oficial, número antiguo
        ("646"), clave base ("T04A") o nombre largo exacto. La más probable va
        primero.
        """
        self._assert_loaded()
        if not query:
            return []
        if query in self._routes:
            return [self._routes[query]]
        normalized = self._normalize(query)
        if not normalized:
            return []
        ids = self._alias.get(normalized)
        if ids:
            return [self._routes[rid] for rid in ids]
        return [
            route
            for route in self._routes.values()
            if route.route_long_name
            and self._normalize(route.route_long_name) == normalized
        ]

    def find_route(self, query: str) -> Optional[Route]:
        matches = self.find_routes(query)
        return matches[0] if matches else None

    def catalog_note(self, query: str) -> Optional[str]:
        """
        Aviso cuando `query` nombra una ruta que está en el catálogo oficial
        pero sin recorrido ni horarios en el feed ("231C" → T15-C06), y por eso
        se muestra la troncal de su familia.
        """
        self._assert_loaded()
        if not query or query in self._routes:
            return None
        alias = self._normalize(query)
        entries = self._catalog.get(alias)
        if not entries:
            return None
        names = [
            f"{short} «{_branch(long_name)}»" if _branch(long_name) else short
            for short, long_name in entries
        ]
        if len(entries) == 1 and self._normalize(entries[0][0]) == alias:
            subject = f"La {names[0]}"
            verb = "está"
        else:
            subject = f"La {query.strip()} ({', '.join(names)})"
            verb = "está" if len(entries) == 1 else "están"
        note = (
            f"{subject} {verb} en el catálogo oficial, pero el feed no trae su "
            "recorrido ni sus horarios."
        )
        trunks = self._alias.get(alias)
        if trunks:
            shown = ", ".join(self._routes[rid].route_short_name for rid in trunks)
            note += f" Se muestra la troncal {shown}."
        return note

    def route_ids_in_region(self, region: str) -> set[str]:
        self._assert_loaded()
        return set(self._regions.get(region, set()))

    def routes_near_route(self, route_id: str, radius_km: float = 0.3) -> list[Route]:
        """Otras rutas con alguna parada a menos de `radius_km` de las de esta."""
        self._assert_loaded()
        key = (route_id, radius_km)
        if key not in self._neighbors:
            found: dict[str, None] = {}
            for stop in self.get_stops_for_route(route_id):
                for near, _ in self.stops_near(stop.lat, stop.lon, radius_km, limit=40):
                    for rid in self._stop_routes.get(near.stop_id, ()):
                        if rid != route_id:
                            found.setdefault(rid)
            ordered = sorted(found, key=self._route_order.index)
            self._neighbors[key] = [self._routes[rid] for rid in ordered]
        return list(self._neighbors[key])

    # ── Patrones ─────────────────────────────────────────────────────────────

    def get_pattern(self, pattern_id: str) -> Optional[Pattern]:
        self._assert_loaded()
        return self._patterns.get(pattern_id)

    def get_patterns(self, route_id: str, main_only: bool = False) -> list[Pattern]:
        self._assert_loaded()
        route = self.find_route(route_id)
        if route is None:
            return []
        patterns = self._route_patterns.get(route.route_id, [])
        return [p for p in patterns if p.is_main] if main_only else list(patterns)

    def patterns_at_stop(self, stop_id: str) -> list[tuple[Pattern, int]]:
        self._assert_loaded()
        return list(self._stop_patterns.get(stop_id, ()))

    def map_coordinates(self, pattern: Pattern) -> list[list[float]]:
        """Trazo simplificado del patrón como [[lon, lat], …] para el mapa."""
        coords = self._map_coords.get(pattern.pattern_id)
        if coords is None:
            coords = [
                [round(lon, 5), round(lat, 5)]
                for lat, lon in decode_polyline(pattern.map_polyline)
            ]
            self._map_coords[pattern.pattern_id] = coords
        return coords

    def get_route_path(self, route_id: str) -> list[tuple[float, float]]:
        """Trazo (lat, lon) del patrón principal de la ruta."""
        patterns = self.get_patterns(route_id, main_only=True) or self.get_patterns(
            route_id
        )
        return list(patterns[0].line.points) if patterns else []

    # ── Paradas ──────────────────────────────────────────────────────────────

    def get_stops(self) -> list[Stop]:
        self._assert_loaded()
        return sorted(self._stops.values(), key=lambda stop: stop.stop_name)

    def get_stop(self, stop_id: str) -> Optional[Stop]:
        self._assert_loaded()
        return self._stops.get(stop_id)

    def get_stops_for_route(
        self, route_id: str, direction_id: int | None = None
    ) -> list[Stop]:
        """
        Paradas de la ruta en orden de recorrido. Sin `direction_id` devuelve las
        de todos los sentidos (primero los patrones principales), sin repetir.
        """
        patterns = self.get_patterns(route_id)
        if direction_id is not None:
            patterns = [p for p in patterns if p.direction_id == direction_id]
        patterns.sort(key=lambda p: (not p.is_main, p.direction_id))
        seen: dict[str, None] = {}
        for pattern in patterns:
            for stop_id in pattern.stop_ids:
                seen.setdefault(stop_id)
        return [self._stops[sid] for sid in seen if sid in self._stops]

    def get_ordered_stops_for_route(
        self, route_id: str, direction_id: int | None = None
    ) -> list[Stop]:
        return self.get_stops_for_route(route_id, direction_id)

    def get_routes_at_stop(self, stop_id: str) -> list[Route]:
        self._assert_loaded()
        if stop_id not in self._stop_routes:
            matches = self.search_stops(stop_id, limit=1)
            if not matches:
                return []
            stop_id = matches[0].stop_id
        return [self._routes[rid] for rid in self._stop_routes.get(stop_id, ())]

    def search_stops(self, query: str, limit: int = 10) -> list[Stop]:
        """
        Búsqueda de paradas por nombre, sin distinguir acentos y con prefijo en
        la última palabra ("tona" encuentra "Tonaltecas"). Puntaje: 100 nombre
        exacto, 80 contiene la frase, 60 contiene todas las palabras, menos si
        solo coinciden algunas.
        """
        self._assert_loaded()
        normalized = self._normalize(query)
        tokens = _TOKEN_RE.findall(normalized)
        if len(tokens) > 1:
            tokens = [t for t in tokens if len(t) >= 2] or tokens
        if not tokens or self._conn is None:
            return []

        def run(operator: str) -> list[str]:
            terms = [f'"{t}"' for t in tokens[:-1]] + [f'"{tokens[-1]}"*']
            try:
                rows = self._conn.execute(
                    "SELECT stop_id FROM stop_fts WHERE stop_fts MATCH ? LIMIT 600",
                    (f" {operator} ".join(terms),),
                ).fetchall()
            except sqlite3.Error:
                return []
            return [row[0] for row in rows]

        all_words = set(run("AND"))
        candidates = all_words or set(run("OR"))
        scored: list[tuple[int, int, int, str, Stop]] = []
        for stop_id in candidates:
            stop = self._stops.get(stop_id)
            if stop is None:
                continue
            name = self._normalize(stop.stop_name)
            if name == normalized:
                score = 100
            elif normalized in name:
                score = 80
            elif stop_id in all_words:
                score = 60
            else:
                score = 20 + 15 * sum(1 for t in tokens if t in name)
            scored.append(
                (
                    -score,
                    -len(self._stop_routes.get(stop_id, ())),
                    len(stop.stop_name),
                    stop.stop_name,
                    stop.stop_id,
                    stop,
                )
            )
        scored.sort(key=lambda item: item[:5])
        return [item[5] for item in scored[:limit]]

    def stops_near(
        self, lat: float, lon: float, radius_km: float = 0.5, limit: int = 10
    ) -> list[tuple[Stop, float]]:
        self._assert_loaded()
        if self._conn is None:
            return []
        dlat = radius_km / _KM_PER_DEGREE
        dlon = radius_km / (_KM_PER_DEGREE * max(cos(radians(lat)), 0.2))
        rows = self._conn.execute(
            "SELECT s.stop_id FROM stop_rtree r JOIN stop_rowid s ON s.id = r.id "
            "WHERE r.min_lat >= ? AND r.max_lat <= ? AND r.min_lon >= ? AND r.max_lon <= ?",
            (lat - dlat, lat + dlat, lon - dlon, lon + dlon),
        ).fetchall()
        matches: list[tuple[Stop, float]] = []
        for (stop_id,) in rows:
            stop = self._stops.get(stop_id)
            if stop is None:
                continue
            distance = haversine_km(lat, lon, stop.lat, stop.lon)
            if distance <= radius_km:
                matches.append((stop, distance))
        matches.sort(key=lambda item: item[1])
        return matches[:limit]

    def nearest_stop(
        self, lat: float, lon: float, route_id: str | None = None
    ) -> Optional[Stop]:
        self._assert_loaded()
        if route_id:
            stops = self.get_stops_for_route(route_id)
            if not stops:
                return None
            return min(stops, key=lambda s: haversine_km(s.lat, s.lon, lat, lon))
        for radius in (0.3, 1.0, 3.0, 10.0, 60.0):
            found = self.stops_near(lat, lon, radius, limit=1)
            if found:
                return found[0][0]
        return None

    def stops_of_route_near(
        self, route_id: str, lat: float, lon: float, radius_km: float = 0.6
    ) -> list[StopOnPattern]:
        """La parada de abordaje más cercana de cada patrón de la ruta, dentro del radio."""
        results: list[StopOnPattern] = []
        for pattern in self.get_patterns(route_id):
            best: StopOnPattern | None = None
            # La última parada es la terminal de llegada: ahí no se aborda.
            for index, stop_id in enumerate(pattern.stop_ids[:-1]):
                stop = self._stops.get(stop_id)
                if stop is None:
                    continue
                meters = haversine_km(lat, lon, stop.lat, stop.lon) * 1000.0
                if meters <= radius_km * 1000.0 and (
                    best is None or meters < best.distance_m
                ):
                    best = StopOnPattern(stop, pattern, index, meters)
            if best is not None:
                results.append(best)
        results.sort(
            key=lambda item: (not item.pattern.is_main, item.pattern.direction_id)
        )
        return results

    def _resolve_stop(
        self, stop_id_or_query: str, route_id: str | None = None
    ) -> Optional[Stop]:
        """
        Parada por identificador o por nombre. Con `route_id`, entre varias
        paradas del mismo nombre se prefiere la que sirve a esa ruta.
        """
        stop = self._stops.get(stop_id_or_query)
        if stop is not None:
            return stop
        matches = self.search_stops(stop_id_or_query, limit=30 if route_id else 1)
        if not matches:
            return None
        if route_id:
            for candidate in matches:
                if route_id in self._stop_routes.get(candidate.stop_id, ()):
                    return candidate
        return matches[0]

    # ── Puntos de interés ────────────────────────────────────────────────────

    def get_landmarks(self) -> list[Landmark]:
        self._assert_loaded()
        return list(self._landmarks)

    def find_landmark(self, query: str) -> Optional[Landmark]:
        """Lugar cuyo nombre o alias coincide con `query` o aparece dentro de ella."""
        self._assert_loaded()
        normalized = self._normalize(query)
        if not normalized:
            return None
        for landmark in self._landmarks:
            if normalized in landmark.aliases:
                return landmark
        padded = f" {normalized} "
        best: tuple[int, Landmark] | None = None
        for landmark in self._landmarks:
            for alias in landmark.aliases:
                if len(alias) >= 4 and f" {alias} " in padded:
                    if best is None or len(alias) > best[0]:
                        best = (len(alias), landmark)
        return best[1] if best else None

    def resolve_place(self, query: str, radius_km: float = 0.6) -> Optional[Place]:
        """
        Interpreta un texto como lugar conocido ("CUCEI") o como nombre de
        parada, y devuelve las paradas físicas correspondientes: las cercanas
        al lugar, o todas las del mismo nombre en el mismo cruce.
        """
        landmark = self.find_landmark(query)
        if landmark is not None:
            near = self.stops_near(landmark.lat, landmark.lon, radius_km, limit=80)
            return Place(
                landmark.name,
                "lugar",
                landmark.lat,
                landmark.lon,
                tuple(stop for stop, _ in near),
            )
        groups = self.place_groups(query, limit=1)
        return groups[0] if groups else None

    def place_groups(
        self, query: str, radius_km: float = 0.5, limit: int = 5
    ) -> list[Place]:
        """
        Paradas con el nombre buscado, agrupadas por cruce: cada grupo reúne
        las del mismo nombre a menos de `radius_km` (las dos aceras). Muchas
        calles se repiten en la ciudad ("Morelos", "Juárez"); el primer grupo
        es el que mejor responde a la búsqueda.
        """
        matches = self.search_stops(query, limit=60)
        if not matches:
            return []
        top_name = self._normalize(matches[0].stop_name)
        groups: list[list[Stop]] = []
        for stop in matches:
            if self._normalize(stop.stop_name) != top_name:
                continue
            for group in groups:
                anchor = group[0]
                if (
                    haversine_km(stop.lat, stop.lon, anchor.lat, anchor.lon)
                    <= radius_km
                ):
                    group.append(stop)
                    break
            else:
                groups.append([stop])
        return [
            Place(
                group[0].stop_name, "parada", group[0].lat, group[0].lon, tuple(group)
            )
            for group in groups[:limit]
        ]

    def place_from_stop(self, stop_id: str, radius_km: float = 0.5) -> Optional[Place]:
        """
        Lugar de una parada concreta: ella y las del mismo nombre a menos de
        `radius_km` (las dos aceras de un cruce). A diferencia de
        `resolve_place`, no depende de qué parada homónima aparezca primero.
        """
        self._assert_loaded()
        stop = self._stops.get(stop_id)
        if stop is None:
            return None
        name = self._normalize(stop.stop_name)
        group = [stop] + [
            near
            for near, _ in self.stops_near(stop.lat, stop.lon, radius_km, limit=60)
            if near.stop_id != stop.stop_id and self._normalize(near.stop_name) == name
        ]
        return Place(stop.stop_name, "parada", stop.lat, stop.lon, tuple(group))

    def routes_at_place(self, place: Place) -> list[Route]:
        """Rutas que pasan por alguna de las paradas del lugar, en orden de catálogo."""
        found: dict[str, None] = {}
        for stop in place.stops:
            for route_id in self._stop_routes.get(stop.stop_id, ()):
                found.setdefault(route_id)
        return [self._routes[rid] for rid in sorted(found, key=self._route_order.index)]

    # ── Calendario de servicio ───────────────────────────────────────────────

    @property
    def feed_end(self) -> Optional[date]:
        return self._feed_end

    def active_services(self, day: date) -> Optional[frozenset[str]]:
        """
        Servicios que operan en `day`. `None` significa "todos" (feed sin
        calendario). Si el feed ya venció, sus servicios se tratan como patrón
        semanal; un día festivo sin excepción publicada opera como domingo.
        """
        if not self._calendar:
            return None
        if day in self._services_cache:
            return self._services_cache[day]

        expired = self._feed_end is not None and day > self._feed_end
        exceptions = self._calendar_dates.get(day.strftime("%Y%m%d"), {})
        weekday = day.weekday()
        if es_festivo(day) and not exceptions:
            weekday = 6

        active: set[str] = set()
        for service_id, (days, start, end) in self._calendar.items():
            if len(days) != 7 or days[weekday] != "1":
                continue
            if not expired and (
                (start is not None and day < start) or (end is not None and day > end)
            ):
                continue
            active.add(service_id)
        for service_id, kind in exceptions.items():
            if kind == 1:
                active.add(service_id)
            elif kind == 2:
                active.discard(service_id)

        result = frozenset(active)
        if len(self._services_cache) > 64:
            self._services_cache.clear()
        self._services_cache[day] = result
        return result

    # ── Pasadas ──────────────────────────────────────────────────────────────

    def pattern_arrivals(
        self, pattern: Pattern, index: int, now: datetime, limit: int = 3
    ) -> list[tuple[float, ServiceWindow, int]]:
        """
        Próximas pasadas programadas del patrón por su parada `index`.
        Devuelve (segundos desde la medianoche de hoy, ventana, día de servicio).
        """
        base_day = now.date()
        now_s = now.hour * 3600 + now.minute * 60 + now.second
        offset = pattern.offset_s[index]
        found: list[tuple[float, ServiceWindow, int]] = []
        # El día -1 cubre los viajes de anoche que cruzan la medianoche.
        for day_offset in (-1, 0, 1, 2):
            services = self.active_services(base_day + timedelta(days=day_offset))
            shift = day_offset * 86400
            for window in pattern.windows:
                if services is not None and window.service_id not in services:
                    continue
                first = window.start_s + offset + shift
                if window.headway_s <= 0:
                    if first >= now_s:
                        found.append((first, window, day_offset))
                    continue
                last = window.end_s + offset + shift
                skip = 0 if first >= now_s else ceil((now_s - first) / window.headway_s)
                when = first + skip * window.headway_s
                taken = 0
                while when < last and taken < limit:
                    found.append((when, window, day_offset))
                    when += window.headway_s
                    taken += 1
            if day_offset >= 0 and len(found) >= limit:
                break
        found.sort(key=lambda item: item[0])
        return found[:limit]

    def get_next_departures(
        self,
        route_id: str,
        stop_id: str,
        current_minutes: float | None = None,
        limit: int = 3,
        *,
        now: datetime | None = None,
        pattern_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """
        Próximas pasadas de la ruta por la parada, de todos los sentidos que la
        usan (cada una indica su destino). `current_minutes` son minutos desde
        la medianoche de hoy; si se omite se usa la hora actual.
        """
        self._assert_loaded()
        route = self.find_route(route_id)
        if route is None:
            return []
        stop = self._resolve_stop(stop_id, route.route_id)
        if stop is None:
            return []
        moment = now or datetime.now(GDL_TZ)
        if current_minutes is not None:
            midnight = moment.replace(hour=0, minute=0, second=0, microsecond=0)
            moment = midnight + timedelta(minutes=float(current_minutes))
        now_s = moment.hour * 3600 + moment.minute * 60 + moment.second

        boarding: list[dict[str, Any]] = []
        arriving: list[dict[str, Any]] = []
        for pattern, index in self._stop_patterns.get(stop.stop_id, ()):
            if pattern.route_id != route.route_id:
                continue
            if pattern_id and pattern.pattern_id != pattern_id:
                continue
            is_terminal = index == len(pattern.stop_ids) - 1
            for when, window, day_offset in self.pattern_arrivals(
                pattern, index, moment, limit
            ):
                in_day = int(when) % 86400
                entry = {
                    "trip_id": pattern.pattern_id,
                    "pattern_id": pattern.pattern_id,
                    "route_id": route.route_id,
                    "stop_id": stop.stop_id,
                    "stop_index": index,
                    "headsign": pattern.headsign,
                    "direction_id": pattern.direction_id,
                    "departure_time": (
                        f"{in_day // 3600:02d}:{in_day % 3600 // 60:02d}:{in_day % 60:02d}"
                    ),
                    "departure_minutes": in_day // 60,
                    "minutes_until": (when - now_s) / 60.0,
                    "service_day_offset": max(day_offset, 0),
                    "absolute_minutes": when / 60.0,
                    "headway_minutes": (
                        window.headway_s / 60.0 if window.headway_s else None
                    ),
                    "exact": window.exact or window.headway_s == 0,
                }
                (arriving if is_terminal else boarding).append(entry)

        # En una terminal compartida interesan las salidas, no las llegadas.
        entries = boarding or arriving
        entries.sort(key=lambda item: item["absolute_minutes"])
        return entries[:limit]

    def active_headway(self, pattern: Pattern, now: datetime) -> Optional[float]:
        """Frecuencia de paso (min) del patrón en este momento, o None."""
        now_s = now.hour * 3600 + now.minute * 60 + now.second
        for day_offset in (0, -1):
            services = self.active_services(now.date() + timedelta(days=day_offset))
            moment = now_s - day_offset * 86400
            for window in pattern.windows:
                if window.headway_s <= 0:
                    continue
                if services is not None and window.service_id not in services:
                    continue
                if window.start_s <= moment < window.end_s:
                    return window.headway_s / 60.0
        return None

    def get_active_headway(
        self,
        route_id: str,
        current_minutes: float | None = None,
        *,
        now: datetime | None = None,
    ) -> Optional[float]:
        """Frecuencia de paso actual de la ruta (min); None si no hay servicio ahora."""
        moment = now or datetime.now(GDL_TZ)
        if current_minutes is not None:
            midnight = moment.replace(hour=0, minute=0, second=0, microsecond=0)
            moment = midnight + timedelta(minutes=float(current_minutes))
        patterns = self.get_patterns(route_id)
        patterns.sort(key=lambda p: (not p.is_main, p.direction_id))
        for pattern in patterns:
            headway = self.active_headway(pattern, moment)
            if headway is not None:
                return headway
        return None

    def get_average_headway(
        self,
        route_id: str,
        stop_id: str,
        current_minutes: float | None = None,
        lookahead: int = 4,
    ) -> Optional[float]:
        """Intervalo típico entre pasadas del mismo sentido en la parada (min)."""
        departures = self.get_next_departures(
            route_id, stop_id, current_minutes, limit=max(lookahead * 2, 4)
        )
        if not departures:
            return None
        first = departures[0]
        if first.get("headway_minutes"):
            return round(float(first["headway_minutes"]), 1)
        same = [d for d in departures if d["pattern_id"] == first["pattern_id"]]
        gaps = [
            b["absolute_minutes"] - a["absolute_minutes"]
            for a, b in zip(same, same[1:], strict=False)
            if b["absolute_minutes"] > a["absolute_minutes"]
        ]
        return round(sum(gaps) / len(gaps), 1) if gaps else None

    # ── Información del feed ─────────────────────────────────────────────────

    def feed_info(self) -> dict[str, Any]:
        self._assert_loaded()
        return {
            "source": self.meta.get("source_name", ""),
            "feed_start": self.meta.get("feed_start", ""),
            "feed_end": self.meta.get("feed_end", ""),
            "built_at": self.meta.get("built_at", ""),
            "expired": bool(
                self._feed_end and datetime.now(GDL_TZ).date() > self._feed_end
            ),
            "routes": len(self._routes),
            "stops": len(self._stops),
            "patterns": len(self._patterns),
            "landmarks": len(self._landmarks),
        }


def _branch(long_name: str) -> str:
    """Nombre del ramal entre comillas en el nombre largo oficial."""
    match = re.search(r'"([^"]+)"', long_name or "")
    return match.group(1).strip() if match else ""


def _parse_date(value: str | None) -> Optional[date]:
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y%m%d").date()
    except ValueError:
        return None
