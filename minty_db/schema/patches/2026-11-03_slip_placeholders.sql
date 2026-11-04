-- 2026-11-03 (owner): fold the bank-slip placeholder payments into their bill's real payment.
--
-- A data repair, not a schema change. Run by hand on each database, like the schema patches:
--
--     python -m minty_db patch minty_db/schema/patches/2026-11-03_slip_placeholders.sql           # dry run
--     python -m minty_db patch minty_db/schema/patches/2026-11-03_slip_placeholders.sql --apply
--
-- The runner sets search_path to DATABASE_URL's schema, so nothing here names one, and runs the
-- file in ONE transaction that it rolls back unless --apply. The SELECTs are the report.
-- Idempotent: a second run finds nothing to fold.
--
-- Why: until 2026-11-03 minty-payment-request-web's bank slip upload made a 0.00 payment with no
-- date to hold a slip when the bill had no payment yet, and the real payment was recorded beside
-- it - two rows for one payment. Where the bill (not voided) has exactly ONE real payment, the
-- holder's slips move onto it; with several, onto the NEXT real payment recorded after the holder
-- (owner, 2026-11-03: the slip was chosen first and its payment added seconds later). Then the
-- holder is deleted - links FIRST, because deleting a payment cascades to its payment_attachment
-- rows. A bill whose real payments all came before the holder is the owner's to settle (listed
-- below); a bill with none keeps its holder (the slip is all there is). Minty's
-- v2->v3 load does the same (docs/schema/generators/gen.py SLIP_PLACEHOLDERS) - change both
-- together.

-- ---- before ---------------------------------------------------------------------------------

-- a holder's payment: the bill's one real payment, or with several the next recorded after it
CREATE TEMP TABLE _slip_holder ON COMMIT DROP AS
SELECT z.id AS holder_id, t.id AS payment_id
  FROM payment z
  JOIN bill b ON b.id = z.bill_id
 CROSS JOIN LATERAL (
       SELECT p.id FROM payment p
        WHERE p.bill_id = z.bill_id AND p.amount > 0
          AND (p.created_at >= z.created_at
               OR (SELECT count(*) FROM payment q WHERE q.bill_id = z.bill_id AND q.amount > 0) = 1)
        ORDER BY p.created_at, p.id LIMIT 1) t
 WHERE z.amount = 0 AND z.payment_date IS NULL AND b.status <> 'void';

CREATE TEMP TABLE _slip_links ON COMMIT DROP AS
SELECT pa.id FROM payment_attachment pa JOIN _slip_holder h ON h.holder_id = pa.payment_id;

SELECT 'slip holders to fold' AS check, count(*) AS rows FROM _slip_holder
UNION ALL
SELECT 'slip links to move', count(*) FROM _slip_links
UNION ALL
SELECT 'holders left: after all of several real payments', count(*) FROM payment z JOIN bill b ON b.id = z.bill_id
 WHERE z.amount = 0 AND z.payment_date IS NULL AND b.status <> 'void'
   AND (SELECT count(*) FROM payment p WHERE p.bill_id = z.bill_id AND p.amount > 0) > 1
   AND NOT EXISTS (SELECT 1 FROM payment p WHERE p.bill_id = z.bill_id AND p.amount > 0
                      AND p.created_at >= z.created_at)
UNION ALL
SELECT 'holders left: no real payment', count(*) FROM payment z JOIN bill b ON b.id = z.bill_id
 WHERE z.amount = 0 AND z.payment_date IS NULL AND b.status <> 'void'
   AND NOT EXISTS (SELECT 1 FROM payment p WHERE p.bill_id = z.bill_id AND p.amount > 0)
UNION ALL
SELECT 'holders left: voided bill', count(*) FROM payment z JOIN bill b ON b.id = z.bill_id
 WHERE z.amount = 0 AND z.payment_date IS NULL AND b.status = 'void';

-- the bills left for the owner (several real payments, all before the holder):
-- bill | Payment No. | real payments | slips
SELECT b.id AS bill, b.reference,
       (SELECT string_agg(p.payment_date::text || ' ' || p.amount::text, ', ' ORDER BY p.payment_date)
          FROM payment p WHERE p.bill_id = b.id AND p.amount > 0) AS real_payments,
       (SELECT string_agg(a.original_name, ', ' ORDER BY a.original_name)
          FROM payment_attachment pa JOIN attachment a ON a.id = pa.attachment_id
         WHERE pa.payment_id = z.id) AS slips
  FROM payment z JOIN bill b ON b.id = z.bill_id
 WHERE z.amount = 0 AND z.payment_date IS NULL AND b.status <> 'void'
   AND (SELECT count(*) FROM payment p WHERE p.bill_id = z.bill_id AND p.amount > 0) > 1
   AND NOT EXISTS (SELECT 1 FROM payment p WHERE p.bill_id = z.bill_id AND p.amount > 0
                      AND p.created_at >= z.created_at)
 ORDER BY b.reference;

-- ---- the repair: links first, then the holders ----------------------------------------------

UPDATE payment_attachment pa SET payment_id = h.payment_id
  FROM _slip_holder h WHERE pa.payment_id = h.holder_id;

DELETE FROM payment p USING _slip_holder h WHERE p.id = h.holder_id;

-- ---- after: every moved link is on its real payment (else the whole run rolls back) ---------

DO $$
DECLARE links bigint; kept bigint;
BEGIN
  SELECT count(*) INTO links FROM _slip_links;
  SELECT count(*) INTO kept FROM payment_attachment pa JOIN _slip_links l ON l.id = pa.id
   WHERE pa.payment_id IN (SELECT payment_id FROM _slip_holder);
  IF links <> kept THEN
    RAISE EXCEPTION '% slip link(s) did not reach the real payment - nothing applied', links - kept;
  END IF;
END $$;

SELECT 'slip links now on the real payment' AS check, count(*) AS rows
  FROM payment_attachment pa JOIN _slip_links l ON l.id = pa.id;
