"""Run minty-db's migrations against one database, by hand. No service migrates at deploy.

    python -m minty_db status              # which migrations the database has
    python -m minty_db migrate --baseline  # an EXISTING schema: record 0001 without running it
    python -m minty_db migrate             # apply what is missing (a missing schema is built first)

The database is ``DATABASE_URL`` (its ``?schema=``, else ``MINTY_DB_SCHEMA``, else pettycashv3).
Django's own record, ``django_migrations``, lives in that schema. Every Supabase run needs the
owner's yes first.
"""

from __future__ import annotations

import argparse
import os
import sys
from urllib.parse import parse_qs, unquote, urlsplit

from minty_db import DEFAULT_SCHEMA


def _settings_for(url: str) -> tuple[str, dict]:
    """The schema and Django's database settings for ``url``. A URI percent-encodes its user and
    password (Supabase's does), so both are decoded; its ``sslmode`` is kept."""
    if not url.startswith(("postgres://", "postgresql://")):
        raise SystemExit("DATABASE_URL must be a postgresql:// URI")
    u = urlsplit(url)
    query = parse_qs(u.query)
    schema = (query.get("schema") or [os.environ.get("MINTY_DB_SCHEMA", DEFAULT_SCHEMA)])[0]
    if not schema.replace("_", "").isalnum() or not schema.islower():
        raise SystemExit(f"refusing schema name {schema!r}")
    options = {"options": f"-c search_path={schema}"}
    if query.get("sslmode"):
        options["sslmode"] = query["sslmode"][0]
    return schema, {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": u.path.lstrip("/"),
        "USER": unquote(u.username or ""),
        "PASSWORD": unquote(u.password or ""),
        "HOST": u.hostname,
        "PORT": u.port or 5432,
        "OPTIONS": options,
    }


def _configure() -> str:
    schema, database = _settings_for(os.environ.get("DATABASE_URL", ""))
    os.environ["MINTY_DB_SCHEMA"] = schema

    import django
    from django.conf import settings

    settings.configure(INSTALLED_APPS=["minty_db"], USE_TZ=True, DATABASES={"default": database})
    django.setup()
    return schema


def _schema_exists(schema: str) -> bool:
    from django.db import connection

    with connection.cursor() as cur:
        cur.execute("SELECT 1 FROM pg_namespace WHERE nspname = %s", [schema])
        return cur.fetchone() is not None


def _has_0001() -> bool:
    from django.db import connection
    from django.db.migrations.recorder import MigrationRecorder

    recorder = MigrationRecorder(connection)
    return recorder.has_table() and ("minty_db", "0001_initial") in recorder.applied_migrations()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m minty_db")
    sub = ap.add_subparsers(dest="command", required=True)
    sub.add_parser("status")
    mig = sub.add_parser("migrate")
    mig.add_argument("--baseline", action="store_true", help="record 0001 on an existing schema, run nothing")
    args = ap.parse_args(argv)

    schema = _configure()
    from django.core.management import call_command

    host = urlsplit(os.environ["DATABASE_URL"]).hostname
    print(f"database: {host} / schema {schema}")
    if args.command == "status":
        call_command("showmigrations", "minty_db")
        return 0

    exists = _schema_exists(schema)
    if args.baseline:
        if not exists:
            raise SystemExit(f"schema {schema!r} does not exist - nothing to baseline (run `migrate`)")
        call_command("migrate", "minty_db", "0001", fake=True)
        return 0
    if not exists:
        # A fresh database: build the schema from the file, then record 0001 as applied.
        from django.db import connection

        from minty_db.testing.pg import run_schema_file

        connection.ensure_connection()
        n = run_schema_file(connection.connection, schema)
        print(f"built schema {schema} ({n} tables) from 01_schema_rebased.sql")
        call_command("migrate", "minty_db", "0001", fake=True)
    elif not _has_0001():
        raise SystemExit(f"schema {schema!r} exists but 0001 is not recorded: run `migrate --baseline` first")
    call_command("migrate", "minty_db")
    return 0


if __name__ == "__main__":
    sys.exit(main())
