-- 2026-11-10: a DATA repair, not a schema change (owner: "fix it"). Run by hand on each database:
--
--     python -m minty_db patch minty_db/schema/patches/2026-11-10_bill_contact_ids.sql           # dry run
--     python -m minty_db patch minty_db/schema/patches/2026-11-10_bill_contact_ids.sql --apply
--
-- What was wrong: the v2->v3 load (Minty docs/schema/generators/gen.py, `("bill", "contact_id")`)
-- copied the source's Xero ContactID into bill.contact_id, but bill.contact_id is a
-- xero_contact_sync row id - so every loaded bill opened with no supplier picked (1,550 of 1,554
-- on the 11-04 copy; the publish found the contact again by its name). Each bill whose contact_id
-- names no supplier row is linked, in order:
--   1. to the company's supplier row with that Xero id;
--   2. else to the company's supplier with the bill's supplier name (ignoring case and spaces;
--      a Xero-synced one first, then the oldest);
--   3. else a supplier row is restored for it (that Xero id, the bill's supplier name, the
--      company's org), as the load restores the contacts expenses name, and the bill linked to it.
-- A bill of a company that no longer exists is left as it is (no supplier row can be made for it):
-- on the 11-04 copy those are the two test bills the owner deleted on 2026-11-09 (Minty's
-- docs/modernisation/minty-xero.sql SWITCH 2c-0), so the count after is 2 there until 2c-0 runs.
-- One transaction, rolled back unless --apply. Re-runnable: a second run finds nothing. The SELECTs
-- are the report. Minty's load runs the same statements at the end of 03 (gen.py BILL_CONTACTS).

-- ---- before ---------------------------------------------------------------------------------

SELECT 'bills naming no supplier row' AS check, count(*) AS bills FROM bill b
 WHERE b.contact_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM xero_contact_sync x WHERE x.id = b.contact_id);

-- ---- 1. by the Xero id, in the same company ----------------------------------------------------

UPDATE bill b SET contact_id = c.id
  FROM xero_contact_sync c
 WHERE b.contact_id IS NOT NULL
   AND NOT EXISTS (SELECT 1 FROM xero_contact_sync x WHERE x.id = b.contact_id)
   AND c.entity_id = b.entity_id AND c.xero_contact_id = b.contact_id::text;

-- ---- 2. by the supplier's name -----------------------------------------------------------------

UPDATE bill b SET contact_id = m.supplier_id
  FROM (SELECT DISTINCT ON (b2.id) b2.id AS bill_id, c.id AS supplier_id
          FROM bill b2
          JOIN xero_contact_sync c
            ON c.entity_id = b2.entity_id AND lower(btrim(c.name)) = lower(btrim(b2.contact_name))
         WHERE b2.contact_id IS NOT NULL
           AND NOT EXISTS (SELECT 1 FROM xero_contact_sync x WHERE x.id = b2.contact_id)
         ORDER BY b2.id, (c.xero_contact_id IS NULL), c.created_at, c.id) m
 WHERE b.id = m.bill_id;

-- ---- 3. restore a supplier row for the rest, and link it ---------------------------------------

INSERT INTO xero_contact_sync (entity_id, xero_contact_id, xero_org_id, name)
SELECT DISTINCT ON (b.entity_id, b.contact_id)
       b.entity_id, b.contact_id::text, e.xero_org_id, coalesce(nullif(btrim(b.contact_name), ''), 'Supplier')
  FROM bill b
  JOIN entities e ON e.id = b.entity_id
 WHERE b.contact_id IS NOT NULL
   AND NOT EXISTS (SELECT 1 FROM xero_contact_sync x WHERE x.id = b.contact_id)
 ORDER BY b.entity_id, b.contact_id, b.created_at
ON CONFLICT DO NOTHING;

UPDATE bill b SET contact_id = c.id
  FROM xero_contact_sync c
 WHERE b.contact_id IS NOT NULL
   AND NOT EXISTS (SELECT 1 FROM xero_contact_sync x WHERE x.id = b.contact_id)
   AND c.entity_id = b.entity_id AND c.xero_contact_id = b.contact_id::text;

-- ---- after ----------------------------------------------------------------------------------

SELECT 'bills naming no supplier row after' AS check, count(*) AS bills FROM bill b
 WHERE b.contact_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM xero_contact_sync x WHERE x.id = b.contact_id);
