"""minty_db.testing.uuid_guard: a UUID compared with uuid text raises in a service's code only."""

import uuid

import pytest

import minty_db.testing.uuid_guard  # noqa: F401  (installs the guard)

pytestmark = pytest.mark.django_db


def test_comparing_a_uuid_with_its_text_raises_both_ways():
    u = uuid.uuid4()
    for compare in (lambda: u == str(u), lambda: str(u) == u, lambda: str(u) in [u], lambda: u != str(u)):
        with pytest.raises(TypeError, match="convert one side first"):
            compare()


def test_other_comparisons_are_untouched():
    u = uuid.uuid4()
    assert u == uuid.UUID(str(u)) and hash(u) == hash(uuid.UUID(str(u)))
    assert (u == "") is False and (u == "not an id") is False


def test_django_setting_a_foreign_key_id_from_text_is_left_alone():
    from minty_db.models import CountryInfo, CurrencyInfo

    currency = CurrencyInfo.objects.create(currency_code="HKD", currency_name="Hong Kong dollar")
    country = CountryInfo.objects.create(country_code="HK", country_name_en="Hong Kong", currency=currency)
    country.currency_id = str(currency.id)  # Django compares old and new inside its own setter
    country.save()
    country.refresh_from_db()
    assert country.currency_id == currency.id
