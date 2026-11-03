-- 2026-11-03: the three Xero unique keys, for a database already recorded at 0001.
--
-- 0001 stays the only migration until the owner says otherwise (2026-11-03), so a schema
-- change is the schema file (which a fresh build runs) plus a patch like this one, run by
-- hand on each database:
--
--     python -m minty_db patch minty_db/schema/patches/2026-11-03_xero_unique_keys.sql           # dry run
--     python -m minty_db patch minty_db/schema/patches/2026-11-03_xero_unique_keys.sql --apply
--
-- The runner sets search_path to DATABASE_URL's schema, so nothing here names one, and runs
-- the file in ONE transaction that it rolls back unless --apply. The SELECTs are the report.
-- Idempotent: duplicates are gone and the constraints exist after the first run.
--
-- Why: entity_account_xero, entity_bill_account_xero and xero_contact_sync had no unique key
-- and their syncs read-then-insert, so two syncs at once could write a row twice (QA #4,
-- onboarding's duplicate chart of accounts). Each table keeps ONE row per key first.

-- ---- before ---------------------------------------------------------------------------------

SELECT 'entity_account_xero duplicates' AS check, count(*) AS rows_to_merge FROM (
  SELECT row_number() OVER (PARTITION BY account_id ORDER BY is_active DESC, id) AS rn
  FROM entity_account_xero
) d WHERE rn > 1
UNION ALL
SELECT 'entity_bill_account_xero duplicates', count(*) FROM (
  SELECT row_number() OVER (PARTITION BY entity_id, xero_account_id
                            ORDER BY is_default DESC, is_active DESC, is_deleted, created_at, id) AS rn
  FROM entity_bill_account_xero WHERE xero_account_id <> ''
) d WHERE rn > 1
UNION ALL
SELECT 'xero_contact_sync duplicates', count(*) FROM (
  SELECT row_number() OVER (PARTITION BY entity_id, xero_contact_id ORDER BY created_at, id) AS rn
  FROM xero_contact_sync WHERE entity_id IS NOT NULL
) d WHERE rn > 1;

-- ---- 1. entity_account_xero: one row per account, a ticked one first ----------------------------

DELETE FROM entity_account_xero e
USING (
  SELECT id, row_number() OVER (PARTITION BY account_id ORDER BY is_active DESC, id) AS rn
  FROM entity_account_xero
) d
WHERE e.id = d.id AND d.rn > 1;

-- ---- 2. entity_bill_account_xero: one row per (company, Xero account) -------------------------
-- The default first, then a ticked one, then one not deleted, then the oldest. Codes added by
-- hand have no Xero id (NULL, or '' as minty-payment-request-api writes it): the key is a
-- partial unique index that leaves them alone, and so does this.

DELETE FROM entity_bill_account_xero b
USING (
  SELECT id, row_number() OVER (PARTITION BY entity_id, xero_account_id
                                ORDER BY is_default DESC, is_active DESC, is_deleted, created_at, id) AS rn
  FROM entity_bill_account_xero WHERE xero_account_id <> ''
) d
WHERE b.id = d.id AND d.rn > 1;

-- ---- 3. xero_contact_sync: one row per (company, Xero contact), the oldest ---------------------
-- Everything pointing at a duplicate moves to the row kept first, so no expense line, payment
-- request or Petty Cash mapping loses its contact. bill.contact_id has no foreign key.

CREATE TEMP TABLE contact_merge ON COMMIT DROP AS
SELECT id AS dup_id, keep_id FROM (
  SELECT id,
         first_value(id) OVER w AS keep_id,
         row_number() OVER w AS rn
  FROM xero_contact_sync
  WHERE entity_id IS NOT NULL
  WINDOW w AS (PARTITION BY entity_id, xero_contact_id ORDER BY created_at, id)
) d
WHERE rn > 1;

SELECT 'contact references repointed' AS check,
       (SELECT count(*) FROM report_expense r JOIN contact_merge m ON r.contact_id = m.dup_id)
     + (SELECT count(*) FROM bill b JOIN contact_merge m ON b.contact_id = m.dup_id)
     + (SELECT count(*) FROM entity_pettycash_settings s JOIN contact_merge m
          ON m.dup_id IN (s.cash_sale_contact_id, s.director_contact_id, s.discrepancy_contact_id))
       AS rows;

UPDATE report_expense r SET contact_id = m.keep_id FROM contact_merge m WHERE r.contact_id = m.dup_id;
UPDATE bill b SET contact_id = m.keep_id FROM contact_merge m WHERE b.contact_id = m.dup_id;
UPDATE entity_pettycash_settings s SET cash_sale_contact_id = m.keep_id
  FROM contact_merge m WHERE s.cash_sale_contact_id = m.dup_id;
UPDATE entity_pettycash_settings s SET director_contact_id = m.keep_id
  FROM contact_merge m WHERE s.director_contact_id = m.dup_id;
UPDATE entity_pettycash_settings s SET discrepancy_contact_id = m.keep_id
  FROM contact_merge m WHERE s.discrepancy_contact_id = m.dup_id;
DELETE FROM xero_contact_sync c USING contact_merge m WHERE c.id = m.dup_id;

-- ---- 4. the keys, named as in 01_schema_rebased.sql -------------------------------------------

CREATE UNIQUE INDEX IF NOT EXISTS uq_ebax_entity_xero_account
    ON entity_bill_account_xero (entity_id, xero_account_id)
 WHERE xero_account_id <> '';

DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'uq_eax_account'
                 AND connamespace = current_schema()::regnamespace) THEN
    ALTER TABLE entity_account_xero ADD CONSTRAINT uq_eax_account UNIQUE (account_id);
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'uq_xcs_entity_contact'
                 AND connamespace = current_schema()::regnamespace) THEN
    ALTER TABLE xero_contact_sync ADD CONSTRAINT uq_xcs_entity_contact UNIQUE (entity_id, xero_contact_id);
  END IF;
END $$;

-- ---- after ---------------------------------------------------------------------------------

SELECT conname AS key_present FROM pg_constraint
WHERE connamespace = current_schema()::regnamespace
  AND conname IN ('uq_eax_account', 'uq_xcs_entity_contact')
UNION ALL
SELECT indexname FROM pg_indexes
WHERE schemaname = current_schema() AND indexname = 'uq_ebax_entity_xero_account'
ORDER BY 1;
