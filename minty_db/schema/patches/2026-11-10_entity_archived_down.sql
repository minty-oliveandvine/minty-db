-- 2026-11-10: the way back from 2026-11-10_entity_archived.sql. Only while nothing archives yet:
-- it forgets which companies were archived. One transaction, rolled back unless --apply.

ALTER TABLE entities
  DROP COLUMN archived_by_user_id,   -- with fk_entities_archived_user
  DROP COLUMN archived_at;

SELECT 'entities.archived_at removed' AS done;
