"""The models, the migration state and the schema file agree - the proof minty-db is a mirror."""

import os
import subprocess
import sys
from pathlib import Path

import pytest
from django.apps import apps
from django.core.management import call_command
from django.db import connection
from django.test import override_settings

from tests.conftest import postgres_only

ROOT = Path(__file__).resolve().parents[1]

# information_schema data_type for each model field's Postgres db_type.
_TYPES = {
    "uuid": "uuid",
    "text": "text",
    "timestamp with time zone": "timestamp with time zone",
    "date": "date",
    "boolean": "boolean",
    "integer": "integer",
    "bigint": "bigint",
    "smallint": "smallint",
    "jsonb": "jsonb",
}


def test_the_models_pass_djangos_system_checks():
    """``runserver`` and ``manage.py check`` run these in every API; pytest does not. A field
    named ``class_`` (a keyword plus "_") passed every test and failed every check (fields.E001)."""
    call_command("check", "minty_db", fail_level="ERROR")


@pytest.mark.django_db
def test_migration_state_matches_the_models():
    """makemigrations has nothing to write: 0001's state is exactly models.py."""
    with override_settings(MIGRATION_MODULES={}):
        call_command("makemigrations", "minty_db", check=True, dry_run=True, verbosity=0)


@postgres_only
def test_generated_models_match_the_schema_file():
    env = dict(os.environ, MINTY_TEST_PG_DBNAME=os.environ.get("MINTY_TEST_PG_DBNAME", "minty_test") + "_gen")
    proc = subprocess.run(
        [sys.executable, "scripts/generate_models.py", "--check"], cwd=ROOT, env=env, capture_output=True, text=True
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr


@postgres_only
@pytest.mark.django_db
def test_every_column_has_the_models_type():
    """The audit's rule, for minty_db: same columns, same types, table by table."""
    findings = []
    with connection.cursor() as cur:
        for model in apps.get_app_config("minty_db").get_models():
            table = model._meta.db_table
            cur.execute(
                "SELECT column_name, data_type, udt_name, character_maximum_length, numeric_precision, numeric_scale "
                "FROM information_schema.columns WHERE table_schema = current_schema() AND table_name = %s",
                [table],
            )
            db = {r[0]: r[1:] for r in cur.fetchall()}
            fields = {f.column: f for f in model._meta.concrete_fields}
            for col in sorted(set(db) ^ set(fields)):
                findings.append(f"{table}.{col}: {'missing from the model' if col in db else 'not in the schema'}")
            for col, f in fields.items():
                if col not in db:
                    continue
                data_type, udt, char_len, num_p, num_s = db[col]
                if f.is_relation:
                    want = f.target_field.db_type(connection)
                elif f.generated:  # GeneratedField's own db_type is its output field's internal type
                    want = f.db_parameters(connection)["type"]
                else:
                    want = f.db_type(connection)
                got = {
                    "character varying": f"varchar({char_len})",
                    "character": f"char({char_len})",
                    "USER-DEFINED": udt,
                    "numeric": f"numeric({num_p}, {num_s})" if num_p is not None else "numeric(14, 2)",
                }.get(data_type, _TYPES.get(data_type, data_type))
                if want != got:
                    findings.append(f"{table}.{col}: model {want}, schema {got}")
    assert findings == []


def _run_cli(*args, database_url):
    env = dict(os.environ, DATABASE_URL=database_url)
    env.pop("MINTY_DB_SCHEMA", None)
    return subprocess.run([sys.executable, "-m", "minty_db", *args], cwd=ROOT, env=env, capture_output=True, text=True)


@postgres_only
def test_migrate_builds_a_fresh_database_and_baselines_an_existing_one():
    from minty_db.testing import pg

    base = os.environ.get("MINTY_TEST_PG_DBNAME", "minty_test")
    fresh, existing = f"{base}_fresh", f"{base}_existing"
    server = pg.server_uri()
    try:
        # A fresh database: the schema is built from the file and 0001 recorded.
        pg.drop_database(fresh)
        admin = pg.connect(pg.with_database(server, "postgres"))
        admin.cursor().execute(f'CREATE DATABASE "{fresh}"')
        admin.close()
        url = pg.with_database(server, fresh) + "?schema=pettycashv3"
        out = _run_cli("migrate", database_url=url)
        assert out.returncode == 0, out.stdout + out.stderr
        assert "built schema pettycashv3" in out.stdout
        status = _run_cli("status", database_url=url)
        assert "[X] 0001_initial" in status.stdout

        # An existing schema with no record (every live database today): refused, then baselined.
        built = pg.build(existing)
        url = built.uri + "?schema=pettycashv3"
        refused = _run_cli("migrate", database_url=url)
        assert refused.returncode != 0 and "--baseline" in (refused.stdout + refused.stderr)
        assert _run_cli("migrate", "--baseline", database_url=url).returncode == 0
        again = _run_cli("migrate", database_url=url)
        assert again.returncode == 0, again.stdout + again.stderr
        assert "[X] 0001_initial" in _run_cli("status", database_url=url).stdout
    finally:
        pg.drop_database(fresh)
        pg.drop_database(existing)


XERO_KEYS = ("uq_eax_account", "uq_ebax_entity_xero_account", "uq_xcs_entity_contact")
XERO_PATCH = ROOT / "minty_db" / "schema" / "patches" / "2026-11-03_xero_unique_keys.sql"


def _keys(conn) -> dict[str, str]:
    """The three Xero keys as Postgres defines them: two constraints and one partial index."""
    cur = conn.cursor()
    cur.execute(
        "SELECT conname, pg_get_constraintdef(oid) FROM pg_constraint "
        "WHERE connamespace = 'pettycashv3'::regnamespace AND conname = ANY(%s) "
        "UNION ALL SELECT indexname, indexdef FROM pg_indexes "
        "WHERE schemaname = 'pettycashv3' AND indexname = ANY(%s)",
        (list(XERO_KEYS), list(XERO_KEYS)),
    )
    return dict(cur.fetchall())


@postgres_only
def test_the_xero_keys_patch_merges_duplicates_and_matches_a_fresh_build():
    """2026-11-03: 0001 stays the only migration, so a database already at 0001 gets the three
    Xero unique keys from a hand-run patch. Built at the OLD shape with duplicates, the patch
    keeps one row per key, moves every reference to the row it keeps, adds the keys exactly as
    a fresh build has them, and is a no-op the second time. A dry run changes nothing."""
    import uuid

    from minty_db.testing import pg

    base = os.environ.get("MINTY_TEST_PG_DBNAME", "minty_test")
    old, fresh = f"{base}_patch_old", f"{base}_patch_fresh"
    try:
        built_fresh = pg.build(fresh)
        conn = pg.connect(built_fresh.uri)
        expected = _keys(conn)
        conn.close()
        assert set(expected) == set(XERO_KEYS)

        built = pg.build(old)
        conn = pg.connect(built.uri)
        conn.autocommit = True
        cur = conn.cursor()
        cur.execute("SET search_path = pettycashv3")
        cur.execute("ALTER TABLE entity_account_xero DROP CONSTRAINT uq_eax_account")
        cur.execute("DROP INDEX uq_ebax_entity_xero_account")
        cur.execute("ALTER TABLE xero_contact_sync DROP CONSTRAINT uq_xcs_entity_contact")
        entity, account = uuid.uuid4(), uuid.uuid4()
        keep_c, dup_c = uuid.uuid4(), uuid.uuid4()
        cur.execute("INSERT INTO entities (id, name) VALUES (%s, 'Dup Co')", (entity,))
        cur.execute(
            "INSERT INTO account_info (id, entity_id, type, name, xero_account_id, xero_code) "
            "VALUES (%s, %s, 'EXPENSE', 'Advertising', 'a-1', '400')",
            (account, entity),
        )
        cur.execute(
            "INSERT INTO entity_account_xero (account_id, is_active) VALUES (%s, false), (%s, true)", (account, account)
        )
        cur.execute(
            "INSERT INTO entity_bill_account_xero (entity_id, account_code, xero_account_id, is_active) "
            "VALUES (%s, '400', 'a-1', false), (%s, '400', 'a-1', true), "
            "(%s, '999', '', true), (%s, '998', '', true)",
            (entity, entity, entity, entity),
        )
        cur.execute(
            "INSERT INTO xero_contact_sync (id, entity_id, xero_contact_id, name, created_at) "
            "VALUES (%s, %s, 'c-1', 'Kept', now() - interval '1 day'), (%s, %s, 'c-1', 'Dup', now())",
            (keep_c, entity, dup_c, entity),
        )
        cur.execute(
            "INSERT INTO entity_pettycash_settings (entity_id, director_contact_id) VALUES (%s, %s)", (entity, dup_c)
        )
        cur.execute("INSERT INTO bill (entity_id, contact_id) VALUES (%s, %s)", (entity, dup_c))
        url = built.uri + "?schema=pettycashv3"
        assert _run_cli("migrate", "--baseline", database_url=url).returncode == 0

        def counts():
            cur.execute("SELECT count(*) FROM entity_account_xero WHERE account_id = %s", (account,))
            eax = cur.fetchone()[0]
            cur.execute("SELECT count(*) FROM entity_bill_account_xero WHERE entity_id = %s", (entity,))
            bills = cur.fetchone()[0]
            cur.execute("SELECT count(*) FROM xero_contact_sync WHERE entity_id = %s", (entity,))
            return eax, bills, cur.fetchone()[0]

        dry = _run_cli("patch", str(XERO_PATCH), database_url=url)
        assert dry.returncode == 0, dry.stdout + dry.stderr
        assert "DRY RUN" in dry.stdout and "xero_contact_sync duplicates  1" in dry.stdout, dry.stdout
        assert counts() == (2, 4, 2)
        assert _keys(conn) == {}

        done = _run_cli("patch", str(XERO_PATCH), "--apply", database_url=url)
        assert done.returncode == 0, done.stdout + done.stderr
        assert "APPLIED" in done.stdout
        assert counts() == (1, 3, 1), "hand-added codes (no Xero id, '') are left alone"
        cur.execute("SELECT is_active FROM entity_account_xero WHERE account_id = %s", (account,))
        assert cur.fetchone()[0] is True, "the ticked row is the one kept"
        cur.execute("SELECT is_active FROM entity_bill_account_xero WHERE xero_account_id = 'a-1'")
        assert cur.fetchone()[0] is True
        cur.execute("SELECT director_contact_id FROM entity_pettycash_settings WHERE entity_id = %s", (entity,))
        assert cur.fetchone()[0] == keep_c
        cur.execute("SELECT contact_id FROM bill WHERE entity_id = %s", (entity,))
        assert cur.fetchone()[0] == keep_c
        assert _keys(conn) == expected

        again = _run_cli("patch", str(XERO_PATCH), "--apply", database_url=url)
        assert again.returncode == 0, again.stdout + again.stderr
        assert counts() == (1, 3, 1) and _keys(conn) == expected
        assert "[X] 0001_initial" in _run_cli("status", database_url=url).stdout
        conn.close()
    finally:
        pg.drop_database(old)
        pg.drop_database(fresh)


def test_the_database_url_is_decoded_and_keeps_its_sslmode():
    """A Supabase URI percent-encodes its password and sets sslmode; the command must pass both on."""
    from minty_db.__main__ import _settings_for

    name, db = _settings_for(
        "postgresql://postgres:p%40ss%2Fw0rd@db.example.co:5432/postgres?schema=development&sslmode=require"
    )
    assert name == "development"
    assert (db["USER"], db["PASSWORD"], db["HOST"], db["PORT"], db["NAME"]) == (
        "postgres",
        "p@ss/w0rd",
        "db.example.co",
        5432,
        "postgres",
    )
    assert db["OPTIONS"] == {"options": "-c search_path=development", "sslmode": "require"}


SLIP_PATCH = ROOT / "minty_db" / "schema" / "patches" / "2026-11-03_slip_placeholders.sql"


@postgres_only
def test_the_slip_placeholder_patch_folds_each_holder_into_the_one_real_payment():
    """2026-11-03 (owner): minty-payment-request-web's bank slip upload made a 0.00 payment with no
    date to hold a slip when the bill had no payment yet. On a bill (not voided) with exactly one
    real payment the slips move onto it and the holder goes; with several, onto the next one recorded
    after the holder (owner, 2026-11-03); all before it, none, or a voided bill: left. A dry run
    changes nothing; a second run has nothing to do. Minty's load does the same
    (docs/schema/generators/gen.py SLIP_PLACEHOLDERS)."""
    import uuid

    from minty_db.testing import pg

    base = os.environ.get("MINTY_TEST_PG_DBNAME", "minty_test")
    name = f"{base}_patch_slips"
    try:
        built = pg.build(name)
        conn = pg.connect(built.uri)
        conn.autocommit = True
        cur = conn.cursor()
        cur.execute("SET search_path = pettycashv3")
        entity = uuid.uuid4()

        def bill(status="paid"):
            b = uuid.uuid4()
            cur.execute(
                "INSERT INTO bill (id, entity_id, status, amount) VALUES (%s, %s, %s, 500)", (b, entity, status)
            )
            return b

        def payment(b, amount, date="2026-09-01", at="2026-09-01 10:00"):
            p = uuid.uuid4()
            cur.execute(
                "INSERT INTO payment (id, bill_id, amount, payment_date, created_at) VALUES (%s, %s, %s, %s, %s)",
                (p, b, amount, date, at),
            )
            return p

        def slip(p):
            a = uuid.uuid4()
            cur.execute(
                "INSERT INTO attachment (id, original_name, stored_name, file_path, mime_type) "
                "VALUES (%s, 'slip.pdf', 'slip.pdf', %s, 'application/pdf')",
                (a, f"payments/{a}"),
            )
            cur.execute(
                "INSERT INTO payment_attachment (payment_id, attachment_id, attachment_role) "
                "VALUES (%s, %s, 'bank_slip')",
                (p, a),
            )
            return a

        clear = bill()
        real = payment(clear, 500)
        holder = payment(clear, 0, None)
        moved = slip(holder)
        several = bill()
        payment(several, 100, at="2026-09-01 09:00")
        next_slip = slip(payment(several, 0, None, at="2026-09-01 10:00"))
        next_one = payment(several, 200, at="2026-09-01 10:01")
        payment(several, 200, at="2026-09-01 10:02")
        late = bill()
        payment(late, 200, at="2026-09-01 09:00")
        payment(late, 300, at="2026-09-01 09:30")
        slip(payment(late, 0, None, at="2026-09-01 10:00"))
        alone = bill("submitted")
        slip(payment(alone, 0, None))
        void = bill("void")
        payment(void, 500)
        slip(payment(void, 0, None))
        url = built.uri + "?schema=pettycashv3"
        assert _run_cli("migrate", "--baseline", database_url=url).returncode == 0

        def state():
            cur.execute("SELECT count(*) FROM payment WHERE amount = 0")
            holders = cur.fetchone()[0]
            cur.execute("SELECT payment_id FROM payment_attachment WHERE attachment_id = %s", (moved,))
            return holders, cur.fetchone()[0]

        dry = _run_cli("patch", str(SLIP_PATCH), database_url=url)
        assert dry.returncode == 0, dry.stdout + dry.stderr
        assert "DRY RUN" in dry.stdout and "slip holders to fold  2" in dry.stdout, dry.stdout
        assert state() == (5, holder)

        done = _run_cli("patch", str(SLIP_PATCH), "--apply", database_url=url)
        assert done.returncode == 0, done.stdout + done.stderr
        assert "APPLIED" in done.stdout
        assert state() == (3, real)
        cur.execute("SELECT payment_id FROM payment_attachment WHERE attachment_id = %s", (next_slip,))
        assert cur.fetchone()[0] == next_one, "with several real payments, the next one after the holder"
        cur.execute("SELECT count(*) FROM attachment WHERE id = %s", (moved,))
        assert cur.fetchone()[0] == 1, "the file's row stays; only its link moved"

        again = _run_cli("patch", str(SLIP_PATCH), "--apply", database_url=url)
        assert again.returncode == 0, again.stdout + again.stderr
        assert "slip holders to fold  0" in again.stdout and state() == (3, real)
        conn.close()
    finally:
        pg.drop_database(name)


EXPAND_PATCH = ROOT / "minty_db" / "schema" / "patches" / "2026-11-10_xero_optional_expand.sql"
EXPAND_DOWN = ROOT / "minty_db" / "schema" / "patches" / "2026-11-10_xero_optional_expand_down.sql"


def _catalogue(conn) -> dict[str, set]:
    """Everything a schema change can touch: columns (with defaults and generation rules),
    constraints, indexes, triggers, functions and enum types."""
    cur = conn.cursor()

    def rows(sql):
        cur.execute(sql)
        return set(cur.fetchall())

    return {
        "columns": rows(
            "SELECT table_name, column_name, data_type, udt_name, is_nullable, column_default, generation_expression "
            "FROM information_schema.columns WHERE table_schema = 'pettycashv3'"
        ),
        "constraints": rows(
            "SELECT c.relname, k.conname, pg_get_constraintdef(k.oid), k.condeferrable FROM pg_constraint k "
            "LEFT JOIN pg_class c ON c.oid = k.conrelid WHERE k.connamespace = 'pettycashv3'::regnamespace"
        ),
        "indexes": rows("SELECT tablename, indexname, indexdef FROM pg_indexes WHERE schemaname = 'pettycashv3'"),
        "triggers": rows(
            "SELECT c.relname, t.tgname, pg_get_triggerdef(t.oid) FROM pg_trigger t "
            "JOIN pg_class c ON c.oid = t.tgrelid "
            "WHERE NOT t.tgisinternal AND c.relnamespace = 'pettycashv3'::regnamespace"
        ),
        "functions": rows(
            "SELECT proname, pg_get_function_identity_arguments(oid), md5(pg_get_functiondef(oid)) FROM pg_proc "
            "WHERE pronamespace = 'pettycashv3'::regnamespace"
        ),
        "types": rows(
            "SELECT t.typname, string_agg(e.enumlabel, ',' ORDER BY e.enumsortorder) FROM pg_type t "
            "JOIN pg_enum e ON e.enumtypid = t.oid WHERE t.typnamespace = 'pettycashv3'::regnamespace GROUP BY 1"
        ),
    }


@postgres_only
def test_the_xero_optional_patch_matches_a_fresh_build_and_its_down_patch_reverses_it():
    """2026-11-10: the Xero-optional EXPAND is in the baseline (01's last section) and in a patch
    for databases already at 0001. Down then up again lands exactly on a fresh build; the patch
    fills the new columns from today's Xero-shaped rows; a second run stops at its guard; a dry
    run changes nothing."""
    import uuid

    from minty_db.testing import pg

    base = os.environ.get("MINTY_TEST_PG_DBNAME", "minty_test")
    name = f"{base}_expand"
    try:
        built = pg.build(name)
        conn = pg.connect(built.uri)
        conn.autocommit = True
        cur = conn.cursor()
        cur.execute("SET search_path = pettycashv3")  # the catalogue prints names relative to it
        url = built.uri + "?schema=pettycashv3"
        assert _run_cli("migrate", "--baseline", database_url=url).returncode == 0
        expected = _catalogue(conn)

        down = _run_cli("patch", str(EXPAND_DOWN), "--apply", database_url=url)
        assert down.returncode == 0, down.stdout + down.stderr
        cur.execute("SELECT to_regclass('xero_account'), to_regtype('account_type')")
        assert cur.fetchone() == (None, None)

        entity, account, contact = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
        cur.execute(
            "INSERT INTO entities (id, name, status, xero_org_id) VALUES (%s, 'Co', 'connected', 'ORG')", (entity,)
        )
        cur.execute(
            "INSERT INTO account_info (id, entity_id, type, name, xero_account_id, xero_code, status) "
            "VALUES (%s, %s, 'EXPENSE', 'Rent', 'g-1', '469', 'ACTIVE')",
            (account, entity),
        )
        cur.execute(
            "INSERT INTO xero_contact_sync (id, entity_id, xero_contact_id, xero_org_id, name) "
            "VALUES (%s, %s, 'c-1', 'ORG', 'Acme')",
            (contact, entity),
        )

        dry = _run_cli("patch", str(EXPAND_PATCH), database_url=url)
        assert dry.returncode == 0 and "DRY RUN" in dry.stdout, dry.stdout + dry.stderr
        cur.execute("SELECT to_regclass('xero_account')")
        assert cur.fetchone()[0] is None

        done = _run_cli("patch", str(EXPAND_PATCH), "--apply", database_url=url)
        assert done.returncode == 0 and "APPLIED" in done.stdout, done.stdout + done.stderr
        got = _catalogue(conn)
        diff = {k: (expected[k] - got[k], got[k] - expected[k]) for k in expected if expected[k] != got[k]}
        assert not diff, diff  # (only in a fresh build, only after the patch)
        cur.execute(
            "SELECT code, account_type::text, class::text, record_status::text, xero_account_link_id IS NOT NULL "
            "FROM account_info WHERE id = %s",
            (account,),
        )
        assert cur.fetchone() == ("469", "expense", "expense", "active", True)
        cur.execute("SELECT xero_contact_link_id IS NOT NULL FROM xero_contact_sync WHERE id = %s", (contact,))
        assert cur.fetchone()[0] is True
        cur.execute("SELECT xero_status::text, setup_mode::text FROM entities WHERE id = %s", (entity,))
        assert cur.fetchone() == ("connected", "xero")

        # A Minty-only company that goes live is written status 'disconnected' until CONTRACT
        # (the only live value there is); it was never connected, and xero_status says so.
        minty = uuid.uuid4()
        cur.execute("INSERT INTO entities (id, name, setup_mode) VALUES (%s, 'Own books', 'minty')", (minty,))
        cur.execute("UPDATE entities SET status = 'disconnected' WHERE id = %s", (minty,))
        cur.execute("SELECT xero_status::text FROM entities WHERE id = %s", (minty,))
        assert cur.fetchone()[0] == "not_connected"
        cur.execute("UPDATE entities SET status = 'disconnected', xero_org_id = NULL WHERE id = %s", (entity,))
        cur.execute("SELECT xero_status::text FROM entities WHERE id = %s", (entity,))
        assert cur.fetchone()[0] == "disconnected", "a Xero company that disconnects stays disconnected"

        again = _run_cli("patch", str(EXPAND_PATCH), "--apply", database_url=url)
        assert again.returncode != 0 and "already in this schema" in again.stdout + again.stderr
        conn.close()
    finally:
        pg.drop_database(name)


ARCHIVED_PATCH = ROOT / "minty_db" / "schema" / "patches" / "2026-11-10_entity_archived.sql"
ARCHIVED_DOWN = ROOT / "minty_db" / "schema" / "patches" / "2026-11-10_entity_archived_down.sql"


@postgres_only
def test_the_archived_patch_matches_a_fresh_build_and_its_down_patch_reverses_it():
    """2026-11-10: entities.archived_at / archived_by_user_id are in the baseline (entities' CREATE
    TABLE) and in a patch for databases at 0001. Down then up lands on a fresh build; the user FK
    sets NULL when the archiver is deleted; a second run stops at its guard."""
    import uuid

    from minty_db.testing import pg

    base = os.environ.get("MINTY_TEST_PG_DBNAME", "minty_test")
    name = f"{base}_archived"
    try:
        built = pg.build(name)
        conn = pg.connect(built.uri)
        conn.autocommit = True
        cur = conn.cursor()
        cur.execute("SET search_path = pettycashv3")
        url = built.uri + "?schema=pettycashv3"
        assert _run_cli("migrate", "--baseline", database_url=url).returncode == 0
        expected = _catalogue(conn)

        down = _run_cli("patch", str(ARCHIVED_DOWN), "--apply", database_url=url)
        assert down.returncode == 0, down.stdout + down.stderr
        cur.execute(
            "SELECT count(*) FROM information_schema.columns WHERE table_name = 'entities' "
            "AND column_name LIKE 'archived%%'"
        )
        assert cur.fetchone()[0] == 0
        done = _run_cli("patch", str(ARCHIVED_PATCH), "--apply", database_url=url)
        assert done.returncode == 0 and "APPLIED" in done.stdout, done.stdout + done.stderr
        # the release order: the tracker view (which reads archived_at) comes back after it
        tracker = _run_cli("patch", str(TRACKER_PATCH), "--apply", database_url=url)
        assert tracker.returncode == 0, tracker.stdout + tracker.stderr
        got = _catalogue(conn)
        diff = {k: (expected[k] - got[k], got[k] - expected[k]) for k in expected if expected[k] != got[k]}
        assert not diff, diff

        user, entity = uuid.uuid4(), uuid.uuid4()
        cur.execute(
            "INSERT INTO \"user\" (id, username, email, password) VALUES (%s, 'arch', 'arch@x.test', '!')", (user,)
        )
        cur.execute(
            "INSERT INTO entities (id, name, archived_at, archived_by_user_id) VALUES (%s, 'Gone', now(), %s)",
            (entity, user),
        )
        cur.execute('DELETE FROM "user" WHERE id = %s', (user,))
        cur.execute("SELECT archived_at IS NOT NULL, archived_by_user_id FROM entities WHERE id = %s", (entity,))
        assert cur.fetchone() == (True, None)

        again = _run_cli("patch", str(ARCHIVED_PATCH), "--apply", database_url=url)
        assert again.returncode != 0 and "already in this schema" in again.stdout + again.stderr
        conn.close()
    finally:
        pg.drop_database(name)


MATCHING_PATCH = ROOT / "minty_db" / "schema" / "patches" / "2026-11-12_xero_matching.sql"
MATCHING_DOWN = ROOT / "minty_db" / "schema" / "patches" / "2026-11-12_xero_matching_down.sql"


@postgres_only
def test_the_xero_matching_patch_matches_a_fresh_build_backfills_and_its_down_patch_reverses_it():
    """2026-11-12 (Phase 3, owner-approved): entities.xero_matching is in the baseline and in a patch
    for databases at 0001. Down then up lands on a fresh build; the patch marks a company Phase 2 has
    mid-matching 'first' and leaves the rest NULL; the type refuses any other value; a second run
    stops at its guard."""
    import uuid

    from minty_db.testing import pg

    base = os.environ.get("MINTY_TEST_PG_DBNAME", "minty_test")
    name = f"{base}_matching"
    try:
        built = pg.build(name)
        conn = pg.connect(built.uri)
        conn.autocommit = True
        cur = conn.cursor()
        cur.execute("SET search_path = pettycashv3")
        url = built.uri + "?schema=pettycashv3"
        assert _run_cli("migrate", "--baseline", database_url=url).returncode == 0
        expected = _catalogue(conn)

        down = _run_cli("patch", str(MATCHING_DOWN), "--apply", database_url=url)
        assert down.returncode == 0, down.stdout + down.stderr
        mid, plain = uuid.uuid4(), uuid.uuid4()
        cur.execute(
            "INSERT INTO entities (id, name, setup_mode, xero_org_id) VALUES (%s, 'Mid', 'minty', 'org-1'), "
            "(%s, 'Plain', 'minty', NULL)",
            (mid, plain),
        )
        for entity in (mid, plain):
            cur.execute(
                "INSERT INTO account_info (id, entity_id, name, code, type, account_type, source) "
                "VALUES (%s, %s, 'Rent', '469', 'EXPENSE', 'expense', 'user')",
                (uuid.uuid4(), entity),
            )
        done = _run_cli("patch", str(MATCHING_PATCH), "--apply", database_url=url)
        assert done.returncode == 0 and "APPLIED" in done.stdout, done.stdout + done.stderr
        got = _catalogue(conn)
        diff = {k: (expected[k] - got[k], got[k] - expected[k]) for k in expected if expected[k] != got[k]}
        assert not diff, diff

        cur.execute("SELECT id, xero_matching FROM entities WHERE id IN (%s, %s)", (mid, plain))
        assert dict(cur.fetchall()) == {mid: "first", plain: None}
        with pytest.raises(Exception, match="invalid input value"):
            cur.execute("UPDATE entities SET xero_matching = 'later' WHERE id = %s", (mid,))

        again = _run_cli("patch", str(MATCHING_PATCH), "--apply", database_url=url)
        assert again.returncode != 0 and "already in this schema" in again.stdout + again.stderr
        conn.close()
    finally:
        pg.drop_database(name)


DESCRIPTION_PATCH = ROOT / "minty_db" / "schema" / "patches" / "2026-11-13_expense_description.sql"
DESCRIPTION_DOWN = ROOT / "minty_db" / "schema" / "patches" / "2026-11-13_expense_description_down.sql"


@postgres_only
def test_the_expense_description_patch_keeps_every_remark_and_matches_a_fresh_build():
    """2026-11-13 (owner): report_expense.remarks becomes report_expense.description, the empty
    `description` beside it dropped. Down then up lands on a fresh build and keeps each line's text;
    a line holding a `description` stops the patch with nothing changed; a second run stops at its
    guard."""
    import uuid

    from minty_db.testing import pg

    base = os.environ.get("MINTY_TEST_PG_DBNAME", "minty_test")
    name = f"{base}_expdesc"
    try:
        built = pg.build(name)
        conn = pg.connect(built.uri)
        conn.autocommit = True
        cur = conn.cursor()
        cur.execute("SET search_path = pettycashv3")
        url = built.uri + "?schema=pettycashv3"
        assert _run_cli("migrate", "--baseline", database_url=url).returncode == 0
        expected = _catalogue(conn)
        cur.execute(
            "SELECT character_maximum_length FROM information_schema.columns "
            "WHERE table_schema = 'pettycashv3' AND table_name = 'report_expense' AND column_name = 'description'"
        )
        assert cur.fetchone() == (300,)

        down = _run_cli("patch", str(DESCRIPTION_DOWN), "--apply", database_url=url)
        assert down.returncode == 0, down.stdout + down.stderr
        entity, report, line, blank = uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
        cur.execute("INSERT INTO entities (id, name) VALUES (%s, 'Shop')", (entity,))
        cur.execute(
            "INSERT INTO report (id, entity_id, transaction_date) VALUES (%s, %s, '2026-11-13')", (report, entity)
        )
        cur.execute(
            "INSERT INTO report_expense (id, report_id, item, remarks) VALUES (%s, %s, 'COGS', 'Lemon bottle'), "
            "(%s, %s, 'COGS', NULL)",
            (line, report, blank, report),
        )

        cur.execute("UPDATE report_expense SET description = 'x' WHERE id = %s", (blank,))
        held = _run_cli("patch", str(DESCRIPTION_PATCH), "--apply", database_url=url)
        assert held.returncode != 0 and "holds values" in held.stdout + held.stderr
        cur.execute("SELECT remarks, description FROM report_expense WHERE id = %s", (blank,))
        assert cur.fetchone() == (None, "x")
        cur.execute("UPDATE report_expense SET description = NULL WHERE id = %s", (blank,))

        done = _run_cli("patch", str(DESCRIPTION_PATCH), "--apply", database_url=url)
        assert done.returncode == 0 and "APPLIED" in done.stdout, done.stdout + done.stderr
        got = _catalogue(conn)
        diff = {k: (expected[k] - got[k], got[k] - expected[k]) for k in expected if expected[k] != got[k]}
        assert not diff, diff
        cur.execute("SELECT id, description FROM report_expense WHERE report_id = %s", (report,))
        assert dict(cur.fetchall()) == {line: "Lemon bottle", blank: None}

        again = _run_cli("patch", str(DESCRIPTION_PATCH), "--apply", database_url=url)
        assert again.returncode != 0 and "already renamed" in again.stdout + again.stderr
        conn.close()
    finally:
        pg.drop_database(name)


OWNER_PATCH = ROOT / "minty_db" / "schema" / "patches" / "2026-11-12_sale_info_owner.sql"
OWNER_DOWN = ROOT / "minty_db" / "schema" / "patches" / "2026-11-12_sale_info_owner_down.sql"


@postgres_only
def test_the_sale_info_owner_patch_matches_a_fresh_build_backfills_and_its_down_patch_reverses_it():
    """2026-11-12 (owner-approved): sale_info.entity_id is in the baseline and in a patch for
    databases at 0001. Down then up lands on a fresh build, and the backfill does what the owner
    chose: Minty's names stay Minty's, a case twin merges into Minty's row (a company holding both
    keeps one link; a report holding both keeps one row, summed), an unused row goes, a typed name
    becomes its company's own and a shared one is split per company. The name rules hold; a second
    run stops at its guard; the down-patch folds the copies back and restores UNIQUE (sale_name)."""
    import uuid

    from minty_db.testing import pg

    base = os.environ.get("MINTY_TEST_PG_DBNAME", "minty_test")
    name = f"{base}_sale_owner"
    try:
        built = pg.build(name)
        conn = pg.connect(built.uri)
        conn.autocommit = True
        cur = conn.cursor()
        cur.execute("SET search_path = pettycashv3")
        url = built.uri + "?schema=pettycashv3"
        assert _run_cli("migrate", "--baseline", database_url=url).returncode == 0
        expected = _catalogue(conn)

        down = _run_cli("patch", str(OWNER_DOWN), "--apply", database_url=url)
        assert down.returncode == 0, down.stdout + down.stderr
        cur.execute("DELETE FROM sale_info")
        a, b = uuid.uuid4(), uuid.uuid4()
        cur.execute("INSERT INTO entities (id, name) VALUES (%s, 'A'), (%s, 'B')", (a, b))
        ids = {}
        for sale_name, kind in [
            ("Mastercard", "electronic"),
            ("MasterCard", "electronic"),
            ("Payme", "electronic"),
            ("PayMe", "electronic"),
            ("Kpay", "electronic"),
            ("Lone", "delivery"),
            ("Gcash", "delivery"),
            ("1", "electronic"),
            ("Cash", "other"),
        ]:
            ids[sale_name] = uuid.uuid4()
            cur.execute(
                "INSERT INTO sale_info (id, sale_name, type) VALUES (%s, %s, %s)", (ids[sale_name], sale_name, kind)
            )
        links = [
            (a, "Mastercard", True, 1),
            (a, "MasterCard", False, 2),
            (b, "MasterCard", True, 3),
            (a, "Payme", False, 4),
            (a, "PayMe", True, 5),
            (a, "Kpay", True, 6),
            (b, "Kpay", True, 7),
            (b, "Lone", True, 8),
            (b, "Gcash", True, 9),
            (a, "Cash", True, 0),
        ]
        for entity, sale_name, on, order in links:
            cur.execute(
                "INSERT INTO entity_sale_setting (entity_id, sale_id, is_active, display_order) "
                "VALUES (%s, %s, %s, %s)",
                (entity, ids[sale_name], on, order),
            )
        ra, ra2, rb = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
        cur.execute(
            "INSERT INTO report (id, entity_id, transaction_date) VALUES "
            "(%s, %s, '2026-11-01'), (%s, %s, '2026-11-02'), (%s, %s, '2026-11-01')",
            (ra, a, ra2, a, rb, b),
        )
        for report, sale_name, amount in [
            (ra, "Mastercard", 10),
            (ra, "MasterCard", 5),
            (rb, "MasterCard", 7),
            (ra, "Kpay", 1),
            (ra2, "Kpay", 2),
            (rb, "Kpay", 3),
        ]:
            cur.execute(
                "INSERT INTO report_sale (report_id, sale_id, amount) VALUES (%s, %s, %s)",
                (report, ids[sale_name], amount),
            )

        done = _run_cli("patch", str(OWNER_PATCH), "--apply", database_url=url)
        assert done.returncode == 0 and "APPLIED" in done.stdout, done.stdout + done.stderr
        got = _catalogue(conn)
        diff = {k: (expected[k] - got[k], got[k] - expected[k]) for k in expected if expected[k] != got[k]}
        assert not diff, diff

        cur.execute("SELECT type FROM sale_info WHERE sale_name = 'Gcash'")
        assert cur.fetchone()[0] == "electronic"  # GCash is a card and wallet payment (owner, 2026-11-12)
        cur.execute("DELETE FROM entity_sale_setting WHERE sale_id = %s", (ids["Gcash"],))
        cur.execute("DELETE FROM sale_info WHERE sale_name = 'Gcash'")
        cur.execute("SELECT sale_name, entity_id FROM sale_info ORDER BY sale_name, entity_id")
        assert sorted(cur.fetchall(), key=str) == sorted(
            [("Cash", None), ("Mastercard", None), ("Payme", None), ("Kpay", a), ("Kpay", b), ("Lone", b)], key=str
        )
        cur.execute(
            "SELECT e.entity_id, s.sale_name, s.entity_id, e.is_active, e.display_order "
            "FROM entity_sale_setting e JOIN sale_info s ON s.id = e.sale_id"
        )
        got_links = {(row[0], row[1]): row[2:] for row in cur.fetchall()}
        assert got_links[(a, "Mastercard")] == (None, True, 1)  # both twins: one link, on, first place
        assert got_links[(b, "Mastercard")] == (None, True, 3)
        assert got_links[(a, "Payme")] == (None, True, 4)
        assert got_links[(a, "Kpay")][0] == a and got_links[(b, "Kpay")][0] == b  # split per company
        assert got_links[(b, "Lone")][0] == b
        cur.execute(
            "SELECT report_id, s.sale_name, s.entity_id, amount FROM report_sale r JOIN sale_info s ON s.id = r.sale_id"
        )
        got_sales = {(row[0], row[1]): (row[2], row[3]) for row in cur.fetchall()}
        assert got_sales == {
            (ra, "Mastercard"): (None, 15),
            (rb, "Mastercard"): (None, 7),  # summed on the report holding both
            (ra, "Kpay"): (a, 1),
            (ra2, "Kpay"): (a, 2),
            (rb, "Kpay"): (b, 3),
        }

        with pytest.raises(Exception, match="uq_sale_info_minty_name"):
            cur.execute("INSERT INTO sale_info (sale_name, type) VALUES ('VISA', 'electronic'), ('visa', 'electronic')")
        with pytest.raises(Exception, match="uq_sale_info_entity_name"):
            cur.execute("INSERT INTO sale_info (sale_name, type, entity_id) VALUES ('KPAY', 'electronic', %s)", (a,))
        cur.execute("INSERT INTO sale_info (sale_name, type, entity_id) VALUES ('Lone', 'delivery', %s)", (a,))

        again = _run_cli("patch", str(OWNER_PATCH), "--apply", database_url=url)
        assert again.returncode != 0 and "already in this schema" in again.stdout + again.stderr

        back = _run_cli("patch", str(OWNER_DOWN), "--apply", database_url=url)
        assert back.returncode == 0, back.stdout + back.stderr
        cur.execute("SELECT sale_name, count(*) FROM sale_info GROUP BY sale_name ORDER BY sale_name")
        assert all(n == 1 for _name, n in cur.fetchall())
        cur.execute(
            "SELECT count(*) FROM report_sale r JOIN sale_info s ON s.id = r.sale_id WHERE s.sale_name = 'Kpay'"
        )
        assert cur.fetchone()[0] == 3
        conn.close()
    finally:
        pg.drop_database(name)


TRACKER_PATCH = ROOT / "minty_db" / "schema" / "patches" / "2026-11-10_tracker_view.sql"
TRACKER_DOWN = ROOT / "minty_db" / "schema" / "patches" / "2026-11-10_tracker_view_down.sql"
TRACKER_COLUMNS = [
    "entities.name",
    "entity_id",
    "pettycash",
    "billing",
    "pc_latest_submitted",
    "pc_latest_submitted_date",
    "pc_latest_published",
    "num_paid",
    "num_partialpaid",
    "num_unpaid",
    "num_published",
    "latest_bill_published",
    "latest_bill_update",
    "country_code",
    "timezone",
]


@postgres_only
def test_the_tracker_view_patch_matches_a_fresh_build_and_leaves_archived_companies_out():
    """2026-11-10 (owner): the client tracker's fifteen-column view, folded in from tracker-backend,
    with no archived company. Down (twelve columns) then up lands on a fresh build; the counts and
    module flags are never NULL; the patch refuses to run before the archived columns exist."""
    import uuid

    from minty_db.testing import pg

    base = os.environ.get("MINTY_TEST_PG_DBNAME", "minty_test")
    name = f"{base}_tracker"
    try:
        built = pg.build(name)
        conn = pg.connect(built.uri)
        conn.autocommit = True
        cur = conn.cursor()
        cur.execute("SET search_path = pettycashv3")
        url = built.uri + "?schema=pettycashv3"
        assert _run_cli("migrate", "--baseline", database_url=url).returncode == 0
        expected = _catalogue(conn)

        def columns():
            cur.execute(
                "SELECT column_name FROM information_schema.columns WHERE table_name = 'tracker' "
                "AND table_schema = 'pettycashv3' ORDER BY ordinal_position"
            )
            return [r[0] for r in cur.fetchall()]

        assert columns() == TRACKER_COLUMNS
        assert _run_cli("patch", str(TRACKER_DOWN), "--apply", database_url=url).returncode == 0
        assert len(columns()) == 12
        done = _run_cli("patch", str(TRACKER_PATCH), "--apply", database_url=url)
        assert done.returncode == 0, done.stdout + done.stderr
        assert columns() == TRACKER_COLUMNS
        got = _catalogue(conn)
        diff = {k: (expected[k] - got[k], got[k] - expected[k]) for k in expected if expected[k] != got[k]}
        assert not diff, diff

        live, gone = uuid.uuid4(), uuid.uuid4()
        cur.execute(
            "INSERT INTO entities (id, name, timezone) VALUES (%s, 'Live', 'Asia/Hong_Kong'), (%s, 'Gone', NULL)",
            (live, gone),
        )
        cur.execute("UPDATE entities SET archived_at = now() WHERE id = %s", (gone,))
        cur.execute("SELECT entity_id, pettycash, billing, num_paid, num_unpaid, timezone FROM tracker")
        assert cur.fetchall() == [(live, False, False, 0, 0, "Asia/Hong_Kong")]

        again = _run_cli("patch", str(TRACKER_PATCH), "--apply", database_url=url)
        assert again.returncode == 0, "re-runnable"
        conn.close()
    finally:
        pg.drop_database(name)


BILL_CONTACTS_PATCH = ROOT / "minty_db" / "schema" / "patches" / "2026-11-10_bill_contact_ids.sql"


@postgres_only
def test_the_bill_contact_repair_links_each_bill_to_a_supplier_row():
    """2026-11-10 (owner): the load put Xero's ContactID in bill.contact_id. The repair links each such
    bill by the Xero id, else by the supplier's name, else to a restored supplier row; a bill already
    right is untouched; a dry run changes nothing; a second run finds nothing."""
    import uuid

    from minty_db.testing import pg

    base = os.environ.get("MINTY_TEST_PG_DBNAME", "minty_test")
    name = f"{base}_billcontacts"
    try:
        built = pg.build(name)
        conn = pg.connect(built.uri)
        conn.autocommit = True
        cur = conn.cursor()
        cur.execute("SET search_path = pettycashv3")
        url = built.uri + "?schema=pettycashv3"
        assert _run_cli("migrate", "--baseline", database_url=url).returncode == 0

        entity = uuid.uuid4()
        cur.execute("INSERT INTO entities (id, name, xero_org_id) VALUES (%s, 'Co', 'ORG')", (entity,))
        by_id_guid, by_name_guid, lost_guid = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
        cur.execute(
            "INSERT INTO xero_contact_sync (entity_id, xero_contact_id, name) VALUES (%s, %s, 'Acme') RETURNING id",
            (entity, str(by_id_guid)),
        )
        acme = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO xero_contact_sync (entity_id, xero_contact_id, name) VALUES (%s, 'other', "
            "'Named Co') RETURNING id",
            (entity,),
        )
        named = cur.fetchone()[0]

        def bill(contact_id, contact_name):
            cur.execute(
                "INSERT INTO bill (entity_id, contact_id, contact_name) VALUES (%s, %s, %s) RETURNING id",
                (entity, contact_id, contact_name),
            )
            return cur.fetchone()[0]

        b_id, b_name, b_lost, b_ok = (
            bill(by_id_guid, "Acme"),
            bill(by_name_guid, " named co "),
            bill(lost_guid, "Lost Ltd"),
            bill(acme, "Acme"),
        )

        def contact(b):
            cur.execute("SELECT contact_id FROM bill WHERE id = %s", (b,))
            return cur.fetchone()[0]

        dry = _run_cli("patch", str(BILL_CONTACTS_PATCH), database_url=url)
        assert dry.returncode == 0 and "DRY RUN" in dry.stdout, dry.stdout + dry.stderr
        assert contact(b_id) == by_id_guid

        done = _run_cli("patch", str(BILL_CONTACTS_PATCH), "--apply", database_url=url)
        assert done.returncode == 0 and "APPLIED" in done.stdout, done.stdout + done.stderr
        assert (contact(b_id), contact(b_name), contact(b_ok)) == (acme, named, acme)
        cur.execute("SELECT name, xero_contact_id FROM xero_contact_sync WHERE id = %s", (contact(b_lost),))
        assert cur.fetchone() == ("Lost Ltd", str(lost_guid)), "a supplier row restored for it"
        cur.execute(
            "SELECT count(*) FROM bill b WHERE NOT EXISTS (SELECT 1 FROM xero_contact_sync c WHERE c.id = b.contact_id)"
        )
        assert cur.fetchone()[0] == 0

        cur.execute("SELECT count(*) FROM xero_contact_sync")
        before = cur.fetchone()[0]
        again = _run_cli("patch", str(BILL_CONTACTS_PATCH), "--apply", database_url=url)
        assert again.returncode == 0
        cur.execute("SELECT count(*) FROM xero_contact_sync")
        assert cur.fetchone()[0] == before, "a second run finds nothing"
        conn.close()
    finally:
        pg.drop_database(name)


SUBSCRIBER_PATCH = ROOT / "minty_db" / "schema" / "patches" / "2026-11-13_entity_subscriber.sql"
SUBSCRIBER_DOWN = ROOT / "minty_db" / "schema" / "patches" / "2026-11-13_entity_subscriber_down.sql"


@postgres_only
def test_the_subscriber_patch_matches_a_fresh_build_copies_the_payers_and_its_down_patch_reverses_it():
    """2026-11-13 (owner-approved): entities.subscriber_user_id - who holds a company's
    subscription - is in the baseline and in a patch for databases at 0001. Down then up lands on a
    fresh build; the patch copies an existing payer and leaves every other company unassigned; the
    user FK sets NULL; a second run stops at its guard."""
    import uuid

    from minty_db.testing import pg

    base = os.environ.get("MINTY_TEST_PG_DBNAME", "minty_test")
    name = f"{base}_subscriber"
    try:
        built = pg.build(name)
        conn = pg.connect(built.uri)
        conn.autocommit = True
        cur = conn.cursor()
        cur.execute("SET search_path = pettycashv3")
        url = built.uri + "?schema=pettycashv3"
        assert _run_cli("migrate", "--baseline", database_url=url).returncode == 0
        expected = _catalogue(conn)

        down = _run_cli("patch", str(SUBSCRIBER_DOWN), "--apply", database_url=url)
        assert down.returncode == 0, down.stdout + down.stderr
        cur.execute(
            "SELECT count(*) FROM information_schema.columns WHERE table_name = 'entities' "
            "AND column_name = 'subscriber_user_id'"
        )
        assert cur.fetchone()[0] == 0

        payer, paid, unpaid = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
        cur.execute(
            "INSERT INTO \"user\" (id, username, email, password) VALUES (%s, 'payer', 'payer@x.test', '!')",
            (payer,),
        )
        cur.execute("INSERT INTO entities (id, name) VALUES (%s, 'Paid'), (%s, 'Trial')", (paid, unpaid))
        cur.execute(
            "INSERT INTO entity_module_subscription (entity_id, function_code, phase, payer_user_id) "
            "VALUES (%s, 'PETTY_CASH', 'active', %s), (%s, 'PETTY_CASH', 'trial', NULL)",
            (paid, payer, unpaid),
        )

        done = _run_cli("patch", str(SUBSCRIBER_PATCH), "--apply", database_url=url)
        assert done.returncode == 0 and "APPLIED" in done.stdout, done.stdout + done.stderr
        got = _catalogue(conn)
        diff = {k: (expected[k] - got[k], got[k] - expected[k]) for k in expected if expected[k] != got[k]}
        assert not diff, diff

        cur.execute("SELECT id, subscriber_user_id FROM entities WHERE id IN (%s, %s)", (paid, unpaid))
        assert dict(cur.fetchall()) == {paid: payer, unpaid: None}

        cur.execute("DELETE FROM entity_module_subscription WHERE entity_id = %s", (paid,))
        cur.execute('DELETE FROM "user" WHERE id = %s', (payer,))
        cur.execute("SELECT subscriber_user_id FROM entities WHERE id = %s", (paid,))
        assert cur.fetchone()[0] is None

        again = _run_cli("patch", str(SUBSCRIBER_PATCH), "--apply", database_url=url)
        assert again.returncode != 0 and "already in this schema" in again.stdout + again.stderr
        conn.close()
    finally:
        pg.drop_database(name)
