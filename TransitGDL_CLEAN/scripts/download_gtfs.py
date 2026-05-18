from __future__ import annotations

import argparse
import html.parser
import urllib.parse
import urllib.request
from pathlib import Path


DEFAULT_SOURCE = "https://hub.tumidata.org/dataset/?tags=City+of+Guadalajara"


class _LinkExtractor(html.parser.HTMLParser):
    def __init__(self):
        super().__init__()
        self.links: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag.lower() != "a":
            return
        href = dict(attrs).get("href")
        if href:
            self.links.append(href)


def _fetch(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=30) as resp:
        return resp.read()


def _resolve_download_url(source_url: str) -> str:
    parsed = urllib.parse.urlparse(source_url)
    if parsed.path.lower().endswith(".zip"):
        return source_url

    html = _fetch(source_url).decode("utf-8", errors="ignore")
    extractor = _LinkExtractor()
    extractor.feed(html)

    candidates = []
    for link in extractor.links:
        href = urllib.parse.urljoin(source_url, link)
        low = href.lower()
        if ".zip" in low or "download" in low or "gtfs" in low:
            candidates.append(href)

    if not candidates:
        raise RuntimeError(
            "No pude encontrar un enlace directo de descarga en la página. "
            "Pasa el URL directo del ZIP con --url."
        )

    # Preferir ZIP / GTFS
    candidates.sort(key=lambda u: (".zip" not in u.lower(), "gtfs" not in u.lower(), len(u)))
    return candidates[0]


def download_gtfs(source_url: str, output_path: Path) -> Path:
    download_url = _resolve_download_url(source_url)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    with urllib.request.urlopen(download_url, timeout=60) as resp, output_path.open("wb") as out:
        out.write(resp.read())

    return output_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Descarga un GTFS de Guadalajara.")
    parser.add_argument("--url", default=DEFAULT_SOURCE, help="URL de la página o del ZIP GTFS")
    parser.add_argument("--output", default="data/gtfs_guadalajara.zip", help="Ruta de salida")
    args = parser.parse_args(argv)

    output = Path(args.output)
    try:
        path = download_gtfs(args.url, output)
        print(f"✓ GTFS descargado en {path}")
        return 0
    except Exception as exc:
        print(f"⚠ No se pudo descargar el GTFS automáticamente: {exc}")
        print("Puedes usar el GTFS demo con: python scripts/seed_demo.py")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())