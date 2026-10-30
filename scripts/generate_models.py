"""Write minty_db/models.py and minty_db/enums.py from a database built from 01_schema_rebased.sql.

    python scripts/generate_models.py            # build a throwaway database, read it, drop it
    python scripts/generate_models.py --check    # exit 1 if the files on disk differ

The models are the schema file's mirror, so they are generated, never hand-edited: change
``minty_db/schema/01_schema_rebased.sql`` (owner's approval first), add a migration, and run
this. The rules it applies are the house conventions every API's copy used:

* ``uuid`` -> ``UUIDField``; a single uuid primary key gets ``default=uuid.uuid4``.
* an enum column -> ``PgEnumField("<type>")``; ``char(n)`` -> ``CharNField``.
* a database default -> ``db_default`` (``now()`` -> ``Now()``, literals as literals), so an
  insert that omits the column leaves it to Postgres, as before; ``gen_random_uuid()`` on a
  non-key column -> ``default=uuid.uuid4`` (SQLite has no such function).
* ``<name>_id`` columns with a foreign key -> ``ForeignKey(<name>)`` with ``db_constraint=False``
  and ``related_name="+"``: the attribute ``<name>_id`` and the filter ``<name>_id=`` work as
  they did on the plain-id copies; the real constraint is the database's (from 01). Other
  referencing columns (``created_by``, ``currency``) stay plain fields.
* ``on_delete`` mirrors the constraint's rule (CASCADE, SET_NULL, else DO_NOTHING - the
  database refuses a restricted delete itself).
* a table whose ``updated_at`` a trigger moves -> ``UpdatedAtMixin``.
* a view -> ``managed = False`` with its id-like first column as the primary key.
"""

from __future__ import annotations

import argparse
import keyword
import re
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

SCHEMA = "pettycashv3"
MODELS = ROOT / "minty_db" / "models.py"
ENUMS = ROOT / "minty_db" / "enums.py"

# Table -> class name where the plain CamelCase is not what every API already calls it.
CLASS_NAMES = {"entities": "Entity"}
# Views: the column Django treats as the primary key (a view has none of its own).
VIEW_KEYS = {"entity_setup_summary": "entity_id", "report_cash_summary": "report_id", "tracker": "entity_id"}
# numeric without precision (a view's computed totals): read as money.
UNBOUNDED_NUMERIC = "max_digits=14, decimal_places=2"


def camel(name: str) -> str:
    return CLASS_NAMES.get(name) or "".join(p.capitalize() for p in name.split("_"))


def pyname(column: str) -> str:
    name = re.sub(r"\W", "_", column)
    return name + "_" if keyword.iskeyword(name) else name


def read_catalog(conn) -> dict:
    q = lambda sql: conn.execute(sql, {"s": SCHEMA}).fetchall()  # noqa: E731
    return {
        "columns": q("""select c.table_name, c.column_name, c.data_type, c.udt_name,
            c.character_maximum_length, c.numeric_precision, c.numeric_scale, c.is_nullable = 'YES',
            c.column_default, t.table_type = 'VIEW',
            col_description(format('%%I.%%I', c.table_schema, c.table_name)::regclass, c.ordinal_position)
            from information_schema.columns c join information_schema.tables t using (table_schema, table_name)
            where c.table_schema = %(s)s order by c.table_name, c.ordinal_position"""),
        "pk": q("""select tc.table_name, kcu.column_name from information_schema.table_constraints tc
            join information_schema.key_column_usage kcu using (constraint_schema, constraint_name)
            where tc.table_schema = %(s)s and tc.constraint_type = 'PRIMARY KEY'
            order by tc.table_name, kcu.ordinal_position"""),
        "fk": q("""select cl.relname, a.attname, fcl.relname, fa.attname, con.confdeltype
            from pg_constraint con join pg_class cl on cl.oid = con.conrelid
            join pg_namespace n on n.oid = cl.relnamespace join pg_class fcl on fcl.oid = con.confrelid
            join pg_attribute a on a.attrelid = con.conrelid and a.attnum = con.conkey[1]
            join pg_attribute fa on fa.attrelid = con.confrelid and fa.attnum = con.confkey[1]
            where n.nspname = %(s)s and con.contype = 'f'"""),
        "triggers": q("""select c.relname from pg_trigger t join pg_class c on c.oid = t.tgrelid
            join pg_namespace n on n.oid = c.relnamespace join pg_proc p on p.oid = t.tgfoid
            where n.nspname = %(s)s and not t.tgisinternal and p.proname = 'set_updated_at'"""),
        "enums": q("""select t.typname, array_agg(e.enumlabel order by e.enumsortorder) from pg_type t
            join pg_enum e on e.enumtypid = t.oid join pg_namespace n on n.oid = t.typnamespace
            where n.nspname = %(s)s group by t.typname order by t.typname"""),
        "tables": q("""select c.relname, obj_description(c.oid)
            from pg_class c join pg_namespace n on n.oid = c.relnamespace
            where n.nspname = %(s)s and c.relkind in ('r', 'v') order by c.relname"""),
    }


def default_kwarg(raw: str | None, data_type: str) -> str | None:
    """The column's database default as a field keyword, or None."""
    if raw is None:
        return None
    value = raw.split("::")[0].strip()
    if value == "now()":
        return "db_default=Now()"
    if value == "gen_random_uuid()":
        return "default=uuid.uuid4"
    if value in ("true", "false"):
        return f"db_default={value.capitalize()}"
    if re.fullmatch(r"-?\d+", value):
        return f"db_default={value}" if data_type != "numeric" else f'db_default=Decimal("{value}")'
    if re.fullmatch(r"-?\d+\.\d+", value):
        return f'db_default=Decimal("{value}")'
    m = re.fullmatch(r"'(.*)'", value, re.S)
    if m:
        return f"db_default={m.group(1).replace(chr(39) * 2, chr(39))!r}"
    raise SystemExit(f"unrecognised default {raw!r} - teach default_kwarg() about it")


def field_line(col, table_pk, fks, enums_len, view_key) -> tuple[str, str]:
    (table, column, data_type, udt, char_len, num_p, num_s, nullable, default, is_view, _comment) = col
    name = pyname(column)
    kw: list[str] = []
    single_pk = table_pk == [column]

    fk = fks.get((table, column))
    if fk and column.endswith("_id") and column != "id":
        target_table, target_col, rule = fk
        cls = "models.OneToOneField" if single_pk else "models.ForeignKey"
        on_delete = {"c": "models.CASCADE", "n": "models.SET_NULL"}.get(rule, "models.DO_NOTHING")
        kw = [repr(camel(target_table)), on_delete, "db_constraint=False", 'related_name="+"']
        if target_col != "id":
            kw.append(f"to_field={target_col!r}")
        if single_pk:
            kw.append("primary_key=True")
        if nullable:
            kw += ["null=True", "blank=True"]
        return column[:-3], f"{cls}({', '.join(kw)})"

    if data_type == "uuid":
        cls = "models.UUIDField"
    elif data_type == "character varying":
        cls = "models.CharField"
        kw.append(f"max_length={char_len}")
    elif data_type == "character":
        cls = "CharNField"
        kw.append(f"max_length={char_len}")
    elif data_type == "text":
        cls = "models.TextField"
    elif data_type == "timestamp with time zone":
        cls = "models.DateTimeField"
    elif data_type == "date":
        cls = "models.DateField"
    elif data_type == "boolean":
        cls = "models.BooleanField"
    elif data_type == "integer":
        cls = "models.IntegerField"
    elif data_type == "bigint":
        cls = "models.BigIntegerField"
    elif data_type == "smallint":
        cls = "models.SmallIntegerField"
    elif data_type == "numeric":
        cls = "models.DecimalField"
        kw.append(f"max_digits={num_p}, decimal_places={num_s}" if num_p is not None else UNBOUNDED_NUMERIC)
    elif data_type == "jsonb":
        cls = "models.JSONField"
    elif data_type == "USER-DEFINED":
        cls = "PgEnumField"
        kw += [repr(udt), f"max_length={enums_len[udt]}"]
    else:
        raise SystemExit(f"unmapped type {data_type} for {table}.{column}")

    if single_pk or view_key == column:
        kw.append("primary_key=True")
    if name != column:
        kw.append(f"db_column={column!r}")
    dk = default_kwarg(default, data_type) if not is_view else None
    if dk:
        kw.append(dk)
    if nullable and not single_pk and view_key != column:
        kw += ["null=True", "blank=True"]
    return name, f"{cls}({', '.join(kw)})"


def render(cat: dict) -> tuple[str, str]:
    pks: dict[str, list[str]] = defaultdict(list)
    for t, c in cat["pk"]:
        pks[t].append(c)
    fks = {(t, c): (ft, fc, rule) for t, c, ft, fc, rule in cat["fk"]}
    triggered = {t for (t,) in cat["triggers"]}
    enums = {name: labels for name, labels in cat["enums"]}
    enums_len = {name: max(len(x) for x in labels) for name, labels in enums.items()}
    comments = dict(cat["tables"])
    columns: dict[str, list] = defaultdict(list)
    for col in cat["columns"]:
        columns[col[0]].append(col)

    out = [
        '"""Every table and view in the Minty schema, generated by scripts/generate_models.py from',
        "minty_db/schema/01_schema_rebased.sql. Do not edit by hand: change the schema file (the owner",
        'approves every schema change), add a migration, regenerate. Behaviour belongs in each API."""',
        "",
        "import uuid",
        "from decimal import Decimal  # noqa: F401  (numeric db_defaults)",
        "",
        "from django.db import models",
        "from django.db.models.functions import Now",
        "",
        "from minty_db.fields import CharNField, PgEnumField, UpdatedAtMixin",
        "",
    ]
    for table in sorted(columns):
        cols = columns[table]
        is_view = cols[0][9]
        pk = pks.get(table, [])
        base = "UpdatedAtMixin" if table in triggered else "models.Model"
        out += ["", "", f"class {camel(table)}({base}):"]
        if comments.get(table):
            out.append(f"    {comments[table]!r}")
            out.append("")
        names = {}
        for col in cols:
            if base == "UpdatedAtMixin" and col[1] == "updated_at":
                names[col[1]] = "updated_at"
                continue
            name, line = field_line(col, pk, fks, enums_len, VIEW_KEYS.get(table) if is_view else None)
            names[col[1]] = name
            out.append(f"    {name} = {line}")
        if len(pk) > 1:
            out.append(f"    pk = models.CompositePrimaryKey({', '.join(repr(names[c]) for c in pk)})")
        out += ["", "    class Meta:"]
        if is_view:
            out.append("        managed = False")
        out.append(f"        db_table = {table!r}")

    enum_out = [
        '"""The schema\'s enum types as Django choices, generated by scripts/generate_models.py."""',
        "",
        "from django.db import models",
    ]
    for name, labels in enums.items():
        enum_out += ["", "", f"class {camel(name)}(models.TextChoices):"]
        for label in labels:
            enum_out.append(f"    {re.sub(r'\W', '_', label).upper()} = {label!r}")
    return "\n".join(out) + "\n", "\n".join(enum_out) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="exit 1 when the generated files differ")
    args = ap.parse_args()

    from minty_db.testing.pg import built_database

    with built_database() as conn:
        models_src, enums_src = render(read_catalog(conn))
    if args.check:
        pairs = ((MODELS, models_src), (ENUMS, enums_src))
        stale = [p.name for p, src in pairs if p.read_text(encoding="utf-8") != src]
        if stale:
            print("stale, regenerate:", ", ".join(stale))
            return 1
        print("models.py and enums.py match the schema file")
        return 0
    MODELS.write_text(models_src, encoding="utf-8", newline="\n")
    ENUMS.write_text(enums_src, encoding="utf-8", newline="\n")
    print(f"wrote {MODELS.relative_to(ROOT)} and {ENUMS.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
