-- 2026-11-10: archiving a company - entities.archived_at / archived_by_user_id, for a database
-- already recorded at 0001 (owner-approved design, requested by the session doing the app code).
--
-- The baseline is minty_db/schema/01_schema_rebased.sql (the columns are in entities' CREATE
-- TABLE), so a fresh build already has them. Schema changes stay baseline (owner, 2026-11-10):
--
--     python -m minty_db patch minty_db/schema/patches/2026-11-10_entity_archived.sql           # dry run
--     python -m minty_db patch minty_db/schema/patches/2026-11-10_entity_archived.sql --apply
--
-- Set = archived (hidden, refused everywhere); NULL = active. A column rather than a status value,
-- because the Xero paths rewrite entities.status and the Xero-optional CONTRACT rebuilds it.
-- Additive: nothing reads them yet. Independent of 2026-11-10_xero_optional_expand.sql (either
-- order). Not re-runnable: a second run stops at the guard. The way back is its _down.sql.

DO $$ BEGIN
  IF EXISTS (SELECT 1 FROM information_schema.columns
             WHERE table_schema = current_schema() AND table_name = 'entities' AND column_name = 'archived_at') THEN
    RAISE EXCEPTION 'entities.archived_at is already in this schema';
  END IF;
END $$;

ALTER TABLE entities
  ADD COLUMN archived_at         TIMESTAMPTZ NULL,
  ADD COLUMN archived_by_user_id UUID        NULL,
  ADD CONSTRAINT fk_entities_archived_user FOREIGN KEY (archived_by_user_id) REFERENCES "user" (id) ON DELETE SET NULL;

SELECT count(*) AS companies, count(archived_at) AS archived FROM entities;
