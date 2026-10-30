# minty-db

Phase 0 of the Flask retirement plan (2026-10-30). This package replaces the four hand-kept `shared_models/` copies in the Django APIs and Flask's Alembic migrations as the only owner of the schema.

## What it guarantees

| Check | Where |
|---|---|
| Every model's columns and types equal the schema's, table by table | `tests/test_schema.py::test_every_column_has_the_models_type` (Postgres) |
| Every model, views included, can be queried on the real schema | `tests/test_models.py::test_every_model_can_be_queried` |
| `models.py`/`enums.py` are what the schema file generates | `scripts/generate_models.py --check`, run by a test |
| Migration state equals the models | `makemigrations --check` test |
| `migrate` builds a fresh database, and refuses then baselines an existing one | `test_migrate_builds_a_fresh_database_and_baselines_an_existing_one` |

CI runs everything twice: on SQLite, and on Postgres 15 built from the packaged schema file.

## Decisions

- **Generated from the database, not merged from the copies.** The copies disagreed in 58 places across 21 shared tables, nearly all on the id type. The generator reads a fresh build's catalogue, so the models are the schema's mirror by construction.
- **Ids are `uuid.UUID`** (owner, 2026-10-28). `MintyUUIDField`, which returned text, is retired. subscription-api and petty-cash-api convert before they switch, and a test-only check turns any `UUID`-vs-`str` comparison into a failure.
- **Foreign keys only on `<name>_id` columns, with `db_constraint=False` and `related_name="+"`.** This keeps the APIs' existing code and lenient SQLite tests working; the real constraints are the database's. `created_by`, `invited_by` and `currency` stay plain fields, because the APIs assign raw values to them.
- **Database defaults are `db_default`.** Django 5.2 returns them on insert, so `created_at` is a datetime straight after `create()` on both SQLite and Postgres.
- **Unique rules come from the schema (v0.2).** Every unique constraint and unique index is read from the catalogue: one column -> `unique=True` (a key becomes a `OneToOneField`), several -> `Meta.constraints`, a partial index -> a `UniqueConstraint` whose `Q` is written once in `PARTIAL_UNIQUE` and checked against the predicate Postgres reports (the generator stops if it changes).
- **Reverse names and ordering are declared exceptions (v0.2).** `RELATED_NAMES` and `ORDERING` in the generator hold what an API's code uses (`user.billing_groups`, `invoice.lines` in creation order) - added as each API switches, never removed while one uses them. v0.2 regenerated `0001`'s STATE only: the rules were already in the schema file, so a baselined database is unchanged.
- **No pyright.** Django ships no type stubs, the same reason the Django APIs run only ruff.

## Where each database stands

Each database was checked column by column against the models before its baseline (`python -m minty_db migrate --baseline` records `0001` without running it).

| Database | State |
|---|---|
| Local `postgres` / `pettycashv3` | Baselined 2026-10-30 (0 differences) |
| Local `pettycashv3_data` / `pettycashv3` | Baselined 2026-10-30 (0 differences) |
| Supabase `development` | Brought up to date 2026-10-30: Minty's `i1a01` and 01's `entity_setup_summary` view had never reached it. Then 0 differences, baselined |
| Supabase `pettycashv3` | 0 differences, **not baselined** (owner, 2026-10-30): it has no `django_migrations` table yet |

`pettycashv3` (and local `postgres`) also hold `sessions`, Flask-Session's table. It is outside the schema file and has no model; it goes with Flask's session cutover.

## Open

- Flask's `docs/schema/generators/audit_models.py` should read `minty-db` too. That's a Minty change, made when Alembic stops (Phase 0, step 5).
- The catalogue loader (`seed_catalogue.sql` is packaged; `Minty/scripts/load_catalogue.py`'s schema-renaming load moves here when Minty stops owning it). `00_enum_coverage_check.sql` (enums must cover what the code produces) moves here with the API switches.
- `py-package.yml` in `.github` for both Python packages, a GitHub-visible change.
