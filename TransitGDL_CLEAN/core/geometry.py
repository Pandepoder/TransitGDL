"""
Geometría sobre trazos de ruta: referencia lineal, simplificación y codificación.

Un `Polyline` guarda la distancia acumulada de cada vértice, de modo que una
posición sobre la ruta se describe con un solo número: los metros recorridos
desde la terminal. Con eso se puede saber qué tan lejos está un vehículo de una
parada *por la ruta* y no en línea recta.

Las distancias se calculan en un plano local (proyección equirectangular
centrada en la latitud media del trazo); el error es despreciable a escala de
ciudad y es mucho más rápido que la fórmula del haversine punto por punto.
"""

from __future__ import annotations

from bisect import bisect_right
from math import atan2, cos, degrees, hypot, pi, radians
from typing import Iterable, Sequence

Point = tuple[float, float]  # (lat, lon)

EARTH_RADIUS_M = 6_371_000.0
METERS_PER_DEGREE = EARTH_RADIUS_M * pi / 180.0


class Polyline:
    """Polilínea con distancias acumuladas en metros."""

    __slots__ = ("points", "cum", "_x", "_y", "_kx")

    def __init__(self, points: Iterable[Point]):
        clean: list[Point] = []
        for lat, lon in points:
            point = (float(lat), float(lon))
            if not clean or point != clean[-1]:
                clean.append(point)
        self.points: list[Point] = clean

        lat0 = sum(p[0] for p in clean) / len(clean) if clean else 0.0
        self._kx = cos(radians(lat0)) * METERS_PER_DEGREE
        self._x = [p[1] * self._kx for p in clean]
        self._y = [p[0] * METERS_PER_DEGREE for p in clean]

        cum = [0.0]
        for i in range(1, len(clean)):
            cum.append(
                cum[-1]
                + hypot(self._x[i] - self._x[i - 1], self._y[i] - self._y[i - 1])
            )
        self.cum: list[float] = cum if clean else []

    def __len__(self) -> int:
        return len(self.points)

    @property
    def length_m(self) -> float:
        return self.cum[-1] if self.cum else 0.0

    def project(
        self,
        lat: float,
        lon: float,
        from_m: float = 0.0,
        window_m: float | None = None,
    ) -> tuple[float, float]:
        """
        Punto del trazo más cercano a (lat, lon).

        Devuelve (distancia sobre el trazo, separación lateral), ambas en metros.
        La búsqueda avanza desde `from_m` y, si se indica `window_m`, se limita
        a ese tramo: así una ruta que pasa dos veces por la misma calle asigna
        cada parada a la pasada correcta.
        """
        n = len(self.points)
        if n == 0:
            return 0.0, float("inf")
        px, py = lon * self._kx, lat * METERS_PER_DEGREE
        if n == 1:
            return 0.0, hypot(px - self._x[0], py - self._y[0])

        from_m = min(max(from_m, 0.0), self.cum[-1])
        start = min(max(bisect_right(self.cum, from_m) - 1, 0), n - 2)
        limit = self.cum[-1] if window_m is None else from_m + window_m

        best_d2 = float("inf")
        best_dist = from_m
        xs, ys, cum = self._x, self._y, self.cum
        for i in range(start, n - 1):
            if cum[i] > limit:
                break
            ax, ay = xs[i], ys[i]
            dx, dy = xs[i + 1] - ax, ys[i + 1] - ay
            seg2 = dx * dx + dy * dy
            t = ((px - ax) * dx + (py - ay) * dy) / seg2 if seg2 > 0.0 else 0.0
            t = 0.0 if t < 0.0 else 1.0 if t > 1.0 else t
            qx, qy = ax + t * dx, ay + t * dy
            d2 = (px - qx) ** 2 + (py - qy) ** 2
            if d2 < best_d2:
                best_d2 = d2
                best_dist = cum[i] + t * (cum[i + 1] - cum[i])
        return max(best_dist, from_m), best_d2**0.5

    def _segment_at(self, dist_m: float) -> tuple[int, float]:
        """Índice del segmento y fracción (0–1) que corresponden a `dist_m`."""
        n = len(self.points)
        if n < 2:
            return 0, 0.0
        dist_m = min(max(dist_m, 0.0), self.cum[-1])
        i = min(max(bisect_right(self.cum, dist_m) - 1, 0), n - 2)
        span = self.cum[i + 1] - self.cum[i]
        return i, ((dist_m - self.cum[i]) / span if span > 0.0 else 0.0)

    def point_at(self, dist_m: float) -> Point:
        """Coordenadas a `dist_m` metros del inicio."""
        if not self.points:
            return 0.0, 0.0
        if len(self.points) == 1:
            return self.points[0]
        i, t = self._segment_at(dist_m)
        (lat_a, lon_a), (lat_b, lon_b) = self.points[i], self.points[i + 1]
        return lat_a + (lat_b - lat_a) * t, lon_a + (lon_b - lon_a) * t

    def bearing_at(self, dist_m: float) -> float:
        """Rumbo en grados (0 = norte, 90 = este) del trazo en `dist_m`."""
        if len(self.points) < 2:
            return 0.0
        i, _ = self._segment_at(dist_m)
        dx = self._x[i + 1] - self._x[i]
        dy = self._y[i + 1] - self._y[i]
        return (degrees(atan2(dx, dy)) + 360.0) % 360.0

    def slice(self, start_m: float, end_m: float) -> list[Point]:
        """Puntos del trazo entre dos distancias (incluye los extremos)."""
        if len(self.points) < 2 or end_m <= start_m:
            return [self.point_at(start_m)]
        start_m = max(start_m, 0.0)
        end_m = min(end_m, self.cum[-1])
        first = bisect_right(self.cum, start_m)
        last = bisect_right(self.cum, end_m)
        return [self.point_at(start_m), *self.points[first:last], self.point_at(end_m)]


def simplify(points: Sequence[Point], tolerance_m: float) -> list[Point]:
    """Simplificación de Douglas–Peucker (iterativa) con tolerancia en metros."""
    n = len(points)
    if n <= 2 or tolerance_m <= 0.0:
        return list(points)

    lat0 = sum(p[0] for p in points) / n
    kx = cos(radians(lat0)) * METERS_PER_DEGREE
    xs = [p[1] * kx for p in points]
    ys = [p[0] * METERS_PER_DEGREE for p in points]
    tol2 = tolerance_m * tolerance_m

    keep = [False] * n
    keep[0] = keep[-1] = True
    stack = [(0, n - 1)]
    while stack:
        first, last = stack.pop()
        if last <= first + 1:
            continue
        ax, ay = xs[first], ys[first]
        dx, dy = xs[last] - ax, ys[last] - ay
        seg2 = dx * dx + dy * dy
        worst, index = -1.0, -1
        for i in range(first + 1, last):
            if seg2 > 0.0:
                t = ((xs[i] - ax) * dx + (ys[i] - ay) * dy) / seg2
                t = 0.0 if t < 0.0 else 1.0 if t > 1.0 else t
                d2 = (xs[i] - ax - t * dx) ** 2 + (ys[i] - ay - t * dy) ** 2
            else:
                d2 = (xs[i] - ax) ** 2 + (ys[i] - ay) ** 2
            if d2 > worst:
                worst, index = d2, i
        if worst > tol2:
            keep[index] = True
            stack.append((first, index))
            stack.append((index, last))
    return [p for p, k in zip(points, keep, strict=True) if k]


def encode_polyline(points: Iterable[Point], precision: int = 5) -> str:
    """Codifica (lat, lon) con el algoritmo de polilínea de Google."""
    factor = 10**precision
    out: list[str] = []
    prev_lat = prev_lon = 0
    for lat, lon in points:
        ilat, ilon = round(lat * factor), round(lon * factor)
        for delta in (ilat - prev_lat, ilon - prev_lon):
            value = ~(delta << 1) if delta < 0 else delta << 1
            while value >= 0x20:
                out.append(chr((0x20 | (value & 0x1F)) + 63))
                value >>= 5
            out.append(chr(value + 63))
        prev_lat, prev_lon = ilat, ilon
    return "".join(out)


def decode_polyline(text: str, precision: int = 5) -> list[Point]:
    """Inversa de `encode_polyline`."""
    factor = 10**precision
    points: list[Point] = []
    index = lat = lon = 0
    length = len(text)
    while index < length:
        for is_lon in (False, True):
            shift = result = 0
            while True:
                byte = ord(text[index]) - 63
                index += 1
                result |= (byte & 0x1F) << shift
                shift += 5
                if byte < 0x20:
                    break
            delta = ~(result >> 1) if result & 1 else result >> 1
            if is_lon:
                lon += delta
            else:
                lat += delta
        points.append((lat / factor, lon / factor))
    return points
