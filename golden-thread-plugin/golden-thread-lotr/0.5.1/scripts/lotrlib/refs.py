"""Opaque references the gateway holds for the model (gt-lotr 0.5.0, gt 0.21.4).

The model is handed an id; the binding it stands for -- which connection, which operation, whose
call, until when -- never leaves the gateway. Two things in gt-lotr use this one store class:

  next-page cursors   `gtc_...`  bound to (connection, seat, op); reusable until they expire
  op references       `gtr_...`  bound to (connection, op, tier, caller); single use. Issued by
                      `find(connection, op)` (describe) and redeemed in the `op` slot of
                      call_read / call_write / call_consent

and a third thing is the same idea one layer up: gt core's single-use ACTION TOKENS (item #32,
`gtk_...`, lotrlib.action_token). A reference is deliberately built the way a token is built, so
there is one mechanism to reason about and not two:

  * an id names a record held elsewhere; the id carries nothing and proves nothing by itself;
  * the record binds action-ish things (connection, op), a caller (client, seat) and an expiry;
  * it is checked in the same order a token is -- unknown, spent, expired, wrong target, wrong
    session -- and refuses with those very codes;
  * one use spends it, and a refused attempt (wrong target, wrong caller) does not burn it;
  * its lifetime is a token's: five minutes by default, fifteen at most.

What a reference is NOT: a grant. It selects an operation; it authorises nothing. A token that a
person minted with presence is still required when `paranoid.action_tokens.lotr_*` is on, and the
policy, tier, client and consent checks all still run on the operation the reference names. The
store lives in the daemon's memory and is gone with it, like the cursors.
"""
import secrets as _pysecrets
import time
from collections import OrderedDict

DEFAULT_TTL = 300           # = gt_action_token.DEFAULT_TTL: a reference lives as long as a token
MAX_TTL = 900               # = gt_action_token.MAX_TTL
CODES = ("unknown", "spent", "expired", "wrong_target", "wrong_session")   # the order of checks
SPENT_MEMORY = 1024         # spent ids remembered so a second use says "spent", not "unknown"

_TARGET_KEYS = ("connection", "op", "tool", "tier")
_SESSION_KEYS = ("client", "seat")


class RefError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code, self.message = code, message


def ttl_from(limits, default=DEFAULT_TTL):
    """The reference lifetime a gateway.json `limits.ref_ttl_s` asks for, held to 1..MAX_TTL."""
    v = (limits or {}).get("ref_ttl_s")
    if isinstance(v, bool) or not isinstance(v, (int, float)) or v < 1:
        return default
    return int(min(v, MAX_TTL))


class RefStore:
    """prefix + url-safe random id -> binding dict + expiry. Memory only. Not thread-safe by
    itself: the engine calls it under its own lock."""

    def __init__(self, prefix, ttl=DEFAULT_TTL, cap=256, clock=None):
        self.prefix, self.ttl, self.cap = prefix, ttl, cap
        self.items = {}                          # id -> {**binding, "exp": float}
        self._spent = OrderedDict()
        self._clock = clock or time.time

    def now(self):
        return self._clock()

    def issue(self, binding, ttl=None):
        now = self.now()
        for k in [k for k, v in self.items.items() if v["exp"] <= now]:
            del self.items[k]
        while len(self.items) >= self.cap:
            del self.items[next(iter(self.items))]
        rid = self.prefix + _pysecrets.token_urlsafe(24)
        self.items[rid] = dict(binding, exp=now + (self.ttl if ttl is None else ttl))
        return rid

    def get(self, rid):
        """The live record, or RefError unknown / spent / expired."""
        e = self.items.get(rid) if isinstance(rid, str) else None
        if e is None:
            if isinstance(rid, str) and rid in self._spent:
                raise RefError("spent", "that reference was already used; each one works once")
            raise RefError("unknown", "no such reference: this gateway did not issue it to you, or "
                                      "it has been cleaned up")
        if e["exp"] <= self.now():
            raise RefError("expired", "that reference has expired; ask for a new one")
        return e

    def verify(self, rid, expect):
        """get() and then the binding: every key in `expect` must equal the record's. A mismatch on
        connection / op / tool / tier is wrong_target, on client / seat wrong_session. Spends nothing."""
        e = self.get(rid)
        for k, want in expect.items():
            if e.get(k) != want:
                if k in _SESSION_KEYS:
                    raise RefError("wrong_session", "that reference was issued to a different caller")
                raise RefError("wrong_target", "that reference is for a different %s"
                                % ("connection" if k == "connection" else "operation"))
        return e

    def claim(self, rid):
        """Spend `rid` only if it is still unspent: RefError `spent` otherwise. For a caller that
        checked it earlier (verify) and was not holding the store since, so two callers that both
        checked cannot both use it. Expiry is not asked again: it was checked."""
        if self.items.pop(rid, None) is None:
            raise RefError("spent", "that reference was already used; each one works once")
        self._remember(rid)

    def spend(self, rid):
        """Use it up. A second spend (or a verify) then says `spent`."""
        if self.items.pop(rid, None) is not None:
            self._remember(rid)

    def _remember(self, rid):
        self._spent[rid] = True
        while len(self._spent) > SPENT_MEMORY:
            self._spent.popitem(last=False)
