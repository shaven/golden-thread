"""Test-only SIGNING keys for the unlock tests (0.20.1).

gt ships verification only (scripts/gt_unlock_crypto.py); the tests need something to sign
with -- a stand-in for the Secure Enclave, the TPM and an identity provider. Everything here
is throwaway: keys are generated per test process, never written to disk, and nothing in the
shipped tree imports this file.
"""
import hashlib
import secrets
import sys

from _harness import SCRIPTS

sys.path.insert(0, str(SCRIPTS))
import gt_unlock_crypto as C  # noqa: E402


# ---------------------------------------------------------------- P-256

def _jmul_affine(k, pt):
    j = C._jmul(k, pt)
    X, Y, Z = j
    zi = C._inv(Z, C.P)
    return ((X * zi * zi) % C.P, (Y * zi * zi * zi) % C.P)


class P256Key:
    def __init__(self):
        self.d = secrets.randbelow(C.N - 1) + 1
        self.x, self.y = _jmul_affine(self.d, C.G)

    def public_bytes(self):
        return b"\x04" + self.x.to_bytes(32, "big") + self.y.to_bytes(32, "big")

    def jwk(self, kid="ec1"):
        return {"kty": "EC", "crv": "P-256", "kid": kid, "use": "sig", "alg": "ES256",
                "x": C.b64url_encode(self.x.to_bytes(32, "big")),
                "y": C.b64url_encode(self.y.to_bytes(32, "big"))}

    def sign_rs(self, msg):
        e = int.from_bytes(hashlib.sha256(msg).digest(), "big")
        while True:
            k = secrets.randbelow(C.N - 1) + 1
            r = _jmul_affine(k, C.G)[0] % C.N
            if not r:
                continue
            s = (C._inv(k, C.N) * (e + r * self.d)) % C.N
            if s:
                return r, s

    def sign_raw(self, msg):
        r, s = self.sign_rs(msg)
        return r.to_bytes(32, "big") + s.to_bytes(32, "big")

    def sign_der(self, msg):
        r, s = self.sign_rs(msg)
        return der_sig(r, s)


def _der_int(v):
    b = v.to_bytes((v.bit_length() + 8) // 8, "big")     # always room for a sign bit
    b = b.lstrip(b"\0") or b"\0"
    if b[0] & 0x80:
        b = b"\0" + b
    return b"\x02" + bytes([len(b)]) + b


def der_sig(r, s):
    body = _der_int(r) + _der_int(s)
    return b"\x30" + bytes([len(body)]) + body


# ---------------------------------------------------------------- RSA

def _probable_prime(bits):
    small = [p for p in range(3, 2000, 2) if all(p % q for q in range(3, int(p ** .5) + 1, 2))]
    while True:
        c = secrets.randbits(bits) | (1 << (bits - 1)) | (1 << (bits - 2)) | 1
        if any(c % p == 0 for p in small):
            continue
        d, r = c - 1, 0
        while d % 2 == 0:
            d //= 2
            r += 1
        for _ in range(24):
            a = secrets.randbelow(c - 3) + 2
            x = pow(a, d, c)
            if x in (1, c - 1):
                continue
            for _ in range(r - 1):
                x = pow(x, 2, c)
                if x == c - 1:
                    break
            else:
                break
        else:
            return c


_RSA_CACHE = {}


class RSAKey:
    """A 2048-bit RSA key, generated once per test process (pure Python, a second or two)."""

    def __init__(self, slot="default"):
        if slot not in _RSA_CACHE:
            e = 65537
            while True:
                p, q = _probable_prime(1024), _probable_prime(1024)
                phi = (p - 1) * (q - 1)
                if p != q and phi % e and (p * q).bit_length() == 2048:
                    break
            n = p * q
            d = pow(e, -1, phi) if sys.version_info >= (3, 8) else None
            _RSA_CACHE[slot] = (n, e, d)
        self.n, self.e, self.d = _RSA_CACHE[slot]

    def jwk(self, kid="rsa1"):
        nb = self.n.to_bytes(256, "big")
        return {"kty": "RSA", "kid": kid, "use": "sig", "alg": "RS256",
                "n": C.b64url_encode(nb), "e": C.b64url_encode(self.e.to_bytes(3, "big"))}

    def spki(self):
        def tlv(tag, body):
            n = len(body)
            if n < 0x80:
                ln = bytes([n])
            else:
                lb = n.to_bytes((n.bit_length() + 7) // 8, "big")
                ln = bytes([0x80 | len(lb)]) + lb
            return bytes([tag]) + ln + body
        nbytes = self.n.to_bytes(257, "big")             # leading 0 for the sign bit
        rsapub = tlv(0x30, tlv(0x02, nbytes) + tlv(0x02, self.e.to_bytes(3, "big")))
        alg = tlv(0x30, tlv(0x06, C._OID_RSA) + b"\x05\x00")
        return tlv(0x30, alg + tlv(0x03, b"\x00" + rsapub))

    def sign(self, msg):
        t = C._SHA256_DIGESTINFO + hashlib.sha256(msg).digest()
        em = b"\x00\x01" + b"\xff" * (256 - len(t) - 3) + b"\x00" + t
        return pow(int.from_bytes(em, "big"), self.d, self.n).to_bytes(256, "big")


def jwt(key, payload, *, alg=None, kid=None, header_extra=None):
    """A compact JWS signed by `key` (P256Key -> ES256, RSAKey -> RS256)."""
    import json
    alg = alg or ("ES256" if isinstance(key, P256Key) else "RS256")
    hdr = {"alg": alg, "typ": "JWT"}
    if kid:
        hdr["kid"] = kid
    hdr.update(header_extra or {})
    h = C.b64url_encode(json.dumps(hdr).encode())
    p = C.b64url_encode(json.dumps(payload).encode())
    si = (h + "." + p).encode()
    sig = key.sign_raw(si) if isinstance(key, P256Key) else key.sign(si)
    return h + "." + p + "." + C.b64url_encode(sig)
