-- 2026-11-12: the way back from 2026-11-12_xero_matching.sql. It forgets which companies have a
-- matching open (minty-accounts-api's Phase 2 rule still finds a first connect's). One transaction,
-- rolled back unless --apply.

ALTER TABLE entities DROP COLUMN xero_matching;
DROP TYPE xero_matching;

SELECT 'entities.xero_matching removed' AS done;
