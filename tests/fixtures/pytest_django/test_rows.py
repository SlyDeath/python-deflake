import pytest

from shop.models import Product


@pytest.mark.django_db
def test_rolled_back():
    Product.objects.create(name="tea")


@pytest.mark.django_db(transaction=True)
def test_flushed():
    Product.objects.create(name="coffee")
    assert Product.objects.count() == 1


def test_no_database():
    assert 1 + 1 == 2


def test_leaves_a_row(django_db_setup, django_db_blocker):
    with django_db_blocker.unblock():
        Product.objects.create(name="milk")
