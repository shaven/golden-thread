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

# 0.22.0 validation F4: the shapes the Stop validator knows (golden-thread gt_secret_shapes.
# HIGH_CONFIDENCE), copied here because this module is a separate plugin. A test compares the two
# lists, so they cannot drift. A shape an earlier rule already reported at the same place is
# not reported twice.
SHARED_SHAPES = (
    ("Google OAuth client secret", re.compile(r'GOCSPX-[A-Za-z0-9_-]{20,}')),
    ("Google access token",        re.compile(r'\bya29\.[A-Za-z0-9_-]{30,}')),
    ("Google refresh token",       re.compile(r'\b1//0[A-Za-z0-9_-]{30,}')),
    ("Google API key",             re.compile(r'\bAIza[0-9A-Za-z_-]{35}\b')),
    ("AWS access key id",          re.compile(r'\bAKIA[0-9A-Z]{16}\b')),
    ("GitHub token",               re.compile(r'\bgh[pousr]_[A-Za-z0-9]{30,}')),
    ("GitHub fine-grained token",  re.compile(r'\bgithub_pat_[A-Za-z0-9_]{22,}')),
    ("Anthropic API key",          re.compile(r'\bsk-ant-[A-Za-z0-9_-]{20,}')),
    ("OpenAI API key",             re.compile(r'\bsk-(?:proj|svcacct|admin)-[A-Za-z0-9_-]{20,}')),
    ("Slack token",                re.compile(r'\bxox[abprse]-[A-Za-z0-9-]{12,}')),
    ("Stripe secret key",          re.compile(r'\b[sr]k_live_[A-Za-z0-9]{16,}')),
    ("npm token",                  re.compile(r'\bnpm_[A-Za-z0-9]{36}\b')),
    ("JSON web token",             re.compile(r'\beyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}')),
    ("private key block",          re.compile(r'-----BEGIN [A-Z ]*PRIVATE KEY-----')),
)

# A shape only the gateway adds: a password inside a connection URL
# (scheme://user:password@host, a `<placeholder>` or `$VAR` password excepted).
EXTRA_SHAPES = (
    ("url_password", re.compile(r"\b[A-Za-z][A-Za-z0-9+.-]*://[^\s/:@]+:(?![$<{])[^\s/@]{3,}@")),
)

# A credential-named field with a literal value: `"password":"..."`, `password=...`, or a dict key.
_FIELD_WORDS = (r"(?:[\w-]*(?:pass(?:word|wd)?|pwd|secret|credential|"
                r"(?:api|access|private|auth|signing|encryption|client)[_-]?(?:key|token))|token)")
_FIELD_TEXT = re.compile(r"(?<![\w])" + _FIELD_WORDS + r"[\"']?\s*[=:]\s*[\"']?([^\s\"'&<>,}]{6,})", re.I)
_FIELD_KEY = re.compile(r"^" + _FIELD_WORDS + r"$", re.I)
_PLACEHOLDER = re.compile(
    r"^[\s\"'`]*(?:[$<{]|(?:x+|\.+|\*+|_+)[\s\"'`]*$|"
    r"(?:redacted|change-?me|example|placeholder|dummy|fake|sample|none|null|secret|password|token)"
    r"[\s\"'`]*$)", re.I)
_LEADING = re.compile(r"^[\s\"'`]*(?:your[-_]|my[-_]|test|example|dummy|fake|sample)", re.I)


def _is_placeholder(val):
    if not val or _PLACEHOLDER.match(val):
        return True
    if _LEADING.match(val):
        return len(val) <= 10 or not any(c.isdigit() for c in val)
    return False


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


def shape(data, **kw):
    """Project, de-noise and cap a result. Returns (data, notes); see shape_info."""
    data, notes, _ = shape_info(data, **kw)
    return data, notes


def shape_info(data, *, noise_keys=(), select=None, max_chars=24000, default_page=20,
               has_cursor=False):
    """Project, de-noise and cap a result. Returns (data, notes, info).

    `info` is {} when nothing was cut. When a list was paged or the size cap cut anything it is
    {"truncated": True, "original_chars": N}: N is the serialized length of the result after
    projection and noise removal, before any cut (0.5.1), so a caller can tell a short answer from
    a clipped one.

    An explicit `select` is the caller's choice of fields, so noise removal is skipped for it:
    asking for `x.clone_url` returns it.
    """
    notes = []
    info = {}
    if select:
        data = project(data, select)
    else:
        data = _drop_noise(data, frozenset(noise_keys or ()))
    uncut = data
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
    if notes:                                   # every note above says something was cut
        info = {"truncated": True, "original_chars": _size(uncut)}
    return data, notes, info


# -- credential scanning ---------------------------------------------------------------

def _scan_text(text, path, out):
    spans = []
    for rule, rx in CREDENTIAL_RULES + SHARED_SHAPES + EXTRA_SHAPES:
        for m in rx.finditer(text):
            if any(m.start() < b and a < m.end() for a, b in spans):
                continue
            spans.append((m.start(), m.end()))
            out.append({"path": path, "rule": rule, "length": len(m.group(0))})
    for m in _FIELD_TEXT.finditer(text):
        if not _is_placeholder(m.group(1)) and not any(m.start(1) < b and a < m.end(1) for a, b in spans):
            out.append({"path": path, "rule": "credential_field", "length": len(m.group(1))})


def _walk(obj, path, out):
    if isinstance(obj, str):
        _scan_text(obj, path, out)
    elif isinstance(obj, dict):
        for k, v in obj.items():
            sub = f"{path}.{k}" if path else str(k)
            if isinstance(k, str):
                _scan_text(k, sub, out)
                if isinstance(v, str) and _FIELD_KEY.match(k) and len(v) >= 6 \
                        and not _is_placeholder(v):
                    out.append({"path": sub, "rule": "credential_field", "length": len(v)})
            _walk(v, sub, out)
    elif isinstance(obj, (list, tuple)):
        for i, v in enumerate(obj):
            _walk(v, f"{path}[{i}]", out)


def scan_credentials(obj):
    """Credential-shaped strings in `obj`: [{"path", "rule", "length"}]. Never the text."""
    out = []
    _walk(obj, "", out)
    return out
