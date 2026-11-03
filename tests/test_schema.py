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
                want = f.target_field.db_type(connection) if f.is_relation else f.db_type(connection)
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
