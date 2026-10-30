"""minty-db: the Minty schema's models and migrations, shared by every backend.

``SCHEMA_SQL`` is the reviewed schema file (the source of truth); ``0001_initial`` runs it.
"""

from pathlib import Path

__version__ = "0.2.0"

SCHEMA_SQL = Path(__file__).parent / "schema" / "01_schema_rebased.sql"
CATALOGUE_SQL = Path(__file__).parent / "schema" / "seed_catalogue.sql"

#: The schema name 01_schema_rebased.sql creates; every build renames it to the target.
BUILT_SCHEMA = "pettycash_test"
DEFAULT_SCHEMA = "pettycashv3"
