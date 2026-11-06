"""Configuration. Consumers keep a reference to the Config and call get() when they
need a value; nothing copies values out, so a change is seen by the next call."""


class Config:
    DEFAULTS = {
        "currency": "USD",
        "tax_rate": "0",          # percent applied to the taxable base
        "terms_days": 30,         # days from issue date to due date
        "bucket_edges": (30, 60, 90),  # upper day-limits of the aging buckets
    }

    def __init__(self, **values):
        self._values = dict(values)

    def get(self, key, default=None):
        if key in self._values:
            return self._values[key]
        return self.DEFAULTS.get(key, default)

    def set(self, key, value):
        self._values[key] = value

    def unset(self, key):
        self._values.pop(key, None)

    def as_dict(self):
        merged = dict(self.DEFAULTS)
        merged.update(self._values)
        return merged
