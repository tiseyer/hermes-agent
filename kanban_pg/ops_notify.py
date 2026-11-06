"""Notifier-Domäne (Etappe 4 / Welle 2): Subscriptions + serialisierter Event-Cursor.

Ersetzt den SQLite-Notifier-Pfad (claim_unseen_events_for_sub). Der Cursor-Vorlauf
läuft unter ``SELECT ... FOR UPDATE`` auf der Sub-Zeile -> zwei nebenläufige Notifier
liefern kein Event doppelt (DB-seitig serialisiert statt flock).

Welle 2 (faithful): die thin-Kurzform (notify_subscribe/unsubscribe/claim_unseen_events)
bleibt (Etappe-4-Regression), DANEBEN die VOLLE reale kanban_db-API —
add_notify_sub/list_notify_subs/remove_notify_sub, unseen_events_for_sub (read-only),
claim_unseen_events_for_sub (atomar claimen), advance_/rewind_notify_cursor. Diese
spiegeln kanban_db.py 1:1 (INSERT-OR-IGNORE + notifier_profile-Backfill, kinds-Filter,
(old,new,events)-Rückgabe, CAS-Advance/Rewind).

BEFUND (Karte↔Code): die Karte nennt last_ping_event_id/delivery_mode-Ping-Semantik —
in kanban_db.py existiert dazu KEIN Code; der Notifier-Pfad keyt ausschließlich auf
last_event_id (+ optionalem kinds-Filter). Die Spalten tragen die Bridge/Zukunft, nicht
den Dispatcher-Notifier. Faithful = kanban_db spiegeln, keine erfundene Ping-Logik.
Events werden als dict geliefert (PG-Reads sind dict-basiert; Konsumenten greifen per Key).
"""
from ._base import now_epoch


def _ev_dicts(cur):
    cols = [d[0] for d in cur.description]
    out = []
    for row in cur.fetchall():
        d = dict(zip(cols, row))
        import json as _json
        try:
            d["payload"] = _json.loads(d["payload"]) if d.get("payload") else None
        except (ValueError, TypeError):
            d["payload"] = None
        out.append(d)
    return out


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

    # ===== Volle reale kanban_db-Notify-API (Welle 2, faithful) ==========
    def add_notify_sub(self, *, task_id, platform, chat_id, thread_id=None,
                       user_id=None, notifier_profile=None):
        """Idempotent auf (task,platform,chat,thread); notifier_profile-Backfill."""
        now = now_epoch()
        self.conn.execute(
            "INSERT INTO kanban_notify_subs "
            "(task_id,platform,chat_id,thread_id,user_id,notifier_profile,created_at) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s) "
            "ON CONFLICT (task_id,platform,chat_id,thread_id) DO NOTHING",
            (task_id, platform, chat_id, thread_id or "", user_id,
             notifier_profile, now),
        )
        if notifier_profile:
            self.conn.execute(
                "UPDATE kanban_notify_subs SET notifier_profile=%s "
                "WHERE task_id=%s AND platform=%s AND chat_id=%s AND thread_id=%s "
                "AND (notifier_profile IS NULL OR notifier_profile='')",
                (notifier_profile, task_id, platform, chat_id, thread_id or ""),
            )
        self.conn.commit()

    def list_notify_subs(self, task_id=None):
        if task_id is not None:
            cur = self.conn.execute(
                "SELECT * FROM kanban_notify_subs WHERE task_id=%s", (task_id,),
            )
        else:
            cur = self.conn.execute("SELECT * FROM kanban_notify_subs")
        cols = [d[0] for d in cur.description]
        rows = [dict(zip(cols, r)) for r in cur.fetchall()]
        self.conn.commit()
        return rows

    def remove_notify_sub(self, *, task_id, platform, chat_id, thread_id=None):
        cur = self.conn.execute(
            "DELETE FROM kanban_notify_subs WHERE task_id=%s AND platform=%s "
            "AND chat_id=%s AND thread_id=%s",
            (task_id, platform, chat_id, thread_id or ""),
        )
        ok = cur.rowcount > 0
        self.conn.commit()
        return ok

    def unseen_events_for_sub(self, *, task_id, platform, chat_id,
                              thread_id=None, kinds=None):
        """(new_cursor, events) — Cursor wird NICHT vorgeschoben (read-only)."""
        row = self.conn.execute(
            "SELECT last_event_id FROM kanban_notify_subs "
            "WHERE task_id=%s AND platform=%s AND chat_id=%s AND thread_id=%s",
            (task_id, platform, chat_id, thread_id or ""),
        ).fetchone()
        if row is None:
            self.conn.commit()
            return 0, []
        cursor = int(row[0])
        kind_list = list(kinds) if kinds else None
        q = ("SELECT id,task_id,run_id,kind,payload,created_at FROM task_events "
             "WHERE task_id=%s AND id > %s "
             + ("AND kind IN (" + ",".join(["%s"] * len(kind_list)) + ") " if kind_list else "")
             + "ORDER BY id ASC")
        params = [task_id, cursor]
        if kind_list:
            params.extend(kind_list)
        cur = self.conn.execute(q, params)
        events = _ev_dicts(cur)
        self.conn.commit()
        max_id = cursor
        for e in events:
            max_id = max(max_id, int(e["id"]))
        return max_id, events

    def claim_unseen_events_for_sub(self, *, task_id, platform, chat_id,
                                    thread_id=None, kinds=None):
        """(old_cursor, new_cursor, events) — atomar claimen (FOR UPDATE auf Sub)."""
        try:
            row = self.conn.execute(
                "SELECT last_event_id FROM kanban_notify_subs "
                "WHERE task_id=%s AND platform=%s AND chat_id=%s AND thread_id=%s "
                "FOR UPDATE",
                (task_id, platform, chat_id, thread_id or ""),
            ).fetchone()
            if row is None:
                self.conn.rollback()
                return 0, 0, []
            old_cursor = int(row[0])
            kind_list = list(kinds) if kinds else None
            q = ("SELECT id,task_id,run_id,kind,payload,created_at FROM task_events "
                 "WHERE task_id=%s AND id > %s "
                 + ("AND kind IN (" + ",".join(["%s"] * len(kind_list)) + ") " if kind_list else "")
                 + "ORDER BY id ASC")
            params = [task_id, old_cursor]
            if kind_list:
                params.extend(kind_list)
            events = _ev_dicts(self.conn.execute(q, params))
            if not events:
                self.conn.commit()
                return old_cursor, old_cursor, []
            new_cursor = max(int(e["id"]) for e in events)
            self.conn.execute(
                "UPDATE kanban_notify_subs SET last_event_id=%s "
                "WHERE task_id=%s AND platform=%s AND chat_id=%s AND thread_id=%s "
                "AND last_event_id=%s",
                (new_cursor, task_id, platform, chat_id, thread_id or "", old_cursor),
            )
            self.conn.commit()
            return old_cursor, new_cursor, events
        except Exception:
            self.conn.rollback()
            raise

    def advance_notify_cursor(self, *, task_id, platform, chat_id,
                              thread_id=None, new_cursor):
        self.conn.execute(
            "UPDATE kanban_notify_subs SET last_event_id=%s "
            "WHERE task_id=%s AND platform=%s AND chat_id=%s AND thread_id=%s",
            (int(new_cursor), task_id, platform, chat_id, thread_id or ""),
        )
        self.conn.commit()

    def rewind_notify_cursor(self, *, task_id, platform, chat_id,
                             thread_id=None, claimed_cursor, old_cursor):
        """CAS-Rewind: nur wenn kein späterer Notifier die Zeile vorschob."""
        cur = self.conn.execute(
            "UPDATE kanban_notify_subs SET last_event_id=%s "
            "WHERE task_id=%s AND platform=%s AND chat_id=%s AND thread_id=%s "
            "AND last_event_id=%s",
            (int(old_cursor), task_id, platform, chat_id, thread_id or "",
             int(claimed_cursor)),
        )
        ok = cur.rowcount > 0
        self.conn.commit()
        return ok
