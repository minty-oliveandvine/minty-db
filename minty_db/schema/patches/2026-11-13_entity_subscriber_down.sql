-- 2026-11-13: the way back from 2026-11-13_entity_subscriber.sql. It forgets every company's
-- subscriber (the payers on entity_module_subscription stay). One transaction, rolled back unless
-- --apply. Run it only with the services back on a minty-db before v0.9.

ALTER TABLE entities
  DROP COLUMN subscriber_user_id;   -- with fk_entities_subscriber_user

SELECT 'entities.subscriber_user_id removed' AS done;
