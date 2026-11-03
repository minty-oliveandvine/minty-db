"""pytest plugin: under test, comparing a ``uuid.UUID`` with uuid-shaped text raises.

    pytest_plugins = ["minty_db.testing.pg", "minty_db.testing.uuid_guard"]

Ids are ``uuid.UUID`` since minty-db (owner, 2026-10-28) while request paths, token claims and
JSON carry text, and ``UUID("x") == "x"`` is quietly False: a missed conversion skips a match
and never raises. With this plugin it raises instead, wherever the comparison is made by the
service's own code. Django's internals are left alone: its foreign-key setter compares an old id
with a new one only to decide whether to drop a cached object, which is harmless.

Only uuid-shaped text counts (Django also compares a lookup value with ``""``). A dict or set
keyed one way and looked up the other compares by hash, so it is invisible here - key
id-indexed collections consistently (the access-sweep and prune bugs of 2026-10-30 were that).
"""

from __future__ import annotations

import re
import sys
import uuid

_UUID_TEXT = re.compile(r"[0-9a-fA-F]{8}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{12}")
_uuid_eq = uuid.UUID.__eq__


def _strict_eq(self: uuid.UUID, other: object):
    if isinstance(other, str) and _UUID_TEXT.fullmatch(other.strip("{}")):
        caller = sys._getframe(1).f_globals.get("__name__", "")
        if not caller.startswith("django."):
            raise TypeError(f"comparing UUID {self} with the str {other!r}: convert one side first")
    return _uuid_eq(self, other)


uuid.UUID.__eq__ = _strict_eq  # type: ignore[method-assign]
