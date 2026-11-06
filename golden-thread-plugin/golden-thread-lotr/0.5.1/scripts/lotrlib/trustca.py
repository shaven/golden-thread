"""trust_ca (0.5.1): reach an https endpoint whose certificate was issued by the owner's OWN private CA.

A connection may carry `trust_ca: {"name": N, "sha256": H}`. N names a PEM file of one or more CA
certificates (public only) that `lotr trust-ca add` copied to `<lotr home>/ca/N.pem`; H is the sha256
of that file as it was when the owner approved it. At request time the file is read from that one
place only; if it changed or went missing the connection refuses, naming how to approve it again.

What it does: for that connection the TLS context is built from that file ALONE (the system trust
store is not consulted), the certificate must verify to it, be in date, and name the host or IP the
registry pins (SAN), TLS 1.2 or newer. What it does not do: it has no revocation list, and a CA that
can sign for any name (no nameConstraints) can sign for this connection's host if its key is stolen.
There is no setting, environment variable or option that turns verification off.

The X.509 reading below is a small DER walker (stdlib only): it extracts exactly what the commands
show and the checks need (names, dates, basicConstraints, nameConstraints), nothing more.
"""
import base64
import binascii
import datetime
import hashlib
import http.client
import os
import re
import ssl
import threading
import time
import urllib.request
from pathlib import Path

from .errors import GatewayError
from .util import atomic_write

# fullmatch only (a `$` anchor lets a trailing newline through); a name never ends in '.'
NAME_RX = re.compile(r"[a-z0-9](?:[a-z0-9._-]{0,62}[a-z0-9_-])?")
SHA256_HEX = re.compile(r"[0-9a-f]{64}")
MAX_PEM_BYTES = 256 * 1024
WARN_DAYS = 14
_BLOCK = re.compile(r"-----BEGIN ([A-Z0-9 ]+)-----(.*?)-----END \1-----", re.S)
_OIDS = {"2.5.4.3": "CN", "2.5.4.10": "O", "2.5.4.11": "OU", "2.5.4.6": "C", "2.5.4.7": "L",
         "2.5.4.8": "ST"}
_OID_BASIC, _OID_NAMECON, _OID_SKI = "2.5.29.19", "2.5.29.30", "2.5.29.14"
# Shown or logged text from a certificate never carries a terminal control: C0, DEL, C1 and the
# bidi overrides become \xNN / \uNNNN, so a subject cannot repaint the approval prompt.
_CONTROL = re.compile("[\x00-\x1f\x7f-\x9f\u200e\u200f\u202a-\u202e\u2066-\u2069]")


def ca_dir(home):
    return Path(home) / "ca"


def ca_path(home, name):
    return ca_dir(home) / (name + ".pem")


def safe_text(s):
    """`s` with every control character written as a visible escape (ESC -> backslash x1b), for
    display and logs."""
    return _CONTROL.sub(lambda m: ("\\x%02x" % ord(m.group()) if ord(m.group()) < 0x100
                                   else "\\u%04x" % ord(m.group())), s)


def check_name(name):
    if not isinstance(name, str) or not NAME_RX.fullmatch(name):
        raise GatewayError("bad_ca_name", "a CA name is lowercase letters, digits, . _ - (starting "
                           "with a letter or digit), at most 64 characters")
    return name


def sha256_of(data):
    return hashlib.sha256(data).hexdigest()


# ------------------------------------------------------------------ DER reading

def _tlv(b, i):
    """-> (tag, content_start, content_end) of the element at b[i]."""
    tag, ln = b[i], b[i + 1]
    i += 2
    if ln & 0x80:
        n = ln & 0x7F
        if n == 0 or n > 4:
            raise ValueError("bad length")
        ln = int.from_bytes(b[i:i + n], "big")
        i += n
    end = i + ln
    if end > len(b):
        raise ValueError("truncated")
    return tag, i, end


def _children(b, start, end):
    out, i = [], start
    while i < end:
        tag, s, e = _tlv(b, i)
        out.append((tag, s, e))
        i = e
    return out


def _oid(b, s, e):
    first, parts, v = b[s], [], 0
    parts += [first // 40, first % 40]
    for byte in b[s + 1:e]:
        v = (v << 7) | (byte & 0x7F)
        if not byte & 0x80:
            parts.append(v)
            v = 0
    return ".".join(str(p) for p in parts)


def _time(b, s, e, tag):
    t = b[s:e].decode("ascii")
    fmt = "%y%m%d%H%M%S" if tag == 0x17 else "%Y%m%d%H%M%S"
    dt = datetime.datetime.strptime(t.rstrip("Z"), fmt).replace(tzinfo=datetime.timezone.utc)
    return int(dt.timestamp())


def _name(b, s, e):
    parts = []
    for rdn in _children(b, s, e):
        for atv in _children(b, rdn[1], rdn[2]):
            kids = _children(b, atv[1], atv[2])
            key = _OIDS.get(_oid(b, kids[0][1], kids[0][2]), "?")
            parts.append(f"{key}={safe_text(b[kids[1][1]:kids[1][2]].decode('utf-8', 'replace'))}")
    return ", ".join(parts)


def _constraints(b, s, e):
    """nameConstraints -> ['permitted DNS:example.org', 'excluded IP:10.0.0.0/8', ...]."""
    out = []
    for tag, ss, ee in _children(b, s, e):
        word = {0xA0: "permitted", 0xA1: "excluded"}.get(tag)
        if not word:
            continue
        for _, gs, ge in _children(b, ss, ee):                         # GeneralSubtree
            for gtag, ns, ne in _children(b, gs, ge)[:1]:              # its base GeneralName
                raw = b[ns:ne]
                if gtag == 0x82:
                    out.append(f"{word} DNS:{safe_text(raw.decode('ascii', 'replace'))}")
                elif gtag == 0x87 and len(raw) in (8, 32):
                    half = len(raw) // 2
                    mask = "".join(f"{x:08b}" for x in raw[half:]).count("1")
                    ip = ".".join(map(str, raw[:half])) if half == 4 else raw[:half].hex()
                    out.append(f"{word} IP:{ip}/{mask}")
                else:
                    out.append(f"{word} name type {gtag & 0x1F}")
    return out


def parse_cert(der):
    """One DER certificate -> {subject, issuer, not_before, not_after, is_ca, name_constraints}."""
    try:
        _, s, e = _tlv(der, 0)
        _, ts, te = _tlv(der, s)                                       # tbsCertificate
        kids = _children(der, ts, te)
        k = 1 if kids[0][0] == 0xA0 else 0                             # explicit version
        issuer, validity, subject = kids[k + 2], kids[k + 3], kids[k + 4]
        times = _children(der, validity[1], validity[2])
        info = {"subject": _name(der, subject[1], subject[2]),
                "issuer": _name(der, issuer[1], issuer[2]),
                "not_before": _time(der, times[0][1], times[0][2], times[0][0]),
                "not_after": _time(der, times[1][1], times[1][2], times[1][0]),
                "is_ca": False, "name_constraints": [], "has_name_constraints": False,
                "has_ski": False, "sha256": sha256_of(der)}
        for tag, xs, xe in kids[k + 6:]:
            if tag != 0xA3:
                continue
            _, ls, le = _tlv(der, xs)
            for ext in _children(der, ls, le):
                parts = _children(der, ext[1], ext[2])
                oid = _oid(der, parts[0][1], parts[0][2])
                body = parts[-1]                                       # the OCTET STRING
                if oid == _OID_BASIC:
                    _, bs, be = _tlv(der, body[1])
                    first = _children(der, bs, be)[:1]
                    info["is_ca"] = bool(first and first[0][0] == 0x01 and der[first[0][1]] != 0)
                elif oid == _OID_SKI:
                    info["has_ski"] = True
                elif oid == _OID_NAMECON:
                    _, ns, ne = _tlv(der, body[1])
                    info["has_name_constraints"] = True
                    info["name_constraints"] = _constraints(der, ns, ne)
        return info
    except (ValueError, IndexError, UnicodeError, TypeError):
        raise GatewayError("trust_ca_invalid", "that is not a readable X.509 certificate")


def inspect_pem(text):
    """Validate a PEM and return {"pem": normalized text, "certs": [info...]}. Refuses, naming the
    problem and never printing the content: any private key; anything but CERTIFICATE blocks; a
    certificate that is not a CA. The normalized text holds the certificate blocks only."""
    if not isinstance(text, str) or len(text) > MAX_PEM_BYTES:
        raise GatewayError("trust_ca_invalid", "the file is missing or too large for a CA bundle")
    if re.search(r"PRIVATE KEY-----", text) or "ENCRYPTED PRIVATE" in text:
        raise GatewayError("trust_ca_private_key",
                           "that file contains a private key; a CA file must hold public "
                           "certificates only. Nothing was stored.",
                           ["export the CA certificate alone (for example: openssl x509 -in ca.pem "
                            "-out ca-cert.pem) and pass that file"])
    blocks = _BLOCK.findall(text)
    if not blocks:
        raise GatewayError("trust_ca_invalid", "no PEM certificate found in that file")
    certs, out = [], []
    for label, body in blocks:
        if label != "CERTIFICATE":
            raise GatewayError("trust_ca_invalid", f"a {label.lower()} block is not a CA "
                               "certificate; the file may hold CERTIFICATE blocks only")
        try:
            der = base64.b64decode("".join(body.split()), validate=True)
        except (binascii.Error, ValueError):
            raise GatewayError("trust_ca_invalid", "a certificate block is not valid base64")
        info = parse_cert(der)
        if not info["is_ca"]:
            raise GatewayError("trust_ca_not_ca",
                               f"the certificate for {info['subject'] or '(no subject)'} is not a CA "
                               "(basicConstraints CA is not true), so it cannot vouch for a server. "
                               "Pass the CA's own certificate, not an endpoint's.")
        certs.append(info)
        b64 = base64.b64encode(der).decode("ascii")
        out.append("-----BEGIN CERTIFICATE-----\n"
                   + "\n".join(b64[i:i + 64] for i in range(0, len(b64), 64))
                   + "\n-----END CERTIFICATE-----\n")
    return {"pem": "".join(out), "certs": certs}


def _describe(info):
    d = {k: info[k] for k in ("subject", "issuer", "sha256", "is_ca", "has_name_constraints",
                              "name_constraints")}
    d["not_before"] = time.strftime("%Y-%m-%d", time.gmtime(info["not_before"]))
    d["not_after"] = time.strftime("%Y-%m-%d", time.gmtime(info["not_after"]))
    return d


# ------------------------------------------------------------------ the files under <home>/ca

def add(home, name, text):
    """Validate and copy a CA file into the home (dir 700, file 600). The caller has already asked
    a person. -> {"name","sha256","path","certs":[...],"warnings":[...],"replaced":bool}."""
    check_name(name)
    res = inspect_pem(text)
    d = ca_dir(home)
    if d.is_symlink():
        raise GatewayError("trust_ca_invalid", "the CA folder of this LOTR home is a symlink; "
                           "nothing was stored", ["replace it with a real folder (mode 700)"])
    d.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(d, 0o700)
    except OSError:
        pass
    path = ca_path(home, name)
    replaced = path.exists()
    atomic_write(path, res["pem"], 0o600)
    return {"name": name, "sha256": sha256_of(res["pem"].encode("ascii")),
            "path": str(path.relative_to(Path(home))),
            "certs": [_describe(c) for c in res["certs"]], "warnings": _warnings(res["certs"]),
            "replaced": replaced}


def _warnings(certs):
    out = []
    for c in certs:
        who = c["subject"] or "(no subject)"
        if not c["has_name_constraints"]:
            out.append(f"{who} has no nameConstraints: this CA can vouch for any host")
        if not c["has_ski"]:
            # Python 3.13+ verifies with VERIFY_X509_STRICT, which refuses a CA without one
            out.append(f"{who} has no subjectKeyIdentifier: Python 3.13 and newer refuse it (strict "
                       "X.509 checks), so connections may fail there. Reissue the CA with one (openssl "
                       "req -x509 ... -addext subjectKeyIdentifier=hash; OpenSSL 3 adds it by default) "
                       "and its leaf certificates with an authorityKeyIdentifier")
    return out


def describe_text(text):
    """What `trust-ca add` shows before it asks (and stores nothing)."""
    res = inspect_pem(text)
    return {"sha256": sha256_of(res["pem"].encode("ascii")),
            "certs": [_describe(c) for c in res["certs"]], "warnings": _warnings(res["certs"])}


def _read(home, name):
    path = ca_path(home, name)
    if ca_dir(home).is_symlink() or path.is_symlink() or not path.is_file():
        return None
    try:
        return path.read_bytes()
    except OSError:
        return None


def list_cas(home):
    out = []
    d = ca_dir(home)
    for p in sorted(d.glob("*.pem")) if d.is_dir() else []:
        name = p.stem
        if not NAME_RX.fullmatch(name):
            continue
        raw = _read(home, name)
        row = {"name": name, "sha256": sha256_of(raw) if raw else None}
        try:
            row["certs"] = [_describe(c) for c in inspect_pem(raw.decode("ascii"))["certs"]]
        except (GatewayError, AttributeError, UnicodeError):
            row["problem"] = "the file is not a valid CA bundle"
        out.append(row)
    return out


def remove(home, name):
    check_name(name)
    path = ca_path(home, name)
    if not path.is_file():
        raise GatewayError("unknown_ca", f"no CA named {name!r}", ["list them: lotr trust-ca list"])
    path.unlink()


def users(connections, name):
    return sorted(cid for cid, c in connections.items()
                  if (c.get("trust_ca") or {}).get("name") == name)


def ref_for(home, name):
    """{"name","sha256"} for the file as it is now: what a connection records when approved."""
    check_name(name)
    raw = _read(home, name)
    if raw is None:
        raise GatewayError("unknown_ca", f"no CA named {name!r} in this home",
                           [f"add it first: lotr trust-ca add {name} --from FILE"])
    return {"name": name, "sha256": sha256_of(raw)}


def snapshot(home, name):
    """(PEM text, {"name","sha256"}) from ONE read of the file, so what `approve` shows is exactly
    what it pins (security review M3)."""
    check_name(name)
    raw = _read(home, name)
    try:
        text = raw.decode("ascii")
    except (AttributeError, UnicodeError):
        raise GatewayError("tls_ca_missing", f"the CA file {name}.pem is missing or unreadable",
                           [f"lotr trust-ca add {name} --from FILE"])
    return text, {"name": name, "sha256": sha256_of(raw)}


def verified_pem(home, conn):
    """The CA PEM text for a connection, after proving it is the file the owner approved: the SAME
    bytes that were hashed are what the TLS context is built from (no second read by path). Raises
    tls_ca_missing / tls_ca_changed; neither names a certificate's content."""
    ref = conn.get("trust_ca") or {}
    name, want = ref.get("name"), ref.get("sha256")
    cid = conn.get("id")
    if not (isinstance(name, str) and NAME_RX.fullmatch(name) and isinstance(want, str)
            and SHA256_HEX.fullmatch(want)):
        raise GatewayError("tls_ca_missing", f"{cid}: its trust_ca record is malformed")
    raw = _read(home, name)
    if raw is None:
        raise GatewayError("tls_ca_missing", f"{cid}: the private CA file {name}.pem is missing from "
                           "this LOTR home, so the connection is refused",
                           [f"put it back: lotr trust-ca add {name} --from FILE, then lotr trust-ca "
                            f"approve {cid}"])
    if sha256_of(raw) != want:
        raise GatewayError("tls_ca_changed", f"{cid}: the private CA file {name}.pem is not the one "
                           "that was approved (its sha256 changed), so the connection is refused",
                           [f"if you changed it on purpose, read it with: lotr trust-ca list, then "
                            f"approve it: lotr trust-ca approve {cid}"])
    try:
        return raw.decode("ascii")
    except UnicodeError:
        raise GatewayError("tls_ca_changed", f"{cid}: the private CA file {name}.pem is not a PEM "
                           "text file, so the connection is refused")


# ------------------------------------------------------------------ TLS

def ssl_context(ca_pem):
    """The ONLY trust anchor is `ca_pem`, the verified PEM text (a cadata argument keeps
    create_default_context from loading the system store, and nothing is re-read from disk).
    Hostname and certificate are always checked."""
    if not isinstance(ca_pem, str) or "-----BEGIN CERTIFICATE-----" not in ca_pem:
        raise GatewayError("tls_ca_missing", "the private CA was not resolved")
    ctx = ssl.create_default_context(cadata=ca_pem)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.check_hostname = True
    ctx.verify_mode = ssl.CERT_REQUIRED
    return ctx


_LEAF = {}                      # connection id -> notAfter (epoch) of the leaf last seen; this process only
_LEAF_LOCK = threading.Lock()


def leaf_seen(cid):
    with _LEAF_LOCK:
        return _LEAF.get(cid)


def _record_leaf(cid, cert):
    try:
        na = ssl.cert_time_to_seconds(cert["notAfter"])
    except (KeyError, ValueError, TypeError):
        return
    with _LEAF_LOCK:
        _LEAF[cid] = na


def https_handler(ca_pem, cid):
    """A urllib HTTPSHandler bound to the private CA that also notes the leaf's expiry (no extra
    network call: it reads the certificate the handshake already got)."""
    ctx = ssl_context(ca_pem)

    class _Conn(http.client.HTTPSConnection):
        def connect(self):
            super().connect()
            try:
                _record_leaf(cid, self.sock.getpeercert())
            except Exception:                                   # noqa: BLE001 - advisory only
                pass

    class _Handler(urllib.request.HTTPSHandler):
        def https_open(self, req):
            return self.do_open(_Conn, req, context=ctx)

    return _Handler(context=ctx)


def tls_error(exc, conn):
    """A GatewayError for an ssl failure on a trust_ca connection, else None. Short, no leak."""
    reason = getattr(exc, "reason", None) or exc
    if not isinstance(reason, ssl.SSLError):
        return None
    name = (conn.get("trust_ca") or {}).get("name")
    if isinstance(reason, ssl.SSLCertVerificationError):
        why = (getattr(reason, "verify_message", "") or "certificate verify failed")[:120]
        return GatewayError(
            "tls_untrusted", f"{conn.get('id')}: the server's certificate was not accepted against "
            f"private CA {name} ({why})",
            ["the certificate must be issued by that CA, in date, and name the host in base_url; "
             "a renewed certificate from the same CA needs no change here",
             "check it, then retry: lotr status --check"])
    return GatewayError("tls_error", f"{conn.get('id')}: the TLS handshake failed "
                        f"({type(reason).__name__})",
                        ["the server must speak TLS 1.2 or newer; then retry"])


# ------------------------------------------------------------------ status

def status_info(home, conn, now=None):
    """What `lotr status` shows for a trust_ca connection; reads files and the leaf note only."""
    now = time.time() if now is None else now
    ref = conn.get("trust_ca") or {}
    name, want = ref.get("name"), ref.get("sha256") or ""
    row = {"name": name, "sha256": want[:12], "warnings": []}
    try:
        certs = inspect_pem(verified_pem(home, conn))["certs"]
    except (GatewayError, AttributeError, UnicodeError) as e:
        row["problem"] = getattr(e, "code", "tls_ca_missing")
        row["warnings"].append(f"private CA {name}: {getattr(e, 'message', 'unreadable')}")
        return row
    ca_days = int((min(c["not_after"] for c in certs) - now) // 86400)
    row["ca_days_left"] = ca_days
    if ca_days <= WARN_DAYS:
        row["warnings"].append(f"private CA {name} expires in {ca_days} days" if ca_days >= 0
                               else f"private CA {name} expired {-ca_days} days ago")
    leaf = leaf_seen(conn.get("id"))
    if leaf is not None:
        ld = int((leaf - now) // 86400)
        row["leaf_days_left"] = ld
        if ld <= WARN_DAYS:
            row["warnings"].append(f"the certificate of {conn.get('id')} expires in {ld} days"
                                   if ld >= 0 else
                                   f"the certificate of {conn.get('id')} expired {-ld} days ago")
    return row
