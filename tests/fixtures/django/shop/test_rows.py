import unittest

from django.test import TestCase

from shop.models import Product


class RolledBackTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        Product.objects.create(name="tea")

    def test_creates(self):
        Product.objects.create(name="milk")
        self.assertEqual(Product.objects.count(), 2)


class PlainTest(unittest.TestCase):
    def test_leaves_a_row(self):
        Product.objects.create(name="coffee")
