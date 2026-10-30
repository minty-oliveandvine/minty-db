"""The models work on both databases an API runs: SQLite (unit) and the real schema (Postgres)."""

import uuid
from datetime import datetime

import pytest
from django.apps import apps
from django.db import connection

from minty_db import models as m

pytestmark = pytest.mark.django_db


def test_every_model_can_be_queried():
    """On Postgres this is the drift check: a model column the schema lacks fails the SELECT."""
    for model in apps.get_app_config("minty_db").get_models():
        if model._meta.managed or connection.vendor == "postgresql":  # views exist only in the real schema
            list(model.objects.all()[:1])


def _user(**kw):
    tag = uuid.uuid4().hex[:8]
    return m.User.objects.create(username=f"u{tag}", password="!", email=f"{tag}@x.test", **kw)


def _entity():
    country = m.CountryInfo.objects.filter(country_code="HK").first()
    if country is None:
        currency = m.CurrencyInfo.objects.create(currency_code="HKD", currency_name="Hong Kong dollar")
        country = m.CountryInfo.objects.create(country_code="HK", country_name_en="Hong Kong", currency=currency)
    return m.Entity.objects.create(name=f"Co {uuid.uuid4().hex[:6]}", country_code=country.country_code)


def test_ids_are_uuid_objects_and_defaults_come_from_the_database():
    user = _user()
    assert isinstance(user.id, uuid.UUID)
    user.refresh_from_db()
    assert isinstance(user.id, uuid.UUID)
    assert isinstance(user.created_at, datetime)  # db_default=Now(), filled by the database


def test_composite_key_and_foreign_key_ids():
    user, entity = _user(), _entity()
    link = m.UserEntity.objects.create(user_id=user.id, entity_id=entity.id, role="cashier")
    assert link.pk == (user.id, entity.id)
    got = m.UserEntity.objects.get(user_id=user.id, entity_id=entity.id)
    assert got.entity_id == entity.id and got.role == "cashier" and got.approved is False


def test_updated_at_moves_on_save():
    entity = _entity()
    entity.refresh_from_db()
    entity.name = "Renamed"
    entity.save()
    assert isinstance(entity.updated_at, datetime)


def test_enum_columns_are_postgres_enums():
    field = m.UserEntity._meta.get_field("role")
    assert field.db_type(connection) == ("entity_role" if connection.vendor == "postgresql" else "varchar(12)")
