"""B1.1 Postgres-Board — P1 Adapter: schmale psycopg3-Zugriffsschicht.

Zweck: EIN Ort, über den alle Schreiber künftig gehen (Dispatcher, CLI, Bridge,
Dashboard, Worker-Tools, Decompose, Heartbeat) — löst strukturell das
call-site-flock-Problem ab (Serialisierung DB-seitig, siehe Bauplan §3/§P3).

Dieser Stand deckt den Dry-Run (P4) ab: connect, transaction, execute, query,
copy_in. Die Lock-Semantik (FOR UPDATE SKIP LOCKED / pg_advisory_lock) kommt in
P3 obendrauf — hier bewusst noch nicht, damit der Dry-Run klein und prüfbar bleibt.
"""
from __future__ import annotations

import contextlib
from typing import Any, Iterable, Sequence

import psycopg
from psycopg import sql


class PgKanban:
    """Dünne Hülle um eine psycopg3-Verbindung (ein Store, ein Zugriffspunkt)."""

    def __init__(self, conninfo: str):
        self._conninfo = conninfo
        self._conn: psycopg.Connection | None = None

    def connect(self) -> "PgKanban":
        self._conn = psycopg.connect(self._conninfo, autocommit=False)
        return self

    @property
    def conn(self) -> psycopg.Connection:
        if self._conn is None:
            raise RuntimeError("PgKanban.connect() wurde nicht aufgerufen")
        return self._conn

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def __enter__(self) -> "PgKanban":
        return self.connect()

    def __exit__(self, *exc) -> None:
        self.close()

    @contextlib.contextmanager
    def transaction(self):
        """Eine echte DB-Transaktion (commit/rollback DB-seitig — nicht Datei-flock)."""
        with self.conn.transaction():
            yield self.conn

    def execute(self, query: str, params: Sequence[Any] | None = None) -> None:
        with self.conn.cursor() as cur:
            cur.execute(query, params)

    def executescript(self, script: str) -> None:
        """Mehrere Statements (z. B. das Schema-SQL) in einer Transaktion ausführen."""
        with self.conn.cursor() as cur:
            cur.execute(script)
        self.conn.commit()

    def query(self, query: str, params: Sequence[Any] | None = None) -> list[tuple]:
        with self.conn.cursor() as cur:
            cur.execute(query, params)
            return cur.fetchall()

    def scalar(self, query: str, params: Sequence[Any] | None = None) -> Any:
        rows = self.query(query, params)
        return rows[0][0] if rows else None

    def copy_in(self, table: str, columns: Sequence[str], rows: Iterable[Sequence[Any]]) -> int:
        """Bulk-Load via COPY (schnell + typsicher). Gibt die Zeilenzahl zurück."""
        n = 0
        cols_sql = sql.SQL(", ").join(sql.Identifier(c) for c in columns)
        copy_sql = sql.SQL("COPY {} ({}) FROM STDIN").format(sql.Identifier(table), cols_sql)
        with self.conn.cursor() as cur:
            with cur.copy(copy_sql) as cp:
                for row in rows:
                    cp.write_row(row)
                    n += 1
        return n

    def reset_identity(self, table: str, id_col: str = "id") -> None:
        """IDENTITY-Sequenz nach explizitem ID-Load auf max(id)+1 setzen."""
        seq = self.scalar(
            "SELECT pg_get_serial_sequence(%s, %s)", (table, id_col)
        )
        if seq is None:
            return
        self.execute(
            f"SELECT setval(%s, COALESCE((SELECT max({id_col}) FROM {table}), 1), "
            f"(SELECT count(*) FROM {table}) > 0)",
            (seq,),
        )
        self.conn.commit()
