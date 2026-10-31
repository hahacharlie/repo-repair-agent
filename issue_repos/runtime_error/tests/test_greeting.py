import unittest
from greeting import greet


class GreetingTests(unittest.TestCase):
    def test_greeting_preserves_name(self):
        self.assertEqual(greet("Ada"), "Hello, Ada")
        self.assertEqual(greet("Lin"), "Hello, Lin")
