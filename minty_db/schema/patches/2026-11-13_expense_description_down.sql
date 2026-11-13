-- 2026-11-13: the way back from 2026-11-13_expense_description.sql. `description` is `remarks`
-- again, and an empty TEXT `description` returns beside it (at the table's end; column order is
-- not compared). One transaction, rolled back unless --apply.

ALTER TABLE report_expense RENAME COLUMN description TO remarks;
ALTER TABLE report_expense ADD COLUMN description TEXT NULL;

SELECT 'report_expense.remarks restored' AS done;
