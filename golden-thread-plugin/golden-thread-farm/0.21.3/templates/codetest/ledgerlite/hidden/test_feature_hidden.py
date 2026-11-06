import unittest
from datetime import date
from decimal import Decimal as D

from ledgerlite import aging
from ledgerlite.config import Config
from ledgerlite.invoice import Invoice, Line
from ledgerlite.ledger import Ledger

ISSUED = date(2026, 1, 1)   # default terms: due 2026-01-31
LATE = date(2026, 2, 15)    # 15 days overdue


def build(pct, prices=(("A", "200.00"),)):
    cfg = Config() if pct is False else Config(late_fee_pct=pct)
    ledger = Ledger()
    for number, price in prices:
        ledger.add_invoice(Invoice(number, "Acme", ISSUED, [Line("x", 1, price)], cfg))
    return ledger, cfg


class LateFeeAcceptance(unittest.TestCase):
    def test_key_absent_means_no_fees_and_same_report(self):
        ledger, cfg = build(False)
        rep = aging.aging_report(ledger, cfg, LATE)
        self.assertEqual(list(rep.fees), [])
        self.assertEqual(rep.fee_total, D("0.00"))
        self.assertEqual(rep.total, D("200.00"))

    def test_none_means_no_fees(self):
        ledger, cfg = build(None)
        rep = aging.aging_report(ledger, cfg, LATE)
        self.assertEqual(list(rep.fees), [])
        self.assertEqual(rep.fee_total, D("0.00"))

    def test_basic_fee_and_exact_note(self):
        ledger, cfg = build("1.5")
        rep = aging.aging_report(ledger, cfg, LATE)
        self.assertEqual(len(rep.fees), 1)
        fee = rep.fees[0]
        self.assertEqual(fee.number, "A")
        self.assertEqual(fee.amount, D("3.00"))
        self.assertEqual(fee.note, "Late fee 1.5% on 200.00")
        self.assertEqual(rep.fee_total, D("3.00"))

    def test_partial_payment_reduces_the_base(self):
        ledger, cfg = build("1.5")
        ledger.record_payment("A", "50.00", date(2026, 2, 1))
        fee = aging.aging_report(ledger, cfg, LATE).fees[0]
        self.assertEqual(fee.amount, D("2.25"))
        self.assertEqual(fee.note, "Late fee 1.5% on 150.00")

    def test_fully_paid_invoice_is_exempt(self):
        ledger, cfg = build("5")
        ledger.record_payment("A", "200.00", date(2026, 2, 1))
        rep = aging.aging_report(ledger, cfg, LATE)
        self.assertEqual(list(rep.fees), [])
        self.assertEqual(rep.fee_total, D("0.00"))

    def test_due_date_itself_is_exempt_next_day_is_charged(self):
        ledger, cfg = build("2")
        self.assertEqual(list(aging.aging_report(ledger, cfg, date(2026, 1, 31)).fees), [])
        fee = aging.aging_report(ledger, cfg, date(2026, 2, 1)).fees[0]
        self.assertEqual(fee.amount, D("4.00"))

    def test_rounds_half_up_to_cents(self):
        ledger, cfg = build("0.05", prices=(("A", "10.00"),))
        fee = aging.aging_report(ledger, cfg, LATE).fees[0]
        self.assertEqual(fee.amount, D("0.01"))
        self.assertEqual(fee.note, "Late fee 0.05% on 10.00")

    def test_applied_once_per_invoice_per_report_and_ledger_untouched(self):
        ledger, cfg = build("1.5")
        first = aging.aging_report(ledger, cfg, LATE)
        second = aging.aging_report(ledger, cfg, LATE)
        self.assertEqual([f.amount for f in first.fees], [D("3.00")])
        self.assertEqual([f.amount for f in second.fees], [D("3.00")])
        self.assertEqual(ledger.balance("A"), D("200.00"))
        self.assertEqual(ledger.entries(), [])

    def test_existing_report_figures_unchanged_when_fee_enabled(self):
        plain, cfg0 = build(False, prices=(("A", "200.00"), ("B", "80.00")))
        fee_l, cfg1 = build("3", prices=(("A", "200.00"), ("B", "80.00")))
        r0 = aging.aging_report(plain, cfg0, LATE)
        r1 = aging.aging_report(fee_l, cfg1, LATE)
        self.assertEqual(r0.total, r1.total)
        self.assertEqual([(r.number, r.balance, r.bucket) for r in r0.rows],
                         [(r.number, r.balance, r.bucket) for r in r1.rows])

    def test_multiple_invoices_follow_row_order_and_sum(self):
        ledger, cfg = build("2", prices=(("B", "80.00"), ("A", "200.00")))
        ledger.add_invoice(Invoice("C", "Acme", date(2026, 2, 10), [Line("x", 1, "50.00")], cfg))
        rep = aging.aging_report(ledger, cfg, LATE)  # C is not yet due
        self.assertEqual([f.number for f in rep.fees], [r.number for r in rep.rows])
        self.assertEqual([f.number for f in rep.fees], ["A", "B"])
        self.assertEqual(rep.fee_total, D("5.60"))


if __name__ == "__main__":
    unittest.main()
