-- 2026-11-13: report_expense.remarks becomes report_expense.description (owner), for a database
-- already recorded at 0001. What a person types in an expense line's Description field always
-- lived in `remarks`; the `description` column beside it was never written (0 of 16,599 rows on
-- the 2026-11-04 copy of production). The empty column goes and `remarks` takes its name, keeping
-- its VARCHAR(300) and every value.
--
-- The baseline is minty_db/schema/01_schema_rebased.sql (the same two statements in its last
-- section), so a fresh build already has it. Schema changes stay baseline (owner, 2026-11-10):
--
--     python -m minty_db patch minty_db/schema/patches/2026-11-13_expense_description.sql           # dry run
--     python -m minty_db patch minty_db/schema/patches/2026-11-13_expense_description.sql --apply
--
-- RELEASE ORDER: code that reads `remarks` (minty-petty-cash-api before minty-db v0.8) fails on
-- every expense line once this is applied, so apply it as the v0.8 build goes live. It stops,
-- changing nothing, when `remarks` is already gone or when any line holds a `description`. Not
-- re-runnable. The way back is its _down.sql.

DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                 WHERE table_schema = current_schema() AND table_name = 'report_expense'
                   AND column_name = 'remarks') THEN
    RAISE EXCEPTION 'report_expense.remarks is already renamed in this schema';
  END IF;
  IF EXISTS (SELECT 1 FROM report_expense WHERE description IS NOT NULL) THEN
    RAISE EXCEPTION 'report_expense.description holds values; nothing was changed';
  END IF;
END $$;

ALTER TABLE report_expense DROP COLUMN description;
ALTER TABLE report_expense RENAME COLUMN remarks TO description;

SELECT count(*) AS lines, count(nullif(description, '')) AS with_description FROM report_expense;
