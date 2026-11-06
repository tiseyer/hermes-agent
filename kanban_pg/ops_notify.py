"""Notifier-Domäne (Etappe 4 / Watchers): Subscriptions + serialisierter Event-Cursor.

Ersetzt den SQLite-Notifier-Pfad (claim_unseen_events_for_sub). Der Cursor-Vorlauf
läuft unter ``SELECT ... FOR UPDATE`` auf der Sub-Zeile -> zwei nebenläufige Notifier
liefern kein Event doppelt (DB-seitig serialisiert statt flock).
"""
from ._base import now_epoch


class NotifyMixin:
    def notify_subscribe(self, task_id, platform, chat_id, thread_id="",
                         user_id=None, delivery_mode="notify"):
        self.conn.execute(
            "INSERT INTO kanban_notify_subs "
            "  (task_id,platform,chat_id,thread_id,user_id,created_at,delivery_mode) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s) "
            "ON CONFLICT (task_id,platform,chat_id,thread_id) DO UPDATE SET "
            "  user_id=EXCLUDED.user_id, delivery_mode=EXCLUDED.delivery_mode",
            (task_id, platform, chat_id, thread_id, user_id, now_epoch(), delivery_mode),
        )
        self.conn.commit()

    def notify_unsubscribe(self, task_id, platform, chat_id, thread_id=""):
        cur = self.conn.execute(
            "DELETE FROM kanban_notify_subs "
            "WHERE task_id=%s AND platform=%s AND chat_id=%s AND thread_id=%s "
            "RETURNING task_id",
            (task_id, platform, chat_id, thread_id),
        )
        ok = cur.fetchone() is not None
        self.conn.commit()
        return ok

    def claim_unseen_events(self, task_id, platform, chat_id, thread_id=""):
        """Holt ungesehene Events (id > last_event_id) für die Sub und schiebt den
        Cursor atomar vor. Serialisiert via FOR UPDATE auf der Sub-Zeile."""
        row = self.conn.execute(
            "SELECT last_event_id FROM kanban_notify_subs "
            "WHERE task_id=%s AND platform=%s AND chat_id=%s AND thread_id=%s FOR UPDATE",
            (task_id, platform, chat_id, thread_id),
        ).fetchone()
        if row is None:
            self.conn.rollback()
            return []
        cursor = row[0]
        evs = self.conn.execute(
            "SELECT id, kind, payload, created_at FROM task_events "
            "WHERE task_id=%s AND id > %s ORDER BY id",
            (task_id, cursor),
        ).fetchall()
        if evs:
            self.conn.execute(
                "UPDATE kanban_notify_subs SET last_event_id=%s "
                "WHERE task_id=%s AND platform=%s AND chat_id=%s AND thread_id=%s",
                (evs[-1][0], task_id, platform, chat_id, thread_id),
            )
        self.conn.commit()
        return evs
