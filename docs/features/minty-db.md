# minty-db

Phase 0 of the Flask retirement plan (2026-10-30). This package replaces the four hand-kept `shared_models/` copies in the Django APIs and Flask's Alembic migrations as the only owner of the schema.

## What it guarantees

| Check | Where |
|---|---|
| Every model's columns and types equal the schema's, table by table | `tests/test_schema.py::test_every_column_has_the_models_type` (Postgres) |
| Every model, views included, can be queried on the real schema | `tests/test_models.py::test_every_model_can_be_queried` |
| `models.py`/`enums.py` are what the schema file generates | `scripts/generate_models.py --check`, run by a test |
| Migration state equals the models | `makemigrations --check` test |
| A patch brings a database at `0001` to what a fresh build has | `test_the_xero_keys_patch_merges_duplicates_and_matches_a_fresh_build` |
| `migrate` builds a fresh database, and refuses then baselines an existing one | `test_migrate_builds_a_fresh_database_and_baselines_an_existing_one` |

CI runs everything twice: on SQLite, and on Postgres 15 built from the packaged schema file.

## Decisions

- **Generated from the database, not merged from the copies.** The copies disagreed in 58 places across 21 shared tables, nearly all on the id type. The generator reads a fresh build's catalogue, so the models are the schema's mirror by construction.
- **Ids are `uuid.UUID`** (owner, 2026-10-28). `MintyUUIDField`, which returned text, is retired. subscription-api and petty-cash-api convert before they switch, and a test-only check turns any `UUID`-vs-`str` comparison into a failure.
- **Foreign keys only on `<name>_id` columns, with `db_constraint=False` and `related_name="+"`.** This keeps the APIs' existing code and lenient SQLite tests working; the real constraints are the database's. `created_by`, `invited_by` and `currency` stay plain fields, because the APIs assign raw values to them.
- **Database defaults are `db_default`.** Django 5.2 returns them on insert, so `created_at` is a datetime straight after `create()` on both SQLite and Postgres.
- **Unique rules come from the schema (v0.2).** Every unique constraint and unique index is read from the catalogue: one column -> `unique=True` (a key becomes a `OneToOneField`), several -> `Meta.constraints`, a partial index -> a `UniqueConstraint` whose `Q` is written once in `PARTIAL_UNIQUE` and checked against the predicate Postgres reports (the generator stops if it changes).
- **Reverse names and ordering are declared exceptions (v0.2).** `RELATED_NAMES` and `ORDERING` in the generator hold what an API's code uses (`user.billing_groups`, `invoice.lines` in creation order) - added as each API switches, never removed while one uses them. v0.2 regenerated `0001`'s STATE only: the rules were already in the schema file, so a baselined database is unchanged.
- **Django-only relations (v0.3).** `VIRTUAL_FKS` declares a relation the schema does not have but an API joins through: `entity_sale_setting.sale_id` -> `sale_info` (onboarding-api). It is `db_constraint=False`, so nothing reaches the database; 01 says `sale_id` is not a real reference, so a join simply skips rows that do not match, as the API's own copy behaved.
- **The id guard is shared (v0.3).** `minty_db.testing.uuid_guard`: under test, a service's code comparing a `UUID` with uuid text raises. Django's own comparisons (its foreign-key setter, `refresh_from_db`) are left alone. A dict or set keyed one way and looked up the other compares by hash and stays invisible.
- **A database default now applies on insert.** The models carry the schema's defaults as `db_default`, where an API's old copy often had none and wrote `NULL`. Audited 2026-10-30 across all four copies: the only such column was `entities.onboarding_saved_step` (default 9 = "finished"); onboarding-api's create now writes `NULL` explicitly, as Flask's does.
- **No pyright.** Django ships no type stubs, the same reason the Django APIs run only ruff.
- **`0001` is the only migration until the owner says otherwise (2026-11-03).** A schema change goes into the schema file (a fresh build runs it) and `0001`'s STATE half, and a database already at `0001` gets it from a hand-run patch in `minty_db/schema/patches/` (`python -m minty_db patch FILE [--apply]`: one transaction, rolled back unless `--apply`; `search_path` is the URL's schema, so a patch names none). Pinned by `test_the_xero_keys_patch_merges_duplicates_and_matches_a_fresh_build`.
- **The Xero unique keys (v0.4, 2026-11-03).** `entity_account_xero (account_id)` (`uq_eax_account`, so `account` is a `OneToOneField`), `entity_bill_account_xero (entity_id, xero_account_id)` (`uq_ebax_entity_xero_account`, a partial unique index `WHERE xero_account_id <> ''`: a code added by hand has no Xero id - NULL, or `''` as minty-payment-request-api writes it - and any number may exist) and `xero_contact_sync (entity_id, xero_contact_id)` (`uq_xcs_entity_contact`). Their syncs read-then-inserted, so two at once wrote a row twice (QA: onboarding's duplicate chart of accounts). The patch `2026-11-03_xero_unique_keys.sql` first keeps one row per key (a ticked one, the default bill code, the oldest contact) and moves every reference to a removed contact (`report_expense.contact_id`, `bill.contact_id`, the three `entity_pettycash_settings` contact columns) to the one kept.

## Where each database stands

Each database was checked column by column against the models before its baseline (`python -m minty_db migrate --baseline` records `0001` without running it).

| Database | State |
|---|---|
| Local `postgres` / `pettycashv3` | Baselined 2026-10-30 (0 differences) |
| Local `pettycashv3_data` / `pettycashv3` | Baselined 2026-10-30 (0 differences) |
| Supabase `development` | Brought up to date 2026-10-30: Minty's `i1a01` and 01's `entity_setup_summary` view had never reached it. Then 0 differences, baselined |
| Supabase `pettycashv3` | 0 differences (re-checked against v0.3), baselined 2026-11-03 |

Patches: `2026-11-03_xero_unique_keys.sql` applied 2026-11-03 on all four (owner: "do all"), each after a dry run, and a second run found nothing: local `postgres` (39 bill codes / 278 contacts merged, 33 references moved), `pettycashv3_data` (39 / 283 / 33), Supabase `development` (no duplicates; keys only), Supabase `pettycashv3` (39 / 284 / 33; the six touched tables saved first to Minty's git-ignored `backups/pg/supabase_pettycashv3_prexerokeys_20261103.dump`). All still record `0001` only.

`2026-11-03_slip_placeholders.sql` is a **data** repair, not a schema change, run the same way (owner, 2026-11-03: "Repair 68 + list the 3"). Until then minty-payment-request-web's bank slip upload made a 0.00 payment with no date to hold a slip when the bill had no payment yet. On a bill (not voided) with exactly one real payment, the holder's `payment_attachment` rows move onto it; with several, onto the next real payment recorded after the holder (owner, 2026-11-03: "go to the next payment recorded"). Then the holder is deleted - links first, since the delete cascades; it stops if any link would be lost. A bill whose real payments all came before the holder is listed for the owner; bills with none keep their holder. Minty's load does the same (`docs/schema/generators/gen.py` `SLIP_PLACEHOLDERS`). Applied 2026-11-03 on Supabase `pettycashv3` (owner: "just do the repair in supabase"), after a dry run, `payment` and `payment_attachment` saved first to Minty's git-ignored `backups/pg/supabase_pettycashv3_preslips_20261103.dump`: 67 holders folded, 69 slip links moved, none lost; 58 left (2 bills with several real payments - `MBIHAR-121128110626`, `MBIJAY-161745270726` - 52 with no real payment, 4 on voided bills); a second run found nothing; every bill's `amount_paid` still matches its completed payments. Run again the same day with the next-payment rule (`preslips2` saved first): 3 more folded - `MBIHAR-121128110626` and `MBIJAY-161745270726` onto their first payment, and `MBIMIC-095721070926`, a holder made 17:20 that day by the still-deployed old upload; 0 left after several payments, 52 with none, 4 voided. Not applied to the local databases or Supabase `development`.

`pettycashv3` (and local `postgres`) also hold `sessions`, Flask-Session's table. It is outside the schema file and has no model; it goes with Flask's session cutover.

## Open

- Flask's `docs/schema/generators/audit_models.py` should read `minty-db` too. That's a Minty change, made when Alembic stops (Phase 0, step 5).
- The catalogue loader (`seed_catalogue.sql` is packaged; `Minty/scripts/load_catalogue.py`'s schema-renaming load moves here when Minty stops owning it). `00_enum_coverage_check.sql` (enums must cover what the code produces) moves here with the API switches.
- `py-package.yml` in `.github` for both Python packages, a GitHub-visible change.
