"""
Intenciones en texto libre que comparten el buscador web y el asistente.

"231 en CUCEI", "T15 por Minerva", "la 646 hacia Tonalá", "ruta 55 a Minerva":
una ruta y una parada o lugar. Si la ruta existe, la consulta es "¿cuándo
llega esta ruta a ese lugar?".
"""

from __future__ import annotations

import re
from typing import NamedTuple, Optional

# Las palabras de relleno ("la", "ruta") se descartan; el nombre del sistema
# ("Línea 3", "Tren 3", "SiTren 3") es parte de la ruta.
_INTENT_RE = re.compile(
    r"^(?:(?:la|el|ruta|cami[oó]n)\s+)*"
    r"(?P<route>(?:(?:l[ií]nea|tren|sitren)\s+)?[\w-]+)\s+"
    r"(?:en|por|hacia|a)\s+(?P<stop>.+?)\s*[?.!]*$",
    re.IGNORECASE,
)


class RouteAtStop(NamedTuple):
    route: str
    stop: str


def parse_route_at_stop(text: str) -> Optional[RouteAtStop]:
    """Separa "ruta en parada"; no comprueba que la ruta exista."""
    match = _INTENT_RE.match((text or "").strip().lstrip("¿¡ "))
    if not match:
        return None
    stop = match.group("stop").strip()
    if len(stop) < 2:
        return None
    return RouteAtStop(match.group("route"), stop)
