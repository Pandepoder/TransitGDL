"""
Descarga el GTFS oficial del Área Metropolitana de Guadalajara.

Fuente: feed publicado por el Gobierno de Jalisco (Setran / SITEUR) en su
portal de datos abiertos, servido desde el espejo estable de Mobility Database
(registro mdb-2366). Se verifica la huella SHA-256 para garantizar que el
archivo es exactamente la versión contra la que se probó el proyecto.

Uso:
    python scripts/download_gtfs.py
    python scripts/download_gtfs.py --accept-new   # adoptar un feed más nuevo
"""

from __future__ import annotations

import argparse
import hashlib
import urllib.request
import zipfile
from pathlib import Path

DEFAULT_URL = "https://files.mobilitydatabase.org/mdb-2366/latest.zip"
DEFAULT_OUTPUT = "data/gtfs_amg.zip"
# Versión del 24-feb-2025 (datos de marzo de 2024, 276 rutas).
KNOWN_SHA256 = "22011e158efdf066057ed882d2c00d205cfaa390c51d01f565619c0049c15e99"
REQUIRED_FILES = {"routes.txt", "stops.txt", "trips.txt", "stop_times.txt"}
USER_AGENT = "gtfs-downloader/1.0"


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def download(url: str, output: Path, *, timeout: float = 120.0) -> Path:
    output.parent.mkdir(parents=True, exist_ok=True)
    partial = output.with_suffix(output.suffix + ".part")
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        with partial.open("wb") as handle:
            for chunk in iter(lambda: response.read(1 << 16), b""):
                handle.write(chunk)
    return partial


def validate_zip(path: Path) -> None:
    with zipfile.ZipFile(path) as archive:
        names = set(archive.namelist())
    missing = REQUIRED_FILES - names
    if missing:
        raise ValueError(f"El zip no es un GTFS válido; faltan: {sorted(missing)}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Descarga el GTFS oficial del AMG.")
    parser.add_argument("--url", default=DEFAULT_URL, help="URL del zip GTFS")
    parser.add_argument("--output", default=DEFAULT_OUTPUT, help="Ruta de salida")
    parser.add_argument(
        "--accept-new",
        action="store_true",
        help="Aceptar un archivo cuya huella no coincide con la versión conocida",
    )
    args = parser.parse_args(argv)
    output = Path(args.output)

    try:
        partial = download(args.url, output)
        validate_zip(partial)
    except Exception as exc:
        print(f"No se pudo descargar el GTFS: {exc}")
        print(
            "Como alternativa, genera datos de demostración: python scripts/seed_demo.py"
        )
        return 1

    digest = sha256_of(partial)
    if digest != KNOWN_SHA256 and not args.accept_new:
        print("La huella del archivo no coincide con la versión conocida.")
        print(f"  esperada: {KNOWN_SHA256}")
        print(f"  obtenida: {digest}")
        print(
            "Puede ser un feed más reciente. Revísalo y vuelve a correr con --accept-new."
        )
        print(f"El archivo quedó en {partial} sin activar.")
        return 2

    partial.replace(output)
    size_mb = output.stat().st_size / 1e6
    print(f"GTFS guardado en {output} ({size_mb:.2f} MB, sha256 {digest[:12]}…)")
    print("Siguiente paso: python scripts/build_transit_db.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
