# minty-db

The Minty schema (`pettycashv3`) as one Python package, shared by every backend:

- `minty_db.models`: a Django model for each of the 60 tables and 3 views. They are generated from the schema file and never edited by hand.
- `minty_db.enums`: the 22 enum types as Django choices.
- `minty_db.fields`: `CharNField`, `PgEnumField` and `UpdatedAtMixin`.
- `minty_db/migrations`: `0001` is the whole schema. It runs `minty_db/schema/01_schema_rebased.sql`, the reviewed source of truth.
- `minty_db.testing.pg`: the test-database builder and pytest plugin, moved from `Minty/tests/pg_harness.py`.
- `python -m minty_db status | migrate [--baseline]`: migrations, run by hand. No service migrates at deploy.

## Use it in a service

Pin the tag's archive, as with minty-shared, never `git+`. The `python:3.13-slim` images have no git.

```
minty-db @ https://github.com/minty-oliveandvine/minty-db/archive/refs/tags/v0.1.tar.gz
```

```python
INSTALLED_APPS = [..., "minty_db"]
MIGRATION_MODULES = {"minty_db": None}  # a service never runs minty_db's migrations itself
```

```python
# conftest.py: with MINTY_TEST_PG_URI set, tests run on a Postgres built from the schema file
pytest_plugins = ["minty_db.testing.pg"]
```

Ids are Python `uuid.UUID` objects (owner, 2026-10-28). A `<name>_id` column with a foreign key is a `ForeignKey(<name>)` with `db_constraint=False` and `related_name="+"`, so `obj.<name>_id` and `filter(<name>_id=...)` work as they did on a plain id. Behaviour that one API needs (properties, reverse names, choices) stays in that API, for example as a proxy model.

## Change the schema

The owner approves every schema change first.

1. Edit `minty_db/schema/01_schema_rebased.sql`.
2. Run `python scripts/generate_models.py`, which builds a throwaway database and rewrites `models.py` and `enums.py`.
3. Run `python -m django makemigrations minty_db` with migrations on. The new migration is ordinary Django; when an enum grows, add `RunSQL` for `ALTER TYPE ... ADD VALUE`.
4. Tag a new version, run `python -m minty_db migrate` on each database (ask first for Supabase), then bump the pin in each service that needs the change.

Keep changes additive first: add now, remove in a later tag, so a service on the older pin keeps working.

## Develop

```
uv venv --python 3.13 .venv
uv pip install --python .venv/Scripts/python.exe -e ".[dev]"
pytest -q                                                    # SQLite
MINTY_TEST_PG_URI=postgresql://postgres:***@localhost:5432/postgres MINTY_TEST_PG_DBNAME=mintydb_<you> pytest -q
```
