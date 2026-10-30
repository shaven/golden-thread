import unittest
from datetime import date
from decimal import Decimal as D

from ledgerlite import aging
from ledgerlite.config import Config
from ledgerlite.invoice import Invoice, Line
from ledgerlite.ledger import Ledger

ISSUED = date(2026, 1, 1)  # default terms: due 2026-01-31


def build(prices=(("A", "Acme", "100.00"), ("B", "Birch", "250.00"))):
    cfg = Config()
    ledger = Ledger()
    for number, customer, price in prices:
        ledger.add_invoice(Invoice(number, customer, ISSUED, [Line("x", 1, price)], cfg))
    return ledger, cfg


class AgingTests(unittest.TestCase):
    def test_due_date_itself_is_not_overdue(self):
        ledger, _ = build()
        inv = ledger.invoice("A")
        self.assertFalse(aging.is_overdue(inv, ledger, date(2026, 1, 31)))
        self.assertTrue(aging.is_overdue(inv, ledger, date(2026, 2, 1)))

    def test_paid_invoice_is_not_overdue(self):
        ledger, _ = build()
        ledger.record_payment("A", "100.00", date(2026, 1, 20))
        self.assertFalse(aging.is_overdue(ledger.invoice("A"), ledger, date(2026, 3, 1)))

    def test_bucket_edges(self):
        edges = (30, 60, 90)
        self.assertEqual(aging.bucket_for(1, edges), "1-30")
        self.assertEqual(aging.bucket_for(30, edges), "1-30")
        self.assertEqual(aging.bucket_for(31, edges), "31-60")
        self.assertEqual(aging.bucket_for(90, edges), "61-90")
        self.assertEqual(aging.bucket_for(91, edges), "90+")

    def test_report_rows_sorted_oldest_first(self):
        ledger, cfg = build()
        rep = aging.aging_report(ledger, cfg, date(2026, 3, 15))
        self.assertEqual([r.number for r in rep.rows], ["A", "B"])
        self.assertEqual(rep.rows[0].days, 43)

    def test_report_uses_unpaid_balance(self):
        ledger, cfg = build()
        ledger.record_payment("B", "50.00", date(2026, 2, 1))
        rep = aging.aging_report(ledger, cfg, date(2026, 2, 10))
        self.assertEqual(rep.total, D("300.00"))
        self.assertEqual(rep.bucket_totals["1-30"], D("300.00"))

    def test_bucket_totals_split(self):
        ledger, cfg = build()
        ledger.add_invoice(Invoice("C", "Acme", date(2025, 12, 1), [Line("x", 1, "40.00")], cfg))
        rep = aging.aging_report(ledger, cfg, date(2026, 2, 10))
        self.assertEqual(rep.bucket_totals["31-60"], D("40.00"))
        self.assertEqual(rep.bucket_totals["1-30"], D("350.00"))
        self.assertEqual(rep.bucket_totals["90+"], D("0.00"))

    def test_edges_read_live_from_config(self):
        ledger, cfg = build()
        cfg.set("bucket_edges", (10, 20))
        rep = aging.aging_report(ledger, cfg, date(2026, 2, 25))
        self.assertEqual({r.bucket for r in rep.rows}, {"20+"})
        self.assertEqual(list(rep.bucket_totals), ["1-10", "11-20", "20+"])

    def test_by_customer_and_render(self):
        ledger, cfg = build()
        rep = aging.aging_report(ledger, cfg, date(2026, 2, 10))
        self.assertEqual([r.number for r in rep.by_customer("Birch")], ["B"])
        text = aging.render(rep)
        self.assertTrue(text.startswith("Aging as of 2026-02-10"))
        self.assertTrue(text.endswith("TOTAL 350.00"))


if __name__ == "__main__":
    unittest.main()
