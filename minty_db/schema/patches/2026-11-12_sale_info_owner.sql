-- 2026-11-12: sale_info.entity_id - who owns a sales method (owner-approved), for a database
-- already recorded at 0001.
--
-- The baseline is minty_db/schema/01_schema_rebased.sql (its last section), so a fresh build
-- already has the column and the two name rules. Schema changes stay baseline (owner, 2026-11-10):
--
--     python -m minty_db patch minty_db/schema/patches/2026-11-12_sale_info_owner.sql           # dry run
--     python -m minty_db patch minty_db/schema/patches/2026-11-12_sale_info_owner.sql --apply
--
-- NULL = Minty's catalogue (offered to every company, never renamed or deleted); set = the company
-- that typed it. The owner's backfill (census on production's data, 2026-11-12):
--   1. Minty's: the fourteen names below.
--   2. A row differing from a Minty name only by case (MasterCard, PayMe, payme) merges into
--      Minty's row: its links and report rows move over; a company or report holding both keeps one
--      link (on if either was) and one report row (the amounts summed).
--   3. A row no company links and no report uses is deleted (electronic '1' and 'Take Out',
--      delivery '2').
--   4. Every other row is its company's own. A name used by several companies is split: the company
--      with the most report rows keeps the row, each other gets a copy, and its links and report rows
--      follow (the same name on screen).
-- Not re-runnable: a second run stops at the guard. The way back is its _down.sql.

DO $$ BEGIN
  IF EXISTS (SELECT 1 FROM information_schema.columns
             WHERE table_schema = current_schema() AND table_name = 'sale_info' AND column_name = 'entity_id') THEN
    RAISE EXCEPTION 'sale_info.entity_id is already in this schema';
  END IF;
END $$;

ALTER TABLE sale_info DROP CONSTRAINT sale_info_name_key, ADD COLUMN entity_id UUID NULL;

CREATE TEMP TABLE minty_method (name TEXT PRIMARY KEY) ON COMMIT DROP;
INSERT INTO minty_method VALUES
  ('Visa'), ('Mastercard'), ('Alipay'), ('WeChat Pay'), ('UnionPay'), ('Amex'), ('Octopus'),
  ('Payme'), ('JCB'), ('Food Panda'), ('Keeta'), ('OpenRice'), ('Deliveroo'), ('Cash');

-- 2. case twins of a Minty name
CREATE TEMP TABLE twin ON COMMIT DROP AS
SELECT t.id AS twin_id, m.id AS minty_id
  FROM sale_info t
  JOIN sale_info m ON lower(m.sale_name) = lower(t.sale_name) AND m.id <> t.id
 WHERE m.sale_name IN (SELECT name FROM minty_method)
   AND t.sale_name NOT IN (SELECT name FROM minty_method);

CREATE TEMP TABLE rs_group ON COMMIT DROP AS
SELECT r.id, r.report_id, COALESCE(w.minty_id, r.sale_id) AS target, r.amount,
       r.sale_id = COALESCE(w.minty_id, r.sale_id) AS is_target
  FROM report_sale r LEFT JOIN twin w ON w.twin_id = r.sale_id
 WHERE r.sale_id IN (SELECT twin_id FROM twin UNION SELECT minty_id FROM twin);
CREATE TEMP TABLE rs_keep ON COMMIT DROP AS
SELECT DISTINCT ON (report_id, target) id, report_id, target
  FROM rs_group ORDER BY report_id, target, is_target DESC, id;
CREATE TEMP TABLE rs_sum ON COMMIT DROP AS
SELECT report_id, target, sum(amount) AS total FROM rs_group GROUP BY report_id, target;
DELETE FROM report_sale WHERE id IN (SELECT id FROM rs_group) AND id NOT IN (SELECT id FROM rs_keep);
UPDATE report_sale r SET sale_id = k.target, amount = s.total
  FROM rs_keep k JOIN rs_sum s USING (report_id, target)
 WHERE r.id = k.id;

CREATE TEMP TABLE ess_group ON COMMIT DROP AS
SELECT e.entity_id, COALESCE(w.minty_id, e.sale_id) AS target, e.is_active, e.display_order
  FROM entity_sale_setting e LEFT JOIN twin w ON w.twin_id = e.sale_id
 WHERE e.sale_id IN (SELECT twin_id FROM twin UNION SELECT minty_id FROM twin);
DELETE FROM entity_sale_setting WHERE sale_id IN (SELECT twin_id FROM twin);
INSERT INTO entity_sale_setting (entity_id, sale_id, is_active, display_order)
SELECT entity_id, target, bool_or(is_active), min(display_order) FROM ess_group GROUP BY entity_id, target
ON CONFLICT (entity_id, sale_id) DO UPDATE
  SET is_active = EXCLUDED.is_active, display_order = EXCLUDED.display_order;
DELETE FROM sale_info WHERE id IN (SELECT twin_id FROM twin);

-- 3. rows nobody uses
DELETE FROM sale_info s
 WHERE s.sale_name NOT IN (SELECT name FROM minty_method)
   AND NOT EXISTS (SELECT 1 FROM entity_sale_setting e WHERE e.sale_id = s.id)
   AND NOT EXISTS (SELECT 1 FROM report_sale r WHERE r.sale_id = s.id);

-- 4. every other row is its company's own; a shared one is split
CREATE TEMP TABLE sale_user ON COMMIT DROP AS
SELECT sale_id, entity_id, sum(n) AS n FROM (
    SELECT e.sale_id, e.entity_id, 0 AS n FROM entity_sale_setting e
    UNION ALL
    SELECT r.sale_id, rep.entity_id, 1 FROM report_sale r JOIN report rep ON rep.id = r.report_id
  ) x
 WHERE sale_id IN (SELECT id FROM sale_info WHERE sale_name NOT IN (SELECT name FROM minty_method))
 GROUP BY sale_id, entity_id;
CREATE TEMP TABLE keeper ON COMMIT DROP AS
SELECT DISTINCT ON (sale_id) sale_id, entity_id FROM sale_user ORDER BY sale_id, n DESC, entity_id;
UPDATE sale_info s SET entity_id = k.entity_id FROM keeper k WHERE s.id = k.sale_id;

CREATE TEMP TABLE sale_copy ON COMMIT DROP AS
SELECT u.sale_id, u.entity_id, gen_random_uuid() AS new_id
  FROM sale_user u LEFT JOIN keeper k ON k.sale_id = u.sale_id AND k.entity_id = u.entity_id
 WHERE k.sale_id IS NULL;
INSERT INTO sale_info (id, type, sale_name, value_name, display_order, enabled, created_at, entity_id)
SELECT c.new_id, s.type, s.sale_name, s.value_name, s.display_order, s.enabled, s.created_at, c.entity_id
  FROM sale_copy c JOIN sale_info s ON s.id = c.sale_id;
UPDATE entity_sale_setting e SET sale_id = c.new_id
  FROM sale_copy c WHERE e.sale_id = c.sale_id AND e.entity_id = c.entity_id;
UPDATE report_sale r SET sale_id = c.new_id
  FROM sale_copy c, report rep
 WHERE rep.id = r.report_id AND r.sale_id = c.sale_id AND rep.entity_id = c.entity_id;

-- 5. GCash is a card and wallet payment, not a delivery service (owner, 2026-11-12): every company's
--    GCash row moves to Electronic; its report rows follow the method, so past reports count it there
UPDATE sale_info SET type = 'electronic' WHERE lower(btrim(sale_name)) = 'gcash' AND type <> 'electronic';

ALTER TABLE sale_info
  ADD CONSTRAINT fk_sale_info_entity FOREIGN KEY (entity_id) REFERENCES entities (id) ON DELETE CASCADE;
CREATE UNIQUE INDEX uq_sale_info_minty_name  ON sale_info (lower(sale_name)) WHERE entity_id IS NULL;
CREATE UNIQUE INDEX uq_sale_info_entity_name ON sale_info (entity_id, lower(sale_name)) WHERE entity_id IS NOT NULL;

SELECT (SELECT count(*) FROM twin) AS case_twins_merged,
       (SELECT count(*) FROM sale_copy) AS copies_made,
       count(*) FILTER (WHERE entity_id IS NULL) AS mintys,
       count(*) FILTER (WHERE entity_id IS NOT NULL) AS companies_own
  FROM sale_info;
