"""Result shaping: projection, noise removal, list and size caps, credential scanning.

Everything a connection returns passes through here before it reaches a model, so the
result is small enough to be useful and never carries a credential-shaped string.
`scan_credentials` reports WHERE a credential-shaped string sits and which rule matched,
never the matched text itself.
"""
import json
import re

# Keys dropped from every result regardless of profile.
ALWAYS_NOISE = frozenset({"node_id", "avatar_url", "_links", "self"})
KEPT_URL_KEYS = frozenset({"html_url"})

CREDENTIAL_RULES = (
    ("github_pat", re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{30,}")),
    ("github_pat", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}")),
    ("slack_token", re.compile(r"\bxox[abpors]-[A-Za-z0-9-]{10,}")),
    ("aws_access_key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("private_key", re.compile(r"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY-----")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]+\.eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]*")),
    ("bearer_token", re.compile(r"(?i:\bbearer)\s+[A-Za-z0-9._~+/=-]{20,}")),
    ("atlassian_token", re.compile(r"\bATATT[A-Za-z0-9_=+/-]{20,}")),
)


# -- projection -----------------------------------------------------------------------

def _parse_select(select):
    """'a.b,c[].d' -> trie {key: {"list": bool, "children": trie | None}}."""
    trie = {}
    for raw in str(select).split(","):
        path = raw.strip()
        if not path:
            continue
        node = trie
        segs = [s for s in path.split(".") if s]
        for i, seg in enumerate(segs):
            is_list = seg.endswith("[]")
            key = seg[:-2] if is_list else seg
            leaf = i == len(segs) - 1
            entry = node.get(key)
            if entry is None:
                entry = {"list": is_list, "children": None if leaf else {}}
                node[key] = entry
            else:
                entry["list"] = entry["list"] or is_list
                if leaf:
                    entry["children"] = None          # the whole value wins over a sub-path
                elif entry["children"] is None:
                    break                             # already taking the whole value
            if leaf:
                break
            node = entry["children"]
    return trie


_MISSING = object()


def _apply(obj, trie):
    if isinstance(obj, list):
        return [_apply(e, trie) for e in obj]
    if not isinstance(obj, dict):
        return _MISSING
    out = {}
    for key, entry in trie.items():
        if key not in obj:
            continue
        val = obj[key]
        if entry["children"] is None:
            out[key] = val
            continue
        if isinstance(val, list):
            items = [_apply(e, entry["children"]) for e in val]
            out[key] = [({} if i is _MISSING else i) for i in items]
        else:
            sub = _apply(val, entry["children"])
            if sub is not _MISSING and sub != {}:
                out[key] = sub
    return out


def project(data, select):
    """Keep only the comma-separated dotted paths in `select`.

    `a[].b` maps over the list at `a`; a top-level list applies each path to its elements;
    missing paths are omitted. A list met on a path without `[]` is mapped over too.
    """
    if not select or not str(select).strip():
        return data
    trie = _parse_select(select)
    if not trie:
        return data
    res = _apply(data, trie)
    if res is _MISSING:
        return data
    if isinstance(res, list):
        return [({} if r is _MISSING else r) for r in res]
    return res


# -- shaping --------------------------------------------------------------------------

def _is_noise(key, noise):
    if not isinstance(key, str):
        return False
    if key in noise or key in ALWAYS_NOISE:
        return True
    return key.endswith("_url") and key not in KEPT_URL_KEYS


def _drop_noise(obj, noise):
    if isinstance(obj, dict):
        return {k: _drop_noise(v, noise) for k, v in obj.items() if not _is_noise(k, noise)}
    if isinstance(obj, list):
        return [_drop_noise(v, noise) for v in obj]
    return obj


def _truncate_lists(obj, limit, path, cuts):
    if isinstance(obj, list):
        if limit and len(obj) > limit:
            slot = cuts.setdefault(path or "(top level)", [0, 0, 0])
            slot[0] += 1
            slot[1] += len(obj)
            slot[2] += len(obj) - limit
            obj = obj[:limit]
        return [_truncate_lists(v, limit, path + "[]", cuts) for v in obj]
    if isinstance(obj, dict):
        return {k: _truncate_lists(v, limit, f"{path}.{k}" if path else str(k), cuts)
                for k, v in obj.items()}
    return obj


def _size(obj):
    return len(json.dumps(obj, ensure_ascii=False, default=str))


def _largest_list(obj):
    """(container, key) of the biggest list directly under a top-level dict, or None."""
    best, best_size = None, -1
    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(v, list) and len(v) > 1:
                s = _size(v)
                if s > best_size:
                    best, best_size = k, s
    return best


def _fit(data, max_chars):
    """Shrink `data` until it serializes within max_chars. Returns (data, how)."""
    if isinstance(data, list):
        lo, hi = 0, len(data)
        while lo < hi:                     # largest n with data[:n] fitting
            mid = (lo + hi + 1) // 2
            if _size(data[:mid]) <= max_chars:
                lo = mid
            else:
                hi = mid - 1
        if lo > 0:
            return data[:lo], f"kept the first {lo} of {len(data)} items"
    key = _largest_list(data)
    if key is not None:
        items = data[key]
        lo, hi = 0, len(items)
        while lo < hi:
            mid = (lo + hi + 1) // 2
            trial = dict(data)
            trial[key] = items[:mid]
            if _size(trial) <= max_chars:
                lo = mid
            else:
                hi = mid - 1
        if lo > 0:
            trial = dict(data)
            trial[key] = items[:lo]
            return trial, f"kept the first {lo} of {len(items)} items of '{key}'"
    text = data if isinstance(data, str) else json.dumps(data, ensure_ascii=False, default=str)
    return text[:max_chars], f"cut to the first {max_chars} characters of the serialized result"


def shape(data, *, noise_keys=(), select=None, max_chars=24000, default_page=20,
          has_cursor=False):
    """Project, de-noise and cap a result. Returns (data, notes).

    An explicit `select` is the caller's choice of fields, so noise removal is skipped for it:
    asking for `x.clone_url` returns it.
    """
    notes = []
    if select:
        data = project(data, select)
    else:
        data = _drop_noise(data, frozenset(noise_keys or ()))
    if default_page:
        cuts = {}
        data = _truncate_lists(data, int(default_page), "", cuts)
        for path, (n, total, cut) in cuts.items():
            where = "the result" if path == "(top level)" else f"'{path}'"
            counts = (f"showing {default_page} of {total}; {cut} of {total} cut"
                      + (f" across {n} lists" if n > 1 else ""))
            if has_cursor:
                how = "use next_cursor for the next page, or a smaller limit or a select"
            else:   # no cursor exists: this list came back whole, so there is nothing to page
                how = (f"{cut} more items were cut and there is no cursor for this result; "
                       "narrow the query (filters, $top/date range) or fetch with a smaller "
                       "page size")
            notes.append(f"list {where} truncated to {default_page} items ({counts}); {how}")
    if max_chars and _size(data) > max_chars:
        before = _size(data)
        data, how = _fit(data, int(max_chars))
        notes.append(f"result was {before} characters, over the {max_chars} cap; {how}. "
                     "Narrow it with select (e.g. select='items[].id,items[].title') "
                     "or ask for fewer items with a limit / page-size argument")
    return data, notes


# -- credential scanning ---------------------------------------------------------------

def _scan_text(text, path, out):
    for rule, rx in CREDENTIAL_RULES:
        for m in rx.finditer(text):
            out.append({"path": path, "rule": rule, "length": len(m.group(0))})


def _walk(obj, path, out):
    if isinstance(obj, str):
        _scan_text(obj, path, out)
    elif isinstance(obj, dict):
        for k, v in obj.items():
            sub = f"{path}.{k}" if path else str(k)
            if isinstance(k, str):
                _scan_text(k, sub, out)
            _walk(v, sub, out)
    elif isinstance(obj, (list, tuple)):
        for i, v in enumerate(obj):
            _walk(v, f"{path}[{i}]", out)


def scan_credentials(obj):
    """Credential-shaped strings in `obj`: [{"path", "rule", "length"}]. Never the text."""
    out = []
    _walk(obj, "", out)
    return out
