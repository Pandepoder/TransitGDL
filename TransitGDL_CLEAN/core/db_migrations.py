"""
Migraciones de las bases de operación (SQLite).

Cada base lleva su versión de esquema en `PRAGMA user_version`. Una migración
es un script SQL o una función que recibe la conexión; se aplican en orden y
cada una una sola vez, así una base existente se actualiza sin perder datos.
Cada migración corre dentro de una transacción: o se aplica completa o no deja
rastro.
"""

from __future__ import annotations

import sqlite3
from typing import Callable, Sequence

Migration = str | Callable[[sqlite3.Connection], None]


def schema_version(conn: sqlite3.Connection) -> int:
    return int(conn.execute("PRAGMA user_version").fetchone()[0])


def apply_migrations(conn: sqlite3.Connection, migrations: Sequence[Migration]) -> int:
    """Aplica las migraciones pendientes y devuelve la versión resultante."""
    current = schema_version(conn)
    for version, migration in enumerate(migrations, start=1):
        if version <= current:
            continue
        conn.commit()
        # PRAGMA no admite parámetros; `version` es un entero propio.
        stamp = f"PRAGMA user_version = {int(version)};"
        try:
            if callable(migration):
                conn.execute("BEGIN")
                migration(conn)
                conn.execute(stamp)
                conn.commit()
            else:
                conn.executescript(f"BEGIN;\n{migration}\n{stamp}\nCOMMIT;")
        except Exception:
            if conn.in_transaction:
                conn.rollback()
            raise
        current = version
    return current
