from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .models import Incident


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
        with self._connect() as conn:
            # WAL permite lecturas concurrentes sin bloquear escrituras
            if self.db_path != ":memory:":
                conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS incidents (
                    incident_id TEXT PRIMARY KEY,
                    description TEXT NOT NULL,
                    affected_routes TEXT NOT NULL,
                    severity TEXT NOT NULL,
                    active INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    resolved_at TEXT,
                    stop_ids TEXT NOT NULL
                )
                """)
        self._initialized = True

    def add_incident(self, incident: Incident) -> Incident:
        self.initialize()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO incidents (
                    incident_id,
                    description,
                    affected_routes,
                    severity,
                    active,
                    created_at,
                    resolved_at,
                    stop_ids
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    incident.incident_id,
                    incident.description,
                    json.dumps(incident.affected_routes, ensure_ascii=False),
                    incident.severity.value,
                    int(incident.active),
                    incident.created_at.isoformat(),
                    incident.resolved_at.isoformat() if incident.resolved_at else None,
                    json.dumps(incident.stop_ids, ensure_ascii=False),
                ),
            )
        return incident

    def list_incidents(
        self,
        *,
        active_only: bool = True,
        route_id: str | None = None,
        stop_id: str | None = None,
        limit: int = 50,
    ) -> list[Incident]:
        self.initialize()
        query = """
            SELECT
                incident_id,
                description,
                affected_routes,
                severity,
                active,
                created_at,
                resolved_at,
                stop_ids
            FROM incidents
        """
        params: list[object] = []
        clauses: list[str] = []
        if active_only:
            clauses.append("active = 1")
        # route_id y stop_id están guardados como JSON; usamos LIKE para filtrar
        # en SQL antes del LIMIT, evitando el bug de resultados silenciosamente
        # incompletos cuando los registros coincidentes caen más allá del límite.
        if route_id:
            clauses.append('affected_routes LIKE ?')
            params.append(f'%"{route_id}"%')
        if stop_id:
            clauses.append('stop_ids LIKE ?')
            params.append(f'%"{stop_id}"%')
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)

        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()

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
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT
                    incident_id,
                    description,
                    affected_routes,
                    severity,
                    active,
                    created_at,
                    resolved_at,
                    stop_ids
                FROM incidents
                WHERE incident_id = ?
                """,
                (incident_id,),
            ).fetchone()
        return self._row_to_incident(row) if row else None

    def count_active(self) -> int:
        self.initialize()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM incidents WHERE active = 1"
            ).fetchone()
        return int(row[0]) if row else 0

    def _connect(self) -> sqlite3.Connection:
        if self._conn is None:
            target = ":memory:" if self.db_path == ":memory:" else self.db_path
            self._conn = sqlite3.connect(target, check_same_thread=False)
            self._conn.row_factory = sqlite3.Row
        return self._conn

    @staticmethod
    def _matches_filters(
        incident: Incident,
        *,
        route_id: str | None = None,
        stop_id: str | None = None,
    ) -> bool:
        if route_id and route_id not in incident.affected_routes:
            return False
        if stop_id and stop_id not in incident.stop_ids:
            return False
        return True

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
        )
