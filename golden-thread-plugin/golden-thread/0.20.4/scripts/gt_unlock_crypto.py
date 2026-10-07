#!/usr/bin/env python3
"""gt_unlock_crypto -- signature VERIFICATION only, standard library only (0.20.1).

Why gt verifies in Python at all: a helper that answers "ok" is a boolean, and a boolean can be
forged by anything that can replace the helper. A SIGNATURE over a fresh challenge cannot be
forged without the private key -- which lives in the Secure Enclave (Touch ID) or the TPM
(Windows Hello), or at the identity provider (SSO). So the helper and the IdP produce
signatures and this module, inside the authority, checks them. gt never signs and never holds a
private key; there is no signing code here on purpose.

  ES256  ECDSA P-256 / SHA-256. DER signatures (the Secure Enclave helper) and JOSE raw r||s
         (64 bytes, JWS) are both accepted, each parsed STRICTLY: minimal DER integers, no
         trailing bytes, 0 < r, s < n, the public point on the curve.
  RS256  RSASSA-PKCS1-v1_5 / SHA-256. The expected encoded message is BUILT and compared byte
         for byte with s^e mod n. Padding is never parsed: parsing it is how Bleichenbacher's
         e=3 forgery works. Moduli under 2048 bits are refused.
  JWS    compact JWS / JWT verification against a key chosen by `kid`, with the algorithm
         taken from a PINNED list -- `none`, HS*, and anything not pinned are refused before
         any key is used.

Side-channel note: these are verifications of public data with public keys; nothing secret
flows through them, so constant-time arithmetic is not needed. The one comparison of a secret
(a TOTP code) is elsewhere and uses hmac.compare_digest.
"""
import base64
import hashlib
import hmac
import json

# ---------------------------------------------------------------- P-256 (secp256r1)
P = 0xffffffff00000001000000000000000000000000ffffffffffffffffffffffff
A = P - 3
B = 0x5ac635d8aa3a93e7b3ebbd55769886bc651d06b0cc53b0f63bce3c3e27d2604b
N = 0xffffffff00000000ffffffffffffffffbce6faada7179e84f3b9cac2fc632551
G = (0x6b17d1f2e12c4247f8bce6e563a440f277037d812deb33a0f4a13945d898c296,
     0x4fe342e2fe1a7f9b8ee7eb4a7c0f9e162bce33576b315ececbb6406837bf51f5)


class CryptoError(ValueError):
    """A malformed key, signature or token. Never carries the token's content."""


def _inv(x, m):
    return pow(x, m - 2, m)          # m is prime for both P and N


# Jacobian coordinates: one modular inversion per scalar multiplication instead of one per
# addition, which keeps a verification in the low milliseconds in pure Python.
def _jdouble(Xp, Yp, Zp):
    if not Yp:
        return (0, 0, 0)
    ysq = (Yp * Yp) % P
    S = (4 * Xp * ysq) % P
    M = (3 * Xp * Xp + A * pow(Zp, 4, P)) % P
    nx = (M * M - 2 * S) % P
    ny = (M * (S - nx) - 8 * ysq * ysq) % P
    nz = (2 * Yp * Zp) % P
    return (nx, ny, nz)


def _jadd(p, q):
    Xp, Yp, Zp = p
    Xq, Yq, Zq = q
    if not Zp:
        return q
    if not Zq:
        return p
    U1 = (Xp * Zq * Zq) % P
    U2 = (Xq * Zp * Zp) % P
    S1 = (Yp * pow(Zq, 3, P)) % P
    S2 = (Yq * pow(Zp, 3, P)) % P
    if U1 == U2:
        if S1 != S2:
            return (0, 0, 0)
        return _jdouble(Xp, Yp, Zp)
    H = U2 - U1
    R = S2 - S1
    H2 = (H * H) % P
    H3 = (H * H2) % P
    U1H2 = (U1 * H2) % P
    nx = (R * R - H3 - 2 * U1H2) % P
    ny = (R * (U1H2 - nx) - S1 * H3) % P
    nz = (H * Zp * Zq) % P
    return (nx, ny, nz)


def _jmul(k, pt):
    r = (0, 0, 0)
    q = (pt[0], pt[1], 1)
    while k:
        if k & 1:
            r = _jadd(r, q)
        q = _jdouble(*q)
        k >>= 1
    return r


def _affine_x(jp):
    X, _Y, Z = jp
    if not Z:
        return None
    zi = _inv(Z, P)
    return (X * zi * zi) % P


def on_curve(x, y):
    return 0 <= x < P and 0 <= y < P and (y * y - (x * x * x + A * x + B)) % P == 0


def p256_point(pub):
    """A public key as an (x, y) on P-256, from 65-byte uncompressed SEC1 (0x04||X||Y), a DER
    SubjectPublicKeyInfo, or a JWK dict {kty: EC, crv: P-256, x, y}. Refuses anything else and
    any point not on the curve (an invalid-curve key is how small-subgroup attacks start)."""
    if isinstance(pub, dict):
        if pub.get("kty") != "EC" or pub.get("crv") != "P-256":
            raise CryptoError("not a P-256 JWK")
        x = int.from_bytes(b64url_decode(pub.get("x", "")), "big")
        y = int.from_bytes(b64url_decode(pub.get("y", "")), "big")
    else:
        raw = bytes(pub)
        if len(raw) != 65:
            raw = _spki_ec_point(raw)
        if len(raw) != 65 or raw[0] != 4:
            raise CryptoError("not an uncompressed P-256 point")
        x = int.from_bytes(raw[1:33], "big")
        y = int.from_bytes(raw[33:], "big")
    if not on_curve(x, y):
        raise CryptoError("public key is not on P-256")
    return (x, y)


def es256_verify(pub, message, signature, *, raw=False):
    """True iff `signature` (DER, or raw r||s when raw=True) is a valid ECDSA P-256 SHA-256
    signature of `message` under `pub`. Malformed input is False, never an exception."""
    try:
        Q = p256_point(pub)
        if raw:
            if len(signature) != 64:
                return False
            r = int.from_bytes(signature[:32], "big")
            s = int.from_bytes(signature[32:], "big")
        else:
            r, s = der_ecdsa_sig(signature)
    except CryptoError:
        return False
    if not (0 < r < N and 0 < s < N):
        return False
    e = int.from_bytes(hashlib.sha256(message).digest(), "big")
    w = _inv(s, N)
    X = _affine_x(_jadd(_jmul(e * w % N, G), _jmul(r * w % N, Q)))
    return X is not None and X % N == r


# ---------------------------------------------------------------- strict DER


def _der_read(data, pos, tag):
    if pos + 2 > len(data) or data[pos] != tag:
        raise CryptoError("DER: unexpected tag")
    length = data[pos + 1]
    pos += 2
    if length & 0x80:
        nbytes = length & 0x7f
        if nbytes == 0 or nbytes > 4 or pos + nbytes > len(data):
            raise CryptoError("DER: bad length")
        length = int.from_bytes(data[pos:pos + nbytes], "big")
        if length < 0x80 or data[pos] == 0:
            raise CryptoError("DER: non-minimal length")
        pos += nbytes
    if pos + length > len(data):
        raise CryptoError("DER: truncated")
    return data[pos:pos + length], pos + length


def _der_uint(body):
    if not body:
        raise CryptoError("DER: empty integer")
    if body[0] & 0x80:
        raise CryptoError("DER: negative integer")
    if len(body) > 1 and body[0] == 0 and not body[1] & 0x80:
        raise CryptoError("DER: non-minimal integer")
    return int.from_bytes(body, "big")


def der_ecdsa_sig(sig):
    """(r, s) from a DER ECDSA-Sig-Value, refusing any non-canonical encoding or trailing byte
    (a malleable signature encoding is how a replay check gets dodged)."""
    sig = bytes(sig)
    seq, end = _der_read(sig, 0, 0x30)
    if end != len(sig):
        raise CryptoError("DER: trailing bytes")
    rb, p2 = _der_read(seq, 0, 0x02)
    sb, p3 = _der_read(seq, p2, 0x02)
    if p3 != len(seq):
        raise CryptoError("DER: trailing bytes in sequence")
    return _der_uint(rb), _der_uint(sb)


_OID_EC = bytes.fromhex("2a8648ce3d0201")         # 1.2.840.10045.2.1 id-ecPublicKey
_OID_P256 = bytes.fromhex("2a8648ce3d030107")     # 1.2.840.10045.3.1.7 prime256v1
_OID_RSA = bytes.fromhex("2a864886f70d010101")    # 1.2.840.113549.1.1.1 rsaEncryption


def _spki(der):
    spki, end = _der_read(der, 0, 0x30)
    if end != len(der):
        raise CryptoError("SPKI: trailing bytes")
    alg, p = _der_read(spki, 0, 0x30)
    bits, p2 = _der_read(spki, p, 0x03)
    if p2 != len(spki) or not bits or bits[0] != 0:
        raise CryptoError("SPKI: bad bit string")
    oid, q = _der_read(alg, 0, 0x06)
    return oid, alg[q:], bits[1:]


def _spki_ec_point(der):
    oid, params, point = _spki(der)
    if oid != _OID_EC:
        raise CryptoError("SPKI: not an EC key")
    curve, _ = _der_read(params, 0, 0x06)
    if curve != _OID_P256:
        raise CryptoError("SPKI: not P-256")
    return point


def rsa_public(pub):
    """(n, e) from a DER SubjectPublicKeyInfo, a DER PKCS#1 RSAPublicKey, a JWK dict
    {kty: RSA, n, e}, or an (n, e) tuple. Moduli under 2048 bits are refused."""
    if isinstance(pub, tuple):
        n, e = pub
    elif isinstance(pub, dict):
        if pub.get("kty") != "RSA":
            raise CryptoError("not an RSA JWK")
        n = int.from_bytes(b64url_decode(pub.get("n", "")), "big")
        e = int.from_bytes(b64url_decode(pub.get("e", "")), "big")
    else:
        der = bytes(pub)
        try:
            oid, _params, inner = _spki(der)
            if oid != _OID_RSA:
                raise CryptoError("SPKI: not an RSA key")
        except CryptoError:
            inner = der                              # maybe a bare RSAPublicKey
        seq, end = _der_read(inner, 0, 0x30)
        if end != len(inner):
            raise CryptoError("RSAPublicKey: trailing bytes")
        nb, p = _der_read(seq, 0, 0x02)
        eb, p2 = _der_read(seq, p, 0x02)
        if p2 != len(seq):
            raise CryptoError("RSAPublicKey: trailing bytes")
        n, e = _der_uint(nb), _der_uint(eb)
    if n.bit_length() < 2048:
        raise CryptoError("RSA modulus under 2048 bits")
    if e < 3 or e % 2 == 0:
        raise CryptoError("RSA exponent invalid")
    return n, e


# DigestInfo prefix for SHA-256 (RFC 8017 section 9.2, note 1).
_SHA256_DIGESTINFO = bytes.fromhex("3031300d060960864801650304020105000420")


def rs256_verify(pub, message, signature):
    """True iff `signature` is a valid RSASSA-PKCS1-v1_5 SHA-256 signature of `message`.

    INVARIANT: the comparison is against the FULL expected encoding
    00 01 FF..FF 00 || DigestInfo || hash, built here; the decrypted block is never parsed.
    """
    try:
        n, e = rsa_public(pub)
    except CryptoError:
        return False
    k = (n.bit_length() + 7) // 8
    if len(signature) != k:
        return False
    s = int.from_bytes(signature, "big")
    if s >= n:
        return False
    em = pow(s, e, n).to_bytes(k, "big")
    t = _SHA256_DIGESTINFO + hashlib.sha256(message).digest()
    if k < len(t) + 11:
        return False
    expected = b"\x00\x01" + b"\xff" * (k - len(t) - 3) + b"\x00" + t
    return hmac.compare_digest(em, expected)


# ---------------------------------------------------------------- base64url / JWS


def b64url_decode(s):
    if isinstance(s, str):
        s = s.encode("ascii")
    if b"=" in s or any(c not in b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
                                   b"0123456789-_" for c in s):
        raise CryptoError("not base64url")
    try:
        return base64.urlsafe_b64decode(s + b"=" * (-len(s) % 4))
    except (ValueError, TypeError):
        raise CryptoError("not base64url")


def b64url_encode(b):
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode("ascii")


SUPPORTED_ALGS = ("RS256", "ES256")


def jws_verify(token, keys, *, algs=SUPPORTED_ALGS):
    """Verify a compact JWS. `keys` is a list of JWK dicts (a JWKS `keys` array).
    -> (header, payload_dict). Raises CryptoError on ANY failure.

    INVARIANTS: the header's `alg` must be in `algs` (pinned by the caller; `none` and HMAC
    algorithms can never be, since only RS256/ES256 are implemented); the key is chosen by
    `kid`, and its own `kty` must match the alg, so an RSA key can never be used as an HMAC
    secret or an EC key for RS256; a key carrying `use` other than "sig" or an `alg` that
    disagrees is skipped."""
    if not isinstance(token, str) or token.count(".") != 2:
        raise CryptoError("not a compact JWS")
    h64, p64, s64 = token.split(".")
    try:
        header = json.loads(b64url_decode(h64))
        payload = json.loads(b64url_decode(p64))
    except ValueError:
        raise CryptoError("JWS header or payload is not JSON")
    if not isinstance(header, dict) or not isinstance(payload, dict):
        raise CryptoError("JWS header or payload is not an object")
    alg = header.get("alg")
    if alg not in algs or alg not in SUPPORTED_ALGS:
        raise CryptoError("JWS alg %r is not allowed" % (alg,))
    if header.get("crit"):
        raise CryptoError("JWS crit headers are not supported")
    sig = b64url_decode(s64)
    signing_input = (h64 + "." + p64).encode("ascii")
    kid = header.get("kid")
    want_kty = "RSA" if alg == "RS256" else "EC"
    cands = [k for k in keys or [] if isinstance(k, dict) and k.get("kty") == want_kty
             and k.get("use", "sig") == "sig" and k.get("alg", alg) == alg
             and (kid is None or k.get("kid") == kid)]
    if not cands:
        raise CryptoError("no signing key for kid %r" % (kid,))
    for k in cands:
        try:
            ok = (rs256_verify(k, signing_input, sig) if alg == "RS256"
                  else es256_verify(k, signing_input, sig, raw=True))
        except CryptoError:
            ok = False
        if ok:
            return header, payload
    raise CryptoError("JWS signature does not verify")
