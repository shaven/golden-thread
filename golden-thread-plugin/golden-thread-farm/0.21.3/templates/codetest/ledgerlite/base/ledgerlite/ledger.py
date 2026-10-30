"""The payment ledger: the single source of truth for what has been paid.
Balances are always derived from entries; nothing else keeps a running total."""
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from .money import ZERO, quantize


class OverpaymentError(ValueError):
    pass


@dataclass(frozen=True)
class Entry:
    invoice_number: str
    kind: str  # "payment" or "refund"
    amount: Decimal
    on: date
    note: str = ""


class Ledger:
    def __init__(self):
        self._invoices = {}
        self._entries = []

    def add_invoice(self, invoice):
        if invoice.number in self._invoices:
            raise ValueError(f"duplicate invoice {invoice.number}")
        self._invoices[invoice.number] = invoice

    def invoice(self, number):
        return self._invoices[number]

    def invoices(self):
        return list(self._invoices.values())

    def _record(self, number, kind, amount, on, note):
        self.invoice(number)  # KeyError for unknown invoices
        amount = quantize(amount)
        if amount <= 0:
            raise ValueError("amount must be positive")
        self._entries.append(Entry(number, kind, amount, on, note))

    def record_payment(self, number, amount, on, note=""):
        if quantize(amount) > self.balance(number):
            raise OverpaymentError(f"payment exceeds balance on {number}")
        self._record(number, "payment", amount, on, note)

    def record_refund(self, number, amount, on, note=""):
        if quantize(amount) > self.paid(number):
            raise ValueError(f"refund exceeds amount paid on {number}")
        self._record(number, "refund", amount, on, note)

    def entries(self, number=None):
        return [e for e in self._entries if number is None or e.invoice_number == number]

    def paid(self, number):
        total = ZERO
        for e in self.entries(number):
            total += e.amount if e.kind == "payment" else -e.amount
        return total

    def balance(self, number):
        return self.invoice(number).total() - self.paid(number)
