-- 2026-11-10: the client tracker's view, fifteen columns and no archived company (owner; minty-db
-- v0.5), for a database already recorded at 0001. The baseline (01_schema_rebased.sql) has it, so a
-- fresh build does too. tracker-backend's own tracker_view_v3.sql is retired: this is the one view.
--
--     python -m minty_db patch minty_db/schema/patches/2026-11-10_tracker_view.sql           # dry run
--     python -m minty_db patch minty_db/schema/patches/2026-11-10_tracker_view.sql --apply
--
-- Runs after 2026-11-10_entity_archived.sql (it reads entities.archived_at; the guard stops it
-- otherwise). DROP + CREATE, not CREATE OR REPLACE: a column can't be inserted mid-list (42P16).
-- Re-runnable. The way back is its _down.sql (the twelve-column view).

DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                 WHERE table_schema = current_schema() AND table_name = 'entities' AND column_name = 'archived_at') THEN
    RAISE EXCEPTION 'run 2026-11-10_entity_archived.sql first: the tracker view reads entities.archived_at';
  END IF;
END $$;

DROP VIEW IF EXISTS tracker;
CREATE VIEW tracker AS
SELECT e.name AS "entities.name",
       e.id   AS entity_id,
       COALESCE(EXISTS (SELECT 1 FROM entity_function_map efm
                JOIN entity_function ef ON ef.id = efm.entity_function_id
               WHERE efm.entity_id = e.id AND ef.function_code = 'PETTY_CASH' AND efm.is_enabled), false) AS pettycash,
       COALESCE(EXISTS (SELECT 1 FROM entity_function_map efm
                JOIN entity_function ef ON ef.id = efm.entity_function_id
               WHERE efm.entity_id = e.id AND ef.function_code = 'PAYMENT_REQUEST' AND efm.is_enabled), false) AS billing,
       rpt.pc_latest_submitted,
       rpt.pc_latest_submitted_date,
       rpt.pc_latest_published,
       COALESCE(bil.num_paid, 0)        AS num_paid,
       COALESCE(bil.num_partialpaid, 0) AS num_partialpaid,
       COALESCE(bil.num_unpaid, 0)      AS num_unpaid,
       COALESCE(bil.num_published, 0)   AS num_published,
       bil.latest_bill_published, bil.latest_bill_update,
       e.country_code,
       e.timezone
  FROM entities e
  LEFT JOIN LATERAL (
        SELECT max(r.transaction_date) AS pc_latest_submitted,
               (array_agg(r.created_at ORDER BY r.transaction_date DESC)
                  FILTER (WHERE r.transaction_date IS NOT NULL))[1] AS pc_latest_submitted_date,
               max(r.transaction_date) FILTER (WHERE r.publishing_status = 'completed') AS pc_latest_published
          FROM report r
         WHERE r.entity_id = e.id) rpt ON true
  LEFT JOIN LATERAL (
        SELECT count(*) FILTER (WHERE b.status = 'paid')            AS num_paid,
               count(*) FILTER (WHERE b.status = 'partially_paid')  AS num_partialpaid,
               count(*) FILTER (WHERE b.status = 'submitted')       AS num_unpaid,
               count(*) FILTER (WHERE b.published = 'published')    AS num_published,
               max(b.updated_at) FILTER (WHERE b.published = 'published') AS latest_bill_published,
               greatest(max(b.updated_at), max(b.created_at))       AS latest_bill_update
          FROM bill b
         WHERE b.entity_id = e.id) bil ON true
 WHERE e.archived_at IS NULL;

SELECT count(*) AS companies_on_the_tracker FROM tracker;
