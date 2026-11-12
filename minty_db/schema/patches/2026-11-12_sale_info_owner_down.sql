-- 2026-11-12: the way back from 2026-11-12_sale_info_owner.sql. Forgets who owns each method and
-- puts back the one-row-per-name catalogue: the copies of one name fold into its oldest row (links
-- and report rows follow; different companies, so nothing collides). The patch's merges of case
-- twins and its deletes of unused rows stay - they carried no data. One transaction, rolled back
-- unless --apply.

DROP INDEX uq_sale_info_minty_name;
DROP INDEX uq_sale_info_entity_name;
ALTER TABLE sale_info DROP CONSTRAINT fk_sale_info_entity;

CREATE TEMP TABLE fold ON COMMIT DROP AS
SELECT s.id AS copy_id, first_value(s.id) OVER (PARTITION BY s.sale_name ORDER BY s.entity_id IS NOT NULL, s.created_at, s.id) AS keep_id
  FROM sale_info s;
DELETE FROM fold WHERE copy_id = keep_id;
UPDATE entity_sale_setting e SET sale_id = f.keep_id FROM fold f WHERE e.sale_id = f.copy_id;
UPDATE report_sale r SET sale_id = f.keep_id FROM fold f WHERE r.sale_id = f.copy_id;
DELETE FROM sale_info WHERE id IN (SELECT copy_id FROM fold);

ALTER TABLE sale_info DROP COLUMN entity_id, ADD CONSTRAINT sale_info_name_key UNIQUE (sale_name);

SELECT 'sale_info.entity_id removed' AS done, (SELECT count(*) FROM fold) AS copies_folded;
