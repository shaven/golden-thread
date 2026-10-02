"""The one error type that crosses module boundaries in gt-lotr.

A GatewayError carries a stable `code` (callers and tests match on it), a human message, and
optional hints that tell the model what to try next. Messages must never contain a secret
value: name the ref, never what it resolves to.
"""


class GatewayError(Exception):
    def __init__(self, code, message, hints=None):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.hints = list(hints or [])

    def to_dict(self):
        return {"code": self.code, "message": self.message, "hints": self.hints}
