#!/usr/bin/env python3
"""gt_model_records.py -- the ONE model record table's reader (0.22.0).

The data is gt_model_prices.json `models`: a row per (provider, model id) with its tier, prices, cache-read
multiple, context window, supported efforts, long-prompt tier, `verified` date and `known: true`. Every
script that needs a fact about a model asks here, so a model that is not Claude (a gateway route, a hosted
engine) is a new row and, at most, a new provider block: not a code change.

MATCHING, in this order, never a bare substring:
  1. normalise: lower case, drop a trailing `[...]` context suffix, drop one of the provider's `route_prefixes`;
  2. an EXACT row id or declared alias (every provider is tried before any prefix rule);
  3. the provider's `prefix_rules`, in the file's order, first match wins: [prefix, row id].
An id that matches nothing is `known: False`. `price()` then hands back FALLBACK_PRICE so a caller that must
produce a number (a cost estimate that already says it is one) has one, but it also says the price is not
known, and a report that adds up dollars shows that model under `unpriced` instead of pricing it.

Never raises: an unreadable file is an empty table, a bad row is skipped and named in `problems`.
"""
import json
import os
import re
import sys

sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__))
FILE = "gt_model_prices.json"
# The labelled fallback for a model with no row: USD per million input tokens. It is the file's `default`.
# Use it ONLY where the figure is already an estimate (the cost-of-no-gt counterfactual, a calibration plan).
FALLBACK_PRICE = 3.0
DEFAULT_WINDOW = 200000
_SUFFIX = re.compile(r"\[[^\]]*\]$")


class Table:
    def __init__(self, doc):
        doc = doc if isinstance(doc, dict) else {}
        self.fallback_price = _num(doc.get("default"), FALLBACK_PRICE)
        self.default_window = int(_num(doc.get("default_window"), DEFAULT_WINDOW))
        self.problems = []
        self.rows = []                       # in file order
        self.by_key = {}                     # (provider, id) -> row, and (provider, alias) -> row
        self.providers = {}                  # name -> {route_prefixes, prefix_rules}
        self.tier_defaults = {}              # tier -> model, cheapest first (file order)
        self._read(doc)

    def _read(self, doc):
        for name, p in (doc.get("providers") or {}).items() if isinstance(doc.get("providers"), dict) else ():
            if not isinstance(p, dict):
                continue
            rules = []
            for r in p.get("prefix_rules") or ():
                if isinstance(r, (list, tuple)) and len(r) == 2 and all(isinstance(x, str) and x for x in r):
                    rules.append((r[0].lower(), r[1].lower()))
                else:
                    self.problems.append("%s: prefix rule %r is not [prefix, row id]" % (name, r))
            self.providers[str(name).lower()] = {
                "route_prefixes": tuple(str(x).lower() for x in p.get("route_prefixes") or () if x),
                "prefix_rules": tuple(rules)}
        rows = doc.get("models")
        for row in rows if isinstance(rows, list) else ():
            if not (isinstance(row, dict) and isinstance(row.get("provider"), str) and row["provider"].strip()
                    and isinstance(row.get("id"), str) and row["id"].strip()):
                self.problems.append("a model row needs a provider and an id: %r" % (row,))
                continue
            row = dict(row, provider=row["provider"].strip().lower(), id=row["id"].strip().lower(), known=True,
                       aliases=[str(a).strip().lower() for a in row.get("aliases") or () if str(a).strip()])
            for name in [row["id"]] + row["aliases"]:
                if (row["provider"], name) in self.by_key:
                    self.problems.append("%s/%s is declared twice" % (row["provider"], name))
                    break
            else:
                self.rows.append(row)
                for name in [row["id"]] + row["aliases"]:
                    self.by_key[(row["provider"], name)] = row
        for p, spec in self.providers.items():
            seen = []
            for prefix, target in spec["prefix_rules"]:
                if (p, target) not in self.by_key:
                    self.problems.append("%s: prefix rule %r points at %r, which is not a row" % (p, prefix, target))
                for earlier, e_target in seen:
                    if prefix.startswith(earlier) and e_target != target:
                        self.problems.append("%s: prefix rule %r is shadowed by the earlier %r" % (p, prefix, earlier))
                seen.append((prefix, target))
        td = doc.get("tier_defaults")
        for tier, model in td.items() if isinstance(td, dict) else ():
            if isinstance(tier, str) and isinstance(model, str):
                self.tier_defaults[tier] = model

    # -- matching ----------------------------------------------------------------------
    def normalise(self, model, provider=None):
        """-> the id with its context suffix removed and, when `provider` is given, that provider's route prefix."""
        m = _SUFFIX.sub("", str(model or "").strip().lower())
        for rp in self.providers.get(str(provider or "").lower(), {}).get("route_prefixes", ()):
            if m.startswith(rp):
                return m[len(rp):]
        return m

    def _candidates(self, model, provider):
        if provider:
            names = [str(provider).strip().lower()]
        else:                                # the providers' own order, then any that only have rows
            names = list(self.providers) + sorted({r["provider"] for r in self.rows} - set(self.providers))
        return [(p, self.normalise(model, p)) for p in names]

    def exact(self, model, provider=None):
        """-> the row whose id or declared alias IS `model` (after normalising), else None."""
        for p, m in self._candidates(model, provider):
            row = self.by_key.get((p, m))
            if row:
                return row
        return None

    def lookup(self, model, provider=None):
        """-> the model's row (exact id or alias first, then the provider's prefix rules), else None."""
        if not str(model or "").strip():
            return None
        row = self.exact(model, provider)
        if row:
            return row
        for p, m in self._candidates(model, provider):
            for prefix, target in self.providers.get(p, {}).get("prefix_rules", ()):
                if m.startswith(prefix):
                    return self.by_key.get((p, target))
        return None

    # -- facts -------------------------------------------------------------------------
    def price(self, model, prompt_tokens=None, provider=None):
        """-> (USD per million input tokens, known?). Unknown -> (fallback_price, False). `prompt_tokens` is ONE
        request's prompt size: past a row's long_prompt threshold the multiple applies; left out, the base tier."""
        row = self.lookup(model, provider)
        p = _num(row.get("in_per_mtok") if row else None, None)
        if p is None:
            return self.fallback_price, False
        lp = row.get("long_prompt")
        if prompt_tokens is not None and isinstance(lp, dict) and str(lp.get("verified") or "").strip():
            over, mult = _num(lp.get("over_tokens"), None), _num(lp.get("multiple"), None)
            if over is not None and mult is not None and prompt_tokens > over:
                return p * mult, True
        return p, True

    def out_price(self, model, provider=None):
        """-> USD per million OUTPUT tokens, or None when the row has none recorded."""
        row = self.lookup(model, provider)
        return _num(row.get("out_per_mtok") if row else None, None)

    def cache_read(self, model, default, provider=None):
        """-> the cache-read price as a multiple of the input price: the row's, else `default`."""
        row = self.lookup(model, provider)
        v = _num(row.get("cache_read") if row else None, None)
        if row and not str(row.get("verified") or "").strip():
            v = None                         # a multiple nobody read from a provider's page is not applied
        return default if v is None else v

    def window(self, model, provider=None):
        """-> (context window in tokens, verified?); an unknown model gets the default window, unverified."""
        row = self.lookup(model, provider)
        w = row.get("window") if row else None
        if isinstance(w, dict) and _num(w.get("tokens"), None):
            return int(w["tokens"]), bool(w.get("verified"))
        return self.default_window, False

    def tier_of(self, model, provider=None):
        """-> the row's tier word (fast, balanced, deep, or whatever the table says), None when unknown."""
        row = self.lookup(model, provider)
        t = row.get("tier") if row else None
        return t if isinstance(t, str) and t else None

    def efforts(self, model, provider=None):
        """-> tuple of efforts the model accepts, () for none, None when the table does not say."""
        row = self.lookup(model, provider)
        e = row.get("efforts") if row else None
        return tuple(str(x) for x in e) if isinstance(e, list) else None

    def priors(self, model, provider=None):
        """-> the auto-clear priors measured on exactly this model (id or alias; no prefix match), else None."""
        row = self.exact(model, provider)
        p = row.get("auto_clear_priors") if row else None
        return dict(p) if isinstance(p, dict) and p else None

    def priors_table(self):
        """-> {model id or alias: priors} for every row that carries measured priors."""
        out = {}
        for row in self.rows:
            if isinstance(row.get("auto_clear_priors"), dict) and row["auto_clear_priors"]:
                for name in [row["id"]] + row["aliases"]:
                    out[name] = dict(row["auto_clear_priors"])
        return out

    def tiers(self):
        """-> the tier words in the table's order, cheapest first."""
        return tuple(self.tier_defaults)

    def tier_model(self, tier):
        """-> the default model of a tier, else None."""
        return self.tier_defaults.get(tier)

    def default_tier_models(self):
        """-> the default model of each tier, cheapest first (the calibration tier table)."""
        return tuple(self.tier_defaults.values())


def _num(v, default):
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return default
    return float(v) if isinstance(v, float) else v


def load(path=None):
    try:
        with open(path or os.path.join(HERE, FILE), encoding="utf-8") as fh:
            return Table(json.load(fh))
    except (OSError, ValueError):
        return Table({})


TABLE = load()
