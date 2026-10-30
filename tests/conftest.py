import os

import pytest

pytest_plugins = ["minty_db.testing.pg"]

postgres_only = pytest.mark.skipif(not os.environ.get("MINTY_TEST_PG_URI"), reason="needs MINTY_TEST_PG_URI")
