"""
Genera tests/fixtures/gtfs_mini.zip: un recorte del GTFS oficial con unas
cuantas rutas reales, para que las pruebas sean rápidas y no dependan del
archivo completo ni de la red.

Uso:
    python scripts/make_test_fixture.py
"""

from __future__ import annotations

import csv
import io
import zipfile
from pathlib import Path

SOURCE = Path("data/gtfs_amg.zip")
OUTPUT = Path("tests/fixtures/gtfs_mini.zip")
# 231, 55, los dos ramales de la 646, 615, Macrobús Calzada y Línea 3 del tren.
ROUTES = ("T15", "T11B", "T04A-1", "T04A-2", "C38-V1", "MC-L1", "MT_L3")


def _read(
    archive: zipfile.ZipFile, name: str
) -> tuple[list[str], list[dict[str, str]]]:
    try:
        text = archive.read(name).decode("utf-8-sig")
    except KeyError:
        return [], []
    reader = csv.DictReader(io.StringIO(text))
    return list(reader.fieldnames or []), list(reader)


def _write(
    archive: zipfile.ZipFile, name: str, fields: list[str], rows: list[dict]
) -> None:
    if not fields:
        return
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=fields, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    # Fecha fija para que el zip sea idéntico entre ejecuciones.
    info = zipfile.ZipInfo(name, date_time=(2024, 3, 12, 0, 0, 0))
    info.compress_type = zipfile.ZIP_DEFLATED
    archive.writestr(info, buffer.getvalue())


def main() -> int:
    if not SOURCE.exists():
        print(f"Falta {SOURCE}. Ejecuta primero: python scripts/download_gtfs.py")
        return 1

    with zipfile.ZipFile(SOURCE) as source:
        tables = {
            name: _read(source, name)
            for name in (
                "agency.txt",
                "routes.txt",
                "trips.txt",
                "stop_times.txt",
                "stops.txt",
                "frequencies.txt",
                "shapes.txt",
                "calendar.txt",
                "calendar_dates.txt",
                "fare_attributes.txt",
            )
        }

    routes = [r for r in tables["routes.txt"][1] if r["route_id"] in ROUTES]
    trips = [t for t in tables["trips.txt"][1] if t["route_id"] in ROUTES]
    trip_ids = {t["trip_id"] for t in trips}
    shape_ids = {t["shape_id"] for t in trips}
    stop_times = [r for r in tables["stop_times.txt"][1] if r["trip_id"] in trip_ids]
    stop_ids = {r["stop_id"] for r in stop_times}
    agencies = {r["agency_id"] for r in routes}

    selected = {
        "agency.txt": [
            r for r in tables["agency.txt"][1] if r["agency_id"] in agencies
        ],
        "routes.txt": routes,
        "trips.txt": trips,
        "stop_times.txt": stop_times,
        "stops.txt": [r for r in tables["stops.txt"][1] if r["stop_id"] in stop_ids],
        "frequencies.txt": [
            r for r in tables["frequencies.txt"][1] if r["trip_id"] in trip_ids
        ],
        "shapes.txt": [
            r for r in tables["shapes.txt"][1] if r["shape_id"] in shape_ids
        ],
        "calendar.txt": tables["calendar.txt"][1],
        "calendar_dates.txt": tables["calendar_dates.txt"][1],
        "fare_attributes.txt": [
            r
            for r in tables["fare_attributes.txt"][1]
            if r.get("agency_id") in agencies
        ],
    }

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(OUTPUT, "w") as target:
        for name, rows in selected.items():
            _write(target, name, tables[name][0], rows)

    print(
        f"{OUTPUT}: {len(routes)} rutas, {len(trips)} viajes, "
        f"{len(selected['stops.txt'])} paradas, {OUTPUT.stat().st_size / 1024:.0f} KB"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
