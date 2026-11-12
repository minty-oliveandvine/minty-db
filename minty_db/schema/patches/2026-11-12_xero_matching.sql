-- 2026-11-12: entities.xero_matching - an open matching of a company's accounts to Xero, and why
-- (Phase 3 of the Xero matching, owner-approved), for a database already recorded at 0001.
--
-- The baseline is minty_db/schema/01_schema_rebased.sql (the type and the column are in its last
-- section), so a fresh build already has them. Schema changes stay baseline (owner, 2026-11-10):
--
--     python -m minty_db patch minty_db/schema/patches/2026-11-12_xero_matching.sql           # dry run
--     python -m minty_db patch minty_db/schema/patches/2026-11-12_xero_matching.sql --apply
--
-- 'first' = a company that kept its books in Minty connects; 'new_accounts' = a reconnect found
-- accounts added while disconnected; 'new_org' = a reconnect to another organisation; 'change' =
-- the matches are being changed. NULL = none open. minty-accounts-api sets and clears it.
-- Backfill: a company Phase 2 already has mid-matching (keeps its books in Minty, an organisation
-- recorded, own accounts with no Xero id) gets 'first', so its page stays open. Not re-runnable:
-- a second run stops at the guard. The way back is its _down.sql.

DO $$ BEGIN
  IF EXISTS (SELECT 1 FROM information_schema.columns
             WHERE table_schema = current_schema() AND table_name = 'entities' AND column_name = 'xero_matching') THEN
    RAISE EXCEPTION 'entities.xero_matching is already in this schema';
  END IF;
END $$;

CREATE TYPE xero_matching AS ENUM ('first','new_accounts','new_org','change');

ALTER TABLE entities ADD COLUMN xero_matching xero_matching NULL;

UPDATE entities e SET xero_matching = 'first'
 WHERE e.setup_mode = 'minty' AND e.xero_org_id IS NOT NULL
   AND EXISTS (SELECT 1 FROM account_info a
                WHERE a.entity_id = e.id AND a.xero_account_id IS NULL AND a.merged_into_id IS NULL
                  AND a.record_status <> 'archived');

SELECT count(*) AS companies, count(xero_matching) AS matching_open FROM entities;
