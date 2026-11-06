"""Customer statements: a read-only view over the ledger. Every figure shown is
derived from the ledger at call time; the statement stores nothing of its own."""
from dataclasses import dataclass
from decimal import Decimal

from .money import ZERO, fmt


@dataclass(frozen=True)
class StatementLine:
    number: str
    total: Decimal
    paid: Decimal
    balance: Decimal


@dataclass(frozen=True)
class Statement:
    customer: str
    lines: tuple

    @property
    def total_billed(self):
        return sum((l.total for l in self.lines), ZERO)

    @property
    def total_paid(self):
        return sum((l.paid for l in self.lines), ZERO)

    @property
    def total_due(self):
        return sum((l.balance for l in self.lines), ZERO)


def build_statement(ledger, customer):
    lines = []
    for inv in ledger.invoices():
        if inv.customer != customer:
            continue
        lines.append(StatementLine(inv.number, inv.total(), ledger.paid(inv.number),
                                   ledger.balance(inv.number)))
    lines.sort(key=lambda l: l.number)
    return Statement(customer, tuple(lines))


def render_statement(statement):
    out = [f"Statement for {statement.customer}"]
    for l in statement.lines:
        out.append(f"{l.number:<8}{fmt(l.total):>10}{fmt(l.paid):>10}{fmt(l.balance):>10}")
    out.append(f"DUE {fmt(statement.total_due)}")
    return "\n".join(out)


def oldest_unpaid(ledger, customer):
    """Invoice number with the earliest due date among those with a balance, else None."""
    open_invoices = [i for i in ledger.invoices()
                     if i.customer == customer and ledger.balance(i.number) > 0]
    if not open_invoices:
        return None
    return min(open_invoices, key=lambda i: (i.due_date, i.number)).number
