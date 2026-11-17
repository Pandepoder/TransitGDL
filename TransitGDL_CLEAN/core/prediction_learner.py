from __future__ import annotations

import logging
from datetime import datetime, timezone

from .feedback_store import BiasStats, FeedbackStore
from .params import APRENDIZAJE

logger = logging.getLogger(__name__)

# Parada comodín: estadísticas de toda la ruta y sentido en una franja.
ROUTE_LEVEL = "*"

EMA_ALPHA = float(APRENDIZAJE["alfa"])
MIN_SAMPLES_TO_APPLY = int(APRENDIZAJE["min_muestras"])
MIN_ROUTE_SAMPLES = int(APRENDIZAJE["min_muestras_ruta"])
WEIGHT_K = float(APRENDIZAJE["peso_k"])
MAX_CORRECTION_MINUTES = float(APRENDIZAJE["correccion_max_min"])
MIN_SAMPLES_FOR_SPREAD = int(APRENDIZAJE["muestras_para_dispersion"])


def period_of(hour: int) -> str:
    """Franja del día a la que pertenece una hora (0–23)."""
    if hour <= 5:
        return "madrugada"
    if hour <= 9:
        return "pico_manana"
    if hour <= 12:
        return "valle_manana"
    if hour <= 15:
        return "pico_mediodia"
    if hour <= 17:
        return "valle_tarde"
    if hour <= 20:
        return "pico_tarde"
    return "noche"


def day_type_of(day_of_week: int) -> str:
    """0=lunes … 4=viernes → 'weekday'; sábado y domingo → 'weekend'."""
    return "weekend" if day_of_week >= 5 else "weekday"


class PredictionLearner:
    """
    Aprende el sesgo de las predicciones de ETA a partir de las calificaciones.

    Por cada ruta, sentido, parada, franja del día y tipo de día mantiene un
    promedio móvil del **error total**: lo que tardó la unidad menos la
    predicción *base* (sin corrección). Medir contra la base es lo que permite
    que la corrección converja al sesgo real; si se midiera contra la
    predicción ya corregida, el promedio se quedaría a medio camino.

    Además lleva las mismas estadísticas para toda la ruta (parada `*`). Una
    parada con pocas calificaciones se apoya en el promedio de su ruta, y pesa
    más conforme acumula datos propios.
    """

    def __init__(self, store: FeedbackStore):
        self._store = store

    # ── API pública ────────────────────────────────────────────────────────────

    def get_correction(
        self,
        route_id: str,
        stop_id: str,
        hour: int,
        day_of_week: int,
        direction_id: int | None = None,
    ) -> float:
        """Minutos a sumar (o restar) a la predicción base. 0 si aún no hay datos."""
        direction = direction_id or 0
        period, day_type = period_of(hour), day_type_of(day_of_week)
        route_stats = self._store.get_bias_stats(
            route_id, direction, ROUTE_LEVEL, period, day_type
        )
        prior = 0.0
        if route_stats and route_stats.sample_count >= MIN_ROUTE_SAMPLES:
            prior = route_stats.mean_error

        stats = self._store.get_bias_stats(
            route_id, direction, stop_id, period, day_type
        )
        if stats is None or stats.sample_count < MIN_SAMPLES_TO_APPLY:
            correction = prior
        else:
            weight = stats.sample_count / (stats.sample_count + WEIGHT_K)
            correction = weight * stats.mean_error + (1.0 - weight) * prior
        return max(-MAX_CORRECTION_MINUTES, min(MAX_CORRECTION_MINUTES, correction))

    def get_spread(
        self,
        route_id: str,
        stop_id: str,
        hour: int,
        day_of_week: int,
        direction_id: int | None = None,
    ) -> float | None:
        """
        Desviación estándar (min) del error observado en la franja, o None si
        aún no hay calificaciones suficientes para confiar en ella.
        """
        direction = direction_id or 0
        period, day_type = period_of(hour), day_type_of(day_of_week)
        for candidate in (stop_id, ROUTE_LEVEL):
            stats = self._store.get_bias_stats(
                route_id, direction, candidate, period, day_type
            )
            if stats and stats.sample_count >= MIN_SAMPLES_FOR_SPREAD:
                return stats.variance**0.5
        return None

    def record_feedback(
        self,
        route_id: str,
        stop_id: str,
        hour: int,
        day_of_week: int,
        error_minutes: float,
        direction_id: int | None = None,
        applied_correction: float = 0.0,
    ) -> None:
        """
        Incorpora una calificación. `error_minutes` es el error contra la
        predicción mostrada; se le suma la corrección que esa predicción ya
        traía para obtener el error contra la predicción base.
        """
        direction = direction_id or 0
        period, day_type = period_of(hour), day_type_of(day_of_week)
        total_error = error_minutes + applied_correction
        for target in (stop_id, ROUTE_LEVEL):
            self._update(route_id, direction, target, period, day_type, total_error)
        logger.debug(
            "Learner: ruta=%s sentido=%s parada=%s franja=%s error_total=%.1f",
            route_id,
            direction,
            stop_id,
            period,
            total_error,
        )

    def route_summary(
        self, route_id: str, hour: int, day_of_week: int
    ) -> tuple[int, float, float]:
        """
        (calificaciones, error medio, desviación) de la ruta en la franja
        actual, sumando ambos sentidos. Lo consume el sistema experto.
        """
        period, day_type = period_of(hour), day_type_of(day_of_week)
        rows = [
            row
            for row in self._store.get_route_stats_summary(route_id)
            if row["stop_id"] == ROUTE_LEVEL
            and row["period"] == period
            and row["day_type"] == day_type
        ]
        total = sum(int(row["sample_count"]) for row in rows)
        if total == 0:
            return 0, 0.0, 0.0
        mean = sum(row["mean_error"] * row["sample_count"] for row in rows) / total
        variance = sum(row["variance"] * row["sample_count"] for row in rows) / total
        return total, mean, variance**0.5

    # ── Privado ────────────────────────────────────────────────────────────────

    def _update(
        self,
        route_id: str,
        direction_id: int,
        stop_id: str,
        period: str,
        day_type: str,
        total_error: float,
    ) -> None:
        stats = self._store.get_bias_stats(
            route_id, direction_id, stop_id, period, day_type
        )
        if stats is None:
            mean, variance, count = total_error, 0.0, 1
        else:
            deviation = total_error - stats.mean_error
            mean = stats.mean_error + EMA_ALPHA * deviation
            # Varianza móvil del error alrededor de la media anterior.
            variance = (1.0 - EMA_ALPHA) * (stats.variance + EMA_ALPHA * deviation**2)
            count = stats.sample_count + 1
        self._store.upsert_bias_stats(
            BiasStats(
                route_id=route_id,
                direction_id=direction_id,
                stop_id=stop_id,
                period=period,
                day_type=day_type,
                mean_error=mean,
                variance=variance,
                sample_count=count,
                last_updated=datetime.now(timezone.utc).isoformat(),
            )
        )
