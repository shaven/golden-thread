import unittest
from datetime import date
from decimal import Decimal as D

from ledgerlite.config import Config
from ledgerlite.invoice import Invoice, Line
from ledgerlite.ledger import Ledger
from ledgerlite.statement import build_statement, oldest_unpaid, render_statement


def build():
    cfg = Config(tax_rate="10")
    ledger = Ledger()
    ledger.add_invoice(Invoice("A", "Acme", date(2026, 1, 1), [Line("x", 1, "100.00")], cfg))
    ledger.add_invoice(Invoice("B", "Acme", date(2026, 2, 1), [Line("y", 2, "20.00")], cfg))
    ledger.add_invoice(Invoice("C", "Birch", date(2026, 1, 5), [Line("z", 1, "9.00")], cfg))
    return ledger


class StatementTests(unittest.TestCase):
    def test_totals_come_from_ledger(self):
        ledger = build()
        ledger.record_payment("A", "50.00", date(2026, 1, 20))
        st = build_statement(ledger, "Acme")
        self.assertEqual(st.total_billed, D("154.00"))
        self.assertEqual(st.total_paid, D("50.00"))
        self.assertEqual(st.total_due, D("104.00"))

    def test_only_that_customer(self):
        self.assertEqual([l.number for l in build_statement(build(), "Birch").lines], ["C"])

    def test_oldest_unpaid(self):
        ledger = build()
        self.assertEqual(oldest_unpaid(ledger, "Acme"), "A")
        ledger.record_payment("A", "110.00", date(2026, 1, 20))
        self.assertEqual(oldest_unpaid(ledger, "Acme"), "B")
        self.assertIsNone(oldest_unpaid(ledger, "Nobody"))


if __name__ == "__main__":
    unittest.main()
