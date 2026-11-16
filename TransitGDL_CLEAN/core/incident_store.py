from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .db_migrations import apply_migrations
from .models import Incident
from .params import INCIDENCIAS

_SCHEMA_V1 = """
CREATE TABLE IF NOT EXISTS incidents (
    incident_id TEXT PRIMARY KEY,
    description TEXT NOT NULL,
    affected_routes TEXT NOT NULL,
    severity TEXT NOT NULL,
    active INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    resolved_at TEXT,
    stop_ids TEXT NOT NULL
);
"""


def _migrate_v2(conn: sqlite3.Connection) -> None:
    """Caducidad, ubicación y tablas de enlace con rutas y paradas."""
    for statement in (
        "ALTER TABLE incidents ADD COLUMN expires_at TEXT",
        "ALTER TABLE incidents ADD COLUMN lat REAL",
        "ALTER TABLE incidents ADD COLUMN lon REAL",
        "ALTER TABLE incidents ADD COLUMN source TEXT NOT NULL DEFAULT 'web'",
        "CREATE TABLE incident_routes (incident_id TEXT NOT NULL, route_id TEXT NOT NULL, "
        "PRIMARY KEY (incident_id, route_id))",
        "CREATE TABLE incident_stops (incident_id TEXT NOT NULL, stop_id TEXT NOT NULL, "
        "PRIMARY KEY (incident_id, stop_id))",
        "CREATE INDEX idx_incident_routes_route ON incident_routes(route_id)",
        "CREATE INDEX idx_incident_stops_stop ON incident_stops(stop_id)",
        "CREATE INDEX idx_incidents_active ON incidents(active, expires_at)",
    ):
        conn.execute(statement)
    # Las incidencias existentes guardaban rutas y paradas como texto JSON.
    rows = conn.execute(
        "SELECT incident_id, affected_routes, stop_ids FROM incidents"
    ).fetchall()
    for incident_id, routes, stops in rows:
        for route_id in set(json.loads(routes or "[]")):
            conn.execute(
                "INSERT OR IGNORE INTO incident_routes VALUES (?, ?)",
                (incident_id, route_id),
            )
        for stop_id in set(json.loads(stops or "[]")):
            conn.execute(
                "INSERT OR IGNORE INTO incident_stops VALUES (?, ?)",
                (incident_id, stop_id),
            )


MIGRATIONS = (_SCHEMA_V1, _migrate_v2)

_COLUMNS = (
    "incident_id, description, affected_routes, severity, active, created_at, "
    "resolved_at, stop_ids, expires_at, lat, lon, source"
)


def default_expiry(incident: Incident) -> datetime:
    """Momento en que la incidencia deja de considerarse activa si nadie la resuelve."""
    hours = float(INCIDENCIAS["caducidad_horas"].get(incident.severity.value, 2))
    return incident.created_at + timedelta(hours=hours)


class IncidentStore:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self._conn: sqlite3.Connection | None = None
        self._initialized = False

    def initialize(self) -> None:
        if self._initialized:
            return
        if self.db_path != ":memory:":
            Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        conn = self._connect()
        # WAL permite lecturas concurrentes sin bloquear escrituras
        if self.db_path != ":memory:":
            conn.execute("PRAGMA journal_mode=WAL")
        apply_migrations(conn, MIGRATIONS)
        self._initialized = True

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None
        self._initialized = False

    def add_incident(self, incident: Incident) -> Incident:
        self.initialize()
        if incident.expires_at is None:
            incident.expires_at = default_expiry(incident)
        with self._connect() as conn:
            conn.execute(
                f"INSERT OR REPLACE INTO incidents ({_COLUMNS}) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    incident.incident_id,
                    incident.description,
                    json.dumps(incident.affected_routes, ensure_ascii=False),
                    incident.severity.value,
                    int(incident.active),
                    incident.created_at.isoformat(),
                    incident.resolved_at.isoformat() if incident.resolved_at else None,
                    json.dumps(incident.stop_ids, ensure_ascii=False),
                    _utc_iso(incident.expires_at),
                    incident.lat,
                    incident.lon,
                    incident.source,
                ),
            )
            conn.execute(
                "DELETE FROM incident_routes WHERE incident_id = ?",
                (incident.incident_id,),
            )
            conn.execute(
                "DELETE FROM incident_stops WHERE incident_id = ?",
                (incident.incident_id,),
            )
            conn.executemany(
                "INSERT OR IGNORE INTO incident_routes VALUES (?, ?)",
                [(incident.incident_id, rid) for rid in incident.affected_routes],
            )
            conn.executemany(
                "INSERT OR IGNORE INTO incident_stops VALUES (?, ?)",
                [(incident.incident_id, sid) for sid in incident.stop_ids],
            )
        return incident

    def list_incidents(
        self,
        *,
        active_only: bool = True,
        route_id: str | None = None,
        stop_id: str | None = None,
        limit: int = 50,
        now: datetime | None = None,
    ) -> list[Incident]:
        """
        Incidencias, las más recientes primero. Con `active_only` se omiten las
        resueltas y las que ya caducaron.
        """
        self.initialize()
        clauses: list[str] = []
        params: list[object] = []
        if active_only:
            clauses.append("active = 1 AND (expires_at IS NULL OR expires_at > ?)")
            params.append(_utc_iso(now or datetime.now(timezone.utc)))
        if route_id:
            clauses.append(
                "incident_id IN (SELECT incident_id FROM incident_routes WHERE route_id = ?)"
            )
            params.append(route_id)
        if stop_id:
            clauses.append(
                "incident_id IN (SELECT incident_id FROM incident_stops WHERE stop_id = ?)"
            )
            params.append(stop_id)
        query = f"SELECT {_COLUMNS} FROM incidents"
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)

        rows = self._connect().execute(query, params).fetchall()
        return [self._row_to_incident(row) for row in rows]

    def resolve_incident(self, incident_id: str) -> Incident | None:
        self.initialize()
        current = self.get_incident(incident_id)
        if current is None:
            return None
        current.active = False
        current.resolved_at = datetime.now(timezone.utc)
        return self.add_incident(current)

    def get_incident(self, incident_id: str) -> Incident | None:
        self.initialize()
        row = (
            self._connect()
            .execute(
                f"SELECT {_COLUMNS} FROM incidents WHERE incident_id = ?",
                (incident_id,),
            )
            .fetchone()
        )
        return self._row_to_incident(row) if row else None

    def count_active(self, now: datetime | None = None) -> int:
        self.initialize()
        row = (
            self._connect()
            .execute(
                "SELECT COUNT(*) FROM incidents "
                "WHERE active = 1 AND (expires_at IS NULL OR expires_at > ?)",
                (_utc_iso(now or datetime.now(timezone.utc)),),
            )
            .fetchone()
        )
        return int(row[0]) if row else 0

    def _connect(self) -> sqlite3.Connection:
        if self._conn is None:
            target = ":memory:" if self.db_path == ":memory:" else self.db_path
            self._conn = sqlite3.connect(target, check_same_thread=False)
            self._conn.row_factory = sqlite3.Row
        return self._conn

    @staticmethod
    def _row_to_incident(row: sqlite3.Row) -> Incident:
        return Incident(
            incident_id=row["incident_id"],
            description=row["description"],
            affected_routes=json.loads(row["affected_routes"] or "[]"),
            severity=row["severity"],
            active=bool(row["active"]),
            created_at=row["created_at"],
            resolved_at=row["resolved_at"],
            stop_ids=json.loads(row["stop_ids"] or "[]"),
            expires_at=row["expires_at"],
            lat=row["lat"],
            lon=row["lon"],
            source=row["source"] or "web",
        )


def _utc_iso(moment: datetime) -> str:
    """Fecha en UTC con formato fijo, para que el orden de texto sea el cronológico."""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).isoformat()
