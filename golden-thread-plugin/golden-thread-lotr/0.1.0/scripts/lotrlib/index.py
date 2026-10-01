"""The search index behind `find`: BM25 over connections, curated ops and recipes.

Docs are built from the registry and the recipes, never from a remote catalog, so search is
local, deterministic and cheap. One doc per connection (the catalog), one per connection x
curated op, one per connection x applicable recipe.

Tokens: lowercase, split on anything that is not a letter or digit (so `_ . / { }` split
too), and a trailing plural "s" is folded ("pulls" ~ "pull", "issues" ~ "issue").
Ranking: BM25 (k1=1.2, b=0.75); recipes x1.5; stale docs x0.3; ties broken by key.
"""
import fnmatch
import math
import re
from collections import Counter

from . import profiles as profiles_mod
from .errors import GatewayError

K1 = 1.2
B = 0.75
RECIPE_BOOST = 1.5
STALE_FACTOR = 0.3
CATALOG_LIMIT = 200

_SPLIT = re.compile(r"[^a-z0-9]+")


def _fold(t):
    if len(t) > 3 and t.endswith("ies"):
        return t[:-3] + "y"
    if len(t) > 3 and t.endswith("s") and not t.endswith("ss"):
        return t[:-1]
    return t


def tokenize(text):
    return [_fold(t) for t in _SPLIT.split(str(text or "").lower()) if t]


def _words(name):
    """An op or recipe name as plain words ("list_pulls" -> "list pulls")."""
    return " ".join(t for t in _SPLIT.split(str(name or "").lower()) if t)


class Index:
    def __init__(self, docs):
        self.docs = sorted((dict(d) for d in docs), key=lambda d: d["key"])
        self._tf = []
        self._len = []
        df = Counter()
        for d in self.docs:
            toks = tokenize(d.get("text", ""))
            tf = Counter(toks)
            self._tf.append(tf)
            self._len.append(len(toks))
            df.update(tf.keys())
        n = len(self.docs)
        self._avgdl = (sum(self._len) / n) if n else 0.0
        self._idf = {t: math.log((n - c + 0.5) / (c + 0.5) + 1.0) for t, c in df.items()}

    def _match_conn(self, doc, connection):
        if not connection:
            return True
        cid = doc.get("connection") or ""
        if any(ch in connection for ch in "*?["):
            return fnmatch.fnmatchcase(cid, connection)
        return cid == connection

    def search(self, query, *, connection=None, limit=8):
        q = tokenize(query)
        if not q:
            hits = [dict(d, score=0.0) for d in self.docs
                    if d.get("kind") == "connection" and self._match_conn(d, connection)]
            cap = min(int(limit), CATALOG_LIMIT) if limit else CATALOG_LIMIT
            return hits[:cap]
        qtf = Counter(q)
        scored = []
        for i, d in enumerate(self.docs):
            if not self._match_conn(d, connection):
                continue
            tf, dl = self._tf[i], self._len[i]
            s = 0.0
            for t, qn in qtf.items():
                f = tf.get(t)
                if not f:
                    continue
                norm = K1 * (1 - B + B * (dl / self._avgdl if self._avgdl else 0))
                s += qn * self._idf[t] * (f * (K1 + 1)) / (f + norm)
            if s <= 0:
                continue
            if d.get("kind") == "recipe":
                s *= RECIPE_BOOST
            if d.get("stale"):
                s *= STALE_FACTOR
            scored.append((-s, d["key"], i))
        scored.sort()
        return [dict(self.docs[i], score=round(-ns, 6)) for ns, _, i in scored[:max(int(limit or 8), 0)]]


def _conn_bits(conn):
    return [conn.get("id", ""), conn.get("identity") or "", conn.get("description") or ""]


def build_docs(connections, recipes):
    """Search docs for the enabled `connections` and the `recipes` that apply to each."""
    docs = []
    for conn in connections:
        if conn.get("enabled") is False:
            continue
        cid = conn["id"]
        try:
            prof = profiles_mod.PROFILES[conn.get("profile")]
        except KeyError:
            raise GatewayError("unknown_profile", f"connection {cid} names unknown profile "
                                                  f"{conn.get('profile')!r}")
        conn_stale = bool(conn.get("stale"))
        stale_ops = set(conn.get("stale_ops") or [])
        docs.append({
            "key": cid, "connection": cid, "op": None, "kind": "connection",
            "summary": f"{conn.get('identity') or cid}: {conn.get('description') or prof['title']}",
            "text": " ".join(_conn_bits(conn) + [prof["title"], prof["name"]]),
            "profile": prof["name"], "stale": conn_stale or None,
        })
        for op in prof["ops"]:
            text = " ".join(_conn_bits(conn) + [_words(op["name"]), op["summary"],
                                                " ".join(op.get("params", {})),
                                                " ".join(op.get("tags", []))])
            d = {"key": f"{cid}:{op['name']}", "connection": cid, "op": op["name"], "kind": "op",
                 "summary": op["summary"], "text": text, "profile": prof["name"],
                 "method": op["method"], "path": op["path"],
                 "stale": (conn_stale or op["name"] in stale_ops) or None}
            if op.get("tier"):
                d["tier"] = op["tier"]
            docs.append(d)
        for r in recipes:
            if conn.get("profile") not in (r.get("profiles") or {}):
                continue
            text = " ".join(_conn_bits(conn) + [_words(r["name"]), r["summary"],
                                                " ".join(r.get("params") or {}),
                                                " ".join(r.get("tags") or [])])
            docs.append({"key": f"{cid}:{r['name']}", "connection": cid, "op": r["name"],
                         "kind": "recipe", "summary": r["summary"], "text": text,
                         "profile": prof["name"], "tier": r.get("tier", "read"),
                         "stale": (conn_stale or r["name"] in stale_ops) or None})
    for d in docs:
        if d.get("stale") is None:
            d.pop("stale", None)
    return docs
