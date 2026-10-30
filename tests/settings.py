"""Settings for minty-db's own tests.

SQLite by default (tables from the models, as an API's unit run builds them). With
MINTY_TEST_PG_URI set, the pytest plugin (minty_db.testing.pg) builds Postgres from the schema
file instead, and the same tests run against the real schema.
"""

import os
from urllib.parse import parse_qs, urlsplit

SECRET_KEY = "minty-db-tests"
USE_TZ = True
INSTALLED_APPS = ["minty_db"]

_pg = os.environ.get("MINTY_TEST_PG_URI")
if _pg:
    _u = urlsplit(_pg)
    _schema = (parse_qs(_u.query).get("schema") or ["pettycashv3"])[0]
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": "replaced-by-the-harness",
            "USER": _u.username,
            "PASSWORD": _u.password,
            "HOST": _u.hostname,
            "PORT": _u.port or 5432,
            "OPTIONS": {"options": f"-c search_path={_schema}"},
        }
    }
    # The harness built the schema from the file; Django must not run migrations over it.
    MIGRATION_MODULES = {"minty_db": None}
else:
    DATABASES = {"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"}}
    # An API's unit run: tables straight from the models (0001 runs Postgres SQL).
    MIGRATION_MODULES = {"minty_db": None}
