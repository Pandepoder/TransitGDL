from __future__ import annotations

import sqlite3
from dataclasses import dataclass, fields
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional
from uuid import uuid4

from .db_migrations import apply_migrations


@dataclass(slots=True)
class PredictionRecord:
    prediction_id: str
    route_id: str
    stop_id: str
    predicted_minutes: float
    weather_condition: str
    hour_of_day: int
    day_of_week: int  # 0=lunes … 6=domingo
    source: str  # 'schedule' | 'live'
    created_at: str
    # Predicción antes de la corrección aprendida y corrección que se aplicó:
    # con ambas se reconstruye el error total y el aprendizaje no se sesga.
    base_minutes: Optional[float] = None
    learned_correction: float = 0.0
    direction_id: Optional[int] = None
    method: Optional[str] = None


@dataclass(slots=True)
class BiasStats:
    route_id: str
    direction_id: int
    stop_id: str  # '*' agrega todas las paradas de la ruta y sentido
    period: str  # franja del día (ver prediction_learner.period_of)
    day_type: str  # 'weekday' | 'weekend'
    mean_error: float  # positivo → la unidad llega más tarde que la predicción base
    variance: float
    sample_count: int
    last_updated: str


_PREDICTION_FIELDS = tuple(f.name for f in fields(PredictionRecord))
_BIAS_FIELDS = tuple(f.name for f in fields(BiasStats))

_SCHEMA_V1 = """
CREATE TABLE IF NOT EXISTS predictions (
    prediction_id   TEXT PRIMARY KEY,
    route_id        TEXT NOT NULL,
    stop_id         TEXT NOT NULL,
    predicted_minutes REAL NOT NULL,
    weather_condition TEXT NOT NULL,
    hour_of_day     INTEGER NOT NULL,
    day_of_week     INTEGER NOT NULL,
    source          TEXT NOT NULL,
    created_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS feedback (
    feedback_id     TEXT PRIMARY KEY,
    prediction_id   TEXT NOT NULL,
    route_id        TEXT NOT NULL,
    stop_id         TEXT NOT NULL,
    predicted_minutes REAL NOT NULL,
    actual_minutes  REAL,
    error_minutes   REAL,
    rating          INTEGER,
    hour_of_day     INTEGER NOT NULL,
    day_of_week     INTEGER NOT NULL,
    created_at      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_feedback_pred
    ON feedback(prediction_id);
CREATE INDEX IF NOT EXISTS idx_feedback_route_stop
    ON feedback(route_id, stop_id);
-- Una sola calificación por predicción: evita que ratings duplicados
-- corrompan el promedio del PredictionLearner.
CREATE UNIQUE INDEX IF NOT EXISTS idx_feedback_unique_pred
    ON feedback(prediction_id);

CREATE TABLE IF NOT EXISTS bias_corrections (
    route_id      TEXT NOT NULL,
    stop_id       TEXT NOT NULL,
    hour_bucket   INTEGER NOT NULL,
    day_type      TEXT NOT NULL,
    mean_error    REAL NOT NULL DEFAULT 0.0,
    variance      REAL NOT NULL DEFAULT 0.0,
    sample_count  INTEGER NOT NULL DEFAULT 0,
    last_updated  TEXT NOT NULL,
    PRIMARY KEY (route_id, stop_id, hour_bucket, day_type)
);
"""

# v2: el aprendizaje anterior promediaba el error contra una predicción ya
# corregida y se estancaba en la mitad del sesgo real; sus estadísticas no son
# reutilizables, así que la tabla se sustituye.
_SCHEMA_V2 = """
ALTER TABLE predictions ADD COLUMN base_minutes REAL;
ALTER TABLE predictions ADD COLUMN learned_correction REAL NOT NULL DEFAULT 0;
ALTER TABLE predictions ADD COLUMN direction_id INTEGER;
ALTER TABLE predictions ADD COLUMN method TEXT;
CREATE INDEX IF NOT EXISTS idx_predictions_created ON predictions(created_at);

DROP TABLE IF EXISTS bias_corrections;
CREATE TABLE bias_stats (
    route_id      TEXT NOT NULL,
    direction_id  INTEGER NOT NULL,
    stop_id       TEXT NOT NULL,
    period        TEXT NOT NULL,
    day_type      TEXT NOT NULL,
    mean_error    REAL NOT NULL DEFAULT 0.0,
    variance      REAL NOT NULL DEFAULT 0.0,
    sample_count  INTEGER NOT NULL DEFAULT 0,
    last_updated  TEXT NOT NULL,
    PRIMARY KEY (route_id, direction_id, stop_id, period, day_type)
);
"""

MIGRATIONS = (_SCHEMA_V1, _SCHEMA_V2)

# rating: 1=llegó antes, 2=puntual, 3=poco tarde, 4=tarde, 5=muy tarde
RATING_TO_ERROR_MINUTES = {1: -5.0, 2: 0.0, 3: 5.0, 4: 12.0, 5: 20.0}
# Las predicciones que nadie calificó se borran pasado este tiempo.
PREDICTION_RETENTION_DAYS = 7


class FeedbackStore:
    """
    Almacena predicciones y calificaciones de usuarios en SQLite.
    Mantiene estadísticas de sesgo (BiasStats) por ruta, sentido, parada y
    franja para que el motor corrija sus estimaciones con el tiempo.
    """

    def __init__(self, db_path: str = "data/feedback.db"):
        self.db_path = db_path
        # Una única conexión por instancia. check_same_thread=False es seguro
        # porque todos los accesos ocurren en el hilo del bucle de eventos.
        self._conn: sqlite3.Connection | None = None
        self._initialized = False
        self._saves_since_purge = 0

    def initialize(self) -> None:
        if self._initialized:
            return
        if self.db_path != ":memory:":
            Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        conn = self._connect()
        # WAL permite lecturas concurrentes sin bloquear escrituras
        # (no aplica a conexiones en memoria)
        if self.db_path != ":memory:":
            conn.execute("PRAGMA journal_mode=WAL")
        apply_migrations(conn, MIGRATIONS)
        self._initialized = True
        self.purge_old_predictions()

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None
        self._initialized = False

    # ── Predicciones ──────────────────────────────────────────────────────────

    def save_prediction(self, record: PredictionRecord) -> None:
        columns = ", ".join(_PREDICTION_FIELDS)
        marks = ", ".join("?" for _ in _PREDICTION_FIELDS)
        with self._connect() as conn:
            conn.execute(
                f"INSERT OR IGNORE INTO predictions ({columns}) VALUES ({marks})",
                tuple(getattr(record, name) for name in _PREDICTION_FIELDS),
            )
        self._saves_since_purge += 1
        if self._saves_since_purge >= 500:
            self.purge_old_predictions()

    def get_prediction(self, prediction_id: str) -> Optional[PredictionRecord]:
        columns = ", ".join(_PREDICTION_FIELDS)
        row = (
            self._connect()
            .execute(
                f"SELECT {columns} FROM predictions WHERE prediction_id = ?",
                (prediction_id,),
            )
            .fetchone()
        )
        return PredictionRecord(**dict(row)) if row else None

    def purge_old_predictions(
        self, max_age_days: int = PREDICTION_RETENTION_DAYS
    ) -> int:
        """Borra las predicciones sin calificar más viejas que `max_age_days`."""
        self._saves_since_purge = 0
        cutoff = (datetime.now(timezone.utc) - timedelta(days=max_age_days)).isoformat()
        with self._connect() as conn:
            cursor = conn.execute(
                "DELETE FROM predictions WHERE created_at < ? AND prediction_id NOT IN "
                "(SELECT prediction_id FROM feedback)",
                (cutoff,),
            )
        return cursor.rowcount

    # ── Feedback de usuarios ──────────────────────────────────────────────────

    def save_feedback(
        self,
        prediction_id: str,
        *,
        actual_minutes: Optional[float] = None,
        rating: Optional[int] = None,
    ) -> Optional[float]:
        """
        Guarda la calificación y/o tiempo real del usuario.
        Retorna el error (minutos reales menos minutos predichos) o None si la
        calificación no se aceptó.
        """
        pred = self.get_prediction(prediction_id)
        if pred is None:
            return None

        # Rechazar feedback sobre predicciones de más de 2 horas
        try:
            created = datetime.fromisoformat(pred.created_at)
            if created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
            age_hours = (datetime.now(timezone.utc) - created).total_seconds() / 3600
            if age_hours > 2.0:
                return None
        except (ValueError, AttributeError):
            pass  # Si no se puede parsear la fecha, permitir el feedback

        error_minutes: Optional[float] = None
        if actual_minutes is not None:
            error_minutes = actual_minutes - pred.predicted_minutes
        elif rating is not None:
            error_minutes = RATING_TO_ERROR_MINUTES.get(rating, 0.0)

        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO feedback
                    (feedback_id, prediction_id, route_id, stop_id,
                     predicted_minutes, actual_minutes, error_minutes,
                     rating, hour_of_day, day_of_week, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    uuid4().hex,
                    prediction_id,
                    pred.route_id,
                    pred.stop_id,
                    pred.predicted_minutes,
                    actual_minutes,
                    error_minutes,
                    rating,
                    pred.hour_of_day,
                    pred.day_of_week,
                    now,
                ),
            )
        # Si rowcount == 0 el INSERT fue ignorado por el UNIQUE constraint
        # (feedback duplicado). Devolvemos None para que el learner NO se actualice.
        if cursor.rowcount == 0:
            return None
        return error_minutes

    # ── Estadísticas de sesgo ─────────────────────────────────────────────────

    def get_bias_stats(
        self,
        route_id: str,
        direction_id: int,
        stop_id: str,
        period: str,
        day_type: str,
    ) -> Optional[BiasStats]:
        columns = ", ".join(_BIAS_FIELDS)
        row = (
            self._connect()
            .execute(
                f"SELECT {columns} FROM bias_stats WHERE route_id=? AND direction_id=? "
                "AND stop_id=? AND period=? AND day_type=?",
                (route_id, direction_id, stop_id, period, day_type),
            )
            .fetchone()
        )
        return BiasStats(**dict(row)) if row else None

    def upsert_bias_stats(self, stats: BiasStats) -> None:
        columns = ", ".join(_BIAS_FIELDS)
        marks = ", ".join("?" for _ in _BIAS_FIELDS)
        with self._connect() as conn:
            conn.execute(
                f"INSERT OR REPLACE INTO bias_stats ({columns}) VALUES ({marks})",
                tuple(getattr(stats, name) for name in _BIAS_FIELDS),
            )

    def get_route_stats_summary(self, route_id: str) -> list[dict]:
        columns = ", ".join(_BIAS_FIELDS)
        rows = (
            self._connect()
            .execute(
                f"SELECT {columns} FROM bias_stats WHERE route_id=? "
                "ORDER BY direction_id, stop_id, period, day_type",
                (route_id,),
            )
            .fetchall()
        )
        return [dict(row) for row in rows]

    def get_global_stats(self) -> dict:
        conn = self._connect()
        total_predictions = conn.execute("SELECT COUNT(*) FROM predictions").fetchone()[
            0
        ]
        total_feedback = conn.execute("SELECT COUNT(*) FROM feedback").fetchone()[0]
        total_slots = conn.execute("SELECT COUNT(*) FROM bias_stats").fetchone()[0]
        avg_error = conn.execute(
            "SELECT AVG(ABS(mean_error)) FROM bias_stats WHERE sample_count >= 3"
        ).fetchone()[0]
        return {
            "total_predictions": total_predictions,
            "total_feedback": total_feedback,
            "active_bias_slots": total_slots,
            "avg_absolute_error_corrected_minutes": round(avg_error or 0.0, 2),
        }

    # ── Utilidades ────────────────────────────────────────────────────────────

    def _connect(self) -> sqlite3.Connection:
        if self._conn is None:
            target = ":memory:" if self.db_path == ":memory:" else self.db_path
            self._conn = sqlite3.connect(target, check_same_thread=False)
            self._conn.row_factory = sqlite3.Row
        return self._conn
