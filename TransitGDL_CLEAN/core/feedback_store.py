from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from uuid import uuid4


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


@dataclass(slots=True)
class BiasStats:
    route_id: str
    stop_id: str
    hour_bucket: int  # 0–3  (hora // 6)
    day_type: str  # 'weekday' | 'weekend'
    mean_error: float  # positivo → camión llega tarde habitualmente
    variance: float  # dispersión del error
    sample_count: int
    last_updated: str


class FeedbackStore:
    """
    Almacena predicciones y calificaciones de usuarios en SQLite.
    Mantiene estadísticas de sesgo (BiasStats) por franja horaria / ruta / parada
    para que el motor corrija sus estimaciones con el tiempo.
    """

    SCHEMA = """
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
    -- corrompan el EMA del PredictionLearner.
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

    def __init__(self, db_path: str = "data/feedback.db"):
        self.db_path = db_path
        # Una única conexión por instancia en ambos modos.
        # check_same_thread=False es seguro porque uvicorn usa un solo hilo
        # por worker y SQLite en WAL maneja la concurrencia a nivel de escrituras.
        self._conn: sqlite3.Connection | None = None
        self._initialized = False

    def initialize(self) -> None:
        if self._initialized:
            return
        if self.db_path != ":memory:":
            Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            # WAL permite lecturas concurrentes sin bloquear escrituras
            # (no aplica a conexiones en memoria)
            if self.db_path != ":memory:":
                conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(self.SCHEMA)
        self._initialized = True

    # ── Predicciones ──────────────────────────────────────────────────────────

    def save_prediction(self, record: PredictionRecord) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO predictions
                    (prediction_id, route_id, stop_id, predicted_minutes,
                     weather_condition, hour_of_day, day_of_week, source, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.prediction_id,
                    record.route_id,
                    record.stop_id,
                    record.predicted_minutes,
                    record.weather_condition,
                    record.hour_of_day,
                    record.day_of_week,
                    record.source,
                    record.created_at,
                ),
            )

    def get_prediction(self, prediction_id: str) -> Optional[PredictionRecord]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM predictions WHERE prediction_id = ?", (prediction_id,)
            ).fetchone()
        if row is None:
            return None
        return PredictionRecord(**dict(row))

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
        Retorna error_minutes calculado si hay suficiente info, o None.
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
            # Convertir rating cualitativo a error aproximado
            # rating: 1=llegó antes (-5min), 2=puntual (0), 3=poco tarde (+5), 4=tarde (+12), 5=muy tarde (+20)
            _rating_to_error = {1: -5.0, 2: 0.0, 3: 5.0, 4: 12.0, 5: 20.0}
            error_minutes = _rating_to_error.get(rating, 0.0)

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
        stop_id: str,
        hour_bucket: int,
        day_type: str,
    ) -> Optional[BiasStats]:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM bias_corrections
                WHERE route_id=? AND stop_id=? AND hour_bucket=? AND day_type=?
                """,
                (route_id, stop_id, hour_bucket, day_type),
            ).fetchone()
        if row is None:
            return None
        return BiasStats(**dict(row))

    def upsert_bias_stats(
        self,
        route_id: str,
        stop_id: str,
        hour_bucket: int,
        day_type: str,
        mean_error: float,
        variance: float,
        sample_count: int,
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO bias_corrections
                    (route_id, stop_id, hour_bucket, day_type,
                     mean_error, variance, sample_count, last_updated)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(route_id, stop_id, hour_bucket, day_type)
                DO UPDATE SET
                    mean_error=excluded.mean_error,
                    variance=excluded.variance,
                    sample_count=excluded.sample_count,
                    last_updated=excluded.last_updated
                """,
                (
                    route_id,
                    stop_id,
                    hour_bucket,
                    day_type,
                    mean_error,
                    variance,
                    sample_count,
                    now,
                ),
            )

    def get_route_stats_summary(self, route_id: str) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT
                    bc.stop_id,
                    bc.hour_bucket,
                    bc.day_type,
                    bc.mean_error,
                    bc.variance,
                    bc.sample_count,
                    COUNT(f.feedback_id) as total_feedback
                FROM bias_corrections bc
                LEFT JOIN feedback f ON f.route_id=bc.route_id AND f.stop_id=bc.stop_id
                WHERE bc.route_id=?
                GROUP BY bc.stop_id, bc.hour_bucket, bc.day_type
                ORDER BY bc.stop_id, bc.hour_bucket
                """,
                (route_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_global_stats(self) -> dict:
        with self._connect() as conn:
            total_predictions = conn.execute(
                "SELECT COUNT(*) FROM predictions"
            ).fetchone()[0]
            total_feedback = conn.execute("SELECT COUNT(*) FROM feedback").fetchone()[0]
            total_slots = conn.execute(
                "SELECT COUNT(*) FROM bias_corrections"
            ).fetchone()[0]
            avg_error = conn.execute(
                "SELECT AVG(ABS(mean_error)) FROM bias_corrections WHERE sample_count >= 3"
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
