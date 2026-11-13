-- 2026-11-13: entities.subscriber_user_id - who holds a company's subscription - for a database
-- already recorded at 0001 (owner-approved, 2026-11-13).
--
-- The baseline is minty_db/schema/01_schema_rebased.sql (the column is in entities' CREATE TABLE),
-- so a fresh build already has it. Schema changes stay baseline (owner, 2026-11-10):
--
--     python -m minty_db patch minty_db/schema/patches/2026-11-13_entity_subscriber.sql           # dry run
--     python -m minty_db patch minty_db/schema/patches/2026-11-13_entity_subscriber.sql --apply
--
-- The owner's rule: a company's creator is its subscriber (onboarding writes it), a handover moves
-- it, and confirming billing makes that person entity_module_subscription.payer_user_id - only they
-- may activate or change the subscription. NULL = not assigned: view-only for everyone.
--
-- Backfill: a company that already has a payer gets that payer (the column mirrors it). Every
-- other company stays NULL - its subscriber is assigned by hand (the owner's list; the cutover load
-- reads it). Release order: this patch before the services that read the column (subscription-api,
-- onboarding-api, accounts-api on minty-db v0.9). Not re-runnable: a second run stops at the guard.
-- The way back is its _down.sql.

DO $$ BEGIN
  IF EXISTS (SELECT 1 FROM information_schema.columns
             WHERE table_schema = current_schema() AND table_name = 'entities' AND column_name = 'subscriber_user_id') THEN
    RAISE EXCEPTION 'entities.subscriber_user_id is already in this schema';
  END IF;
END $$;

ALTER TABLE entities
  ADD COLUMN subscriber_user_id UUID NULL,
  ADD CONSTRAINT fk_entities_subscriber_user FOREIGN KEY (subscriber_user_id) REFERENCES "user" (id) ON DELETE SET NULL;

-- One payer per company (store.upsert_module_row enforces it), so any row that has one answers.
UPDATE entities e
   SET subscriber_user_id = p.payer_user_id
  FROM (SELECT DISTINCT ON (entity_id) entity_id, payer_user_id
          FROM entity_module_subscription
         WHERE payer_user_id IS NOT NULL
         ORDER BY entity_id, payer_user_id) p
 WHERE p.entity_id = e.id;

SELECT count(*) AS companies, count(subscriber_user_id) AS with_subscriber FROM entities;
