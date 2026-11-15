"""
Construye la base de transporte (data/transit.db) a partir del zip GTFS.

La aplicación la reconstruye sola al arrancar si falta o si el zip cambió;
este script sirve para hacerlo a mano y ver el reporte de validación.

Uso:
    python scripts/build_transit_db.py
    python scripts/build_transit_db.py --gtfs data/gtfs_amg.zip --db data/transit.db
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.config import CONFIG  # noqa: E402
from core.transit import build_transit_db  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Construye la base de transporte.")
    parser.add_argument("--gtfs", default=CONFIG.gtfs_path, help="Zip GTFS de entrada")
    parser.add_argument(
        "--db", default=CONFIG.transit_db_path, help="Base SQLite de salida"
    )
    parser.add_argument(
        "--landmarks", default=CONFIG.landmarks_path, help="JSON de puntos de interés"
    )
    args = parser.parse_args(argv)

    if not Path(args.gtfs).exists():
        print(f"No existe {args.gtfs}. Descárgalo con: python scripts/download_gtfs.py")
        return 1

    report = build_transit_db(args.gtfs, args.db, args.landmarks)
    print(report.summary())
    print(f"Base escrita en {args.db}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
