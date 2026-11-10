-- 2026-11-10: the way back from 2026-11-10_tracker_view.sql - the twelve-column tracker view, as
-- 0001 built it before (archived companies included again). One transaction, rolled back unless --apply.

DROP VIEW IF EXISTS tracker;
CREATE VIEW tracker AS
SELECT e.name AS "entities.name",
       e.id   AS entity_id,
       EXISTS (SELECT 1 FROM entity_function_map efm
                JOIN entity_function ef ON ef.id = efm.entity_function_id
               WHERE efm.entity_id = e.id AND ef.function_code = 'PETTY_CASH' AND efm.is_enabled) AS pettycash,
       EXISTS (SELECT 1 FROM entity_function_map efm
                JOIN entity_function ef ON ef.id = efm.entity_function_id
               WHERE efm.entity_id = e.id AND ef.function_code = 'PAYMENT_REQUEST' AND efm.is_enabled) AS billing,
       rpt.pc_latest_submitted,
       rpt.pc_latest_published,
       bil.num_paid, bil.num_partialpaid, bil.num_unpaid, bil.num_published,
       bil.latest_bill_published, bil.latest_bill_update
  FROM entities e
  LEFT JOIN LATERAL (
        SELECT max(r.transaction_date) AS pc_latest_submitted,
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
         WHERE b.entity_id = e.id) bil ON true;

SELECT 'tracker view back to twelve columns' AS done;
