from __future__ import annotations

import logging
from dataclasses import dataclass

from .gtfs_loader import GTFSLoader, _Departure
from .models import Route, Stop, haversine_km

logger = logging.getLogger(__name__)

REGION_ZONES: tuple[tuple[float, float, float], ...] = (
    (20.582, -103.446, 5.5),
    (20.624, -103.245, 4.0),
    (20.598, -103.430, 3.5),
)

OVERLAY_SERVICE_ID = "RG_WD"
OVERLAY_AGENCY = "MI_TRANSPORTE_RG"

# IDs históricos a purgar al actualizar el overlay.
LEGACY_OVERLAY_PREFIXES = ("TON_", "CUCEI_", "TON_R", "CUCEI_C", "CUCEI_2", "CUCEI_5")


@dataclass(frozen=True, slots=True)
class _OverlayStop:
    stop_id: str
    name: str
    lat: float
    lon: float


@dataclass(frozen=True, slots=True)
class _OverlayRoute:
    route_id: str
    short_name: str
    long_name: str
    stops: tuple[_OverlayStop, ...]
    """Waypoints (lat, lon) siguiendo arteriales — evita líneas rectas entre paradas."""
    path_waypoints: tuple[tuple[float, float], ...]
    headway_minutes: int = 14
    route_type: int = 3


# Paradas y waypoints alineados al corredor real (T-04-A / T11-B / T15 / Tonalá).
# path_waypoints sigue arteriales; las paradas son solo puntos sobre ese trazo.
OVERLAY_ROUTES: tuple[_OverlayRoute, ...] = (
    _OverlayRoute(
        route_id="RG_55",
        short_name="55",
        long_name="T11-B Cordilleras - Minerva - Federalismo - Abastos - CUCEI",
        stops=(
            # Corredor real T11-B: Av. Vallarta → Glorieta Minerva → Federalismo
            # → 8 de Julio → Mercado de Abastos → Calz. Lázaro Cárdenas
            # → Av. López Mateos → CUCEI.
            # Plaza del Sol fue eliminada — T11-B no pasa por ese punto.
            _OverlayStop("R55_S01", "Plaza Cordilleras",             20.7048, -103.4615),
            _OverlayStop("R55_S02", "Glorieta Minerva",              20.6743, -103.3893),
            _OverlayStop("R55_S03", "Ávila Camacho / Federalismo",   20.6785, -103.3920),
            _OverlayStop("R55_S04", "8 de Julio",                    20.6528, -103.3625),
            _OverlayStop("R55_S05", "Mercado de Abastos",            20.6594, -103.3252),
            _OverlayStop("R55_S06", "Calz. Lázaro Cárdenas",        20.6390, -103.3510),
            _OverlayStop("R55_S07", "Av. López Mateos / Dr. Atl",    20.6100, -103.3900),
            _OverlayStop("R55_S08", "López Mateos Sur",              20.5808, -103.4483),
            _OverlayStop("R55_S09", "CUCEI",                         20.5821, -103.4466),
        ),
        path_waypoints=(
            # Av. Vallarta hacia el oriente hasta Minerva
            (20.7048, -103.4615),
            (20.6980, -103.4530),
            (20.6900, -103.4430),
            (20.6820, -103.4200),
            (20.6743, -103.3893),
            # Bajada por Federalismo / Eje Central hacia el sur
            (20.6785, -103.3920),
            (20.6700, -103.3860),
            (20.6620, -103.3750),
            (20.6528, -103.3625),
            # Rumbo a Abastos por Calz. Independencia
            (20.6570, -103.3430),
            (20.6594, -103.3252),
            # Calz. Lázaro Cárdenas hacia el sur-oriente
            (20.6480, -103.3380),
            (20.6390, -103.3510),
            # Av. López Mateos hacia el poniente
            (20.6280, -103.3680),
            (20.6100, -103.3900),
            (20.5950, -103.4150),
            (20.5808, -103.4483),
            (20.5821, -103.4466),
        ),
        headway_minutes=18,
    ),
    _OverlayRoute(
        route_id="RG_231",
        short_name="231",
        long_name="T15 Prolongación Alcalde - Calzada Revolución - CUCEI",
        stops=(
            _OverlayStop("R231_S01", "Periférico Norte (Tren Ligero)", 20.7455, -103.3502),
            _OverlayStop("R231_S02", "Prolongación Alcalde", 20.7287, -103.3466),
            _OverlayStop("R231_S03", "La Normal", 20.7120, -103.3440),
            _OverlayStop("R231_S04", "Alcalde y Federalismo", 20.6935, -103.3484),
            _OverlayStop("R231_S05", "Colón", 20.6731, -103.3800),
            _OverlayStop("R231_S06", "Calzada Revolución", 20.6380, -103.4120),
            _OverlayStop("R231_S07", "García Barragán", 20.5985, -103.4380),
            _OverlayStop("R231_S08", "CUCEI", 20.5821, -103.4466),
            _OverlayStop("R231_S09", "Valle del Silicio", 20.5757, -103.4427),
        ),
        path_waypoints=(
            (20.7455, -103.3502),
            (20.7380, -103.3485),
            (20.7287, -103.3466),
            (20.7180, -103.3450),
            (20.7120, -103.3440),
            (20.7020, -103.3460),
            (20.6935, -103.3484),
            (20.6850, -103.3550),
            (20.6731, -103.3800),
            (20.6600, -103.3950),
            (20.6450, -103.4050),
            (20.6380, -103.4120),
            (20.6200, -103.4250),
            (20.6050, -103.4350),
            (20.5985, -103.4380),
            (20.5900, -103.4420),
            (20.5821, -103.4466),
            (20.5780, -103.4440),
            (20.5757, -103.4427),
        ),
        headway_minutes=12,
    ),
    _OverlayRoute(
        route_id="RG_646",
        short_name="646",
        long_name="T-04-A Loma Dorada - Lázaro Cárdenas - López Mateos - CUCEI",
        stops=(
            _OverlayStop("R646_S01", "Loma Dorada (base)",                20.6182, -103.2518),
            _OverlayStop("R646_S02", "Río Nilo",                          20.6080, -103.2580),
            _OverlayStop("R646_S03", "Malecón",                           20.6125, -103.2780),
            _OverlayStop("R646_S04", "Av. Patria (Tonalá)",               20.6146, -103.3192),
            # Corrección: Lázaro Cárdenas en lon=-103.3280 (al oeste de Av. Patria,
            # evita el backtrack este-oeste que generaba zigzag en el mapa).
            _OverlayStop("R646_S05", "Calz. Lázaro Cárdenas",            20.6300, -103.3280),
            _OverlayStop("R646_S06", "Macrobús Lázaro Cárdenas",         20.6320, -103.3180),
            _OverlayStop("R646_S07", "Clínica 46 IMSS",                  20.6348, -103.3275),
            _OverlayStop("R646_S07B", "Av. López Mateos (Tlaquepaque)",  20.6150, -103.3950),
            _OverlayStop("R646_S08", "Unidad Deportiva López Mateos",    20.5980, -103.4370),
            _OverlayStop("R646_S09", "CUCEI (López Mateos Sur)",         20.5821, -103.4466),
        ),
        path_waypoints=(
            (20.6182, -103.2518),
            (20.6140, -103.2540),
            (20.6080, -103.2580),
            (20.6095, -103.2680),
            (20.6125, -103.2780),
            (20.6135, -103.2950),
            (20.6146, -103.3192),
            # Waypoint corregido: flujo continuo hacia el noroeste sin retroceder
            (20.6220, -103.3240),
            (20.6300, -103.3280),
            (20.6320, -103.3180),
            (20.6348, -103.3275),
            (20.6280, -103.3500),
            (20.6150, -103.3950),
            (20.6020, -103.4100),
            (20.5980, -103.4370),
            (20.5870, -103.4410),
            (20.5821, -103.4466),
        ),
        headway_minutes=8,
    ),
    _OverlayRoute(
        route_id="RG_615",
        short_name="615",
        long_name="Tonalá Centro - Zapotlanejo - López Mateos - CUCEI",
        stops=(
            _OverlayStop("R615_S01", "Tonaltecas", 20.6270, -103.2455),
            _OverlayStop("R615_S02", "16 de Septiembre", 20.6225, -103.2471),
            _OverlayStop("R615_S03", "Zaragoza", 20.6241, -103.2468),
            _OverlayStop("R615_S04", "Av. Zapotlanejo", 20.6277, -103.2906),
            _OverlayStop("R615_S05", "Calzada Lázaro Cárdenas", 20.6271, -103.3013),
            _OverlayStop("R615_S05B", "Av. López Mateos (Tonalá)", 20.6100, -103.3500),
            _OverlayStop("R615_S05C", "Puente Matute Remus Sur", 20.5950, -103.4000),
            _OverlayStop("R615_S06", "López Mateos Sur", 20.5808, -103.4483),
            _OverlayStop("R615_S07", "CUCEI", 20.5821, -103.4466),
        ),
        path_waypoints=(
            (20.6270, -103.2455),
            (20.6250, -103.2460),
            (20.6225, -103.2471),
            (20.6241, -103.2468),
            (20.6260, -103.2650),
            (20.6277, -103.2906),
            (20.6271, -103.3013),
            (20.6200, -103.3200),
            (20.6100, -103.3500),
            (20.5950, -103.4000),
            (20.5880, -103.4150),
            (20.5850, -103.4200),
            (20.5808, -103.4483),
            (20.5821, -103.4466),
        ),
        headway_minutes=14,
    ),
)

CURRENT_OVERLAY_IDS = frozenset(r.route_id for r in OVERLAY_ROUTES)


def _max_stop_leg_km(stops: tuple[_OverlayStop, ...]) -> float:
    """Distancia máxima entre paradas consecutivas (detecta saltos fuera de corredor)."""
    if len(stops) < 2:
        return 0.0
    worst = 0.0
    for index, stop in enumerate(stops[:-1]):
        nxt = stops[index + 1]
        leg = haversine_km(stop.lat, stop.lon, nxt.lat, nxt.lon)
        worst = max(worst, leg)
    return worst


def _densify_path(
    points: list[tuple[float, float]], segments_per_leg: int = 8
) -> list[tuple[float, float]]:
    if len(points) < 2:
        return points
    dense: list[tuple[float, float]] = []
    for index, (lat_a, lon_a) in enumerate(points[:-1]):
        lat_b, lon_b = points[index + 1]
        dense.append((lat_a, lon_a))
        for step in range(1, segments_per_leg):
            t = step / segments_per_leg
            dense.append((lat_a + (lat_b - lat_a) * t, lon_a + (lon_b - lon_a) * t))
    dense.append(points[-1])
    return dense


def _time_str(total_minutes: int) -> str:
    hours, minutes = divmod(total_minutes, 60)
    return f"{hours:02d}:{minutes:02d}:00"


def _remove_route(gtfs: GTFSLoader, route_id: str) -> None:
    if route_id not in gtfs._routes:
        return

    route = gtfs._routes.pop(route_id)
    for key in list(gtfs._routes_by_short.keys()):
        if gtfs._routes_by_short.get(key) is route:
            del gtfs._routes_by_short[key]

    stop_ids = gtfs._route_stops.pop(route_id, [])
    for stop_id in stop_ids:
        if stop_id in gtfs._stop_routes:
            gtfs._stop_routes[stop_id].discard(route_id)
            if not gtfs._stop_routes[stop_id]:
                del gtfs._stop_routes[stop_id]

    shape_id = gtfs._route_shape_id.pop(route_id, None)
    if shape_id:
        gtfs._shape_points.pop(shape_id, None)

    gtfs._trips_by_route.pop(route_id, None)
    for key in list(gtfs._departures_by_route_stop.keys()):
        if key[0] == route_id:
            del gtfs._departures_by_route_stop[key]


def _purge_legacy_overlays(gtfs: GTFSLoader) -> None:
    to_remove = [
        rid
        for rid in list(gtfs._routes.keys())
        if rid in CURRENT_OVERLAY_IDS
        or rid.startswith(LEGACY_OVERLAY_PREFIXES)
        or gtfs._routes[rid].agency_id == OVERLAY_AGENCY
    ]
    for route_id in to_remove:
        _remove_route(gtfs, route_id)


# Tiempos de viaje entre paradas consecutivas (en minutos) para cada ruta del overlay.
# Se mide parada-a-parada siguiendo el corredor real en condiciones normales.
# Total T11-B (~57 min), T15 (~56 min), T-04-A (~48 min), 615 (~41 min).
# Si una ruta no aparece aquí, se usa el promedio uniforme como fallback.
ROUTE_STOP_TRAVEL_MINUTES: dict[str, tuple[int, ...]] = {
    # T11-B: Cordilleras→Minerva→Federalismo→8Jul→Abastos→LázCárd→LMAteos→LMSur→CUCEI
    "RG_55":  (12, 3, 8, 8, 6, 10, 8, 2),   # 57 min total, 9 paradas
    # T15: PeriféNorte→ProlAlcalde→LaNormal→Alcalde→Colón→CalzRev→GarcíaBarr→CUCEI→Valle
    "RG_231": (4, 4, 5, 8, 10, 12, 8, 5),    # 56 min total, 9 paradas
    # T-04-A: LomaDor→RíoNilo→Malecón→Patria→LázCárd→Macrobús→Clín46→LMTlaq→UnidDep→CUCEI
    "RG_646": (3, 4, 8, 4, 3, 2, 10, 8, 6),  # 48 min total, 10 paradas
    # 615: Tonaltecas→16Sep→Zaragoza→Zapotlanejo→LázCárd→LMTonalá→PuenteMatute→LMSur→CUCEI
    "RG_615": (2, 2, 8, 6, 6, 8, 5, 4),      # 41 min total, 9 paradas
}


def _inject_route(gtfs: GTFSLoader, definition: _OverlayRoute) -> None:
    route = Route(
        route_id=definition.route_id,
        route_short_name=definition.short_name,
        route_long_name=definition.long_name,
        route_type=definition.route_type,
        agency_id=OVERLAY_AGENCY,
    )
    gtfs._routes[route.route_id] = route
    gtfs._routes_by_short[gtfs._normalize(route.route_short_name)] = route
    gtfs._routes_by_short[gtfs._normalize(route.route_id)] = route

    stop_ids: list[str] = []
    for stop_def in definition.stops:
        stop = Stop(
            stop_id=stop_def.stop_id,
            stop_name=stop_def.name,
            lat=stop_def.lat,
            lon=stop_def.lon,
        )
        gtfs._stops[stop.stop_id] = stop
        for token in gtfs._normalize(stop.stop_name).split():
            if len(token) >= 3:
                bucket = gtfs._stop_token_index.setdefault(token, [])
                if stop.stop_id not in bucket:
                    bucket.append(stop.stop_id)
        stop_ids.append(stop.stop_id)
        gtfs._stop_routes.setdefault(stop.stop_id, set()).add(route.route_id)

    gtfs._route_stops[route.route_id] = stop_ids
    shape_id = f"SHAPE_{definition.route_id}"
    path_pts = (
        list(definition.path_waypoints)
        if definition.path_waypoints
        else [(s.lat, s.lon) for s in definition.stops]
    )
    from .route_geometry import fetch_road_path

    road_path = fetch_road_path(path_pts, route_id=definition.route_id)
    if road_path:
        path_pts = road_path
    gtfs._shape_points[shape_id] = _densify_path(path_pts, segments_per_leg=3)
    gtfs._route_shape_id[route.route_id] = shape_id

    trip_prefix = f"TRIP_{definition.route_id}"
    gtfs._trips_by_route.setdefault(route.route_id, set())

    # Tiempos acumulados por parada: usa el dict calibrado o fallback uniforme.
    leg_times = ROUTE_STOP_TRAVEL_MINUTES.get(definition.route_id, ())
    n_stops = len(stop_ids)
    if not leg_times and n_stops > 1:
        # Fallback: distribuir uniformemente asumiendo ~5 min/parada promedio
        uniform = max(3, round(300 / max(n_stops - 1, 1)))
        leg_times = tuple(uniform for _ in range(n_stops - 1))

    # cumulative_offsets[i] = minutos desde la terminal hasta la parada i
    cumulative_offsets: list[int] = [0]
    for leg in leg_times:
        cumulative_offsets.append(cumulative_offsets[-1] + leg)
    # Si hay más paradas que tiempos definidos, extender con el último valor
    while len(cumulative_offsets) < n_stops:
        cumulative_offsets.append(cumulative_offsets[-1] + (leg_times[-1] if leg_times else 5))

    start_min = 5 * 60
    end_min = 22 * 60
    trip_seq = 0
    for depart_min in range(start_min, end_min, definition.headway_minutes):
        current_trip = f"{trip_prefix}_{trip_seq:03d}"
        trip_seq += 1
        gtfs._trips_by_route[route.route_id].add(current_trip)
        for seq, stop_id in enumerate(stop_ids, start=1):
            arrival = depart_min + cumulative_offsets[seq - 1]
            time_label = _time_str(arrival)
            departure = _Departure(
                trip_id=current_trip,
                route_id=route.route_id,
                service_id=OVERLAY_SERVICE_ID,
                stop_id=stop_id,
                departure_minutes=arrival,
                departure_seconds=float(arrival * 60),
                departure_time=time_label,
                stop_sequence=seq,
            )
            gtfs._departures_by_route_stop.setdefault(
                (route.route_id, stop_id), []
            ).append(departure)

    for items in gtfs._departures_by_route_stop.values():
        if items:
            items.sort(key=lambda dep: (dep.departure_minutes, dep.stop_sequence))


def apply_regional_overlay(gtfs: GTFSLoader) -> int:
    gtfs._assert_loaded()
    _purge_legacy_overlays(gtfs)

    if OVERLAY_SERVICE_ID not in gtfs._service_calendars:
        gtfs._service_calendars[OVERLAY_SERVICE_ID] = {
            "service_id": OVERLAY_SERVICE_ID,
            "monday": "1",
            "tuesday": "1",
            "wednesday": "1",
            "thursday": "1",
            "friday": "1",
            "saturday": "1",
            "sunday": "1",
            "start_date": "20250101",
            "end_date": "20301231",
        }

    for definition in OVERLAY_ROUTES:
        _inject_route(gtfs, definition)

    logger.info(
        "Regional overlay: rutas %s activas (55, 231, 646, 615).",
        ", ".join(r.short_name for r in OVERLAY_ROUTES),
    )
    return len(OVERLAY_ROUTES)


def _near_any_zone(lat: float, lon: float) -> bool:
    for clat, clon, radius in REGION_ZONES:
        if haversine_km(clat, clon, lat, lon) <= radius:
            return True
    return False


def route_serves_tonala_cucei(gtfs: GTFSLoader, route_id: str) -> bool:
    if route_id in CURRENT_OVERLAY_IDS:
        return True

    if route_id in {"MP-A04", "MP-A05-2", "MP-C01", "ST_L2", "MT_L3"}:
        return True

    path = gtfs.get_route_path(route_id)
    if path:
        for lat, lon in path:
            if _near_any_zone(lat, lon):
                return True

    for stop in gtfs.get_ordered_stops_for_route(route_id):
        if _near_any_zone(stop.lat, stop.lon):
            return True
    return False


def regional_route_ids(gtfs: GTFSLoader) -> set[str]:
    ids: set[str] = set()
    for route in gtfs.get_routes():
        if route_serves_tonala_cucei(gtfs, route.route_id):
            ids.add(route.route_id)
    return ids
