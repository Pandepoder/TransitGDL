"""Pruebas de la base de transporte: geometría, construcción y consultas."""

from __future__ import annotations

import zipfile
from datetime import date, datetime

import pytest

from core.geometry import Polyline, decode_polyline, encode_polyline, simplify
from core.models import haversine_km
from core.timezone_utils import GDL_TZ
from core.transit import TransitStore, build_transit_db, needs_rebuild
from core.transit.builder import (
    ISSUE_APPROXIMATE_SHAPE,
    ISSUE_ESTIMATED_TIMES,
    ISSUE_ONLY_TERMINALS,
    _assign_distances,
    _repair_offsets,
    classify_route,
    clean_stop_name,
)
from tests.conftest import FIXTURE_GTFS, LANDMARKS

# ── Geometría ────────────────────────────────────────────────────────────────

# Una "L": 1 km al norte y luego ~1 km al este (a la latitud de Guadalajara).
ELE = [(20.6000, -103.3000), (20.6090, -103.3000), (20.6090, -103.2904)]


def test_polyline_length_matches_haversine():
    line = Polyline(ELE)
    expected = (haversine_km(*ELE[0], *ELE[1]) + haversine_km(*ELE[1], *ELE[2])) * 1000
    assert line.length_m == pytest.approx(expected, rel=0.002)


def test_project_returns_distance_along_and_lateral_offset():
    line = Polyline(ELE)
    # Un punto 50 m al este del primer tramo, a media altura.
    dist, offset = line.project(20.6045, -103.29952)
    assert dist == pytest.approx(500, abs=5)
    assert offset == pytest.approx(50, abs=3)


def test_project_searches_forward_only():
    """En un trazo de ida y vuelta, cada parada cae en la pasada que le toca."""
    ida_y_vuelta = [(20.60, -103.30), (20.61, -103.30), (20.60, -103.30)]
    line = Polyline(ida_y_vuelta)
    dist_ida, _ = line.project(20.605, -103.30)
    dist_vuelta, _ = line.project(20.605, -103.30, from_m=line.length_m / 2)

    assert dist_ida == pytest.approx(line.length_m * 0.25, rel=0.01)
    assert dist_vuelta == pytest.approx(line.length_m * 0.75, rel=0.01)


def test_project_does_not_hide_points_behind_the_start():
    """Un punto que quedó atrás no se reporta como si estuviera sobre el trazo."""
    line = Polyline([(20.60, -103.30), (20.61, -103.30)])
    dist, offset = line.project(20.602, -103.30, from_m=500)
    assert dist == pytest.approx(500, abs=1)
    assert offset == pytest.approx(278, abs=5)


def test_candidates_returns_every_pass():
    ida_y_vuelta = Polyline([(20.60, -103.30), (20.61, -103.30), (20.60, -103.30)])
    passes = ida_y_vuelta.candidates(20.605, -103.3001, radius_m=50)
    assert [round(d / ida_y_vuelta.length_m, 2) for d, _ in passes] == [0.25, 0.75]
    assert all(offset < 15 for _, offset in passes)
    assert ida_y_vuelta.candidates(20.605, -103.31, radius_m=50) == []


def test_stops_follow_a_loop_instead_of_skipping_it():
    """
    Ruta en "gota": entra por el sur, da una vuelta y sale al oeste. La parada
    de la entrada queda más cerca de la segunda pasada que de la primera; elegir
    siempre la más cercana se saltaría la vuelta entera (caso C123-V1).
    """
    line = Polyline(
        [
            (20.600, -103.300),
            (20.610, -103.300),  # cruce
            (20.610, -103.290),
            (20.620, -103.290),
            (20.620, -103.300),
            (20.6102, -103.300),
            (20.6102, -103.310),  # salida
        ]
    )
    stops = [
        (20.6000, -103.3000),
        (20.6102, -103.3000),  # entrada (también sobre la pasada de regreso)
        (20.6100, -103.2950),
        (20.6150, -103.2900),
        (20.6200, -103.2950),
        (20.6102, -103.3050),
    ]
    assigned = _assign_distances(line, stops)
    dists = [d for d, _ in assigned]
    assert dists == sorted(dists)
    assert dists[1] < 1200  # la entrada, no la pasada de regreso
    assert all(offset < 30 for _, offset in assigned)


def test_point_and_bearing_at_distance():
    line = Polyline(ELE)
    lat, lon = line.point_at(500)
    assert (lat, lon) == pytest.approx((20.6045, -103.3000), abs=1e-4)
    assert line.bearing_at(500) == pytest.approx(0, abs=1)  # hacia el norte
    assert line.bearing_at(1500) == pytest.approx(90, abs=1)  # hacia el este
    assert line.point_at(10**9) == pytest.approx(ELE[-1])  # se acota al final


def test_simplify_drops_collinear_points_and_keeps_corners():
    recta = [(20.60 + i * 0.001, -103.30) for i in range(11)]
    assert simplify(recta, 2.0) == [recta[0], recta[-1]]
    assert simplify(ELE, 2.0) == ELE


def test_polyline_encoding_roundtrip():
    encoded = encode_polyline(ELE)
    assert isinstance(encoded, str) and len(encoded) < 40
    decoded = decode_polyline(encoded)
    assert decoded == [pytest.approx(p, abs=1e-5) for p in ELE]
    # Ejemplo de la documentación del algoritmo de polilínea codificada.
    assert encode_polyline([(38.5, -120.2), (40.7, -120.95), (43.252, -126.453)]) == (
        "_p~iF~ps|U_ulLnnqC_mqNvxq`@"
    )


# ── Limpieza y reparación ────────────────────────────────────────────────────


def test_clean_stop_name():
    assert clean_stop_name("PLAZA DE LA BANDERA") == "Plaza de la Bandera"
    assert clean_stop_name("  Río   Nilo ") == "Río Nilo"
    assert clean_stop_name("ESTACION L3") == "Estacion L3"
    assert clean_stop_name("Cucei") == "Cucei"


def test_classify_route():
    assert classify_route("MT_L3", "MT", 0, "L3") == ("tren", "tren")
    assert classify_route("MC-L1", "MM", 3, "MC-L1") == ("brt", "macro")
    assert classify_route("MP-A04", "MM", 3, "MP-A04") == ("bus", "alimentadora")
    assert classify_route("ST_L2", "ST", 3, "L2") == ("bus", "sitren")
    assert classify_route("T15", "BUS", 3, "T15") == ("bus", "troncal")
    assert classify_route("C38-V1", "BUS", 3, "C38-V1") == ("bus", "complementaria")


def test_repair_offsets_fixes_impossible_segments():
    # 4 paradas cada 500 m. El 2.º tramo no avanza en el tiempo y el 3.º exige 180 km/h.
    times = [0, 100, 100, 110]
    distances = [0.0, 500.0, 1000.0, 1500.0]

    offsets, repaired = _repair_offsets(
        times, distances, max_speed=22.2, fallback_speed=5.0
    )

    assert repaired == 2
    # Los tramos reparados usan la velocidad del tramo válido (500 m en 100 s).
    assert offsets == pytest.approx([0, 100, 200, 300])


def test_repair_offsets_without_any_time_uses_fallback_speed():
    offsets, repaired = _repair_offsets(
        [None, None, None], [0.0, 500.0, 1000.0], max_speed=22.2, fallback_speed=5.0
    )
    assert repaired == 2
    assert offsets == pytest.approx([0, 100, 200])


# ── Construcción ─────────────────────────────────────────────────────────────


def test_build_report_on_fixture(gtfs_loader):
    report = gtfs_loader.build_report
    assert report is not None
    assert report.routes == 7
    assert report.patterns == 14  # dos sentidos por ruta
    assert report.feed_end == "20241231"
    assert report.landmarks >= 40
    assert report.stops_far_from_shape <= 5
    assert report.seconds < 10


def test_every_pattern_is_consistent(gtfs_loader):
    for pattern in gtfs_loader._patterns.values():
        assert len(pattern.stop_ids) == len(pattern.dist_m) == len(pattern.offset_s)
        # Distancias y tiempos crecen con la secuencia de paradas.
        assert list(pattern.dist_m) == sorted(pattern.dist_m)
        assert list(pattern.offset_s) == sorted(pattern.offset_s)
        assert pattern.length_m > 5000
        assert pattern.windows, pattern.pattern_id
        # Velocidad comercial creíble.
        speed_kmh = pattern.length_m / pattern.run_time_s * 3.6
        assert 8 < speed_kmh < 60, (pattern.pattern_id, speed_kmh)


def test_stops_lie_on_their_pattern_line(gtfs_loader):
    """El trazo híbrido pasa cerca de todas las paradas (el feed trae trazos invertidos)."""
    far = 0
    total = 0
    for pattern in gtfs_loader._patterns.values():
        for stop_id, dist in zip(pattern.stop_ids, pattern.dist_m, strict=True):
            stop = gtfs_loader.get_stop(stop_id)
            lat, lon = pattern.line.point_at(dist)
            total += 1
            if haversine_km(stop.lat, stop.lon, lat, lon) * 1000 > 80:
                far += 1
    assert far / total < 0.01


def test_reversed_shape_is_detected(tmp_path):
    """Un shape dibujado al revés no desordena las paradas."""
    stops = [("A", 20.60), ("B", 20.61), ("C", 20.62), ("D", 20.63)]
    files = {
        "routes.txt": "route_id,route_short_name,route_long_name,route_type\nR1,1,Uno,3\n",
        "stops.txt": "stop_id,stop_name,stop_lat,stop_lon\n"
        + "".join(f"{sid},Parada {sid},{lat},-103.30\n" for sid, lat in stops),
        "trips.txt": "route_id,service_id,trip_id,shape_id\nR1,S,T1,SH\n",
        "stop_times.txt": "trip_id,arrival_time,departure_time,stop_id,stop_sequence\n"
        + "".join(
            f"T1,06:{i * 5:02d}:00,06:{i * 5:02d}:00,{sid},{i + 1}\n"
            for i, (sid, _) in enumerate(stops)
        ),
        # El trazo va de norte a sur, al revés que las paradas.
        "shapes.txt": "shape_id,shape_pt_lat,shape_pt_lon,shape_pt_sequence\n"
        + "".join(f"SH,{20.63 - i * 0.005},-103.30,{i + 1}\n" for i in range(7)),
    }
    zip_path = tmp_path / "invertido.zip"
    with zipfile.ZipFile(zip_path, "w") as archive:
        for name, text in files.items():
            archive.writestr(name, text)

    store = TransitStore(zip_path, tmp_path / "t.db").load()
    try:
        pattern = store.get_patterns("R1")[0]
        assert store.build_report.shapes_reassigned == 1
        assert pattern.shape_source == "shape"
        assert list(pattern.dist_m) == sorted(pattern.dist_m)
        assert pattern.dist_m[-1] == pytest.approx(3336, rel=0.01)
        # Sin calendario, el servicio se considera activo todos los días.
        assert store.active_services(date(2026, 11, 14)) is None
        assert (
            store.get_next_departures("R1", "A", 0, limit=1)[0]["departure_time"]
            == "06:00:00"
        )
    finally:
        store.close()


def _write_feed(path, files: dict[str, str]):
    with zipfile.ZipFile(path, "w") as archive:
        for name, text in files.items():
            archive.writestr(name, text)
    return path


def test_twin_platforms_are_merged(tmp_path):
    """El feed lista la misma estación dos veces seguidas (otro id, mismo punto y hora)."""
    feed = _write_feed(
        tmp_path / "gemelas.zip",
        {
            "routes.txt": "route_id,route_short_name,route_long_name,route_type\n"
            "L1,L1,Linea 1,0\n",
            "stops.txt": "stop_id,stop_name,stop_lat,stop_lon\n"
            "A,Juárez,20.6000,-103.3500\n"
            "A2,Juárez,20.60003,-103.35001\n"
            "B,Mexicaltzingo,20.6100,-103.3500\n"
            "C,Washington,20.6200,-103.3500\n",
            "trips.txt": "route_id,service_id,trip_id\nL1,S,T1\n",
            "stop_times.txt": "trip_id,arrival_time,departure_time,stop_id,stop_sequence\n"
            "T1,06:00:00,06:00:00,A,1\n"
            "T1,06:00:00,06:00:00,A2,2\n"
            "T1,06:02:00,06:02:00,B,3\n"
            "T1,06:04:00,06:04:00,C,4\n",
        },
    )
    store = TransitStore(feed, tmp_path / "t.db").load()
    try:
        pattern = store.get_patterns("L1")[0]
        assert pattern.stop_ids == ("A", "B", "C")
        assert store.build_report.twin_stops_merged == 1
        assert store.get_stop("A2") is None  # ya no la usa ningún recorrido
    finally:
        store.close()


def test_impossible_times_are_rebuilt_and_flagged(tmp_path):
    """Dos terminales a 3.3 km con 70 min de recorrido (2.8 km/h): tiempos de relleno.
    Sin shapes.txt, el trazo también es aproximado."""
    feed = _write_feed(
        tmp_path / "lento.zip",
        {
            "routes.txt": "route_id,route_short_name,route_long_name,route_type\n"
            "C9,C9,Corta,3\n",
            "stops.txt": "stop_id,stop_name,stop_lat,stop_lon\n"
            "A,Terminal Sur,20.60,-103.30\n"
            "B,Terminal Norte,20.63,-103.30\n",
            "trips.txt": "route_id,service_id,trip_id\nC9,S,T1\n",
            "stop_times.txt": "trip_id,arrival_time,departure_time,stop_id,stop_sequence\n"
            "T1,06:00:00,06:00:00,A,1\n"
            "T1,07:10:00,07:10:00,B,2\n",
        },
    )
    store = TransitStore(feed, tmp_path / "t.db").load()
    try:
        pattern = store.get_patterns("C9")[0]
        assert set(pattern.issues) == {
            ISSUE_ONLY_TERMINALS,
            ISSUE_ESTIMATED_TIMES,
            ISSUE_APPROXIMATE_SHAPE,
        }
        # Reconstruido a la velocidad de respaldo de camión (18 km/h): unos 11 min.
        assert pattern.run_time_s / 60 == pytest.approx(11.1, abs=0.3)
        assert len(pattern.notes) == 3
        assert all(note.endswith(".") for note in pattern.notes)
    finally:
        store.close()


def test_catalog_route_without_trips_leads_to_its_trunk(tmp_path):
    feed = _write_feed(
        tmp_path / "catalogo.zip",
        {
            "routes.txt": "route_id,route_short_name,route_long_name,route_type\n"
            'T15,T15,"Troncal T15 - 231 ""Prolongacion Alcalde""",3\n'
            'T15-C06,T15-C06,"Troncal T15-C06 - 231C ""Prolongacion Alcalde '
            'Complementaria""",3\n',
            "stops.txt": "stop_id,stop_name,stop_lat,stop_lon\n"
            "A,Uno,20.60,-103.30\nB,Dos,20.61,-103.30\nC,Tres,20.62,-103.30\n",
            "trips.txt": "route_id,service_id,trip_id\nT15,S,T1\n",
            "stop_times.txt": "trip_id,arrival_time,departure_time,stop_id,stop_sequence\n"
            "T1,06:00:00,06:00:00,A,1\n"
            "T1,06:04:00,06:04:00,B,2\n"
            "T1,06:08:00,06:08:00,C,3\n",
        },
    )
    store = TransitStore(feed, tmp_path / "t.db").load()
    try:
        assert [r.route_id for r in store.find_routes("231C")] == ["T15"]
        assert [r.route_id for r in store.find_routes("T15-C06")] == ["T15"]
        note = store.catalog_note("231C")
        assert "T15-C06 «Prolongacion Alcalde Complementaria»" in note
        assert note.endswith("Se muestra la troncal T15.")
        assert store.catalog_note("231") is None
        assert store.catalog_note("T15") is None
    finally:
        store.close()


def test_rebuild_only_when_source_changes(tmp_path):
    db_path = tmp_path / "t.db"
    assert needs_rebuild(FIXTURE_GTFS, db_path) is True
    build_transit_db(FIXTURE_GTFS, db_path)
    assert needs_rebuild(FIXTURE_GTFS, db_path) is False
    # Agregar los puntos de interés cambia la firma de la fuente.
    assert needs_rebuild(FIXTURE_GTFS, db_path, LANDMARKS) is True


# ── Rutas y alias ────────────────────────────────────────────────────────────


def test_legacy_numbers_resolve_to_official_routes(gtfs_loader):
    assert gtfs_loader.find_route("231").route_id == "T15"
    assert gtfs_loader.find_route("55").route_id == "T11B"
    assert gtfs_loader.find_route("615").route_id == "C38-V1"
    assert [r.route_id for r in gtfs_loader.find_routes("646")] == ["T04A-1", "T04A-2"]
    # Clave base, identificador antiguo del overlay y mayúsculas indistintas.
    assert [r.route_id for r in gtfs_loader.find_routes("t04a")] == ["T04A-1", "T04A-2"]
    assert gtfs_loader.find_route("RG_231").route_id == "T15"
    assert gtfs_loader.find_route("l3").route_id == "MT_L3"
    assert gtfs_loader.find_route("no existe") is None


def test_route_metadata(gtfs_loader):
    t15 = gtfs_loader.find_route("T15")
    assert (t15.legacy_number, t15.group, t15.mode) == ("231", "troncal", "bus")
    assert t15.label == "T15 (antes 231)"
    assert t15.branch_name == "Prolongacion Alcalde"
    tren = gtfs_loader.find_route("MT_L3")
    assert (tren.group, tren.mode, tren.label) == ("tren", "tren", "L3")
    # Los dos ramales de la 646 comparten color; rutas distintas no.
    assert (
        gtfs_loader.find_route("T04A-1").color == gtfs_loader.find_route("T04A-2").color
    )
    assert t15.color != gtfs_loader.find_route("T11B").color


def test_routes_are_ordered_by_group(gtfs_loader):
    ids = [r.route_id for r in gtfs_loader.get_routes()]
    assert ids == ["MT_L3", "MC-L1", "T04A-1", "T04A-2", "T11B", "T15", "C38-V1"]
    region = {r.route_id for r in gtfs_loader.get_routes("tonala_cucei")}
    assert "T15" in region and "MC-L1" not in region


def test_both_directions_are_available(gtfs_loader):
    patterns = gtfs_loader.get_patterns("T15", main_only=True)
    assert [(p.direction_id, p.headsign) for p in patterns] == [
        (0, "La Guadalupana"),
        (1, "La Coronilla"),
    ]
    ida = gtfs_loader.get_stops_for_route("T15", direction_id=0)
    todas = gtfs_loader.get_stops_for_route("T15")
    assert len(ida) == 97
    assert len(todas) > len(ida)
    assert len({s.stop_id for s in todas}) == len(todas)


# ── Paradas y lugares ────────────────────────────────────────────────────────


def test_search_ignores_accents_and_accepts_prefix(gtfs_loader):
    assert gtfs_loader.search_stops("artes plasticas")[0].stop_name == "Artes Plásticas"
    assert gtfs_loader.search_stops("ARTES PLÁSTICAS")[0].stop_name == "Artes Plásticas"
    assert any(
        s.stop_name == "Artes Plásticas" for s in gtfs_loader.search_stops("artes pla")
    )
    assert gtfs_loader.search_stops("zzzz inexistente") == []


def test_stops_near_uses_spatial_index(gtfs_loader):
    cucei = gtfs_loader.find_landmark("CUCEI")
    near = gtfs_loader.stops_near(cucei.lat, cucei.lon, radius_km=0.3, limit=50)

    assert near
    assert [d for _, d in near] == sorted(d for _, d in near)
    assert all(d <= 0.3 for _, d in near)
    assert any(stop.stop_name == "Cucei" for stop, _ in near)


def test_landmark_lookup(gtfs_loader):
    assert gtfs_loader.find_landmark("cucei").name == "CUCEI"
    assert gtfs_loader.find_landmark("en el CUCEI por favor").name == "CUCEI"
    assert gtfs_loader.find_landmark("Tonalá Centro").name == "Tonalá Centro"
    assert gtfs_loader.find_landmark("lugar inexistente") is None


def test_place_resolves_to_real_stops_per_direction(gtfs_loader):
    cucei = gtfs_loader.find_landmark("CUCEI")
    options = gtfs_loader.stops_of_route_near("T15", cucei.lat, cucei.lon)

    assert {o.pattern.headsign for o in options} == {"La Guadalupana", "La Coronilla"}
    assert all(o.distance_m < 400 for o in options)
    # La 646 corre por Lázaro Cárdenas: no tiene parada a distancia caminable de CUCEI.
    assert gtfs_loader.stops_of_route_near("T04A-1", cucei.lat, cucei.lon) == []

    place = gtfs_loader.resolve_place("CUCEI")
    labels = [r.label for r in gtfs_loader.routes_at_place(place)]
    assert place.kind == "lugar"
    assert "T15 (antes 231)" in labels and "L3" in labels
    assert "T04A-1 (antes 646)" not in labels


# ── Calendario y pasadas ─────────────────────────────────────────────────────


def test_expired_feed_runs_as_weekly_pattern(gtfs_loader):
    assert gtfs_loader.feed_end == date(2024, 12, 31)
    # Un martes cualquiera de 2026: servicios de lunes a viernes y diarios.
    assert gtfs_loader.active_services(date(2026, 11, 10)) == {"LD", "LS", "LV"}
    assert gtfs_loader.active_services(date(2026, 11, 14)) == {"LD", "LS", "S", "SD"}
    assert gtfs_loader.active_services(date(2026, 11, 15)) == {"D", "LD", "SD"}


def test_holiday_runs_sunday_service(gtfs_loader):
    # Lunes 16 de noviembre de 2026: festivo (Revolución), opera como domingo.
    assert gtfs_loader.active_services(date(2026, 11, 16)) == {"D", "LD", "SD"}


def test_departures_carry_direction_and_headway(gtfs_loader):
    martes_8am = datetime(2026, 11, 10, 8, 0, tzinfo=GDL_TZ)
    departures = gtfs_loader.get_next_departures(
        "T15", "Cucei", limit=4, now=martes_8am
    )

    assert len(departures) == 4
    assert {d["headsign"] for d in departures} == {"La Coronilla"}
    assert all(d["headway_minutes"] == 5 for d in departures)
    assert all(0 <= d["minutes_until"] <= 20 for d in departures)
    minutes = [d["absolute_minutes"] for d in departures]
    assert [round(b - a) for a, b in zip(minutes, minutes[1:], strict=False)] == [
        5,
        5,
        5,
    ]


def test_shared_station_separates_directions(gtfs_loader):
    """En una estación del tren pasan ambos sentidos; se puede pedir uno solo."""
    martes_8am = datetime(2026, 11, 10, 8, 0, tzinfo=GDL_TZ)
    ambos = gtfs_loader.get_next_departures(
        "MT_L3", "MT-L3-113", limit=6, now=martes_8am
    )
    assert {d["headsign"] for d in ambos} == {
        "Arcos de Zapopan",
        "Central de Autobuses",
    }

    uno = gtfs_loader.get_next_departures(
        "MT_L3", "MT-L3-113", limit=6, now=martes_8am, pattern_id="MT_L3:0"
    )
    assert {d["headsign"] for d in uno} == {"Arcos de Zapopan"}


def test_terminal_shows_departures_not_arrivals(gtfs_loader):
    martes_8am = datetime(2026, 11, 10, 8, 0, tzinfo=GDL_TZ)
    # Central de Autobuses: fin del sentido 1 e inicio del sentido 0.
    departures = gtfs_loader.get_next_departures(
        "MT_L3", "MT-L3-118", limit=3, now=martes_8am
    )
    assert {d["headsign"] for d in departures} == {"Arcos de Zapopan"}


def test_active_headway_by_time_of_day(gtfs_loader):
    assert (
        gtfs_loader.get_active_headway(
            "T15", now=datetime(2026, 11, 10, 8, 0, tzinfo=GDL_TZ)
        )
        == 5
    )
    assert (
        gtfs_loader.get_active_headway(
            "T15", now=datetime(2026, 11, 15, 8, 0, tzinfo=GDL_TZ)
        )
        == 6
    )
    # De madrugada no hay servicio.
    assert (
        gtfs_loader.get_active_headway(
            "T15", now=datetime(2026, 11, 10, 2, 0, tzinfo=GDL_TZ)
        )
        is None
    )


def test_pattern_time_and_distance_are_inverse(gtfs_loader):
    pattern = gtfs_loader.get_pattern("T15:0")
    middle = pattern.dist_m[len(pattern.dist_m) // 2]

    assert pattern.time_at(0) == 0
    assert pattern.time_at(pattern.length_m) == pytest.approx(pattern.run_time_s)
    assert pattern.dist_at_time(pattern.time_at(middle)) == pytest.approx(middle, abs=1)
    assert pattern.dist_at_time(10**9) == pytest.approx(pattern.dist_m[-1])


def test_missing_gtfs_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        TransitStore(tmp_path / "no_existe.zip", tmp_path / "t.db").load()
