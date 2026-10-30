import unittest
from decimal import Decimal as D

from ledgerlite import money


class MoneyTests(unittest.TestCase):
    def test_quantize_half_up(self):
        self.assertEqual(money.quantize("2.675"), D("2.68"))
        self.assertEqual(money.quantize("0.005"), D("0.01"))

    def test_percent_of_rounds_half_up(self):
        self.assertEqual(money.percent_of("33.35", "10"), D("3.34"))

    def test_percent_of_exact(self):
        self.assertEqual(money.percent_of("200.00", "12.5"), D("25.00"))

    def test_allocate_distributes_leftover_cents_first(self):
        self.assertEqual(money.allocate("100.00", 3), [D("33.34"), D("33.33"), D("33.33")])

    def test_allocate_sums_to_total(self):
        self.assertEqual(sum(money.allocate("10.01", 4)), D("10.01"))

    def test_allocate_rejects_bad_input(self):
        with self.assertRaises(ValueError):
            money.allocate("-1.00", 2)
        with self.assertRaises(ValueError):
            money.allocate("1.00", 0)


if __name__ == "__main__":
    unittest.main()
