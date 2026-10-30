# ledgerlite

A small invoicing and payments ledger in plain Python 3 (stdlib only).

## Layout

| Path | Role |
|---|---|
| `ledgerlite/money.py` | Decimal helpers: `quantize`, `percent_of`, `allocate`, `fmt` |
| `ledgerlite/config.py` | `Config` object with defaults; `get()` at use time |
| `ledgerlite/invoice.py` | `Line` (gross, discount, net) and `Invoice` (subtotal, tax, total, due date) |
| `ledgerlite/ledger.py` | `Ledger`: payments and refunds, `paid()` and `balance()` |
| `ledgerlite/aging.py` | Overdue test, buckets, `aging_report`, text `render` |
| `ledgerlite/statement.py` | Per-customer statement view |
| `tests/` | unittest suite, one `test_*.py` per module |

## Conventions

- Money is `Decimal`, never float. Round only through `money.quantize`/`percent_of`
  (half-up to cents). No bare `round()`.
- One source of truth: the ledger owns what has been paid. Derive balances from it;
  do not keep a second running total or a cached balance anywhere.
- Config is read live: hold the `Config` object and call `config.get(...)` where the
  value is used. Do not copy config values into attributes at construction.
- Dates are always passed in (`as_of`, `on`). Never call `date.today()` or read the clock.
- Each module opens with a docstring stating its rules; keep it true when behaviour changes.
- New behaviour comes with tests in `tests/`.

## Run the tests

    python3 -m unittest discover -s tests -t .

## Landmines

- Order of operations on a line is gross, then discount, then net; tax applies to net.
- The due date itself is not overdue; the day after is.
- `Decimal` sums need a `ZERO` start value, or an empty sum returns int `0`.
