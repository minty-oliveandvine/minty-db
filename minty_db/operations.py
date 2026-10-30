"""The migration operation that builds the schema from the reviewed SQL file."""

import os

from django.db.migrations.operations.base import Operation

from minty_db import BUILT_SCHEMA, DEFAULT_SCHEMA, SCHEMA_SQL


class RunSchemaFile(Operation):
    """Run ``01_schema_rebased.sql`` and rename its schema to ``MINTY_DB_SCHEMA``.

    Postgres only, and only into a database that does not have the schema yet. An existing
    database (production, development, a local copy) already IS this schema: baseline it with
    ``python -m minty_db migrate --baseline`` (which records 0001 without running it). A SQLite
    unit run never migrates minty_db (``MIGRATION_MODULES = {"minty_db": None}``).
    """

    reversible = False
    reduces_to_sql = False

    def state_forwards(self, app_label, state):
        pass

    def database_forwards(self, app_label, schema_editor, from_state, to_state):
        connection = schema_editor.connection
        if connection.vendor != "postgresql":
            raise RuntimeError(
                "minty_db 0001 builds the Postgres schema; a SQLite run sets MIGRATION_MODULES = {'minty_db': None}"
            )
        schema = os.environ.get("MINTY_DB_SCHEMA", DEFAULT_SCHEMA)
        with connection.cursor() as cur:
            cur.execute("SELECT 1 FROM pg_namespace WHERE nspname = %s", [schema])
            if cur.fetchone():
                raise RuntimeError(
                    f"schema {schema!r} already exists: record 0001 with `python -m minty_db migrate --baseline` "
                    "instead of running it"
                )
            cur.execute(SCHEMA_SQL.read_text(encoding="utf-8"))
            cur.execute(f'ALTER SCHEMA {BUILT_SCHEMA} RENAME TO "{schema}"')

    def database_backwards(self, app_label, schema_editor, from_state, to_state):
        raise NotImplementedError("0001 is the whole schema; it is never reversed")

    def describe(self):
        return "Run 01_schema_rebased.sql and rename its schema to MINTY_DB_SCHEMA"
