import unittest
from datetime import date
from decimal import Decimal as D

from ledgerlite.config import Config
from ledgerlite.invoice import Invoice, Line

ISSUED = date(2026, 1, 1)


def mixed(config):
    return Invoice("INV-1", "Acme", ISSUED, [
        Line("widgets", 2, "50.00", discount_pct=10),
        Line("shipping", 1, "25.00", taxable=False),
    ], config)


class LineTests(unittest.TestCase):
    def test_gross(self):
        self.assertEqual(Line("a", 3, "19.99").gross(), D("59.97"))

    def test_discount_rounds_half_up(self):
        self.assertEqual(Line("a", 3, "19.99", discount_pct=15).discount(), D("9.00"))

    def test_net(self):
        self.assertEqual(Line("a", 3, "19.99", discount_pct=15).net(), D("50.97"))

    def test_rejects_bad_values(self):
        with self.assertRaises(ValueError):
            Line("a", 0, "1.00")
        with self.assertRaises(ValueError):
            Line("a", 1, "1.00", discount_pct=101)


class InvoiceTests(unittest.TestCase):
    def test_subtotal(self):
        self.assertEqual(mixed(Config()).subtotal(), D("115.00"))

    def test_tax_only_on_discounted_taxable_lines(self):
        inv = mixed(Config(tax_rate="8.25"))
        self.assertEqual(inv.taxable_base(), D("90.00"))
        self.assertEqual(inv.tax(), D("7.43"))

    def test_config_is_read_live(self):
        cfg = Config(tax_rate="10")
        inv = mixed(cfg)
        self.assertEqual(inv.tax(), D("9.00"))
        cfg.set("tax_rate", "5")
        self.assertEqual(inv.tax(), D("4.50"))

    def test_due_date_uses_terms(self):
        cfg = Config(terms_days=15)
        self.assertEqual(mixed(cfg).due_date, date(2026, 1, 16))
        cfg.set("terms_days", 45)
        self.assertEqual(mixed(cfg).due_date, date(2026, 2, 15))


if __name__ == "__main__":
    unittest.main()
