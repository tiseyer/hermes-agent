-- B1.1 Postgres-Board — P2b Trigger-Port (plpgsql) mit Rekursions-Guard.
-- Wird NACH dem Daten-Load angewandt (nie während der Migration — sonst würde
-- sync_family_root beim Insert von Kindern die Eltern-Status umrechnen).
--
-- SEMANTIK-TREUE zu SQLite (dort recursive_triggers=OFF by default):
--   * PG feuert Trigger IMMER rekursiv. Deshalb bekommt JEDE task-Trigger-Funktion
--     den Guard `IF pg_trigger_depth() > 1 THEN RETURN ...`. Damit feuert ein
--     trigger-ausgelöster UPDATE (Tiefe>1) NICHT erneut — exakt wie SQLite ohne
--     recursive_triggers. Terminierung garantiert, keine Endlosschleife.
--   * SQLite `strftime('%s','now')`  -> `extract(epoch from now())::bigint`
--   * SQLite `IS NOT` (null-sicher)  -> `IS DISTINCT FROM`
--   * stamp_* werden als BEFORE-Trigger auf NEW gesetzt (kein Selbst-UPDATE nötig,
--     sauberer als SQLites AFTER-self-UPDATE; Ergebnis identisch).

BEGIN;

-- ---------- stamp_status_changed_at ----------
CREATE OR REPLACE FUNCTION kb_stamp_scat_insert() RETURNS trigger AS $$
BEGIN
  IF pg_trigger_depth() > 1 THEN RETURN NEW; END IF;
  IF NEW.status_changed_at IS NULL THEN
    NEW.status_changed_at := COALESCE(NEW.created_at, extract(epoch from now())::bigint);
  END IF;
  RETURN NEW;
END $$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION kb_stamp_scat_update() RETURNS trigger AS $$
BEGIN
  IF pg_trigger_depth() > 1 THEN RETURN NEW; END IF;
  IF NEW.status IS DISTINCT FROM OLD.status THEN
    NEW.status_changed_at := extract(epoch from now())::bigint;
  END IF;
  RETURN NEW;
END $$ LANGUAGE plpgsql;

CREATE TRIGGER stamp_status_changed_at_insert
  BEFORE INSERT ON tasks FOR EACH ROW
  EXECUTE FUNCTION kb_stamp_scat_insert();

CREATE TRIGGER stamp_status_changed_at_update
  BEFORE UPDATE OF status ON tasks FOR EACH ROW
  EXECUTE FUNCTION kb_stamp_scat_update();

-- ---------- sync_family_root (Status-Rollup auf die Wurzel) ----------
-- Rollt den Status der Familien-Wurzel (id = NEW.family_root_id) aus den
-- 'work'-Mitgliedern hoch. Gleiche Prioritätsreihenfolge wie der SQLite-Trigger.
CREATE OR REPLACE FUNCTION kb_sync_family_root() RETURNS trigger AS $$
DECLARE
  new_status text;
BEGIN
  -- Guard: nicht rekursiv feuern (Wurzel-UPDATE unten läuft auf Tiefe 2).
  IF pg_trigger_depth() > 1 THEN RETURN NULL; END IF;
  IF NEW.family_root_id IS NULL OR NEW.id = NEW.family_root_id THEN RETURN NULL; END IF;

  new_status := CASE
    WHEN NOT EXISTS (SELECT 1 FROM tasks m WHERE m.family_root_id = NEW.family_root_id
         AND m.id <> NEW.family_root_id AND m.child_role = 'work' AND m.status <> 'done') THEN 'done'
    WHEN EXISTS (SELECT 1 FROM tasks m WHERE m.family_root_id = NEW.family_root_id
         AND m.id <> NEW.family_root_id AND m.child_role = 'work' AND m.status = 'blocked') THEN 'blocked'
    WHEN EXISTS (SELECT 1 FROM tasks m WHERE m.family_root_id = NEW.family_root_id
         AND m.id <> NEW.family_root_id AND m.child_role = 'work' AND m.status = 'human') THEN 'human'
    WHEN EXISTS (SELECT 1 FROM tasks m WHERE m.family_root_id = NEW.family_root_id
         AND m.id <> NEW.family_root_id AND m.child_role = 'work' AND m.status = 'running') THEN 'running'
    WHEN EXISTS (SELECT 1 FROM tasks m WHERE m.family_root_id = NEW.family_root_id
         AND m.id <> NEW.family_root_id AND m.child_role = 'work' AND m.status = 'review') THEN 'review'
    WHEN EXISTS (SELECT 1 FROM tasks m WHERE m.family_root_id = NEW.family_root_id
         AND m.id <> NEW.family_root_id AND m.child_role = 'work' AND m.status = 'ready') THEN 'ready'
    WHEN EXISTS (SELECT 1 FROM tasks m WHERE m.family_root_id = NEW.family_root_id
         AND m.id <> NEW.family_root_id AND m.child_role = 'work' AND m.status = 'scheduled') THEN 'scheduled'
    WHEN EXISTS (SELECT 1 FROM tasks m WHERE m.family_root_id = NEW.family_root_id
         AND m.id <> NEW.family_root_id AND m.child_role = 'work' AND m.status = 'triage') THEN 'triage'
    ELSE 'todo'
  END;

  UPDATE tasks SET
    status = new_status,
    completed_at = CASE WHEN new_status = 'done'
                        THEN extract(epoch from now())::bigint ELSE NULL END,
    status_changed_at = CASE WHEN new_status IS DISTINCT FROM status
                        THEN extract(epoch from now())::bigint ELSE status_changed_at END
  WHERE id = NEW.family_root_id;

  RETURN NULL;
END $$ LANGUAGE plpgsql;

CREATE TRIGGER sync_family_root_after_insert
  AFTER INSERT ON tasks FOR EACH ROW
  WHEN (NEW.family_root_id IS NOT NULL AND NEW.id <> NEW.family_root_id)
  EXECUTE FUNCTION kb_sync_family_root();

CREATE TRIGGER sync_family_root_after_update
  AFTER UPDATE OF status, child_role, family_root_id ON tasks FOR EACH ROW
  WHEN (NEW.family_root_id IS NOT NULL AND NEW.id <> NEW.family_root_id)
  EXECUTE FUNCTION kb_sync_family_root();

COMMIT;
