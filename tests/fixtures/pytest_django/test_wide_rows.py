import pytest

from shop.models import Product


@pytest.fixture(scope="module")
def catalog(django_db_setup, django_db_blocker):
    with django_db_blocker.unblock():
        Product.objects.create(name="milk")


@pytest.mark.django_db
def test_reads(catalog):
    assert Product.objects.count() == 1


def test_plain():
    assert 1 + 1 == 2
