"""Build a throwaway Postgres database from the packaged ``01_schema_rebased.sql``.

Moved from ``Minty/tests/pg_harness.py`` (2026-10-30) so no repo has to check Minty out to
build its test database. The contract is unchanged:

    MINTY_TEST_PG_URI      a Postgres SERVER (superuser/owner); its database part is only the
                           maintenance connection; its ``?schema=`` names the schema to build
                           (default pettycashv3). Falls back to DATABASE_URL.
    MINTY_TEST_PG_DBNAME   database to (re)create        default minty_test (one per xdist worker)
    MINTY_TEST_PG_KEEP=1   leave the database behind for inspection

The file creates schema ``pettycash_test``; the build renames it to the target, exactly as the
production cutover did. It runs through the database driver (psycopg 3, else psycopg2): the
file has no psql meta-commands. Never ``create_all``: if a model names a column the schema
does not have, the test fails on the SELECT - that failure is the spec, not a harness bug.

As a pytest plugin (``pytest_plugins = ["minty_db.testing.pg"]``) it replaces pytest-django's
database creation whenever ``MINTY_TEST_PG_URI`` is set, so the models run against the real
schema; unset, it does nothing (the SQLite unit run).
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, urlsplit, urlunsplit

from minty_db import BUILT_SCHEMA, DEFAULT_SCHEMA, SCHEMA_SQL

#: The fewest tables a real build of 01 has; fewer means the build went wrong.
MIN_TABLES = 50
_IDENT = re.compile(r"[a-z_][a-z0-9_]*")


def server_uri() -> str:
    uri = os.environ.get("MINTY_TEST_PG_URI") or os.environ.get("DATABASE_URL")
    if not uri or not uri.startswith(("postgres://", "postgresql://")):
        raise RuntimeError("no Postgres server: set MINTY_TEST_PG_URI (or a postgresql:// DATABASE_URL)")
    return uri


def target_schema(uri: str | None = None) -> str:
    """The ``?schema=`` of the server URI, else pettycashv3; identifiers only."""
    query = parse_qs(urlsplit(uri or server_uri()).query)
    schema = (query.get("schema") or [DEFAULT_SCHEMA])[0]
    if not _IDENT.fullmatch(schema):
        raise RuntimeError(f"refusing schema name {schema!r}: lower-case identifiers only")
    return schema


def with_database(uri: str, dbname: str) -> str:
    """``uri`` pointed at ``dbname``, its query (``?schema=``) dropped - what a driver accepts."""
    p = urlsplit(uri)
    return urlunsplit((p.scheme, p.netloc, "/" + dbname, "", ""))


def connect(uri: str):
    """A DB-API connection with psycopg 3 if installed, else psycopg2, in autocommit."""
    try:
        import psycopg

        return psycopg.connect(uri, autocommit=True)
    except ImportError:
        import psycopg2

        conn = psycopg2.connect(uri)
        conn.autocommit = True
        return conn


def _scalar(conn, sql: str, params: tuple = ()) -> Any:
    cur = conn.cursor()
    try:
        cur.execute(sql, params)
        return cur.fetchone()[0]
    finally:
        cur.close()


def run_schema_file(conn, schema: str) -> int:
    """Run 01 into ``conn``'s database, rename its schema to ``schema``; return the table count."""
    if not _IDENT.fullmatch(schema):
        raise RuntimeError(f"refusing schema name {schema!r}")
    cur = conn.cursor()
    try:
        cur.execute(SCHEMA_SQL.read_text(encoding="utf-8"))
        if schema != BUILT_SCHEMA:
            cur.execute(f"ALTER SCHEMA {BUILT_SCHEMA} RENAME TO {schema}")
    finally:
        cur.close()
    n = _scalar(
        conn,
        "SELECT count(*) FROM information_schema.tables WHERE table_schema = %s AND table_type = 'BASE TABLE'",
        (schema,),
    )
    if n < MIN_TABLES:
        raise RuntimeError(f"schema build produced only {n} tables in {schema}")
    return n


def test_dbname() -> str:
    name = os.environ.get("MINTY_TEST_PG_DBNAME", "minty_test")
    worker = os.environ.get("PYTEST_XDIST_WORKER")
    name = f"{name}_{worker}" if worker else name
    if not _IDENT.fullmatch(name):
        raise RuntimeError(f"refusing database name {name!r}")
    return name


def drop_database(dbname: str) -> None:
    admin = connect(with_database(server_uri(), urlsplit(server_uri()).path.lstrip("/") or "postgres"))
    try:
        cur = admin.cursor()
        cur.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = %s AND pid <> pg_backend_pid()",
            (dbname,),
        )
        cur.execute(f'DROP DATABASE IF EXISTS "{dbname}"')
        cur.close()
    finally:
        admin.close()


@dataclass
class BuiltDatabase:
    uri: str
    dbname: str
    schema: str
    keep: bool

    def drop(self) -> None:
        if not self.keep:
            drop_database(self.dbname)


def build(dbname: str | None = None) -> BuiltDatabase:
    """Create ``dbname`` (dropping any old one), run 01 into it, rename the schema."""
    dbname = dbname or test_dbname()
    server = server_uri()
    schema = target_schema(server)
    drop_database(dbname)
    admin = connect(with_database(server, urlsplit(server).path.lstrip("/") or "postgres"))
    try:
        cur = admin.cursor()
        cur.execute(f"CREATE DATABASE \"{dbname}\" ENCODING 'UTF8' TEMPLATE template0")
        cur.close()
    finally:
        admin.close()
    uri = with_database(server, dbname)
    try:
        conn = connect(uri)
        try:
            run_schema_file(conn, schema)
        finally:
            conn.close()
    except Exception:
        drop_database(dbname)
        raise
    return BuiltDatabase(uri=uri, dbname=dbname, schema=schema, keep=os.environ.get("MINTY_TEST_PG_KEEP") == "1")


@contextmanager
def built_database(dbname: str | None = None) -> Iterator[Any]:
    """A connection to a fresh build (search_path = the schema), dropped afterwards."""
    built = build(dbname or test_dbname())
    conn = connect(built.uri)
    try:
        cur = conn.cursor()
        cur.execute(f"SET search_path TO {built.schema}")
        cur.close()
        yield conn
    finally:
        conn.close()
        built.drop()


# --- pytest plugin -----------------------------------------------------------------------------

if os.environ.get("MINTY_TEST_PG_URI"):
    import pytest

    @pytest.fixture(scope="session")
    def django_db_setup(django_db_blocker: Any) -> Iterator[None]:
        """Replace pytest-django's database creation with a build of the schema file."""
        from django.db import connections

        built = build()
        try:
            for alias in connections:
                connections[alias].close()
            settings_dict = connections["default"].settings_dict
            settings_dict["NAME"] = built.dbname
            settings_dict.setdefault("TEST", {})["NAME"] = built.dbname
            with django_db_blocker.unblock():
                with connections["default"].cursor() as cur:
                    cur.execute(
                        "SELECT count(*) FROM information_schema.tables WHERE table_schema = %s",
                        [built.schema],
                    )
                    (n,) = cur.fetchone()
                    assert n >= MIN_TABLES, f"harness database has only {n} tables"
            yield
        finally:
            for alias in connections:
                connections[alias].close()
            built.drop()
