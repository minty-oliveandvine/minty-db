-- 2026-11-10: the way back from 2026-11-10_xero_optional_expand.sql - drops everything it added.
--
--     python -m minty_db patch minty_db/schema/patches/2026-11-10_xero_optional_expand_down.sql           # dry run
--     python -m minty_db patch minty_db/schema/patches/2026-11-10_xero_optional_expand_down.sql --apply
--
-- Only while nothing writes the new shapes yet (before the first SWITCH release): it throws
-- away the Xero copies, the links, the module ticks, both change logs, the saved picks, the
-- templates and onboarding's two answers. It refuses rather than lose a supplier: if any
-- supplier has no Xero contact id (one made in Minty), restoring NOT NULL fails and the whole
-- run rolls back. One transaction, rolled back unless --apply.

DROP TRIGGER trg_entities_fill_xero_status ON entities;
DROP TRIGGER trg_account_info_fill ON account_info;
DROP FUNCTION entities_fill_xero_status();
DROP FUNCTION account_info_fill();

DROP TABLE account_template;          -- with trg_account_template_no_overlap
DROP FUNCTION account_template_no_overlap();
DROP TABLE contact_change_log;
DROP TABLE account_match_draft;
DROP TABLE account_change_log;
DROP TABLE account_module;            -- with trg_account_module_updated

ALTER TABLE bill DROP COLUMN published_contact_name;
ALTER TABLE report_expense
  DROP COLUMN published_account_code,
  DROP COLUMN published_account_name,
  DROP COLUMN published_contact_name;
ALTER TABLE bill_line
  DROP COLUMN account_id,
  DROP COLUMN published_account_code,
  DROP COLUMN published_account_name;
ALTER TABLE entities
  DROP COLUMN xero_status,
  DROP COLUMN setup_mode,
  DROP COLUMN xero_publish_from,
  DROP COLUMN business_type,
  DROP COLUMN uses_sales_cash;

ALTER TABLE xero_contact_sync
  DROP COLUMN merged_into_id,           -- with fk_contact_merged_into and the two checks
  DROP COLUMN created_by,
  DROP COLUMN xero_contact_link_id,     -- with fk_contact_xero_link
  DROP COLUMN source,
  DROP COLUMN record_status,
  DROP COLUMN phone,
  DROP COLUMN email,
  DROP COLUMN is_customer,
  DROP COLUMN is_supplier,
  DROP CONSTRAINT uq_contact_id_entity,
  ALTER COLUMN xero_contact_id SET NOT NULL;

ALTER TABLE account_info
  DROP COLUMN active_xero_link,         -- with uq_account_active_xero_link
  DROP COLUMN merged_into_id,           -- with fk_account_merged_into and the two checks
  DROP COLUMN created_by,
  DROP COLUMN xero_account_link_id,     -- with fk_account_xero_link
  DROP COLUMN source,
  DROP COLUMN is_system,
  DROP COLUMN record_status,
  DROP COLUMN class,
  DROP COLUMN account_type,
  DROP COLUMN code,
  DROP CONSTRAINT uq_account_id_entity;

DROP TABLE xero_contact;
DROP TABLE xero_account;

DROP FUNCTION xero_status_of(entity_status, TEXT);
DROP FUNCTION record_status_from_xero(TEXT);
DROP FUNCTION account_code_from_xero(TEXT, TEXT);
DROP FUNCTION account_type_from_xero(TEXT);

DROP TYPE business_type;
DROP TYPE record_change_kind;
DROP TYPE pettycash_role;
DROP TYPE setup_mode;
DROP TYPE xero_status;
DROP TYPE record_source;
DROP TYPE record_status;
DROP TYPE account_class;
DROP TYPE account_type;

SELECT 'Xero-optional EXPAND removed' AS done;
