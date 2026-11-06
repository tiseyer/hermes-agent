"""B1.1 Postgres-Board — P5 Backend-Fassade (psycopg3), EIN Schreibpfad.

Beweist das Portier-Muster für die rohen sqlite3-Schreiber end-to-end gegen PG
(Schema + Trigger + Lock-Modell zusammen). Die Operationen sind nach Domäne in
``ops_*.py``-Mixins aufgeteilt, damit die Writer-Etappen (Watchers/Tools/Dashboard)
konfliktfrei parallel an ihrer jeweiligen Datei arbeiten können.

SQLite bleibt unangetastet (Rollback bis PG stabil). Voll-Port-Fortschritt:
siehe P5-CHECKLIST.md / HANDOFF-PORT.md.
"""
from ._base import now_epoch  # noqa: F401 (re-export)
from .lock_model import claim_one_ready, try_board_lock  # noqa: F401 (re-export)
from .ops_tasks import TasksMixin
from .ops_claims import ClaimsMixin
from .ops_comments import CommentsEventsMixin
from .ops_runs import RunsMixin
from .ops_links import LinksMixin


class PgBackend(
    TasksMixin,
    ClaimsMixin,
    CommentsEventsMixin,
    RunsMixin,
    LinksMixin,
):
    """Schmales Schreib-/Lese-Backend über eine psycopg3-Verbindung (autocommit=False)."""

    def __init__(self, conn):
        self.conn = conn
