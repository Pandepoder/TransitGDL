"""
Genera data/landmarks.json: puntos de interés del Área Metropolitana de
Guadalajara con coordenadas tomadas de OpenStreetMap (servicio Nominatim).

Sirve para que "CUCEI" o "Minerva" se resuelvan a las paradas reales cercanas
aunque ninguna parada se llame así. El archivo generado se versiona; este
script solo hace falta para regenerarlo o agregar lugares.

Uso:
    python scripts/build_landmarks.py            # consulta todos los lugares
    python scripts/build_landmarks.py --only cucei minerva

Respeta la política de uso de Nominatim: una consulta por segundo.
"""

from __future__ import annotations

import argparse
import json
import time
import urllib.parse
import urllib.request
from datetime import date
from pathlib import Path

OUTPUT = Path("data/landmarks.json")
ENDPOINT = "https://nominatim.openstreetmap.org/search"
USER_AGENT = "landmarks-builder/1.0"
# Caja del Área Metropolitana: oeste, norte, este, sur.
VIEWBOX = "-103.62,20.92,-103.14,20.38"

# (id, nombre, tipo, alias, consultas para Nominatim separadas por "|")
PLACES: list[tuple[str, str, str, list[str], str]] = [
    (
        "cucei",
        "CUCEI",
        "universidad",
        ["centro universitario de ciencias exactas e ingenierias", "udg cucei"],
        "CUCEI, Guadalajara|Centro Universitario de Ciencias Exactas e Ingenierías (CUCEI)|CUCEI",
    ),
    (
        "cucea",
        "CUCEA",
        "universidad",
        ["centro universitario de ciencias economico administrativas"],
        "Centro Universitario de Ciencias Económico Administrativas",
    ),
    (
        "cucsh",
        "CUCSH Belenes",
        "universidad",
        ["cucsh", "centro universitario de ciencias sociales y humanidades"],
        "CUCSH Belenes|CUCSH, Zapopan|Centro Universitario de Ciencias Sociales y Humanidades",
    ),
    (
        "cucs",
        "CUCS",
        "universidad",
        ["centro universitario de ciencias de la salud"],
        "Centro Universitario de Ciencias de la Salud, Guadalajara",
    ),
    (
        "cuaad",
        "CUAAD",
        "universidad",
        ["centro universitario de arte arquitectura y diseno"],
        "CUAAD, Guadalajara|Centro Universitario de Arte Arquitectura y Diseño|CUAAD",
    ),
    (
        "cucba",
        "CUCBA",
        "universidad",
        ["centro universitario de ciencias biologicas y agropecuarias"],
        "Centro Universitario de Ciencias Biológicas y Agropecuarias",
    ),
    (
        "cutonala",
        "CUTonalá",
        "universidad",
        ["cu tonala", "centro universitario de tonala", "cutonala"],
        "Centro Universitario de Tonalá",
    ),
    (
        "iteso",
        "ITESO",
        "universidad",
        ["iteso universidad jesuita"],
        "ITESO, Tlaquepaque",
    ),
    (
        "tec",
        "Tec de Monterrey",
        "universidad",
        ["tecnologico de monterrey", "tec"],
        "Tecnológico de Monterrey, Zapopan|ITESM Campus Guadalajara|Tec de Monterrey",
    ),
    (
        "uag",
        "UAG",
        "universidad",
        ["universidad autonoma de guadalajara", "autonoma"],
        "Universidad Autónoma de Guadalajara",
    ),
    (
        "univa",
        "UNIVA",
        "universidad",
        ["universidad del valle de atemajac"],
        "Universidad del Valle de Atemajac",
    ),
    (
        "central_nueva",
        "Central Nueva",
        "transporte",
        [
            "nueva central camionera",
            "central de autobuses",
            "central camionera",
            "nueva central",
        ],
        "Nueva Central Camionera, Tlaquepaque",
    ),
    (
        "central_vieja",
        "Central Vieja",
        "transporte",
        ["antigua central camionera", "vieja central", "central vieja"],
        "Central Vieja, Guadalajara|Antigua Central de Autobuses|Central Camionera Vieja",
    ),
    (
        "aeropuerto",
        "Aeropuerto de Guadalajara",
        "transporte",
        ["aeropuerto", "aeropuerto internacional"],
        "Aeropuerto Internacional de Guadalajara",
    ),
    (
        "catedral",
        "Catedral de Guadalajara",
        "centro",
        ["catedral", "centro historico", "centro de guadalajara"],
        "Catedral de Guadalajara",
    ),
    (
        "san_juan_de_dios",
        "Mercado San Juan de Dios",
        "centro",
        ["san juan de dios", "mercado libertad"],
        "Mercado Libertad, Guadalajara",
    ),
    (
        "cabanas",
        "Hospicio Cabañas",
        "centro",
        ["cabanas", "museo cabanas", "plaza tapatia"],
        "Museo Cabañas, Guadalajara",
    ),
    ("degollado", "Teatro Degollado", "centro", ["degollado"], "Teatro Degollado"),
    (
        "expiatorio",
        "Templo Expiatorio",
        "centro",
        ["expiatorio"],
        "Templo Expiatorio del Santísimo Sacramento, Guadalajara",
    ),
    (
        "minerva",
        "Glorieta Minerva",
        "centro",
        ["minerva", "la minerva"],
        "Glorieta Minerva, Guadalajara",
    ),
    (
        "arcos",
        "Arcos Vallarta",
        "centro",
        ["arcos", "los arcos"],
        "Arcos Vallarta, Guadalajara",
    ),
    (
        "agua_azul",
        "Parque Agua Azul",
        "parque",
        ["agua azul"],
        "Parque Agua Azul, Guadalajara",
    ),
    (
        "zoologico",
        "Zoológico Guadalajara",
        "parque",
        ["zoologico", "zoo"],
        "Zoológico Guadalajara",
    ),
    (
        "metropolitano",
        "Parque Metropolitano",
        "parque",
        ["metropolitano"],
        "Parque Metropolitano, Zapopan|Parque Metropolitano",
    ),
    (
        "colomos",
        "Bosque Los Colomos",
        "parque",
        ["colomos", "los colomos"],
        "Bosque Los Colomos",
    ),
    (
        "estadio_jalisco",
        "Estadio Jalisco",
        "estadio",
        ["jalisco estadio"],
        "Estadio Jalisco",
    ),
    (
        "estadio_akron",
        "Estadio Akron",
        "estadio",
        ["akron", "estadio chivas"],
        "Estadio Akron",
    ),
    ("auditorio_telmex", "Auditorio Telmex", "estadio", ["telmex"], "Auditorio Telmex"),
    ("expo", "Expo Guadalajara", "comercio", ["expo"], "Expo Guadalajara"),
    (
        "plaza_del_sol",
        "Plaza del Sol",
        "comercio",
        ["plaza sol"],
        "Plaza del Sol, Zapopan",
    ),
    (
        "galerias",
        "Galerías Guadalajara",
        "comercio",
        ["galerias", "plaza galerias"],
        "Galerías Guadalajara",
    ),
    (
        "andares",
        "Andares",
        "comercio",
        ["plaza andares"],
        "Plaza Andares, Zapopan|Centro Comercial Andares|Andares",
    ),
    (
        "gran_plaza",
        "La Gran Plaza",
        "comercio",
        ["gran plaza"],
        "La Gran Plaza, Zapopan|Gran Plaza Fashion Mall|La Gran Plaza",
    ),
    (
        "centro_magno",
        "Centro Magno",
        "comercio",
        ["magno"],
        "Centro Magno, Guadalajara",
    ),
    ("forum", "Forum Tlaquepaque", "comercio", ["forum"], "Forum Tlaquepaque"),
    (
        "plaza_patria",
        "Plaza Patria",
        "comercio",
        ["patria plaza"],
        "Plaza Patria, Zapopan",
    ),
    (
        "abastos",
        "Mercado de Abastos",
        "comercio",
        ["abastos"],
        "Mercado de Abastos, Guadalajara",
    ),
    (
        "tonala_centro",
        "Tonalá Centro",
        "municipio",
        ["tonala", "centro de tonala", "plaza cihualpilli"],
        "Plaza Cihualpilli, Tonalá",
    ),
    (
        "tlaquepaque_centro",
        "Tlaquepaque Centro",
        "municipio",
        ["tlaquepaque", "centro de tlaquepaque", "el parian"],
        "El Parián, Tlaquepaque",
    ),
    (
        "zapopan_centro",
        "Zapopan Centro",
        "municipio",
        ["zapopan", "basilica de zapopan", "centro de zapopan"],
        "Basílica de Zapopan",
    ),
    (
        "loma_dorada",
        "Loma Dorada",
        "colonia",
        ["loma dorada tonala"],
        "Loma Dorada, Tonalá",
    ),
    (
        "hospital_civil_viejo",
        "Hospital Civil Viejo",
        "salud",
        ["hospital civil", "fray antonio alcalde", "civil viejo"],
        "Hospital Civil Fray Antonio Alcalde|Antiguo Hospital Civil de Guadalajara|Hospital Civil Viejo",
    ),
    (
        "hospital_civil_nuevo",
        "Hospital Civil Nuevo",
        "salud",
        ["juan i menchaca", "civil nuevo", "nuevo hospital civil"],
        "Hospital Civil Dr. Juan I. Menchaca|Nuevo Hospital Civil de Guadalajara|Hospital Civil Nuevo",
    ),
    (
        "centro_medico",
        "Centro Médico de Occidente",
        "salud",
        ["centro medico", "imss centro medico"],
        "Centro Médico Nacional de Occidente IMSS|Centro Médico de Occidente|Hospital de Especialidades Centro Médico Nacional de Occidente",
    ),
]


def lookup(query: str) -> dict | None:
    params = urllib.parse.urlencode(
        {
            "q": query,
            "format": "jsonv2",
            "limit": 1,
            "viewbox": VIEWBOX,
            "bounded": 1,
            "accept-language": "es",
        }
    )
    request = urllib.request.Request(
        f"{ENDPOINT}?{params}", headers={"User-Agent": USER_AGENT}
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        results = json.loads(response.read().decode("utf-8"))
    return results[0] if results else None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Genera data/landmarks.json.")
    parser.add_argument("--only", nargs="*", help="Consultar solo estos ids")
    parser.add_argument("--output", default=str(OUTPUT))
    args = parser.parse_args(argv)
    output = Path(args.output)

    previous: dict[str, dict] = {}
    if output.exists():
        for item in json.loads(output.read_text(encoding="utf-8")).get("lugares", []):
            previous[item["id"]] = item

    places: list[dict] = []
    missing: list[str] = []
    for place_id, name, kind, aliases, query in PLACES:
        if args.only and place_id not in args.only and place_id in previous:
            places.append(previous[place_id])
            continue
        hit = None
        for attempt in query.split("|"):
            try:
                hit = lookup(attempt)
            except Exception as exc:
                print(f"  {place_id}: error de red ({exc})")
            time.sleep(1.1)
            if hit is not None:
                break
        if hit is None:
            missing.append(place_id)
            if place_id in previous:
                places.append(previous[place_id])
            continue
        item = {
            "id": place_id,
            "nombre": name,
            "tipo": kind,
            "alias": aliases,
            "lat": round(float(hit["lat"]), 6),
            "lon": round(float(hit["lon"]), 6),
            "osm": f"{hit.get('osm_type', '')}/{hit.get('osm_id', '')}",
            "referencia": hit.get("display_name", "")[:120],
        }
        places.append(item)
        print(
            f"  {place_id:22s} {item['lat']:.5f}, {item['lon']:.5f}  {item['referencia'][:70]}"
        )

    payload = {
        "fuente": "OpenStreetMap (Nominatim) — © colaboradores de OpenStreetMap, licencia ODbL",
        "generado": date.today().isoformat(),
        "lugares": places,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"{len(places)} lugares escritos en {output}")
    if missing:
        print("Sin resultado:", ", ".join(missing))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
