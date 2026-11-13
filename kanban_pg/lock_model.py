"""B1.1 Postgres-Board — P3 Lock-Modell (DB-seitige Serialisierung).

Ersetzt strukturell die freiwilligen Datei-flocks (`<db>.dispatch.lock`,
write_serialize_lock) durch echte DB-Mechanik. Egal ob ein Aufrufer "den Lock
vergisst" — die DB serialisiert.

Zwei Bausteine:
  * claim_one_ready(): atomarer Claim via `SELECT ... FOR UPDATE SKIP LOCKED`.
    Nebenläufige Claimer überspringen gesperrte Zeilen -> kein Doppelclaim,
    kein Warten. Ersetzt claim_task + Claim-Race.
  * board-Advisory-Lock via `pg_advisory_xact_lock` / `pg_try_advisory_xact_lock`:
    board-scoped Single-Writer-Sektion (z. B. Dispatcher-Tick) OHNE Datei-flock;
    transaktionsgebunden -> wird bei commit/rollback automatisch freigegeben
    (kein verwaister Lock mehr).
"""
from __future__ import annotations

import hashlib


def board_lock_key(board: str) -> int:
    """Stabiler 63-bit-Schlüssel aus dem Board-Slug für pg_advisory_lock."""
    h = hashlib.blake2b(board.encode("utf-8"), digest_size=8).digest()
    return int.from_bytes(h, "big", signed=True)


def try_board_lock(conn, board: str) -> bool:
    """Transaktionsgebundenes Try-Lock. True = erhalten, False = anderer hält es."""
    cur = conn.execute("SELECT pg_try_advisory_xact_lock(%s)", (board_lock_key(board),))
    return bool(cur.fetchone()[0])


def try_board_session_lock(conn, board: str) -> bool:
    """Session-gebundenes Try-Lock (NICHT txn-gebunden).

    Ersatz für den Dispatcher-Tick-Datei-flock (``<db>.dispatch.lock``): der
    Tick committet intern pro Teiloperation (reclaim/recompute/claim), ein
    txn-gebundener Lock würde beim ersten Commit fallen. Ein Session-Lock hält
    über diese Commits hinweg, bis :func:`release_board_session_lock`. MUSS
    explizit freigegeben werden (anders als try_board_lock). True = erhalten.
    """
    cur = conn.execute("SELECT pg_try_advisory_lock(%s)", (board_lock_key(board),))
    return bool(cur.fetchone()[0])


def release_board_session_lock(conn, board: str) -> bool:
    """Gibt den Session-Advisory-Lock frei. True wenn er gehalten wurde."""
    cur = conn.execute("SELECT pg_advisory_unlock(%s)", (board_lock_key(board),))
    return bool(cur.fetchone()[0])


def claim_one_ready(conn, assignee: str, claim_lock: str, now_epoch: int,
                    worker_pid: int | None = None, ttl_seconds: int = 600) -> str | None:
    """Claimt EINE ready-Karte des Assignees atomar. Gibt die id oder None.

    Setzt ``claim_expires = now + ttl_seconds`` (Grundlage für reclaim_stale).
    Muss in einer Transaktion laufen (conn autocommit=False); der Aufrufer
    committet danach (gibt die FOR-UPDATE-Sperre frei + macht 'running' sichtbar).
    """
    cur = conn.execute(
        """
        UPDATE tasks SET status='running', claim_lock=%s, started_at=%s,
               worker_pid=%s, claim_expires=%s
        WHERE id = (
            SELECT id FROM tasks
            WHERE status='ready' AND assignee=%s
            ORDER BY priority DESC, created_at ASC
            FOR UPDATE SKIP LOCKED
            LIMIT 1
        )
        RETURNING id
        """,
        (claim_lock, now_epoch, worker_pid, now_epoch + ttl_seconds, assignee),
    )
    row = cur.fetchone()
    return row[0] if row else None
