-- 2026-11-10: Xero optional, EXPAND - for a database already recorded at 0001.
--
-- The baseline is minty_db/schema/01_schema_rebased.sql (its last section is this DDL), so a fresh
-- build already has it. 0001 stays the only migration (owner, 2026-11-03; schema changes stay
-- baseline, 2026-11-10); this patch brings an existing database to the same baseline:
--
--     python -m minty_db patch minty_db/schema/patches/2026-11-10_xero_optional_expand.sql           # dry run
--     python -m minty_db patch minty_db/schema/patches/2026-11-10_xero_optional_expand.sql --apply
--
-- The runner sets search_path to DATABASE_URL's schema and runs the file in ONE transaction,
-- rolled back unless --apply. The SELECTs at the end are the report. Not re-runnable: a second
-- run stops at the guard. The way back is 2026-11-10_xero_optional_expand_down.sql.
--
-- What it is: Minty owns its accounts and suppliers, Xero becomes optional. Additive only: every
-- service keeps working on the old shapes, and triggers keep the new columns in step while old
-- code writes only the old ones. Design: Minty/docs/modernisation/xero_optional_database.md;
-- source text: Minty/docs/modernisation/minty-xero.sql part 1.

DO $$ BEGIN
  IF to_regclass('xero_account') IS NOT NULL THEN
    RAISE EXCEPTION 'the Xero-optional EXPAND is already in this schema';
  END IF;
END $$;

-- enums
CREATE TYPE account_type AS ENUM
  ('bank','current','fixed','inventory','noncurrent','prepayment',
   'current_liability','liability','term_liability','equity',
   'sales','revenue','other_income','direct_costs','expense','overheads','depreciation');
CREATE TYPE account_class  AS ENUM ('asset','liability','equity','revenue','expense');
CREATE TYPE record_status  AS ENUM ('active','archived');
CREATE TYPE record_source  AS ENUM ('template','xero','user');
CREATE TYPE xero_status    AS ENUM ('not_connected','connected','disconnected');
CREATE TYPE setup_mode     AS ENUM ('minty','xero');
CREATE TYPE pettycash_role AS ENUM ('petty_cash','bank','cash_sale','discrepancy','director');
CREATE TYPE record_change_kind AS ENUM
  ('linked','overwritten','merged','unlinked','archived','created_in_xero');
CREATE TYPE business_type  AS ENUM ('fnb','retail','service','general');

-- Xero value -> Minty value (backfill and triggers; dropped in CONTRACT). SQL-standard bodies are
-- stored parsed and PL/pgSQL bodies reach their schema through TG_TABLE_SCHEMA, so both survive
-- minty-db's build, which creates the schema under another name and renames it
CREATE FUNCTION account_type_from_xero(t TEXT) RETURNS account_type
LANGUAGE sql IMMUTABLE
RETURN (CASE upper(btrim(t))
    WHEN 'BANK' THEN 'bank'                  WHEN 'CURRENT' THEN 'current'
    WHEN 'FIXED' THEN 'fixed'                WHEN 'INVENTORY' THEN 'inventory'
    WHEN 'NONCURRENT' THEN 'noncurrent'      WHEN 'PREPAYMENT' THEN 'prepayment'
    WHEN 'CURRLIAB' THEN 'current_liability' WHEN 'LIABILITY' THEN 'liability'
    WHEN 'TERMLIAB' THEN 'term_liability'    WHEN 'EQUITY' THEN 'equity'
    WHEN 'SALES' THEN 'sales'                WHEN 'REVENUE' THEN 'revenue'
    WHEN 'OTHERINCOME' THEN 'other_income'   WHEN 'DIRECTCOSTS' THEN 'direct_costs'
    WHEN 'EXPENSE' THEN 'expense'            WHEN 'OVERHEADS' THEN 'overheads'
    WHEN 'DEPRECIATN' THEN 'depreciation'
    WHEN 'WAGESEXPENSE' THEN 'expense'       WHEN 'SUPERANNUATIONEXPENSE' THEN 'expense'
    WHEN 'PAYGLIABILITY' THEN 'current_liability' WHEN 'SUPERANNUATIONLIABILITY' THEN 'current_liability'
  END)::account_type;   -- unknown -> NULL, refused by CONTRACT's NOT NULL

CREATE FUNCTION account_code_from_xero(code TEXT, t TEXT) RETURNS VARCHAR(10)
LANGUAGE sql IMMUTABLE
RETURN CASE WHEN NULLIF(btrim(code), '') IS NULL THEN NULL
            WHEN upper(btrim(t)) = 'BANK' AND length(btrim(code)) > 10 THEN NULL   -- the name, not a code
            ELSE btrim(code) END;

CREATE FUNCTION record_status_from_xero(s TEXT) RETURNS record_status
LANGUAGE sql IMMUTABLE
RETURN (CASE WHEN upper(btrim(coalesce(s, ''))) IN ('INACTIVE','ARCHIVED','DELETED')
             THEN 'archived' ELSE 'active' END)::record_status;

CREATE FUNCTION xero_status_of(s entity_status, org TEXT) RETURNS xero_status
LANGUAGE sql IMMUTABLE
RETURN (CASE WHEN s = 'disconnected' THEN 'disconnected'   -- with or without a kept org
             WHEN org IS NULL THEN 'not_connected'
             ELSE 'connected' END)::xero_status;

-- Xero copies
CREATE TABLE xero_account (
  id                  UUID         NOT NULL DEFAULT gen_random_uuid(),
  entity_id           UUID         NOT NULL,
  xero_org_id         VARCHAR(36)  NOT NULL,
  xero_guid           VARCHAR(36)  NOT NULL,   -- Xero's AccountID
  code                VARCHAR(10)  NULL,       -- Xero's code (bank accounts may have none)
  name                VARCHAR(150) NOT NULL,
  xero_type           VARCHAR(50)  NOT NULL,   -- Xero's Type, verbatim (EXPENSE, CURRLIAB, ...)
  xero_class          VARCHAR(50)  NULL,       -- Xero's Class, verbatim
  status              VARCHAR(50)  NOT NULL DEFAULT 'ACTIVE',
  bank_account_number VARCHAR(50)  NULL,
  bank_account_type   VARCHAR(50)  NULL,
  synced_at           TIMESTAMPTZ  NOT NULL DEFAULT now(),
  CONSTRAINT xero_account_pkey PRIMARY KEY (id),
  CONSTRAINT uq_xero_account_id_entity UNIQUE (id, entity_id),
  CONSTRAINT uq_xero_account_guid UNIQUE (entity_id, xero_org_id, xero_guid),
  CONSTRAINT fk_xero_account_entity FOREIGN KEY (entity_id) REFERENCES entities (id) ON DELETE CASCADE
);

CREATE TABLE xero_contact (
  id          UUID         NOT NULL DEFAULT gen_random_uuid(),
  entity_id   UUID         NOT NULL,
  xero_org_id VARCHAR(36)  NOT NULL,
  xero_guid   VARCHAR(36)  NOT NULL,           -- Xero's ContactID
  name        VARCHAR(255) NOT NULL,
  status      VARCHAR(50)  NOT NULL DEFAULT 'ACTIVE',
  synced_at   TIMESTAMPTZ  NOT NULL DEFAULT now(),
  CONSTRAINT xero_contact_pkey PRIMARY KEY (id),
  CONSTRAINT uq_xero_contact_id_entity UNIQUE (id, entity_id),
  CONSTRAINT uq_xero_contact_guid UNIQUE (entity_id, xero_org_id, xero_guid),
  CONSTRAINT fk_xero_contact_entity FOREIGN KEY (entity_id) REFERENCES entities (id) ON DELETE CASCADE
);

-- account_info: Minty's columns beside the Xero ones (renamed to account in CONTRACT).
-- xero_account_id stays Xero's GUID; the link is xero_account_link_id.
ALTER TABLE account_info
  ADD COLUMN code                 VARCHAR(10)                NULL,
  ADD COLUMN account_type         account_type   NULL,   -- becomes type
  ADD COLUMN class                account_class  GENERATED ALWAYS AS (
    CASE
      WHEN account_type IS NULL THEN NULL
      WHEN account_type IN ('bank','current','fixed','inventory','noncurrent','prepayment') THEN 'asset'::account_class
      WHEN account_type IN ('current_liability','liability','term_liability')               THEN 'liability'::account_class
      WHEN account_type = 'equity'                                                          THEN 'equity'::account_class
      WHEN account_type IN ('sales','revenue','other_income')                               THEN 'revenue'::account_class
      ELSE 'expense'::account_class
    END) STORED,
  ADD COLUMN record_status        record_status  NOT NULL DEFAULT 'active',  -- becomes status
  ADD COLUMN is_system            BOOLEAN                    NOT NULL DEFAULT FALSE,
  ADD COLUMN source               record_source  NOT NULL DEFAULT 'xero',    -- 'user' in CONTRACT
  ADD COLUMN xero_account_link_id UUID                       NULL,
  ADD COLUMN created_by           UUID                       NULL,
  ADD CONSTRAINT uq_account_id_entity UNIQUE (id, entity_id),
  ADD CONSTRAINT fk_account_xero_link FOREIGN KEY (xero_account_link_id, entity_id)
    REFERENCES xero_account (id, entity_id),
  ADD COLUMN merged_into_id       UUID                       NULL,   -- set when combined into another account
  ADD CONSTRAINT fk_account_creator FOREIGN KEY (created_by) REFERENCES "user" (id) ON DELETE SET NULL,
  ADD CONSTRAINT chk_account_merged_archived CHECK (merged_into_id IS NULL OR record_status = 'archived'),
  ADD CONSTRAINT chk_account_merged_self CHECK (merged_into_id <> id);
ALTER TABLE account_info
  ADD CONSTRAINT fk_account_merged_into FOREIGN KEY (merged_into_id, entity_id)
    REFERENCES account_info (id, entity_id);
-- one active account per Xero account (Q2, 2026-11-09): accounts matched to the same Xero account are combined
-- in the replace's transaction, before the survivor takes the link; a merged (archived) account keeps its link.
-- Safe on today's data: uq_account_entity_xero already holds one account per Xero id per company
ALTER TABLE account_info
  ADD COLUMN active_xero_link UUID GENERATED ALWAYS AS
    (CASE WHEN record_status = 'active' THEN xero_account_link_id END) STORED,
  ADD CONSTRAINT uq_account_active_xero_link UNIQUE (entity_id, active_xero_link) DEFERRABLE INITIALLY IMMEDIATE;

-- xero_contact_sync: the same (renamed to contact in CONTRACT)
ALTER TABLE xero_contact_sync
  ALTER COLUMN xero_contact_id DROP NOT NULL,
  ADD COLUMN is_supplier          BOOLEAN                    NOT NULL DEFAULT TRUE,
  ADD COLUMN is_customer          BOOLEAN                    NOT NULL DEFAULT FALSE,
  ADD COLUMN email                VARCHAR(255)               NULL,
  ADD COLUMN phone                VARCHAR(50)                NULL,
  ADD COLUMN record_status        record_status  NOT NULL DEFAULT 'active',  -- becomes status
  ADD COLUMN source               record_source  NOT NULL DEFAULT 'xero',    -- 'user' in CONTRACT
  ADD COLUMN xero_contact_link_id UUID                       NULL,
  ADD COLUMN created_by           UUID                       NULL,
  ADD CONSTRAINT uq_contact_id_entity UNIQUE (id, entity_id),
  ADD CONSTRAINT fk_contact_xero_link FOREIGN KEY (xero_contact_link_id, entity_id)
    REFERENCES xero_contact (id, entity_id),
  ADD COLUMN merged_into_id       UUID                       NULL,
  ADD CONSTRAINT fk_contact_creator FOREIGN KEY (created_by) REFERENCES "user" (id) ON DELETE SET NULL,
  ADD CONSTRAINT chk_contact_merged_archived CHECK (merged_into_id IS NULL OR record_status = 'archived'),
  ADD CONSTRAINT chk_contact_merged_self CHECK (merged_into_id <> id);
ALTER TABLE xero_contact_sync
  ADD CONSTRAINT fk_contact_merged_into FOREIGN KEY (merged_into_id, entity_id)
    REFERENCES xero_contact_sync (id, entity_id);

-- account_module: replaces entity_account_xero and entity_bill_account_xero
CREATE TABLE account_module (
  account_id  UUID                     NOT NULL,
  entity_id   UUID                     NOT NULL,   -- the account's company, for the one-default rule
  module_code module_code  NOT NULL,
  is_active   BOOLEAN                  NOT NULL DEFAULT TRUE,
  is_default  BOOLEAN                  NOT NULL DEFAULT FALSE,
  sort_order  INTEGER                  NOT NULL DEFAULT 0,
  created_at  TIMESTAMPTZ              NOT NULL DEFAULT now(),
  updated_at  TIMESTAMPTZ              NOT NULL DEFAULT now(),
  CONSTRAINT account_module_pkey PRIMARY KEY (account_id, module_code),
  CONSTRAINT chk_account_module_default CHECK (NOT is_default OR is_active),
  CONSTRAINT fk_account_module_account FOREIGN KEY (account_id, entity_id)
    REFERENCES account_info (id, entity_id) ON DELETE CASCADE
);
CREATE UNIQUE INDEX uq_account_module_default ON account_module (entity_id, module_code)
  WHERE is_default;
CREATE TRIGGER trg_account_module_updated BEFORE UPDATE ON account_module
  FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- account_change_log: before and after for every account the replace, the sync or a merge changes,
-- and the unused accounts the replace archives
-- ("405 Rent & premises -> 469 Rent", "402 merged into 477")
CREATE TABLE account_change_log (
  id             UUID                             NOT NULL DEFAULT gen_random_uuid(),
  entity_id      UUID                             NOT NULL,
  account_id     UUID                             NOT NULL,
  batch_id       UUID                             NOT NULL,   -- one replace, sync run or clean-up
  kind           record_change_kind   NOT NULL,
  before_code    VARCHAR(10)                      NULL,
  before_name    VARCHAR(150)                     NULL,
  before_type    account_type         NULL,
  after_code     VARCHAR(10)                      NULL,
  after_name     VARCHAR(150)                     NULL,
  after_type     account_type         NULL,
  merged_into_id UUID                             NULL,
  xero_org_id    VARCHAR(36)                      NULL,
  xero_guid      VARCHAR(36)                      NULL,
  changed_by     UUID                             NULL,       -- NULL = the sync or the migration
  changed_at     TIMESTAMPTZ                      NOT NULL DEFAULT now(),
  CONSTRAINT account_change_log_pkey PRIMARY KEY (id),
  CONSTRAINT chk_account_change_merged CHECK ((kind = 'merged') = (merged_into_id IS NOT NULL)),
  CONSTRAINT chk_account_change_not_created CHECK (kind <> 'created_in_xero'),   -- Minty never creates accounts in Xero
  CONSTRAINT fk_account_change_account FOREIGN KEY (account_id, entity_id)
    REFERENCES account_info (id, entity_id) ON DELETE CASCADE,
  CONSTRAINT fk_account_change_merged_into FOREIGN KEY (merged_into_id, entity_id)
    REFERENCES account_info (id, entity_id) ON DELETE CASCADE,
  CONSTRAINT fk_account_change_user FOREIGN KEY (changed_by) REFERENCES "user" (id) ON DELETE SET NULL
);
CREATE INDEX idx_account_change_log_entity  ON account_change_log (entity_id, changed_at);
CREATE INDEX idx_account_change_log_account ON account_change_log (account_id);

-- account_match_draft: the matching screen's picks until the replace is confirmed, so "Save and finish later" keeps them.
-- One pick per Minty account; several accounts may pick one Xero account (401 and 402 -> 477), because
-- nothing is linked yet: the replace combines them, writes the links and deletes the picks in one transaction
CREATE TABLE account_match_draft (
  account_id       UUID         NOT NULL,
  entity_id        UUID         NOT NULL,
  xero_account_id  UUID         NOT NULL,   -- the Xero account picked (a mirror row)
  picked_by        UUID         NULL,       -- NULL = Minty's own pick (same code, same type), not changed by a user
  picked_at        TIMESTAMPTZ  NOT NULL DEFAULT now(),
  CONSTRAINT account_match_draft_pkey PRIMARY KEY (account_id),
  CONSTRAINT fk_account_match_account FOREIGN KEY (account_id, entity_id)
    REFERENCES account_info (id, entity_id) ON DELETE CASCADE,
  CONSTRAINT fk_account_match_xero FOREIGN KEY (xero_account_id, entity_id)
    REFERENCES xero_account (id, entity_id) ON DELETE CASCADE,   -- a Xero account gone = pick again
  CONSTRAINT fk_account_match_user FOREIGN KEY (picked_by) REFERENCES "user" (id) ON DELETE SET NULL
);
CREATE INDEX idx_account_match_draft_entity ON account_match_draft (entity_id);
CREATE INDEX idx_account_match_draft_xero   ON account_match_draft (xero_account_id);

-- contact_change_log: the same for suppliers: linked to a Xero contact of the same name, created in Xero,
-- merged. A supplier keeps its Minty name, so nothing is ever 'overwritten'
CREATE TABLE contact_change_log (
  id             UUID                            NOT NULL DEFAULT gen_random_uuid(),
  entity_id      UUID                            NOT NULL,
  contact_id     UUID                            NOT NULL,
  batch_id       UUID                            NOT NULL,
  kind           record_change_kind  NOT NULL,
  before_name    VARCHAR(255)                    NULL,
  after_name     VARCHAR(255)                    NULL,
  merged_into_id UUID                            NULL,
  xero_org_id    VARCHAR(36)                     NULL,
  xero_guid      VARCHAR(36)                     NULL,
  changed_by     UUID                            NULL,       -- NULL = the sync or the migration
  changed_at     TIMESTAMPTZ                     NOT NULL DEFAULT now(),
  CONSTRAINT contact_change_log_pkey PRIMARY KEY (id),
  CONSTRAINT chk_contact_change_merged CHECK ((kind = 'merged') = (merged_into_id IS NOT NULL)),
  CONSTRAINT chk_contact_change_kind CHECK (kind IN ('linked','created_in_xero','merged','unlinked')),
  CONSTRAINT fk_contact_change_contact FOREIGN KEY (contact_id, entity_id)
    REFERENCES xero_contact_sync (id, entity_id) ON DELETE CASCADE,
  CONSTRAINT fk_contact_change_merged_into FOREIGN KEY (merged_into_id, entity_id)
    REFERENCES xero_contact_sync (id, entity_id) ON DELETE CASCADE,
  CONSTRAINT fk_contact_change_user FOREIGN KEY (changed_by) REFERENCES "user" (id) ON DELETE SET NULL
);
CREATE INDEX idx_contact_change_log_entity  ON contact_change_log (entity_id, changed_at);
CREATE INDEX idx_contact_change_log_contact ON contact_change_log (contact_id);

-- account_template: the owner's default charts, a catalogue the accounting team revises without a release.
-- One set per business type and cash handling (8 sets), plus shared rows (the system accounts).
-- NULL in country_code, business_type or uses_sales_cash = the row applies to every value of it
CREATE TABLE account_template (
  id                      UUID                        NOT NULL DEFAULT gen_random_uuid(),
  country_code            CHAR(2)                     NULL,
  business_type           business_type   NULL,
  uses_sales_cash         BOOLEAN                     NULL,   -- TRUE = "with Cash Sales", FALSE = "without Cash Sales"
  code                    VARCHAR(10)                 NULL,
  name                    VARCHAR(150)                NOT NULL,
  type                    account_type    NOT NULL,
  description             VARCHAR(255)                NULL,   -- the card's short description
  ticked_by_default       BOOLEAN                     NOT NULL DEFAULT TRUE,   -- Auto Fill adds it
  pettycash_default       BOOLEAN                     NOT NULL DEFAULT FALSE,
  payment_request_default BOOLEAN                     NOT NULL DEFAULT FALSE,
  pettycash_role          pettycash_role  NULL,
  is_system               BOOLEAN                     GENERATED ALWAYS AS (
    coalesce(pettycash_role IN ('petty_cash','bank','director'), FALSE)) STORED,   -- created hidden
  sort_order              INTEGER                     NOT NULL DEFAULT 0,
  CONSTRAINT account_template_pkey PRIMARY KEY (id),
  CONSTRAINT chk_account_template_code CHECK (code IS NOT NULL OR type = 'bank'),
  CONSTRAINT chk_account_template_system_ticked CHECK (
    pettycash_role IS NULL OR pettycash_role NOT IN ('petty_cash','bank','director') OR ticked_by_default),
  CONSTRAINT fk_account_template_country FOREIGN KEY (country_code) REFERENCES country_info (country_code) ON DELETE RESTRICT
);
CREATE UNIQUE INDEX uq_account_template_code ON account_template
  (country_code, business_type, uses_sales_cash, code) NULLS NOT DISTINCT WHERE code IS NOT NULL;
CREATE UNIQUE INDEX uq_account_template_role ON account_template
  (country_code, business_type, uses_sales_cash, pettycash_role) NULLS NOT DISTINCT WHERE pettycash_role IS NOT NULL;
CREATE UNIQUE INDEX uq_account_template_name ON account_template
  (country_code, business_type, uses_sales_cash, lower(btrim(name))) NULLS NOT DISTINCT;

-- the indexes compare equal scopes only; this also refuses a shared row and a set row that would both
-- land in one company with the same code, name or role (e.g. a shared "1000 Petty Cash" and F&B's "1000 Float")
CREATE FUNCTION account_template_no_overlap() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE clash BOOLEAN;
BEGIN
  PERFORM pg_advisory_xact_lock(hashtext(TG_TABLE_SCHEMA || '.account_template'));   -- one writer at a time
  EXECUTE format($q$
    SELECT EXISTS (SELECT 1 FROM %I.account_template t
      WHERE t.id <> $1
        AND (t.country_code    IS NULL OR $2 IS NULL OR t.country_code    = $2)
        AND (t.business_type   IS NULL OR $3 IS NULL OR t.business_type   = $3)
        AND (t.uses_sales_cash IS NULL OR $4 IS NULL OR t.uses_sales_cash = $4)
        AND (t.code = $5 OR lower(btrim(t.name)) = lower(btrim($6)) OR t.pettycash_role = $7))$q$, TG_TABLE_SCHEMA)
    INTO clash
    USING NEW.id, NEW.country_code, NEW.business_type, NEW.uses_sales_cash, NEW.code, NEW.name, NEW.pettycash_role;
  IF clash THEN
    RAISE EXCEPTION 'account_template row "%" clashes with a row that applies to the same set (code, name or role)', NEW.name
      USING ERRCODE = 'unique_violation';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER trg_account_template_no_overlap BEFORE INSERT OR UPDATE ON account_template
  FOR EACH ROW EXECUTE FUNCTION account_template_no_overlap();

-- entities: status keeps today's values until CONTRACT
ALTER TABLE entities
  ADD COLUMN xero_status xero_status NOT NULL DEFAULT 'not_connected',
  ADD COLUMN setup_mode  setup_mode  NULL,
  ADD COLUMN xero_publish_from DATE NULL,   -- the connect's choice: records dated earlier are never sent; NULL = all
  ADD COLUMN business_type   business_type NULL,   -- onboarding's two answers: the template set
  ADD COLUMN uses_sales_cash BOOLEAN NULL;                     --   Auto Fill and Settings suggest from
-- every company answers the cash question ("Not sure" = TRUE); business_type NULL = "None of these"
-- (only the shared rows). Both NULL = onboarded before the questions

-- lines point at accounts by id; published_* is what the publish sent, frozen at publish time
ALTER TABLE bill_line
  ADD COLUMN account_id             UUID         NULL,
  ADD COLUMN published_account_code VARCHAR(10)  NULL,
  ADD COLUMN published_account_name VARCHAR(150) NULL;
ALTER TABLE report_expense
  ADD COLUMN published_account_code VARCHAR(10)  NULL,
  ADD COLUMN published_account_name VARCHAR(150) NULL,
  ADD COLUMN published_contact_name VARCHAR(255) NULL;
ALTER TABLE bill ADD COLUMN published_contact_name VARCHAR(255) NULL;   -- bills follow contact_id

-- triggers: keep the new columns in step while old code writes only the old ones
CREATE FUNCTION account_info_fill() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE s TEXT := quote_ident(TG_TABLE_SCHEMA);
BEGIN
  IF TG_OP = 'INSERT' THEN
    IF NEW.code IS NULL THEN
      EXECUTE format('SELECT %s.account_code_from_xero($1, $2)', s) INTO NEW.code USING NEW.xero_code, NEW.type;
    END IF;
    IF NEW.account_type IS NULL THEN
      EXECUTE format('SELECT %s.account_type_from_xero($1)', s) INTO NEW.account_type USING NEW.type;
    END IF;
    IF NEW.status IS NOT NULL THEN
      EXECUTE format('SELECT %s.record_status_from_xero($1)', s) INTO NEW.record_status USING NEW.status;
    END IF;
  ELSE
    IF NEW.xero_code IS DISTINCT FROM OLD.xero_code OR NEW.type IS DISTINCT FROM OLD.type THEN
      EXECUTE format('SELECT %s.account_code_from_xero($1, $2)', s) INTO NEW.code USING NEW.xero_code, NEW.type;
    END IF;
    IF NEW.type IS DISTINCT FROM OLD.type THEN
      EXECUTE format('SELECT %s.account_type_from_xero($1)', s) INTO NEW.account_type USING NEW.type;
    END IF;
    IF NEW.status IS DISTINCT FROM OLD.status THEN
      EXECUTE format('SELECT %s.record_status_from_xero($1)', s) INTO NEW.record_status USING NEW.status;
    END IF;
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER trg_account_info_fill BEFORE INSERT OR UPDATE ON account_info
  FOR EACH ROW EXECUTE FUNCTION account_info_fill();

CREATE FUNCTION entities_fill_xero_status() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NEW.setup_mode = 'minty' AND NEW.xero_org_id IS NULL THEN
    -- Minty on its own: never connected, whatever status says (until CONTRACT a Minty-only
    -- company that goes live is written status = 'disconnected', the only live value there is)
    NEW.xero_status := 'not_connected';
  -- else derive only when the writer left xero_status alone (moved code sets it itself)
  ELSIF (TG_OP = 'INSERT' AND NEW.xero_status = 'not_connected')
     OR (TG_OP = 'UPDATE' AND NEW.xero_status IS NOT DISTINCT FROM OLD.xero_status) THEN
    EXECUTE format('SELECT %I.xero_status_of($1, $2)', TG_TABLE_SCHEMA) INTO NEW.xero_status
      USING NEW.status, NEW.xero_org_id;
  END IF;
  IF NEW.xero_org_id IS NOT NULL AND NEW.setup_mode IS NULL THEN
    NEW.setup_mode := 'xero';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER trg_entities_fill_xero_status BEFORE INSERT OR UPDATE OF status, xero_org_id, setup_mode
  ON entities FOR EACH ROW EXECUTE FUNCTION entities_fill_xero_status();

-- backfill
UPDATE account_info SET
  code          = account_code_from_xero(xero_code, type),
  account_type  = account_type_from_xero(type),
  record_status = record_status_from_xero(status);

INSERT INTO xero_account
  (entity_id, xero_org_id, xero_guid, code, name, xero_type, xero_class, status, bank_account_number, bank_account_type)
SELECT a.entity_id, coalesce(x.xero_org_id, e.xero_org_id), a.xero_account_id,
       account_code_from_xero(a.xero_code, a.type), a.name, a.type, a.class_type,
       coalesce(a.status, 'ACTIVE'), a.bank_account_number, a.bank_account_type
FROM account_info a
JOIN entities e ON e.id = a.entity_id
LEFT JOIN entity_account_xero x ON x.account_id = a.id
WHERE a.xero_account_id IS NOT NULL AND coalesce(x.xero_org_id, e.xero_org_id) IS NOT NULL;

UPDATE account_info a SET xero_account_link_id = m.id
FROM xero_account m
WHERE m.entity_id = a.entity_id AND m.xero_guid = a.xero_account_id;

INSERT INTO xero_contact (entity_id, xero_org_id, xero_guid, name)
SELECT c.entity_id, coalesce(c.xero_org_id, e.xero_org_id), c.xero_contact_id, c.name
FROM xero_contact_sync c
JOIN entities e ON e.id = c.entity_id
WHERE c.xero_contact_id IS NOT NULL AND coalesce(c.xero_org_id, e.xero_org_id) IS NOT NULL;

UPDATE xero_contact_sync c SET xero_contact_link_id = m.id
FROM xero_contact m, entities e
WHERE e.id = c.entity_id AND m.entity_id = c.entity_id AND m.xero_guid = c.xero_contact_id
  AND m.xero_org_id = coalesce(c.xero_org_id, e.xero_org_id);

UPDATE entities e SET
  xero_status = xero_status_of(e.status, e.xero_org_id),
  setup_mode  = CASE WHEN e.xero_org_id IS NOT NULL
                       OR EXISTS (SELECT 1 FROM account_info a WHERE a.entity_id = e.id)
                     THEN 'xero' END::setup_mode;

-- ---- report ---------------------------------------------------------------------------------

SELECT 'accounts' AS what, count(*) AS total,
       count(*) FILTER (WHERE xero_account_link_id IS NOT NULL) AS linked,
       count(*) FILTER (WHERE code IS NULL) AS no_code,
       count(*) FILTER (WHERE account_type IS NULL) AS no_type
FROM account_info
UNION ALL
SELECT 'suppliers', count(*), count(*) FILTER (WHERE xero_contact_link_id IS NOT NULL), NULL, NULL
FROM xero_contact_sync
UNION ALL
SELECT 'xero_account copies', count(*), NULL, NULL, NULL FROM xero_account
UNION ALL
SELECT 'xero_contact copies', count(*), NULL, NULL, NULL FROM xero_contact;

SELECT status::text AS status, xero_status::text AS xero_status, coalesce(setup_mode::text, '-') AS setup_mode,
       count(*) AS companies
FROM entities GROUP BY 1, 2, 3 ORDER BY 1, 2, 3;
