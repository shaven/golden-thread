import unittest
from datetime import date
from decimal import Decimal as D

from ledgerlite.config import Config
from ledgerlite.invoice import Invoice, Line
from ledgerlite.ledger import Ledger, OverpaymentError

DAY = date(2026, 1, 10)


def build(tax="0"):
    cfg = Config(tax_rate=tax)
    ledger = Ledger()
    ledger.add_invoice(Invoice("A", "Acme", date(2026, 1, 1), [Line("x", 1, "100.00")], cfg))
    return ledger, cfg


class LedgerTests(unittest.TestCase):
    def test_new_invoice_owes_total(self):
        ledger, _ = build()
        self.assertEqual(ledger.balance("A"), D("100.00"))
        self.assertEqual(ledger.paid("A"), D("0.00"))

    def test_payment_reduces_balance(self):
        ledger, _ = build()
        ledger.record_payment("A", "40.00", DAY)
        self.assertEqual(ledger.balance("A"), D("60.00"))

    def test_multiple_payments_sum(self):
        ledger, _ = build()
        ledger.record_payment("A", "40.00", DAY)
        ledger.record_payment("A", "10.005", DAY)
        self.assertEqual(ledger.paid("A"), D("50.01"))

    def test_refund_restores_balance(self):
        ledger, _ = build()
        ledger.record_payment("A", "100.00", DAY)
        ledger.record_refund("A", "30.00", DAY)
        self.assertEqual(ledger.balance("A"), D("30.00"))

    def test_overpayment_rejected(self):
        ledger, _ = build()
        with self.assertRaises(OverpaymentError):
            ledger.record_payment("A", "100.01", DAY)
        self.assertEqual(ledger.entries(), [])

    def test_refund_cannot_exceed_paid(self):
        ledger, _ = build()
        ledger.record_payment("A", "10.00", DAY)
        with self.assertRaises(ValueError):
            ledger.record_refund("A", "10.01", DAY)

    def test_nonpositive_and_unknown_rejected(self):
        ledger, _ = build()
        with self.assertRaises(ValueError):
            ledger.record_payment("A", "0", DAY)
        with self.assertRaises(KeyError):
            ledger.record_payment("Z", "1.00", DAY)

    def test_balance_follows_invoice_total(self):
        ledger, cfg = build()
        ledger.record_payment("A", "50.00", DAY)
        cfg.set("tax_rate", "10")
        self.assertEqual(ledger.balance("A"), D("60.00"))


if __name__ == "__main__":
    unittest.main()
