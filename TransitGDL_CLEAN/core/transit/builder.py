"""
Construye la base de transporte (`transit.db`, SQLite) a partir de un zip GTFS.

El zip se lee una sola vez y se deja todo limpio y precalculado:

    • patrones: una fila por ruta, sentido y secuencia de paradas;
    • distancia de cada parada a lo largo del trazo (referencia lineal);
    • tiempos entre paradas reparados cuando el feed trae valores imposibles;
    • ventanas de frecuencia por patrón y tipo de día;
    • alias de rutas (clave oficial, número antiguo, clave base);
    • índices de texto (FTS5) y de cercanía (R-tree) para las paradas.

Así la aplicación arranca leyendo tablas ya resueltas en lugar de volver a
interpretar el GTFS cada vez.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import logging
import os
import re
import sqlite3
import time
import zipfile
from array import array
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from statistics import median
from typing import Any, Iterator

from ..geometry import METERS_PER_DEGREE, Polyline, encode_polyline, simplify
from ..gtfs_loader import GTFSLoader

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 2

SHAPE_TOLERANCE_M = 2.0  # trazo para referencia lineal
MAP_TOLERANCE_M = 7.0  # trazo ligero para dibujar en el mapa
SHAPE_FIT_GOOD = 0.9  # ajuste con el que se acepta el trazo sin buscar otro
SHAPE_FIT_MIN = 0.35  # por debajo, el trazo no sirve y se unen las paradas
NEAR_SHAPE_M = 60.0  # una parada a más distancia no está sobre el trazo
FAR_FROM_SHAPE_M = 80.0  # separación con la que una parada cuenta como fuera del trazo
SHAPE_FIT_SAMPLE = 16  # paradas que se muestrean para medir el ajuste
CANDIDATE_RADIUS_M = 150.0  # pasadas del trazo que se consideran para cada parada

# Andenes duplicados: el feed lista algunas estaciones dos veces seguidas, con
# otro identificador, en el mismo punto y a la misma hora.
TWIN_STOP_M = 30.0
TWIN_STOP_MAX_GAP_S = 5

# Velocidad máxima creíble entre dos paradas consecutivas (m/s).
MAX_SPEED_MS = {"tren": 100 / 3.6}
DEFAULT_MAX_SPEED_MS = 80 / 3.6
# Velocidad comercial de respaldo cuando un patrón no trae tiempos útiles (m/s).
FALLBACK_SPEED_MS = {"tren": 35 / 3.6, "brt": 24 / 3.6}
DEFAULT_FALLBACK_SPEED_MS = 18 / 3.6
# Velocidad comercial creíble de un recorrido completo (km/h). Fuera de este
# rango los tiempos del feed son de relleno y se reconstruyen.
CREDIBLE_SPEED_KMH = {"tren": (20.0, 60.0)}
DEFAULT_CREDIBLE_SPEED_KMH = (8.0, 32.0)

# Defectos de un recorrido que se avisan al usuario (ver store.PATTERN_NOTES).
ISSUE_ONLY_TERMINALS = "solo_terminales"
ISSUE_ESTIMATED_TIMES = "tiempos_estimados"
ISSUE_APPROXIMATE_SHAPE = "trazo_aproximado"
ISSUE_STOPS_OFF_SHAPE = "paradas_lejos_del_trazo"
ISSUE_DOUBTFUL = "recorrido_dudoso"
DOUBTFUL_REPEATED_STOPS = 3  # paradas distintas que se repiten en un mismo viaje

# Alias de rutas del catálogo que el feed no trae con viajes.
CATALOG_ALIAS_RANK = 5

# Zonas que definen cada región del mapa: (nombre, lat, lon, radio en metros).
REGIONS: dict[str, tuple[tuple[str, float, float, float], ...]] = {
    "tonala_cucei": (
        ("CUCEI", 20.6566, -103.3250, 600.0),
        ("Tonalá Centro", 20.6243, -103.2420, 900.0),
    ),
}

# Colores para rutas que comparten el mismo color en el feed (sobre fondo oscuro).
DISPLAY_PALETTE = (
    "#4CC9F0",
    "#F4A261",
    "#90BE6D",
    "#E9C46A",
    "#F28482",
    "#B5A8D4",
    "#56CFE1",
    "#FF9F1C",
    "#80ED99",
    "#C77DFF",
    "#FFD166",
    "#EF476F",
    "#06D6A0",
    "#A0C4FF",
    "#F6BD60",
    "#84A59D",
)
BRAND_GROUPS = frozenset({"tren", "macro", "sitren"})

DAY_COLUMNS = (
    "monday",
    "tuesday",
    "wednesday",
    "thursday",
    "friday",
    "saturday",
    "sunday",
)

_LEGACY_RE = re.compile(r"^(?:Ruta\s+)?Troncal\s+\S+\s+-\s+(\d{1,3}[A-Z]?)(?=\s|\"|$)")
_BRANCH_RE = re.compile(r'"([^"]+)"')
_BUS_CODE_RE = re.compile(r"^([TC]\d+[A-Z]?)(?:[-_].+)?$")
_SMALL_WORDS = {"de", "del", "la", "las", "los", "y", "e", "el", "en", "a", "al"}

SCHEMA = """
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);

CREATE TABLE routes (
    route_id       TEXT PRIMARY KEY,
    agency_id      TEXT,
    short_name     TEXT NOT NULL,
    long_name      TEXT NOT NULL,
    route_type     INTEGER NOT NULL,
    mode           TEXT NOT NULL,
    grp            TEXT NOT NULL,
    legacy_number  TEXT,
    branch_name    TEXT NOT NULL,
    color          TEXT,
    text_color     TEXT,
    display_color  TEXT NOT NULL,
    has_frequency  INTEGER NOT NULL,
    sort_key       TEXT NOT NULL
);

CREATE TABLE route_alias (
    alias     TEXT NOT NULL,
    route_id  TEXT NOT NULL,
    kind      TEXT NOT NULL,
    rank      INTEGER NOT NULL,
    PRIMARY KEY (alias, route_id)
);

CREATE TABLE route_region (
    route_id  TEXT NOT NULL,
    region    TEXT NOT NULL,
    PRIMARY KEY (route_id, region)
);

CREATE TABLE stops (
    stop_id  TEXT PRIMARY KEY,
    name     TEXT NOT NULL,
    lat      REAL NOT NULL,
    lon      REAL NOT NULL
);
CREATE TABLE stop_rowid (id INTEGER PRIMARY KEY, stop_id TEXT NOT NULL);
CREATE VIRTUAL TABLE stop_fts USING fts5(
    stop_id UNINDEXED, name, tokenize = 'unicode61 remove_diacritics 2'
);
CREATE VIRTUAL TABLE stop_rtree USING rtree(id, min_lat, max_lat, min_lon, max_lon);

CREATE TABLE patterns (
    pattern_id    TEXT PRIMARY KEY,
    route_id      TEXT NOT NULL,
    direction_id  INTEGER NOT NULL,
    headsign      TEXT NOT NULL,
    length_m      REAL NOT NULL,
    run_time_s    REAL NOT NULL,
    n_stops       INTEGER NOT NULL,
    is_main       INTEGER NOT NULL,
    shape_source  TEXT NOT NULL,
    shape         BLOB NOT NULL,
    map_polyline  TEXT NOT NULL,
    issues        TEXT NOT NULL   -- defectos del feed, separados por comas
);
CREATE INDEX idx_patterns_route ON patterns(route_id);

-- Rutas del catálogo oficial sin viajes: su clave o número antiguo lleva a la
-- troncal de la misma familia, con un aviso.
CREATE TABLE catalog_routes (
    alias       TEXT NOT NULL,
    route_id    TEXT NOT NULL,
    short_name  TEXT NOT NULL,
    long_name   TEXT NOT NULL,
    PRIMARY KEY (alias, route_id)
);

CREATE TABLE pattern_stops (
    pattern_id  TEXT NOT NULL,
    seq         INTEGER NOT NULL,
    stop_id     TEXT NOT NULL,
    dist_m      REAL NOT NULL,
    offset_s    REAL NOT NULL,
    PRIMARY KEY (pattern_id, seq)
);
CREATE INDEX idx_pattern_stops_stop ON pattern_stops(stop_id);

-- headway_s = 0 significa una salida única a la hora start_s.
CREATE TABLE pattern_service (
    pattern_id   TEXT NOT NULL,
    service_id   TEXT NOT NULL,
    start_s      INTEGER NOT NULL,
    end_s        INTEGER NOT NULL,
    headway_s    INTEGER NOT NULL,
    exact_times  INTEGER NOT NULL
);
CREATE INDEX idx_pattern_service ON pattern_service(pattern_id);

CREATE TABLE calendar (
    service_id  TEXT PRIMARY KEY,
    days        TEXT NOT NULL,   -- 7 caracteres 0/1, lunes a domingo
    start_date  TEXT,
    end_date    TEXT
);
CREATE TABLE calendar_dates (
    service_id      TEXT NOT NULL,
    date            TEXT NOT NULL,
    exception_type  INTEGER NOT NULL
);

CREATE TABLE fares (
    fare_id    TEXT PRIMARY KEY,
    agency_id  TEXT,
    price      REAL,
    currency   TEXT,
    transfers  INTEGER
);

CREATE TABLE landmarks (
    landmark_id  TEXT PRIMARY KEY,
    name         TEXT NOT NULL,
    kind         TEXT NOT NULL,
    lat          REAL NOT NULL,
    lon          REAL NOT NULL,
    aliases      TEXT NOT NULL
);
"""


@dataclass
class BuildReport:
    source: str = ""
    routes: int = 0
    stops: int = 0
    stops_without_service: int = 0
    stop_names_fixed: int = 0
    patterns: int = 0
    pattern_stops: int = 0
    service_windows: int = 0
    patterns_by_source: dict[str, int] = field(default_factory=dict)
    shapes_reassigned: int = 0
    odd_speed_patterns: int = 0
    service_gaps_filled: int = 0
    service_gaps_dropped: int = 0
    routes_without_trips: list[str] = field(default_factory=list)
    routes_without_frequency: int = 0
    segments_repaired: int = 0
    stops_far_from_shape: int = 0
    twin_stops_merged: int = 0
    speeds_corrected: int = 0
    patterns_with_issues: dict[str, int] = field(default_factory=dict)
    catalog_aliases: int = 0
    landmarks: int = 0
    feed_start: str = ""
    feed_end: str = ""
    median_speed_kmh: float = 0.0
    seconds: float = 0.0
    regions: dict[str, int] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def summary(self) -> str:
        lines = [
            f"Fuente: {self.source}",
            f"Vigencia del feed: {self.feed_start or '?'} a {self.feed_end or '?'}",
            f"Rutas con servicio: {self.routes} ({self.routes_without_frequency} sin frecuencia "
            f"publicada; {len(self.routes_without_trips)} del catálogo sin viajes, omitidas)",
            f"Paradas: {self.stops} (descartadas sin servicio: {self.stops_without_service}; "
            f"nombres corregidos: {self.stop_names_fixed})",
            f"Patrones: {self.patterns} (trazo: "
            + ", ".join(f"{k}={v}" for k, v in sorted(self.patterns_by_source.items()))
            + f"; {self.shapes_reassigned} tomados de otro sentido o invertidos)",
            f"Paradas por patrón: {self.pattern_stops} "
            f"(a más de 80 m del trazo: {self.stops_far_from_shape}; "
            f"andenes duplicados fusionados: {self.twin_stops_merged})",
            "Recorridos con defectos del feed: "
            + (
                ", ".join(
                    f"{k}={v}" for k, v in sorted(self.patterns_with_issues.items())
                )
                or "ninguno"
            )
            + f" ({self.speeds_corrected} con tiempos reconstruidos)",
            f"Alias de rutas del catálogo sin viajes: {self.catalog_aliases}",
            f"Ventanas de servicio: {self.service_windows} (tipos de día sin frecuencia en el "
            f"feed: {self.service_gaps_filled} rellenados con el sentido contrario o el día "
            f"más parecido, {self.service_gaps_dropped} sin datos)",
            f"Tramos con tiempo reparado: {self.segments_repaired}",
            f"Velocidad comercial mediana: {self.median_speed_kmh:.1f} km/h "
            f"({self.odd_speed_patterns} patrones con velocidad atípica en el feed)",
            f"Puntos de interés: {self.landmarks}",
            "Rutas por región: "
            + ", ".join(f"{name}={count}" for name, count in self.regions.items()),
            f"Tiempo de construcción: {self.seconds:.1f} s",
        ]
        lines.extend(f"Aviso: {warning}" for warning in self.warnings)
        return "\n".join(lines)


# ── Utilidades ───────────────────────────────────────────────────────────────


def file_sha256(path: str | Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _source_signature(zip_path: Path, landmarks_path: Path | None) -> str:
    parts = [file_sha256(zip_path)]
    if landmarks_path and landmarks_path.exists():
        parts.append(file_sha256(landmarks_path))
    return "+".join(parts)


def needs_rebuild(
    zip_path: str | Path, db_path: str | Path, landmarks_path: str | Path | None = None
) -> bool:
    db_path = Path(db_path)
    if not db_path.exists():
        return True
    try:
        conn = sqlite3.connect(f"{db_path.resolve().as_uri()}?mode=ro", uri=True)
        try:
            meta = dict(conn.execute("SELECT key, value FROM meta"))
        finally:
            conn.close()
    except sqlite3.Error:
        return True
    if meta.get("schema_version") != str(SCHEMA_VERSION):
        return True
    signature = _source_signature(
        Path(zip_path), Path(landmarks_path) if landmarks_path else None
    )
    return meta.get("source_signature") != signature


def ensure_transit_db(
    zip_path: str | Path, db_path: str | Path, landmarks_path: str | Path | None = None
) -> BuildReport | None:
    """Construye la base solo si falta o si el zip (o los lugares) cambiaron."""
    if not needs_rebuild(zip_path, db_path, landmarks_path):
        return None
    logger.info("Construyendo base de transporte %s desde %s ...", db_path, zip_path)
    report = build_transit_db(zip_path, db_path, landmarks_path)
    logger.info("Base de transporte lista en %.1f s.", report.seconds)
    return report


def _rows(archive: zipfile.ZipFile, name: str) -> Iterator[dict[str, str]]:
    try:
        raw = archive.open(name)
    except KeyError:
        return
    with io.TextIOWrapper(raw, encoding="utf-8-sig", newline="") as text:
        for row in csv.DictReader(text):
            yield {k: (v or "").strip() for k, v in row.items() if k is not None}


def _seconds(value: str) -> int | None:
    try:
        hours, minutes, seconds = value.split(":")
        return int(hours) * 3600 + int(minutes) * 60 + int(float(seconds))
    except (ValueError, AttributeError):
        return None


def _int(value: str, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def clean_stop_name(name: str) -> str:
    """Quita espacios sobrantes y pasa a formato de título los nombres en mayúsculas."""
    name = " ".join(name.split())
    if not name.isupper() or len(name) <= 3:
        return name
    words = []
    for index, word in enumerate(name.split(" ")):
        lower = word.lower()
        if any(ch.isdigit() for ch in word):
            words.append(word)
        elif index > 0 and lower in _SMALL_WORDS:
            words.append(lower)
        else:
            words.append(lower.capitalize())
    return " ".join(words)


def classify_route(
    route_id: str, agency_id: str, route_type: int, short_name: str
) -> tuple[str, str]:
    """(modo, grupo) de una ruta a partir de su tipo GTFS, agencia y clave."""
    rid = route_id.upper()
    if route_type in (0, 1, 2):
        return "tren", "tren"
    if rid.startswith(("MC-L", "MP-T", "MACRO")):
        return "brt", "macro"
    if rid.startswith(("MC-A", "MP-A", "MP-C")):
        return "bus", "alimentadora"
    if agency_id.upper() == "ST" or rid.startswith("ST_"):
        return ("trolebus" if route_type == 11 else "bus"), "sitren"
    if route_type == 11:
        return "trolebus", "bus"
    code = short_name.upper()
    if code.startswith("T") and len(code) > 1 and code[1].isdigit():
        return "bus", "troncal"
    if code.startswith("C") and len(code) > 1 and code[1].isdigit():
        return "bus", "complementaria"
    return "bus", "bus"


_GROUP_ORDER = {
    "tren": 0,
    "macro": 1,
    "sitren": 2,
    "troncal": 3,
    "complementaria": 4,
    "alimentadora": 5,
    "bus": 6,
}


def _natural_key(text: str) -> str:
    """Clave de orden que respeta los números (T2 antes que T10)."""
    return re.sub(r"\d+", lambda m: m.group(0).zfill(5), text.upper())


# ── Construcción ─────────────────────────────────────────────────────────────


def build_transit_db(
    zip_path: str | Path, db_path: str | Path, landmarks_path: str | Path | None = None
) -> BuildReport:
    started = time.perf_counter()
    zip_path, db_path = Path(zip_path), Path(db_path)
    landmarks_path = Path(landmarks_path) if landmarks_path else None
    report = BuildReport(source=zip_path.name)
    normalize = GTFSLoader._normalize

    with zipfile.ZipFile(zip_path) as archive:
        route_rows = list(_rows(archive, "routes.txt"))
        stop_rows = list(_rows(archive, "stops.txt"))
        trip_rows = list(_rows(archive, "trips.txt"))
        calendar_rows = list(_rows(archive, "calendar.txt"))
        calendar_date_rows = list(_rows(archive, "calendar_dates.txt"))
        fare_rows = list(_rows(archive, "fare_attributes.txt"))

        trip_stop_times: dict[str, list[tuple[int, str, int | None]]] = defaultdict(
            list
        )
        for row in _rows(archive, "stop_times.txt"):
            trip_id, stop_id = row.get("trip_id", ""), row.get("stop_id", "")
            if not trip_id or not stop_id:
                continue
            when = _seconds(row.get("departure_time", ""))
            if when is None:
                when = _seconds(row.get("arrival_time", ""))
            trip_stop_times[trip_id].append(
                (_int(row.get("stop_sequence", "0")), stop_id, when)
            )

        trip_windows: dict[str, list[tuple[int, int, int, int]]] = defaultdict(list)
        for row in _rows(archive, "frequencies.txt"):
            start, end = _seconds(row.get("start_time", "")), _seconds(
                row.get("end_time", "")
            )
            headway = _int(row.get("headway_secs", "0"))
            if start is None or end is None or headway <= 0 or end <= start:
                continue
            exact = 1 if row.get("exact_times", "") == "1" else 0
            trip_windows[row.get("trip_id", "")].append((start, end, headway, exact))

        shapes: dict[str, list[tuple[int, float, float]]] = defaultdict(list)
        for row in _rows(archive, "shapes.txt"):
            try:
                shapes[row["shape_id"]].append(
                    (
                        _int(row.get("shape_pt_sequence", "0")),
                        float(row["shape_pt_lat"]),
                        float(row["shape_pt_lon"]),
                    )
                )
            except (KeyError, ValueError):
                continue

    # ── Paradas ──────────────────────────────────────────────────────────────
    stop_info: dict[str, tuple[str, float, float]] = {}
    for row in stop_rows:
        try:
            lat, lon = float(row["stop_lat"]), float(row["stop_lon"])
        except (KeyError, ValueError):
            continue
        raw_name = row.get("stop_name", "") or row.get("stop_id", "")
        name = clean_stop_name(raw_name)
        if name != raw_name:
            report.stop_names_fixed += 1
        stop_info[row["stop_id"]] = (name, lat, lon)

    # Secuencia de cada viaje: en orden, solo paradas conocidas y sin andenes
    # duplicados (misma estación listada dos veces seguidas).
    twins: set[tuple[str, str]] = set()
    for trip_id, items in trip_stop_times.items():
        kept: list[tuple[int, str, int | None]] = []
        for item in sorted(items):
            if item[1] not in stop_info:
                continue
            if kept and _is_twin_stop(kept[-1], item, stop_info, normalize):
                twins.add((kept[-1][1], item[1]))
                continue
            kept.append(item)
        trip_stop_times[trip_id] = kept
    report.twin_stops_merged = len(twins)

    # ── Rutas ────────────────────────────────────────────────────────────────
    routes: dict[str, dict[str, Any]] = {}
    for row in route_rows:
        route_id = row.get("route_id", "")
        if not route_id:
            continue
        short = row.get("route_short_name", "") or route_id
        long_name = row.get("route_long_name", "")
        route_type = _int(row.get("route_type", "3"), 3)
        agency = row.get("agency_id", "")
        mode, group = classify_route(route_id, agency, route_type, short)
        legacy = _LEGACY_RE.match(long_name)
        branch = _BRANCH_RE.search(long_name)
        routes[route_id] = {
            "route_id": route_id,
            "agency_id": agency,
            "short_name": short,
            "long_name": long_name,
            "route_type": route_type,
            "mode": mode,
            "grp": group,
            "legacy_number": legacy.group(1) if legacy else None,
            "branch_name": branch.group(1).strip() if branch else "",
            "color": (row.get("route_color", "") or "").upper() or None,
            "text_color": (row.get("route_text_color", "") or "").upper() or None,
            "has_frequency": 0,
            "sort_key": f"{_GROUP_ORDER.get(group, 9)}|{_natural_key(short)}|{route_id}",
        }

    # ── Patrones ─────────────────────────────────────────────────────────────
    trips: dict[str, dict[str, str]] = {}
    for row in trip_rows:
        if row.get("trip_id") and row.get("route_id") in routes:
            trips[row["trip_id"]] = row

    groups: dict[tuple[str, str, tuple[str, ...]], list[str]] = defaultdict(list)
    for trip_id, trip in trips.items():
        sequence = sorted(trip_stop_times.get(trip_id, ()))
        stop_ids = tuple(sid for _, sid, _ in sequence if sid in stop_info)
        if len(stop_ids) < 2:
            continue
        groups[(trip["route_id"], trip.get("shape_id", ""), stop_ids)].append(trip_id)

    by_route: dict[str, list[tuple[tuple[str, str, tuple[str, ...]], list[str]]]] = (
        defaultdict(list)
    )
    for key, trip_ids in groups.items():
        by_route[key[0]].append((key, sorted(trip_ids)))

    pattern_rows: list[tuple] = []
    pattern_stop_rows: list[tuple] = []
    service_rows: list[tuple] = []
    route_stops: dict[str, set[str]] = defaultdict(set)
    used_stops: set[str] = set()
    speeds_kmh: list[float] = []

    frequency_based = bool(trip_windows)
    service_days = {
        row.get("service_id", ""): "".join(
            "1" if row.get(day) == "1" else "0" for day in DAY_COLUMNS
        )
        for row in calendar_rows
    }

    route_shape_ids: dict[str, list[str]] = defaultdict(list)
    for trip in trips.values():
        shape_id = trip.get("shape_id", "")
        if shape_id and shape_id not in route_shape_ids[trip["route_id"]]:
            route_shape_ids[trip["route_id"]].append(shape_id)

    line_cache: dict[tuple[str, bool], Polyline | None] = {}

    def get_line(shape_id: str, reverse: bool) -> Polyline | None:
        """Trazo simplificado de un shape del feed, en su sentido o invertido."""
        cache_key = (shape_id, reverse)
        if cache_key not in line_cache:
            if (shape_id, False) not in line_cache:
                raw = [(lat, lon) for _, lat, lon in sorted(shapes.get(shape_id, ()))]
                points = simplify(raw, SHAPE_TOLERANCE_M) if len(raw) >= 2 else []
                line_cache[(shape_id, False)] = (
                    Polyline(points) if len(points) >= 2 else None
                )
            forward = line_cache[(shape_id, False)]
            if reverse:
                line_cache[cache_key] = (
                    Polyline(forward.points[::-1]) if forward is not None else None
                )
        return line_cache[cache_key]

    for route_id, route_groups in by_route.items():
        route = routes[route_id]
        max_speed = MAX_SPEED_MS.get(route["mode"], DEFAULT_MAX_SPEED_MS)
        fallback_speed = FALLBACK_SPEED_MS.get(route["mode"], DEFAULT_FALLBACK_SPEED_MS)

        # Sentido: se respeta direction_id si distingue a los patrones; si el
        # feed marca todo con el mismo valor, se separa por destino.
        declared = []
        for _, trip_ids in route_groups:
            votes = Counter(_int(trips[t].get("direction_id", "0")) for t in trip_ids)
            declared.append(votes.most_common(1)[0][0])
        headsigns = []
        for _, trip_ids in route_groups:
            votes = Counter(trips[t].get("trip_headsign", "") for t in trip_ids)
            headsigns.append(votes.most_common(1)[0][0])
        if len(set(declared)) > 1 or len(set(headsigns)) <= 1:
            directions = declared
        else:
            order = {name: index for index, name in enumerate(sorted(set(headsigns)))}
            directions = [order[name] for name in headsigns]

        built = []
        for (key, trip_ids), direction, headsign in zip(
            route_groups, directions, headsigns, strict=True
        ):
            _, shape_id, stop_ids = key
            coords = [stop_info[sid][1:] for sid in stop_ids]

            # Trazo: el feed trae trazos invertidos, intercambiados entre
            # sentidos o que se desvían de las paradas. Se elige el trazo de la
            # ruta que mejor se ajusta y se arma un trazo híbrido: sigue el
            # trazo oficial donde coincide con las paradas y une las paradas en
            # línea recta donde no.
            base, fit = get_line(shape_id, False), 0.0
            if base is not None:
                fit = _shape_fit(base, coords)
            reassigned = False
            if fit < SHAPE_FIT_GOOD:
                for candidate_id in route_shape_ids[route_id]:
                    for reverse in (False, True):
                        if candidate_id == shape_id and not reverse:
                            continue
                        candidate = get_line(candidate_id, reverse)
                        if candidate is None:
                            continue
                        candidate_fit = _shape_fit(candidate, coords)
                        if candidate_fit > fit + 0.05:
                            base, fit = candidate, candidate_fit
                            reassigned = True

            if base is None or fit < SHAPE_FIT_MIN:
                line = Polyline(coords)
                source = "paradas"
            else:
                points, straight = _hybrid_points(
                    base, coords, _assign_distances(base, coords)
                )
                line = Polyline(points)
                source = "mixto" if straight else "shape"
                if reassigned:
                    report.shapes_reassigned += 1
            report.patterns_by_source[source] = (
                report.patterns_by_source.get(source, 0) + 1
            )

            assigned = _assign_distances(line, coords)
            distances = [dist for dist, _ in assigned]
            far = sum(1 for _, offset in assigned if offset > FAR_FROM_SHAPE_M)
            report.stops_far_from_shape += far

            issues: list[str] = []
            if len(stop_ids) == 2:
                issues.append(ISSUE_ONLY_TERMINALS)
            if source == "paradas":
                issues.append(ISSUE_APPROXIMATE_SHAPE)
            if far >= max(2, 0.05 * len(stop_ids)):
                issues.append(ISSUE_STOPS_OFF_SHAPE)
            repeated = sum(1 for count in Counter(stop_ids).values() if count > 1)
            if repeated >= DOUBTFUL_REPEATED_STOPS:
                issues.append(ISSUE_DOUBTFUL)

            # Tiempos del viaje plantilla, relativos a la primera parada.
            times = [when for _, _, when in trip_stop_times[trip_ids[0]]]
            offsets, repaired = _repair_offsets(
                times, distances, max_speed, fallback_speed
            )
            report.segments_repaired += repaired
            offsets, odd = _credible_offsets(
                offsets, line.length_m, route["mode"], fallback_speed
            )
            if odd:
                report.odd_speed_patterns += 1
                report.speeds_corrected += 1
                issues.append(ISSUE_ESTIMATED_TIMES)
            run_time = offsets[-1]
            if run_time > 0 and line.length_m > 0:
                speeds_kmh.append(line.length_m / run_time * 3.6)
            for issue in issues:
                report.patterns_with_issues[issue] = (
                    report.patterns_with_issues.get(issue, 0) + 1
                )

            # Servicio: ventanas de frecuencia o salidas únicas.
            windows: set[tuple[str, int, int, int, int]] = set()
            first_departure = times[0] if times and times[0] is not None else 0
            for trip_id in trip_ids:
                service_id = trips[trip_id].get("service_id", "")
                trip_freq = trip_windows.get(trip_id)
                if trip_freq:
                    for start, end, headway, exact in trip_freq:
                        windows.add((service_id, start, end, headway, exact))
                else:
                    trip_times = sorted(trip_stop_times[trip_id])
                    start = (
                        trip_times[0][2]
                        if trip_times and trip_times[0][2] is not None
                        else first_departure
                    )
                    windows.add((service_id, start, start, 0, 1))
            runs_per_day = sum(
                (end - start) / headway if headway else 1.0
                for _, start, end, headway, _ in windows
            )
            if any(headway for _, _, _, headway, _ in windows):
                route["has_frequency"] = 1

            map_points = simplify(line.points, MAP_TOLERANCE_M)
            built.append(
                {
                    "direction": direction,
                    "headsign": headsign or route["short_name"],
                    "stop_ids": stop_ids,
                    "distances": distances,
                    "offsets": offsets,
                    "line": line,
                    "source": source,
                    "windows": sorted(windows),
                    "score": (runs_per_day, len(stop_ids)),
                    "map_polyline": encode_polyline(map_points),
                    "shape_id": shape_id,
                    "issues": issues,
                }
            )

        built.sort(
            key=lambda p: (
                p["direction"],
                -p["score"][0],
                -p["score"][1],
                p["headsign"],
                p["shape_id"],
            )
        )
        if frequency_based:
            _fill_service_gaps(built, service_days, report)
            if any(h for p in built for _, _, _, h, _ in p["windows"]):
                route["has_frequency"] = 1
        seen_directions: set[int] = set()
        for index, pattern in enumerate(built):
            pattern_id = f"{route_id}:{index}"
            is_main = pattern["direction"] not in seen_directions
            seen_directions.add(pattern["direction"])
            flat = array("d")
            for lat, lon in pattern["line"].points:
                flat.append(lat)
                flat.append(lon)
            pattern_rows.append(
                (
                    pattern_id,
                    route_id,
                    pattern["direction"],
                    pattern["headsign"],
                    pattern["line"].length_m,
                    pattern["offsets"][-1],
                    len(pattern["stop_ids"]),
                    int(is_main),
                    pattern["source"],
                    flat.tobytes(),
                    pattern["map_polyline"],
                    ",".join(pattern["issues"]),
                )
            )
            for seq, (sid, dist, offset) in enumerate(
                zip(
                    pattern["stop_ids"],
                    pattern["distances"],
                    pattern["offsets"],
                    strict=True,
                )
            ):
                pattern_stop_rows.append((pattern_id, seq, sid, dist, offset))
                route_stops[route_id].add(sid)
                used_stops.add(sid)
            for service_id, start, end, headway, exact in pattern["windows"]:
                service_rows.append(
                    (pattern_id, service_id, start, end, headway, exact)
                )

    # Rutas sin ningún patrón utilizable se descartan.
    report.routes_without_trips = sorted(
        rid for rid in routes if rid not in route_stops
    )
    catalog_only = [routes[rid] for rid in report.routes_without_trips]
    routes = {rid: route for rid, route in routes.items() if rid in route_stops}

    # ── Colores de visualización ─────────────────────────────────────────────
    # Tren, Macrobús y SiTren conservan su color de marca. Las rutas de
    # autobús comparten casi todas dos colores en el feed, así que cada familia
    # (T04A-1 y T04A-2 son la misma) recibe un color distinguible de la paleta.
    def family_of(route: dict[str, Any]) -> str:
        base = _BUS_CODE_RE.match(route["short_name"].upper())
        return base.group(1) if base else route["route_id"]

    def valid(color: str | None) -> bool:
        return bool(color) and re.fullmatch(r"[0-9A-F]{6}", color) is not None

    families_by_color: dict[str, set[str]] = defaultdict(set)
    for route in routes.values():
        if route["grp"] not in BRAND_GROUPS and valid(route["color"]):
            families_by_color[route["color"]].add(family_of(route))

    palette_index: dict[str, int] = {}
    for route in sorted(routes.values(), key=lambda r: r["sort_key"]):
        color = route["color"]
        is_brand = route["grp"] in BRAND_GROUPS
        if valid(color) and (is_brand or len(families_by_color[color]) == 1):
            route["display_color"] = f"#{color}"
            continue
        family = family_of(route)
        if family not in palette_index:
            palette_index[family] = len(palette_index) % len(DISPLAY_PALETTE)
        route["display_color"] = DISPLAY_PALETTE[palette_index[family]]

    # ── Alias de rutas ───────────────────────────────────────────────────────
    alias_rows: dict[tuple[str, str], tuple[str, int]] = {}

    def add_alias(text: str, route_id: str, kind: str, rank: int) -> None:
        alias = normalize(text)
        if alias and (alias, route_id) not in alias_rows:
            alias_rows[(alias, route_id)] = (kind, rank)

    for route in routes.values():
        rid = route["route_id"]
        add_alias(rid, rid, "id", 0)
        add_alias(route["short_name"], rid, "short", 1)
        if route["legacy_number"]:
            add_alias(route["legacy_number"], rid, "legacy", 2)
            add_alias(f"RG_{route['legacy_number']}", rid, "legacy", 2)
        base = _BUS_CODE_RE.match(route["short_name"].upper())
        if base and base.group(1) != route["short_name"].upper():
            add_alias(base.group(1), rid, "base", 3)

    # Rutas del catálogo sin viajes (complementarias como T15-C06, "231C"):
    # su clave y su número antiguo llevan a la troncal de la familia, y la
    # aplicación avisa que esa variante no tiene recorrido en el feed.
    catalog_rows: list[tuple[str, str, str, str]] = []
    for missing in catalog_only:
        family = family_of(missing)
        trunks = [r["route_id"] for r in routes.values() if r["short_name"] == family]
        if not trunks:
            trunks = [r["route_id"] for r in routes.values() if family_of(r) == family]
        names = {missing["short_name"], missing["route_id"]}
        if missing["legacy_number"]:
            names.add(missing["legacy_number"])
        for text in names:
            alias = normalize(text)
            if not alias:
                continue
            catalog_rows.append(
                (
                    alias,
                    missing["route_id"],
                    missing["short_name"],
                    missing["long_name"],
                )
            )
            for trunk in trunks:
                add_alias(text, trunk, "catalogo", CATALOG_ALIAS_RANK)
    catalog_rows = sorted(set(catalog_rows))
    report.catalog_aliases = len(catalog_rows)

    # ── Regiones ─────────────────────────────────────────────────────────────
    region_rows: list[tuple[str, str]] = []
    for region, zones in REGIONS.items():
        members = 0
        for rid in routes:
            if any(
                _within(stop_info[sid][1], stop_info[sid][2], lat, lon, radius)
                for sid in route_stops[rid]
                for _, lat, lon, radius in zones
            ):
                region_rows.append((rid, region))
                members += 1
        report.regions[region] = members

    # ── Calendario ───────────────────────────────────────────────────────────
    calendar_out = []
    for row in calendar_rows:
        if not row.get("service_id"):
            continue
        days = "".join("1" if row.get(day) == "1" else "0" for day in DAY_COLUMNS)
        calendar_out.append(
            (
                row["service_id"],
                days,
                row.get("start_date") or None,
                row.get("end_date") or None,
            )
        )
    starts = [c[2] for c in calendar_out if c[2]]
    ends = [c[3] for c in calendar_out if c[3]]
    report.feed_start = min(starts) if starts else ""
    report.feed_end = max(ends) if ends else ""

    # ── Lugares ──────────────────────────────────────────────────────────────
    landmark_rows = []
    if landmarks_path and landmarks_path.exists():
        payload = json.loads(landmarks_path.read_text(encoding="utf-8"))
        for item in payload.get("lugares", []):
            try:
                landmark_rows.append(
                    (
                        str(item["id"]),
                        str(item["nombre"]),
                        str(item.get("tipo", "lugar")),
                        float(item["lat"]),
                        float(item["lon"]),
                        json.dumps(
                            sorted(
                                {
                                    normalize(a)
                                    for a in [item["nombre"], *item.get("alias", [])]
                                    if a
                                }
                            ),
                            ensure_ascii=False,
                        ),
                    )
                )
            except (KeyError, TypeError, ValueError):
                report.warnings.append(
                    f"Lugar ignorado por datos incompletos: {item!r}"
                )

    # ── Totales ──────────────────────────────────────────────────────────────
    report.routes = len(routes)
    report.stops = len(used_stops)
    report.stops_without_service = len(stop_info) - len(used_stops)
    report.patterns = len(pattern_rows)
    report.pattern_stops = len(pattern_stop_rows)
    report.service_windows = len(service_rows)
    report.routes_without_frequency = sum(
        1 for r in routes.values() if not r["has_frequency"]
    )
    report.landmarks = len(landmark_rows)
    report.median_speed_kmh = round(median(speeds_kmh), 1) if speeds_kmh else 0.0

    # ── Escritura ────────────────────────────────────────────────────────────
    db_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = db_path.with_name(db_path.name + ".tmp")
    if tmp_path.exists():
        tmp_path.unlink()
    conn = sqlite3.connect(tmp_path)
    try:
        conn.executescript("PRAGMA journal_mode=OFF; PRAGMA synchronous=OFF;")
        conn.executescript(SCHEMA)
        conn.executemany(
            "INSERT INTO routes VALUES (:route_id, :agency_id, :short_name, :long_name, "
            ":route_type, :mode, :grp, :legacy_number, :branch_name, :color, :text_color, "
            ":display_color, :has_frequency, :sort_key)",
            list(routes.values()),
        )
        conn.executemany(
            "INSERT INTO route_alias VALUES (?, ?, ?, ?)",
            [
                (alias, rid, kind, rank)
                for (alias, rid), (kind, rank) in alias_rows.items()
            ],
        )
        conn.executemany("INSERT INTO route_region VALUES (?, ?)", region_rows)
        ordered_stops = sorted(used_stops)
        conn.executemany(
            "INSERT INTO stops VALUES (?, ?, ?, ?)",
            [(sid, *stop_info[sid]) for sid in ordered_stops],
        )
        conn.executemany(
            "INSERT INTO stop_rowid VALUES (?, ?)",
            [(index + 1, sid) for index, sid in enumerate(ordered_stops)],
        )
        conn.executemany(
            "INSERT INTO stop_fts (stop_id, name) VALUES (?, ?)",
            [(sid, stop_info[sid][0]) for sid in ordered_stops],
        )
        conn.executemany(
            "INSERT INTO stop_rtree VALUES (?, ?, ?, ?, ?)",
            [
                (
                    index + 1,
                    stop_info[sid][1],
                    stop_info[sid][1],
                    stop_info[sid][2],
                    stop_info[sid][2],
                )
                for index, sid in enumerate(ordered_stops)
            ],
        )
        conn.executemany(
            "INSERT INTO patterns VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            pattern_rows,
        )
        conn.executemany("INSERT INTO catalog_routes VALUES (?, ?, ?, ?)", catalog_rows)
        conn.executemany(
            "INSERT INTO pattern_stops VALUES (?, ?, ?, ?, ?)", pattern_stop_rows
        )
        conn.executemany(
            "INSERT INTO pattern_service VALUES (?, ?, ?, ?, ?, ?)", service_rows
        )
        conn.executemany("INSERT INTO calendar VALUES (?, ?, ?, ?)", calendar_out)
        conn.executemany(
            "INSERT INTO calendar_dates VALUES (?, ?, ?)",
            [
                (row["service_id"], row["date"], _int(row.get("exception_type", "0")))
                for row in calendar_date_rows
                if row.get("service_id") and row.get("date")
            ],
        )
        conn.executemany(
            "INSERT OR IGNORE INTO fares VALUES (?, ?, ?, ?, ?)",
            [
                (
                    row.get("fare_id"),
                    row.get("agency_id") or None,
                    float(row["price"]) if row.get("price") else None,
                    row.get("currency_type") or None,
                    _int(row.get("transfers", ""), -1),
                )
                for row in fare_rows
                if row.get("fare_id")
            ],
        )
        conn.executemany(
            "INSERT OR REPLACE INTO landmarks VALUES (?, ?, ?, ?, ?, ?)", landmark_rows
        )

        report.seconds = round(time.perf_counter() - started, 2)
        meta = {
            "schema_version": str(SCHEMA_VERSION),
            "source_signature": _source_signature(zip_path, landmarks_path),
            "source_name": zip_path.name,
            "built_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "feed_start": report.feed_start,
            "feed_end": report.feed_end,
            "report": json.dumps(report.to_dict(), ensure_ascii=False),
        }
        conn.executemany("INSERT INTO meta VALUES (?, ?)", list(meta.items()))
        conn.commit()
    finally:
        conn.close()
    os.replace(tmp_path, db_path)
    return report


def _fill_service_gaps(
    built: list[dict[str, Any]], service_days: dict[str, str], report: BuildReport
) -> None:
    """
    En un feed por frecuencias, un viaje sin filas en frequencies.txt es un
    hueco de datos, no un viaje único. Para ese tipo de día se toma la
    frecuencia del sentido contrario de la misma ruta o, si no existe, la del
    tipo de día más parecido del mismo patrón. Si no hay de dónde tomarla, el
    tipo de día queda sin servicio en lugar de inventar una salida.
    """

    def similarity_gap(target: str, other: str) -> int:
        if len(target) != 7 or len(other) != 7:
            return 7
        return sum(a != b for a, b in zip(target, other, strict=True))

    for pattern in built:
        by_service: dict[str, list[tuple]] = defaultdict(list)
        for window in pattern["windows"]:
            by_service[window[0]].append(window)
        changed = False
        for service_id, windows in list(by_service.items()):
            if any(window[3] > 0 for window in windows):
                # Con frecuencia publicada, las plantillas sueltas del mismo
                # tipo de día no son salidas reales: se descartan.
                frequent = [window for window in windows if window[3] > 0]
                if len(frequent) != len(windows):
                    by_service[service_id] = frequent
                    changed = True
                continue
            donor: list[tuple] = []
            for other in built:
                if other is not pattern:
                    donor = [
                        w for w in other["windows"] if w[0] == service_id and w[3] > 0
                    ]
                    if donor:
                        break
            if not donor:
                options = [
                    (sid, ws)
                    for sid, ws in by_service.items()
                    if sid != service_id and any(w[3] > 0 for w in ws)
                ]
                if options:
                    target = service_days.get(service_id, "")
                    _, closest = min(
                        options,
                        key=lambda item: similarity_gap(
                            target, service_days.get(item[0], "")
                        ),
                    )
                    donor = [w for w in closest if w[3] > 0]
            if donor:
                by_service[service_id] = [
                    (service_id, start, end, headway, 0)
                    for _, start, end, headway, _ in donor
                ]
                report.service_gaps_filled += 1
            else:
                del by_service[service_id]
                report.service_gaps_dropped += 1
            changed = True
        if changed:
            pattern["windows"] = sorted(w for ws in by_service.values() for w in ws)


def _planar_m(lat_a: float, lon_a: float, lat_b: float, lon_b: float) -> float:
    """Distancia aproximada en metros entre dos puntos cercanos."""
    dy = (lat_b - lat_a) * METERS_PER_DEGREE
    dx = (lon_b - lon_a) * METERS_PER_DEGREE * 0.936  # cos(20.6°)
    return (dx * dx + dy * dy) ** 0.5


def _assign_distances(
    line: Polyline, coords: list[tuple[float, float]]
) -> list[tuple[float, float]]:
    """
    (distancia sobre el trazo, separación lateral) de cada parada, en orden.

    Una ruta puede pasar dos veces cerca de la misma parada (vueltas en "gota",
    ida y regreso por la misma calle). Elegir siempre la pasada más cercana
    puede saltarse un tramo entero y dejar a las paradas siguientes sin lugar
    en el trazo. Por eso se consideran todas las pasadas de cada parada y se
    elige la secuencia que avanza en orden con menor costo: separación lateral
    más el exceso de recorrido sobre lo razonable entre paradas consecutivas.
    """
    n = len(coords)
    if n == 0:
        return []
    total = line.length_m

    def passes(
        lat: float, lon: float, from_m: float, window_m: float | None
    ) -> list[tuple[float, float]]:
        found = line.candidates(lat, lon, CANDIDATE_RADIUS_M, from_m, window_m)
        if not found:
            found = [line.project(lat, lon, from_m, window_m)]
        return found

    # Programación dinámica sobre las pasadas candidatas. Las de cada parada
    # se buscan a partir de las de la anterior, en una ventana amplia.
    options: list[list[tuple[float, float]]] = [passes(*coords[0], 0.0, None)]
    costs: list[list[float]] = [[offset for _, offset in options[0]]]
    back: list[list[int]] = [[-1] * len(options[0])]
    for i in range(1, n):
        crow = _planar_m(*coords[i - 1], *coords[i])
        reasonable = 2.0 * crow + 300.0
        alive = [
            d
            for (d, _), c in zip(options[i - 1], costs[i - 1], strict=True)
            if c < float("inf")
        ]
        start = max(0.0, min(alive) - 30.0)
        window = max(alive) - start + 4.0 * crow + 3000.0
        options.append(passes(*coords[i], start, window))
        row_cost: list[float] = []
        row_back: list[int] = []
        for dist, offset in options[i]:
            best, best_k = float("inf"), -1
            for k, (prev_dist, _) in enumerate(options[i - 1]):
                prev_cost = costs[i - 1][k]
                if prev_cost == float("inf") or dist < prev_dist - 30.0:
                    continue
                excess = max(0.0, (dist - prev_dist) - reasonable)
                cost = prev_cost + offset + 0.5 * excess
                if cost < best:
                    best, best_k = cost, k
            row_cost.append(best)
            row_back.append(best_k)
        if all(cost == float("inf") for cost in row_cost):
            # Ninguna pasada avanza en orden: la parada se ubica donde quedó la
            # anterior (la más barata) y la secuencia continúa desde ahí.
            k = min(range(len(costs[i - 1])), key=costs[i - 1].__getitem__)
            dist = min(options[i - 1][k][0], total)
            point = line.point_at(dist)
            offset = _planar_m(point[0], point[1], *coords[i])
            options[i] = [(dist, offset)]
            row_cost, row_back = [costs[i - 1][k] + offset], [k]
        costs.append(row_cost)
        back.append(row_back)

    k = min(range(len(costs[-1])), key=costs[-1].__getitem__)
    assigned: list[tuple[float, float]] = [(0.0, 0.0)] * n
    for i in range(n - 1, -1, -1):
        assigned[i] = options[i][k]
        k = back[i][k]
    # Distancias no decrecientes (la tolerancia de 30 m permite pequeños retrocesos).
    floor = 0.0
    for i, (dist, offset) in enumerate(assigned):
        floor = max(floor, dist)
        assigned[i] = (floor, offset)
    return assigned


def _hybrid_points(
    line: Polyline,
    coords: list[tuple[float, float]],
    assigned: list[tuple[float, float]],
) -> tuple[list[tuple[float, float]], int]:
    """
    Trazo que pasa por todas las paradas: usa el tramo del trazo oficial entre
    dos paradas consecutivas cuando ambas están sobre él y el tramo es
    coherente con la distancia entre ellas; si no, las une en línea recta.
    Devuelve (puntos, número de tramos rectos).
    """
    near = [offset <= NEAR_SHAPE_M for _, offset in assigned]

    def anchor(index: int) -> tuple[float, float]:
        return line.point_at(assigned[index][0]) if near[index] else coords[index]

    points = [anchor(0)]
    straight = 0
    for i in range(len(coords) - 1):
        crow = _planar_m(coords[i][0], coords[i][1], coords[i + 1][0], coords[i + 1][1])
        gap = assigned[i + 1][0] - assigned[i][0]
        if near[i] and near[i + 1] and gap <= 3.0 * crow + 300.0:
            points.extend(line.slice(assigned[i][0], assigned[i + 1][0])[1:])
        else:
            points.append(anchor(i + 1))
            straight += 1
    return points, straight


def _shape_fit(line: Polyline, coords: list[tuple[float, float]]) -> float:
    """
    Qué tan bien describe un trazo a una secuencia de paradas (0 a 1): fracción
    de paradas cerca del trazo por fracción de paradas que avanzan en orden.
    Se evalúa sobre una muestra de paradas para que sea rápido.
    """
    if len(coords) > SHAPE_FIT_SAMPLE:
        step = (len(coords) - 1) / (SHAPE_FIT_SAMPLE - 1)
        coords = [coords[round(i * step)] for i in range(SHAPE_FIT_SAMPLE)]
    projected = [line.project(lat, lon) for lat, lon in coords]
    near = sum(1 for _, offset in projected if offset <= 60.0) / len(projected)
    pairs = list(zip(projected, projected[1:], strict=False))
    in_order = (
        sum(1 for a, b in pairs if b[0] >= a[0] - 30.0) / len(pairs) if pairs else 1.0
    )
    return near * in_order


def _within(
    lat: float, lon: float, zone_lat: float, zone_lon: float, radius_m: float
) -> bool:
    dy = (lat - zone_lat) * METERS_PER_DEGREE
    if abs(dy) > radius_m:
        return False
    dx = (lon - zone_lon) * METERS_PER_DEGREE * 0.936  # cos(20.6°)
    return dx * dx + dy * dy <= radius_m * radius_m


def _is_twin_stop(
    previous: tuple[int, str, int | None],
    current: tuple[int, str, int | None],
    stop_info: dict[str, tuple[str, float, float]],
    normalize: Any,
) -> bool:
    """¿`current` es la misma estación que `previous`, repetida por el feed?"""
    name_a, lat_a, lon_a = stop_info[previous[1]]
    name_b, lat_b, lon_b = stop_info[current[1]]
    if previous[1] == current[1]:
        return True
    if normalize(name_a) != normalize(name_b):
        return False
    if _planar_m(lat_a, lon_a, lat_b, lon_b) > TWIN_STOP_M:
        return False
    if previous[2] is None or current[2] is None:
        return False
    return 0 <= current[2] - previous[2] <= TWIN_STOP_MAX_GAP_S


def _credible_offsets(
    offsets: list[float], length_m: float, mode: str, fallback_speed: float
) -> tuple[list[float], bool]:
    """
    Si la velocidad comercial del recorrido completo no es creíble, los tiempos
    del feed son de relleno: se reescalan (conservando la forma del perfil) a la
    velocidad de respaldo, si eran demasiado lentos, o al máximo creíble, si
    eran demasiado rápidos. Devuelve (tiempos, se corrigieron).
    """
    run_time = offsets[-1] if offsets else 0.0
    if run_time <= 0 or length_m <= 0:
        return offsets, False
    low, high = CREDIBLE_SPEED_KMH.get(mode, DEFAULT_CREDIBLE_SPEED_KMH)
    speed = length_m / run_time * 3.6
    if low <= speed <= high:
        return offsets, False
    target = fallback_speed * 3.6 if speed < low else high
    factor = speed / target
    return [offset * factor for offset in offsets], True


def _repair_offsets(
    times: list[int | None],
    distances: list[float],
    max_speed: float,
    fallback_speed: float,
) -> tuple[list[float], int]:
    """
    Tiempos acumulados (s) desde la primera parada, corrigiendo tramos imposibles.

    Un tramo es inválido si no tiene tiempo, si el tiempo no avanza aunque la
    distancia sí, o si exige más velocidad que `max_speed`. Los tramos inválidos
    se rellenan con la velocidad media de los tramos válidos del mismo patrón.
    """
    n = len(distances)
    if n == 0:
        return [], 0
    valid: list[float | None] = []
    good_time = good_dist = 0.0
    for i in range(n - 1):
        a, b = times[i] if i < len(times) else None, (
            times[i + 1] if i + 1 < len(times) else None
        )
        gap = distances[i + 1] - distances[i]
        if a is None or b is None:
            valid.append(None)
            continue
        dt = b - a
        if dt < 0 or (dt == 0 and gap > 1.0) or (dt > 0 and gap / dt > max_speed):
            valid.append(None)
            continue
        valid.append(float(dt))
        good_time += dt
        good_dist += gap
    speed = good_dist / good_time if good_time > 0 and good_dist > 0 else fallback_speed
    speed = min(max(speed, 1.5), max_speed)

    offsets = [0.0]
    repaired = 0
    for i in range(n - 1):
        dt = valid[i]
        if dt is None:
            dt = (distances[i + 1] - distances[i]) / speed
            repaired += 1
        offsets.append(offsets[-1] + dt)
    return offsets, repaired
