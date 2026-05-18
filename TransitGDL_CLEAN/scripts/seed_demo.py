from __future__ import annotations

import argparse
import csv
import io
import zipfile
from pathlib import Path

AGENCIES = [
    {
        "agency_id": "SITEUR",
        "agency_name": "SITEUR Jalisco",
        "agency_url": "https://www.siteur.gob.mx",
        "agency_timezone": "America/Mexico_City",
    },
]

ROUTES = [
    {
        "route_id": "R045",
        "agency_id": "SITEUR",
        "route_short_name": "45",
        "route_long_name": "Plaza del Sol - Centro Historico",
        "route_type": "3",
    },
    {
        "route_id": "R110",
        "agency_id": "SITEUR",
        "route_short_name": "110",
        "route_long_name": "Zapopan - Tlaquepaque",
        "route_type": "3",
    },
    {
        "route_id": "MMACRO_L1",
        "agency_id": "SITEUR",
        "route_short_name": "MiMacro L1",
        "route_long_name": "MiMacro Periferico Linea 1",
        "route_type": "3",
    },
]

STOPS = [
    ("S001", "Plaza del Sol", 20.6469, -103.4044),
    ("S002", "Av. Lopez Mateos y Americas", 20.6521, -103.3932),
    ("S003", "Glorieta Minerva", 20.6686, -103.3862),
    ("S004", "Av. Vallarta y Chapultepec", 20.6750, -103.3840),
    ("S005", "Av. Hidalgo y 16 de Septiembre", 20.6736, -103.3468),
    ("S006", "Centro Historico / Catedral", 20.6752, -103.3464),
    ("S010", "Plaza Patria Zapopan", 20.7214, -103.3912),
    ("S011", "Av. Patria y Periferico", 20.7031, -103.3824),
    ("S012", "Av. Americas y Terranova", 20.6789, -103.3901),
    ("S013", "Calzada Independencia Norte", 20.6682, -103.3395),
    ("S014", "Tlaquepaque Centro", 20.6428, -103.3118),
    ("S020", "Periferico Norte (Terminal)", 20.7512, -103.3745),
    ("S021", "Av. Vallarta y Patria", 20.7011, -103.3891),
    ("S022", "Glorieta Colon", 20.6731, -103.3799),
    ("S023", "Av. Revolucion y Lopez Cotilla", 20.6682, -103.3652),
    ("S024", "Periferico Sur (Terminal)", 20.6081, -103.3612),
]

CALENDAR = [
    ("WD", "1", "1", "1", "1", "1", "0", "0", "20250101", "20301231"),
    ("SA", "0", "0", "0", "0", "0", "1", "0", "20250101", "20301231"),
    ("SU", "0", "0", "0", "0", "0", "0", "1", "20250101", "20301231"),
]


def _build_trips_and_times():
    trips = []
    stop_times = []
    route_stops = {
        "R045": ["S001", "S002", "S003", "S004", "S005", "S006"],
        "R110": ["S010", "S011", "S012", "S013", "S014"],
        "MMACRO_L1": ["S020", "S021", "S022", "S023", "S024"],
    }

    trip_seq = 1
    for route_id, stops in route_stops.items():
        for service in ["WD", "SA", "SU"]:
            step_min = 15 if service == "WD" else 25
            t = 5 * 60
            end_min = 22 * 60
            while t < end_min:
                trip_id = f"T{trip_seq:04d}"
                trips.append(
                    {
                        "trip_id": trip_id,
                        "route_id": route_id,
                        "service_id": service,
                        "trip_headsign": route_id,
                        "direction_id": "0",
                        "shape_id": "",
                    }
                )
                for seq, stop_id in enumerate(stops):
                    arr_min = t + seq * 4
                    h, m = divmod(arr_min, 60)
                    arr_str = f"{h:02d}:{m:02d}:00"
                    stop_times.append(
                        {
                            "trip_id": trip_id,
                            "stop_id": stop_id,
                            "stop_sequence": str(seq + 1),
                            "arrival_time": arr_str,
                            "departure_time": arr_str,
                        }
                    )
                trip_seq += 1
                t += step_min
    return trips, stop_times


def _csv_bytes(rows: list[dict], fieldnames: list[str]) -> bytes:
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=fieldnames, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(rows)
    return buf.getvalue().encode("utf-8")


def build_gtfs_zip(output_path: Path):
    trips, stop_times = _build_trips_and_times()

    stops_rows = [
        {"stop_id": sid, "stop_name": name, "stop_lat": lat, "stop_lon": lon}
        for sid, name, lat, lon in STOPS
    ]
    calendar_rows = [
        {
            "service_id": row[0],
            "monday": row[1],
            "tuesday": row[2],
            "wednesday": row[3],
            "thursday": row[4],
            "friday": row[5],
            "saturday": row[6],
            "sunday": row[7],
            "start_date": row[8],
            "end_date": row[9],
        }
        for row in CALENDAR
    ]

    files = {
        "agency.txt": _csv_bytes(
            AGENCIES,
            ["agency_id", "agency_name", "agency_url", "agency_timezone"],
        ),
        "routes.txt": _csv_bytes(
            ROUTES,
            [
                "route_id",
                "agency_id",
                "route_short_name",
                "route_long_name",
                "route_type",
            ],
        ),
        "stops.txt": _csv_bytes(
            stops_rows,
            ["stop_id", "stop_name", "stop_lat", "stop_lon"],
        ),
        "calendar.txt": _csv_bytes(
            calendar_rows,
            [
                "service_id",
                "monday",
                "tuesday",
                "wednesday",
                "thursday",
                "friday",
                "saturday",
                "sunday",
                "start_date",
                "end_date",
            ],
        ),
        "trips.txt": _csv_bytes(
            trips,
            [
                "trip_id",
                "route_id",
                "service_id",
                "trip_headsign",
                "direction_id",
                "shape_id",
            ],
        ),
        "stop_times.txt": _csv_bytes(
            stop_times,
            [
                "trip_id",
                "stop_id",
                "stop_sequence",
                "arrival_time",
                "departure_time",
            ],
        ),
    }

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output_path, "w", zipfile.ZIP_DEFLATED) as archive:
        for filename, content in files.items():
            archive.writestr(filename, content)

    return len(trips), len(stop_times)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Genera un GTFS demo para Guadalajara.")
    parser.add_argument("--output", default="data/gtfs_guadalajara.zip", help="Ruta del archivo ZIP a crear")
    parser.add_argument("--force", action="store_true", help="Sobrescribe el archivo de salida si ya existe")
    args = parser.parse_args(argv)

    output = Path(args.output)
    if output.exists() and not args.force:
        print(f"[WARN] Ya existe: {output}")
        try:
            resp = input("   Sobreescribir? [s/N] ").strip().lower()
        except EOFError:
            print("Cancelado por entrada no interactiva. Usa --force para sobrescribir.")
            return 1
        if resp != "s":
            print("Cancelado.")
            return 0

    print("Generando GTFS de demostracion para Guadalajara...")
    n_trips, n_times = build_gtfs_zip(output)
    size_kb = output.stat().st_size // 1024

    print(f"\n[OK] Archivo generado: {output} ({size_kb} KB)")
    print(f"   Rutas:      {len(ROUTES)}")
    print(f"   Paradas:    {len(STOPS)}")
    print(f"   Viajes:     {n_trips}")
    print(f"   Stop times: {n_times}")
    print("\nListo. Ahora puedes correr el sistema:")
    print("  PYTHONPATH=. python tests/test_core.py")
    print("  uvicorn api.main:app --reload")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
