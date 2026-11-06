"""Invoices and their lines. Order of operations per line: gross, then discount, then
net; tax is charged on the net of taxable lines only. An invoice never stores what has
been paid: that lives in the ledger."""
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal

from .money import ZERO, percent_of, quantize, to_decimal


@dataclass(frozen=True)
class Line:
    description: str
    qty: Decimal
    unit_price: Decimal
    discount_pct: Decimal = Decimal("0")
    taxable: bool = True

    def __post_init__(self):
        for name in ("qty", "unit_price", "discount_pct"):
            object.__setattr__(self, name, to_decimal(getattr(self, name)))
        if self.qty <= 0:
            raise ValueError("qty must be positive")
        if not Decimal("0") <= self.discount_pct <= Decimal("100"):
            raise ValueError("discount_pct must be between 0 and 100")

    def gross(self):
        return quantize(self.qty * self.unit_price)

    def discount(self):
        return percent_of(self.gross(), self.discount_pct)

    def net(self):
        return self.gross() - self.discount()


class Invoice:
    def __init__(self, number, customer, issued, lines, config):
        if not lines:
            raise ValueError("an invoice needs at least one line")
        self.number = number
        self.customer = customer
        self.issued = issued
        self.lines = tuple(lines)
        self.config = config

    def subtotal(self):
        return sum((l.net() for l in self.lines), ZERO)

    def taxable_base(self):
        return sum((l.net() for l in self.lines if l.taxable), ZERO)

    def tax(self):
        return percent_of(self.taxable_base(), self.config.get("tax_rate"))

    def total(self):
        return self.subtotal() + self.tax()

    @property
    def due_date(self):
        return self.issued + timedelta(days=int(self.config.get("terms_days")))
