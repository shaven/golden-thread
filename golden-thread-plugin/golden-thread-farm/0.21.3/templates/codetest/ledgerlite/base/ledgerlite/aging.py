"""Aging report: unpaid balances of overdue invoices, grouped by days past due.
An invoice is overdue only after its due date (the due date itself is not late), and
only while the ledger shows a balance. Bucket edges come from config at report time."""
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from .money import ZERO, fmt


@dataclass(frozen=True)
class AgingRow:
    number: str
    customer: str
    due: date
    days: int
    balance: Decimal
    bucket: str


def days_overdue(invoice, as_of):
    return (as_of - invoice.due_date).days


def is_overdue(invoice, ledger, as_of):
    return ledger.balance(invoice.number) > 0 and as_of > invoice.due_date


def bucket_labels(edges):
    labels, lo = [], 1
    for edge in edges:
        labels.append(f"{lo}-{edge}")
        lo = edge + 1
    labels.append(f"{edges[-1]}+")
    return labels


def bucket_for(days, edges):
    labels = bucket_labels(edges)
    for label, edge in zip(labels, edges):
        if days <= edge:
            return label
    return labels[-1]


class AgingReport:
    def __init__(self, as_of, rows, edges):
        self.as_of = as_of
        self.rows = list(rows)
        self.edges = tuple(edges)

    @property
    def total(self):
        return sum((r.balance for r in self.rows), ZERO)

    @property
    def bucket_totals(self):
        totals = {label: ZERO for label in bucket_labels(self.edges)}
        for r in self.rows:
            totals[r.bucket] += r.balance
        return totals

    def by_customer(self, customer):
        return [r for r in self.rows if r.customer == customer]


def aging_report(ledger, config, as_of):
    edges = tuple(config.get("bucket_edges"))
    rows = []
    for inv in ledger.invoices():
        if is_overdue(inv, ledger, as_of):
            days = days_overdue(inv, as_of)
            rows.append(AgingRow(inv.number, inv.customer, inv.due_date, days,
                                 ledger.balance(inv.number), bucket_for(days, edges)))
    rows.sort(key=lambda r: (-r.days, r.number))
    return AgingReport(as_of, rows, edges)


def render(report):
    lines = [f"Aging as of {report.as_of.isoformat()}"]
    for r in report.rows:
        lines.append(f"{r.number:<8}{r.customer:<12}{r.days:>4}d {fmt(r.balance):>10}  {r.bucket}")
    lines.append(f"TOTAL {fmt(report.total)}")
    return "\n".join(lines)
