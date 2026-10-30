"""Field types the schema needs beyond Django's own - one copy for every API.

Before minty-db each API kept its own ``shared_models/fields.py``; these are those, merged.
Ids are Django's own ``UUIDField`` (Python ``uuid.UUID`` values; owner, 2026-10-28), so the
text-returning ``MintyUUIDField`` two of the copies had is gone.
"""

from django.db import models
from django.db.models.expressions import Combinable
from django.db.models.functions import Now


class CharNField(models.CharField):
    """``char(n)`` in Postgres (``country_code CHAR(2)``, ``currency_code CHAR(3)``); an ordinary
    CharField elsewhere. The schema audit maps it to ``character``."""

    def db_type(self, connection):
        if connection.vendor == "postgresql":
            return f"char({self.max_length})"
        return super().db_type(connection)


class PgEnumField(models.CharField):
    """A column of a Postgres enum type; the first argument names the type.

    At runtime it is a CharField: the driver sends text and Postgres casts it to the enum,
    refusing an unknown word. ``db_type`` names the enum, so a migration that adds such a
    column creates an enum column rather than a varchar. Elsewhere (SQLite unit tests) it
    is a varchar.
    """

    def __init__(self, pg_type: str, *args, **kwargs):
        self.pg_type = pg_type
        kwargs.setdefault("max_length", 40)
        super().__init__(*args, **kwargs)

    def deconstruct(self):
        name, path, args, kwargs = super().deconstruct()
        return name, path, [self.pg_type, *args], kwargs

    def db_type(self, connection):
        if connection.vendor == "postgresql":
            return self.pg_type
        return super().db_type(connection)


class UpdatedAtMixin(models.Model):
    """``updated_at`` that the DATABASE moves on every update.

    In the schema these tables carry a BEFORE UPDATE trigger (``set_updated_at``) that sets
    ``NEW.updated_at = now()`` whatever the application wrote. This mixin is that trigger's
    mirror for SQLite, which has none: ``save()`` on an update writes ``Now()`` and refreshes
    the attribute from the row, so callers never hold the expression object. Inside a Postgres
    test transaction ``now()`` is the transaction START, so a test may assert that the stamp
    moved, never that it moved forward. Bulk ``.update()`` calls pass ``updated_at=Now()``
    themselves (the ORM does not route them through ``save()``).
    """

    updated_at = models.DateTimeField(db_default=Now())

    class Meta:
        abstract = True

    def save(self, *args, **kwargs):
        if not self._state.adding:
            self.updated_at = Now()
            update_fields = kwargs.get("update_fields")
            if update_fields is not None and "updated_at" not in update_fields:
                kwargs["update_fields"] = [*update_fields, "updated_at"]
        super().save(*args, **kwargs)
        if isinstance(self.updated_at, Combinable):
            self.refresh_from_db(fields=["updated_at"])
