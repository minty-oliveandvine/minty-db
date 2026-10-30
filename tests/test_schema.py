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
