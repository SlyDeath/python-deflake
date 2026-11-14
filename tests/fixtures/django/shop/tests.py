from django.core.cache import cache
from django.test import SimpleTestCase, TestCase, TransactionTestCase

from shop.models import Product


class CacheTest(SimpleTestCase):
    def test_pollutes(self):
        cache.set("discount", 50)

    def test_victim(self):
        self.assertIsNone(cache.get("discount"))

    def test_unrelated(self):
        self.assertEqual(1 + 1, 2)


class ProductTest(TestCase):
    def test_creates(self):
        Product.objects.create(name="tea")
        self.assertEqual(Product.objects.count(), 1)


class LeakyRowsTest(TransactionTestCase):
    def test_leaves_a_row(self):
        Product.objects.create(name="coffee")
