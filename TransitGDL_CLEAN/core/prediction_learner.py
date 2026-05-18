from __future__ import annotations

import logging

from .feedback_store import FeedbackStore

logger = logging.getLogger(__name__)

# ── Constantes de aprendizaje ──────────────────────────────────────────────────
#
# EMA_ALPHA controla qué tan rápido aprende el sistema.
#   - 0.1 → aprende lento, más estable ante valores atípicos.
#   - 0.3 → aprende más rápido, más reactivo a cambios recientes.
#
# MIN_SAMPLES_TO_APPLY: mínimo de calificaciones para activar la corrección.
# MAX_CORRECTION_MINUTES: límite absoluto para no sobrecompensar.

EMA_ALPHA = 0.2
MIN_SAMPLES_TO_APPLY = 3
MAX_CORRECTION_MINUTES = 20.0
MAX_CONFIDENCE_BOOST = 0.07
MAX_CONFIDENCE_PENALTY = 0.10


class PredictionLearner:
    """
    Calcula y aplica correcciones de sesgo a las predicciones de ETA.

    El aprendizaje funciona por franjas:
        • route_id + stop_id  →  contexto operativo
        • hour_bucket (0-3)   →  franja del día (madrugada/mañana/tarde/noche)
        • day_type            →  'weekday' (L-V) o 'weekend' (S-D)

    Por cada franja mantiene:
        mean_error  = promedio EMA de (tiempo_real − predicción)
                      + positivo → el camión llega tarde habitualmente
                      - negativo → el camión llega antes de lo estimado
        variance    = varianza EMA del error (mide qué tan impredecible es la ruta)
        sample_count = cuántas calificaciones hay acumuladas

    Cuando hay ≥ MIN_SAMPLES_TO_APPLY muestras:
        • get_correction()         → devuelve los minutos a sumar/restar al ETA
        • get_confidence_adjustment() → sube o baja la confianza según varianza
    """

    def __init__(self, store: FeedbackStore):
        self._store = store

    # ── API pública ────────────────────────────────────────────────────────────

    def get_correction(
        self, route_id: str, stop_id: str, hour: int, day_of_week: int
    ) -> float:
        """
        Retorna los minutos a añadir (o restar) a la predicción base.
        Devuelve 0.0 si no hay datos suficientes.
        """
        stats = self._get_stats(route_id, stop_id, hour, day_of_week)
        if stats is None or stats.sample_count < MIN_SAMPLES_TO_APPLY:
            return 0.0
        correction = stats.mean_error
        # Clampeamos para evitar correcciones extremas
        return max(-MAX_CORRECTION_MINUTES, min(MAX_CORRECTION_MINUTES, correction))

    def get_confidence_adjustment(
        self, route_id: str, stop_id: str, hour: int, day_of_week: int
    ) -> float:
        """
        Retorna un ajuste de confianza basado en la varianza histórica.
          +MAX_CONFIDENCE_BOOST → ruta muy predecible en esta franja
          -MAX_CONFIDENCE_PENALTY → ruta muy impredecible
          0.0 → sin datos suficientes
        """
        stats = self._get_stats(route_id, stop_id, hour, day_of_week)
        if stats is None or stats.sample_count < MIN_SAMPLES_TO_APPLY:
            return 0.0

        std_dev = stats.variance**0.5

        # Umbral de alta confianza: desviación < 3 min con ≥ 15 muestras
        if std_dev < 3.0 and stats.sample_count >= 15:
            return MAX_CONFIDENCE_BOOST
        # Umbral de baja confianza: desviación > 10 min
        if std_dev > 10.0:
            return -MAX_CONFIDENCE_PENALTY
        # Penalización proporcional entre 3 y 10 min de std
        if std_dev > 3.0:
            slope = MAX_CONFIDENCE_PENALTY / 7.0  # degradación lineal
            return -slope * (std_dev - 3.0)
        return 0.0

    def record_feedback(
        self,
        route_id: str,
        stop_id: str,
        hour: int,
        day_of_week: int,
        error_minutes: float,
    ) -> None:
        """
        Actualiza el modelo con un nuevo error observado usando EMA.
        Llamar esto cada vez que llega una calificación con error_minutes conocido.
        """
        bucket = _hour_bucket(hour)
        day_type = _day_type(day_of_week)
        stats = self._store.get_bias_stats(route_id, stop_id, bucket, day_type)

        if stats is None:
            new_mean = error_minutes
            new_variance = 0.0
            new_count = 1
        else:
            # EMA del error medio
            new_mean = EMA_ALPHA * error_minutes + (1.0 - EMA_ALPHA) * stats.mean_error
            # EMA de la varianza (aproximación de Welford sobre ventana deslizante)
            new_variance = (1.0 - EMA_ALPHA) * (
                stats.variance + EMA_ALPHA * (error_minutes - stats.mean_error) ** 2
            )
            new_count = stats.sample_count + 1

        self._store.upsert_bias_stats(
            route_id, stop_id, bucket, day_type, new_mean, new_variance, new_count
        )
        logger.debug(
            "Learner actualizado: route=%s stop=%s bucket=%s day=%s "
            "error=%.1f nueva_media=%.1f varianza=%.1f n=%d",
            route_id,
            stop_id,
            bucket,
            day_type,
            error_minutes,
            new_mean,
            new_variance,
            new_count,
        )

    # ── Privado ────────────────────────────────────────────────────────────────

    def _get_stats(self, route_id: str, stop_id: str, hour: int, day_of_week: int):
        bucket = _hour_bucket(hour)
        day_type = _day_type(day_of_week)
        return self._store.get_bias_stats(route_id, stop_id, bucket, day_type)


# ── Funciones auxiliares ───────────────────────────────────────────────────────


def _hour_bucket(hour: int) -> int:
    """
    Divide el día en 4 franjas de 6 horas:
        0 → 00–05 h  (madrugada)
        1 → 06–11 h  (mañana)
        2 → 12–17 h  (tarde)
        3 → 18–23 h  (noche)
    """
    return hour // 6


def _day_type(day_of_week: int) -> str:
    """0=lunes … 4=viernes → 'weekday', 5–6 → 'weekend'."""
    return "weekend" if day_of_week >= 5 else "weekday"
