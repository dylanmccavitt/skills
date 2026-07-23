import unittest

from counter import Counter


class CounterTests(unittest.TestCase):
    def test_increment_from_zero(self):
        counter = Counter()
        self.assertEqual(counter.increment(), 1)


if __name__ == "__main__":
    unittest.main()
