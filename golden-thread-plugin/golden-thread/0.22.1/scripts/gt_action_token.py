#!/usr/bin/env python3
"""gt_action_token -- single-use action tokens for the `paranoid` group (0.21.0, item #32).

    check(action, token_id, target, args_sha256)   the ONE shared check every chokepoint calls
    Store.mint / Store.spend / Store.audit_verify  the store behind it
    gt_action_token.py mint <action> <target>      the user's way to make one (a terminal, with presence)

Request 2026-10-08-single-use-action-tokens-paranoid-level. When `paranoid.action_tokens.<action>`
is on, that action refuses to run without a token the USER created with presence. A token is bound
to one action, one target (a path, a URL, `connection/op`, a repo), optionally a fingerprint of
the arguments (the content of a write), and an expiry a few minutes away. It is spent at most once.

WHAT THE MODEL HOLDS. Only the token's id (`gtk_` + 20 hex). The id names a record on disk; it is
not a bearer secret: it is bound to one action, so a stolen id opens exactly what it was minted
for, once, and nothing else. The record's signature and the key never leave this module's files
and no function an agent-facing tool calls returns either (tests pin it).

THE STORE (<gt home>/tokens, directory 0700, files 0600)
  token.key       32 random bytes (hex), made the first time anything needs it. An HMAC-SHA256 key
                  nobody but this module reads; the assistant's tools have no route to it.
  live/<id>.json  one record per unspent token: id, action, target, args_sha256, session, factor
                  (how it was approved), minted_at, expires_at, and `mac` = HMAC(key, the rest).
  spent/<id>.json the same file, moved there by a rename -- THE spend. A rename is atomic, so of
                  any number of uses racing for one token (threads or processes) exactly one
                  succeeds; the rest find the file gone and are told `spent`.
  audit.jsonl     one line per decision (mint, spend, refusal), never the signature or the key,
                  chained: each line carries the previous line's chain value and its own
                  HMAC(key, previous + this line). A line edited, deleted, reordered or added
                  without the key breaks the chain; audit.head records the last link, so a
                  truncated tail is caught too. An audit that cannot be written refuses the
                  action (the token is already spent: it is not given back).

REFUSALS (TokenError.code): token_required, malformed, unknown, forged, wrong_action,
wrong_target, wrong_args, wrong_session, expired, spent, audit_failed, token_unavailable. Every
one carries hints that name the route: how to mint a token.

HONEST LIMITS. A process running as you can read token.key and could mint; what stops that is the
rest of the paranoid group (lockdown denying the shell and file tools to the assistant, so it has
only the brokers). A refused retarget or changed-argument attempt does not burn the token: its own
target still works once. Targets are compared as text after each chokepoint's own normalisation
(the real path; the canonical URL). Standard library only.
"""
import argparse
import hashlib
import hmac
import json
import os
import re
import secrets
import shlex
import sys
import time
import urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import gt_tty  # noqa: E402
import gt_paranoid as GP  # noqa: E402
import gt_sandbox as sb  # noqa: E402

DEFAULT_TTL = 300
MAX_TTL = 900
ID_RE = re.compile(r"^gtk_[0-9a-f]{20}$")
HEX64 = re.compile(r"^[0-9a-f]{64}$")
ACTION_NAMES = GP.ACTION_NAMES
CONFIRM = "I APPROVE ONE ACTION"
GENESIS = "0" * 64
SELF = "python3 ~/.claude/golden-thread/hooks/gt_action_token.py"
ENV_VAR = "GT_ACTION_TOKEN"
_IO_SCOPE = {"disk_read": "gt:io:read", "disk_list": "gt:io:read", "net_fetch": "gt:io:fetch",
             "disk_write": "gt:io:write"}


class TokenError(Exception):
    def __init__(self, code, message, hints=None):
        super().__init__(message)
        self.code, self.message, self.hints = code, message, list(hints or [])

    def to_dict(self):
        return {"code": self.code, "message": self.message, "hints": self.hints}


# ------------------------------------------------------------------------------ small helpers
def sha256_text(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def args_hash(obj):
    """The fingerprint of an arguments object, the same on every side."""
    blob = json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str, ensure_ascii=True)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def canon_url(url):
    """One spelling per address: lower-case scheme and host, default port dropped, no fragment,
    an empty path is `/`. The query is kept: another query is another request."""
    p = urllib.parse.urlsplit(str(url).strip())
    scheme = (p.scheme or "").lower()
    host = (p.hostname or "").lower()
    port = p.port
    if (scheme, port) in (("http", 80), ("https", 443)):
        port = None
    netloc = host if ":" not in host else "[%s]" % host
    if port:
        netloc += ":%d" % port
    return urllib.parse.urlunsplit((scheme, netloc, p.path or "/", p.query, ""))


def _clean(s, n=400):
    s = re.sub(r"[\x00-\x1f\x7f]", "?", str(s))
    return s if len(s) <= n else s[:n - 3] + "..."


def _canon(rec):
    return json.dumps({k: v for k, v in rec.items() if k != "mac"}, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")


def _mac(key_hex, rec):
    return hmac.new(bytes.fromhex(key_hex), _canon(rec), hashlib.sha256).hexdigest()


def required(action, cfg=None):
    """Does `action` need a token, going by the paranoid settings? Unknown actions: no."""
    if action not in ACTION_NAMES:
        return False
    return GP.effective(GP.ACTION_PREFIX + action, cfg)


def token_from_env(env=None):
    """The token id a git hook or a command-line chokepoint was given: the environment variable
    GT_ACTION_TOKEN, read when needed and never stored anywhere. -> the id, or None."""
    v = (os.environ if env is None else env).get(ENV_VAR)
    v = v.strip() if isinstance(v, str) else ""
    return v or None


def _real(path):
    return os.path.realpath(os.path.abspath(str(path)))


def git_push_target(repo, refs):
    """The target of a git_push token: the repository's real path, a space, then the remote refs
    being updated, sorted and separated by spaces (e.g. `/work/repo refs/heads/main`)."""
    return "%s %s" % (_real(repo), " ".join(sorted(set(str(r) for r in refs))))


def git_commit_target(repo, tree):
    """The target of a git_commit token: the repository's real path, a space, the tree hash."""
    return "%s %s" % (_real(repo), tree)


def refusal_text(e, via_env=False):
    """A refusal for a person at a terminal: what went wrong, then the route (how to mint)."""
    lines = ["gt: refused: %s (%s)" % (e.message, e.code)]
    lines += ["  " + h for h in e.hints]
    if via_env:
        lines.append("  then run the command with %s=<the token id> in its environment (it is "
                     "read from there, used once and never stored)" % ENV_VAR)
    return "\n".join(lines) + "\n"


def mint_hint(action, target=None, args_sha256=None):
    cmd = "%s mint %s" % (SELF, action)
    if target:
        cmd += " " + shlex.quote(_clean(target, 300))
    if args_sha256:
        cmd += " --args-sha256 " + args_sha256
    return ["mint a single-use token in a terminal (Touch ID, or a typed confirmation): " + cmd,
            "then give the assistant the token's id, to pass as the `token` argument"]


def mint_scope(action):
    if action in _IO_SCOPE:
        return _IO_SCOPE[action]
    try:
        import gt_unlock_policy as P
        if "gt:action:token" in P.GT_SCOPES:
            return "gt:action:token"
    except Exception:                                    # noqa: BLE001
        pass
    return "gt:settings:security"


# ------------------------------------------------------------------------------ the store
class Store(object):
    def __init__(self, h=None, now=None):
        self.h = h
        self._now = now
        self.dir = os.path.join(sb.gt_home(h), "tokens")

    def now(self):
        return float(self._now()) if self._now else time.time()

    # -- files -----------------------------------------------------------------------------
    def _path(self, sub, ident=None):
        return os.path.join(self.dir, sub, (ident + ".json") if ident else "")

    def _ensure(self):
        for sub in ("live", "spent"):
            os.makedirs(os.path.join(self.dir, sub), exist_ok=True)
        try:
            os.chmod(self.dir, 0o700)
        except OSError:
            pass

    def _write_private(self, path, text, exclusive=False):
        flags = os.O_WRONLY | os.O_CREAT | getattr(os, "O_BINARY", 0)
        flags |= os.O_EXCL if exclusive else os.O_TRUNC
        fd = os.open(path, flags, 0o600)
        try:
            os.write(fd, text.encode("utf-8"))
        finally:
            os.close(fd)

    def _key(self, create=False):
        path = os.path.join(self.dir, "token.key")
        for _ in range(2):
            try:
                with open(path, encoding="ascii") as fh:
                    k = fh.read().strip()
                if HEX64.match(k):
                    return k
                return None
            except OSError:
                pass
            if not create:
                return None
            self._ensure()
            try:
                self._write_private(path, secrets.token_hex(32) + "\n", exclusive=True)
            except FileExistsError:
                continue
        return None

    # -- mint ------------------------------------------------------------------------------
    def mint(self, action, target, args_sha256=None, ttl=None, factor=None, session=None):
        """Make one token. The CALLER has already obtained presence (mint_with_presence does).
        -> the public view {id, action, target, args_sha256, session, factor, expires_at}."""
        if action not in ACTION_NAMES:
            raise TokenError("bad_action", "unknown action %r" % (action,),
                             ["actions: " + ", ".join(ACTION_NAMES)])
        if not isinstance(target, str) or not target.strip() or "\0" in target or len(target) > 4096:
            raise TokenError("bad_target", "a token needs a target (a path, URL, connection/op, "
                             "repo) of at most 4096 characters")
        if args_sha256 is not None and not (isinstance(args_sha256, str)
                                            and HEX64.match(args_sha256)):
            raise TokenError("bad_args", "the argument fingerprint must be 64 hex characters")
        ttl = DEFAULT_TTL if ttl is None else ttl
        if isinstance(ttl, bool) or not isinstance(ttl, (int, float)) or not 1 <= ttl <= MAX_TTL:
            raise TokenError("bad_ttl", "the expiry must be 1 to %d seconds" % MAX_TTL)
        if session is not None and not (isinstance(session, str) and 0 < len(session) <= 200):
            raise TokenError("bad_session", "the session must be a short string")
        try:
            self._ensure()
            key = self._key(create=True)
            if key is None:
                raise OSError("no key")
            now = self.now()
            rec = {"v": 1, "id": "gtk_" + secrets.token_hex(10), "action": action,
                   "target": target, "args_sha256": args_sha256, "session": session,
                   "factor": factor or "unknown", "minted_at": now, "expires_at": now + ttl}
            rec["mac"] = _mac(key, rec)
            tmp = os.path.join(self.dir, "live", ".%s.tmp" % rec["id"])
            self._write_private(tmp, json.dumps(rec, sort_keys=True) + "\n", exclusive=True)
            os.rename(tmp, self._path("live", rec["id"]))
            self._append({"event": "mint", "decision": "minted", "action": action,
                          "target": _clean(target), "id": rec["id"], "factor": rec["factor"],
                          "args_sha256": args_sha256, "session": session,
                          "expires_at": rec["expires_at"]})
            self._sweep()
        except OSError as e:
            raise TokenError("store_failed", "the token store cannot be written (%s)"
                             % (e.strerror or type(e).__name__), ["check " + self.dir])
        return self._public(rec)

    @staticmethod
    def _public(rec):
        return {k: rec.get(k) for k in ("id", "action", "target", "args_sha256", "session",
                                        "factor", "expires_at")}

    def live(self):
        """The unspent, unexpired tokens, without signatures."""
        out, now = [], self.now()
        try:
            names = sorted(os.listdir(self._path("live")))
        except OSError:
            return out
        for n in names:
            if not n.endswith(".json") or n.startswith("."):
                continue
            try:
                with open(self._path("live", n[:-5]), encoding="utf-8") as fh:
                    rec = json.load(fh)
            except (OSError, ValueError):
                continue
            if isinstance(rec, dict) and rec.get("expires_at", 0) > now:
                out.append(self._public(rec))
        return out

    def _sweep(self):
        """Forget what no longer matters: spent records after a week, expired ones after a day.
        Judged by the files' own modification times against the real clock."""
        now = time.time()
        for sub, age in (("spent", 7 * 86400), ("live", 86400)):
            try:
                for n in os.listdir(self._path(sub)):
                    p = os.path.join(self._path(sub), n)
                    if n.endswith(".json") and now - os.stat(p).st_mtime > age:
                        os.unlink(p)
            except OSError:
                pass

    # -- spend -----------------------------------------------------------------------------
    def spend(self, action, token_id, target, args_sha256=None, session=None):
        """Verify and consume one token for exactly this action. -> {id, action, target, factor,
        minted_at}. Raises TokenError, and writes the decision to the audit either way."""
        try:
            rec = self._verify(action, token_id, target, args_sha256, session)
            self._consume(rec["id"])
        except TokenError as e:
            self._audit_refusal(action, token_id, target, e.code, args_sha256, session)
            raise
        try:
            self._append({"event": "spend", "decision": "spent", "action": action,
                          "target": _clean(target), "id": rec["id"], "factor": rec.get("factor"),
                          "args_sha256": args_sha256, "session": session})
        except OSError:
            raise TokenError("audit_failed", "the token audit cannot be written, so the action is "
                             "refused (the token was used and is not given back)",
                             ["check " + self.dir] + mint_hint(action, target, args_sha256))
        return {"id": rec["id"], "action": rec["action"], "target": rec["target"],
                "factor": rec.get("factor"), "minted_at": rec.get("minted_at")}

    def _verify(self, action, token_id, target, args_sha256, session):
        if action not in ACTION_NAMES:
            raise TokenError("bad_action", "unknown action %r" % (action,))
        if not isinstance(token_id, str) or not ID_RE.match(token_id):
            raise TokenError("malformed", "that is not a token id (they look like gtk_ followed "
                             "by 20 hex characters)", mint_hint(action, target, args_sha256))
        try:
            rec = self._read_live(token_id)
        except FileNotFoundError:
            if os.path.exists(self._path("spent", token_id)):
                raise TokenError("spent", "that token was already used; a token works once",
                                 mint_hint(action, target, args_sha256))
            raise TokenError("unknown", "no such token: it was never minted here, or has been "
                             "cleaned up", mint_hint(action, target, args_sha256))
        except (OSError, ValueError):
            raise TokenError("forged", "the token record is unreadable, so it is refused",
                             mint_hint(action, target, args_sha256))
        key = self._key(create=False)
        if (key is None or not isinstance(rec, dict) or rec.get("id") != token_id
                or not isinstance(rec.get("mac"), str)
                or not hmac.compare_digest(rec["mac"], _mac(key, rec))):
            raise TokenError("forged", "the token's signature does not verify, so it is refused",
                             mint_hint(action, target, args_sha256))
        hints = mint_hint(action, target, args_sha256)
        if rec.get("action") != action:
            raise TokenError("wrong_action", "that token is for %s, not %s"
                             % (rec.get("action"), action), hints)
        if self.now() >= float(rec.get("expires_at") or 0):
            raise TokenError("expired", "that token has expired (tokens last minutes)", hints)
        if rec.get("target") != target:
            raise TokenError("wrong_target", "that token is for a different target (%s)"
                             % _clean(rec.get("target"), 200), hints)
        if rec.get("args_sha256") != args_sha256:
            raise TokenError("wrong_args", "that token is for different arguments or content",
                             hints)
        if rec.get("session") is not None and rec.get("session") != session:
            raise TokenError("wrong_session", "that token is for a different session", hints)
        return rec

    def _read_live(self, token_id):
        """The live record. On Windows an open file cannot be moved and a file being moved cannot
        be opened, so a PermissionError that is only another spender mid-move is retried briefly."""
        for attempt in range(40):
            try:
                with open(self._path("live", token_id), encoding="utf-8") as fh:
                    return json.load(fh)
            except PermissionError:
                if os.name != "nt" or attempt == 39:
                    raise
                time.sleep(0.005)

    def _consume(self, token_id):
        """THE spend: move the record out of live/. Atomic, so one of any number of racers wins."""
        try:
            os.makedirs(self._path("spent"), exist_ok=True)
            live, spent = self._path("live", token_id), self._path("spent", token_id)
            if os.name == "nt":
                # On Windows os.rename is NOT an exclusive claim: racers that all opened the live
                # record before any of them renamed it each "succeed" (the later ones rename the
                # file onto its own new name), so one token was spent up to eight times (a Windows test machine,
                # 0.21.4). Creating the spent name O_EXCL is the claim; only its winner moves the
                # record.
                os.close(os.open(spent, os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_BINARY", 0)))
                for attempt in range(200):          # losers still reading the record hold it open
                    try:
                        os.replace(live, spent)
                        break
                    except PermissionError:
                        if attempt == 199:
                            raise
                        time.sleep(0.005)
            else:
                os.rename(live, spent)
        except OSError:
            raise TokenError("spent", "that token was already used; a token works once",
                             ["mint a new one: %s mint <action> <target>" % SELF])

    # -- audit -----------------------------------------------------------------------------
    def _audit_refusal(self, action, token_id, target, code, args_sha256, session):
        ident = token_id if isinstance(token_id, str) and ID_RE.match(token_id) else None
        try:
            self._append({"event": "spend", "decision": "refused", "code": code,
                          "action": action if action in ACTION_NAMES else None,
                          "target": _clean(target), "id": ident, "args_sha256": args_sha256,
                          "session": session})
        except OSError:
            pass

    def _lock(self):
        return _DirLock(os.path.join(self.dir, ".audit.lock"))

    def _append(self, rec):
        """Add one chained line. Raises OSError when it cannot."""
        self._ensure()
        key = self._key(create=True)
        if key is None:
            raise OSError("no key")
        rec = dict(rec, ts=time.strftime("%Y-%m-%d %H:%M:%S %z", time.localtime(self.now())),
                   epoch=self.now())
        path = os.path.join(self.dir, "audit.jsonl")
        with self._lock():
            prev, count = self._tail(path)
            rec["prev"] = prev
            body = json.dumps(rec, sort_keys=True, separators=(",", ":"))
            rec["chain"] = hmac.new(bytes.fromhex(key), (prev + body).encode("utf-8"),
                                    hashlib.sha256).hexdigest()
            line = json.dumps(rec, sort_keys=True, separators=(",", ":")) + "\n"
            fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_BINARY", 0),
                         0o600)
            try:
                os.write(fd, line.encode("utf-8"))
            finally:
                os.close(fd)
            head = {"count": count + 1, "chain": rec["chain"]}
            head["mac"] = hmac.new(bytes.fromhex(key), ("%d:%s" % (head["count"], head["chain"]))
                                   .encode("utf-8"), hashlib.sha256).hexdigest()
            self._write_private(os.path.join(self.dir, "audit.head"), json.dumps(head) + "\n")

    def _tail(self, path):
        """-> (chain of the last line, number of lines). From audit.head when it agrees."""
        try:
            with open(os.path.join(self.dir, "audit.head"), encoding="utf-8") as fh:
                head = json.load(fh)
            size = os.path.getsize(path)
            with open(path, "rb") as fh:
                fh.seek(max(0, size - 65536))
                last = fh.read().decode("utf-8", "replace").splitlines()[-1]
            if json.loads(last).get("chain") == head.get("chain"):
                return head["chain"], int(head["count"])
        except (OSError, ValueError, IndexError, KeyError, TypeError):
            pass
        prev, n = GENESIS, 0
        try:
            with open(path, encoding="utf-8") as fh:
                for ln in fh:
                    try:
                        prev = json.loads(ln).get("chain") or prev
                    except ValueError:
                        pass
                    n += 1
        except OSError:
            pass
        return prev, n

    def audit_verify(self):
        """-> (ok, lines, problem). Recomputes the whole chain with the key."""
        path = os.path.join(self.dir, "audit.jsonl")
        if not os.path.exists(path):
            return True, 0, ""
        key = self._key(create=False)
        if key is None:
            return False, 0, "the key is missing, so the chain cannot be checked"
        prev, n = GENESIS, 0
        with open(path, encoding="utf-8") as fh:
            for ln in fh:
                n += 1
                try:
                    rec = json.loads(ln)
                    chain = rec.pop("chain")
                except (ValueError, KeyError, AttributeError):
                    return False, n, "line %d is not a chained record" % n
                if rec.get("prev") != prev:
                    return False, n, "line %d does not follow the one before it" % n
                body = json.dumps(rec, sort_keys=True, separators=(",", ":"))
                want = hmac.new(bytes.fromhex(key), (prev + body).encode("utf-8"),
                                hashlib.sha256).hexdigest()
                if not hmac.compare_digest(chain, want):
                    return False, n, "line %d was changed" % n
                prev = chain
        try:
            with open(os.path.join(self.dir, "audit.head"), encoding="utf-8") as fh:
                head = json.load(fh)
            ok = (head.get("chain") == prev and head.get("count") == n and hmac.compare_digest(
                head.get("mac", ""), hmac.new(bytes.fromhex(key), ("%d:%s" % (n, prev)).encode(
                    "utf-8"), hashlib.sha256).hexdigest()))
        except (OSError, ValueError, AttributeError, TypeError):
            ok = False
        if not ok:
            return False, n, "the log's last record does not match audit.head (lines were removed)"
        return True, n, ""


class _DirLock(object):
    """A lock made of a directory: atomic to create on every platform, shared by threads and
    processes. A lock older than 30 seconds is stale (its holder died) and is taken over."""

    def __init__(self, path):
        self.path = path

    def __enter__(self):
        deadline = time.time() + 10
        while True:
            try:
                os.mkdir(self.path)
                return self
            except FileExistsError:
                try:
                    if time.time() - os.stat(self.path).st_mtime > 30:
                        os.rmdir(self.path)
                        continue
                except OSError:
                    pass
                if time.time() > deadline:
                    raise OSError("the audit lock is held")
                time.sleep(0.005)

    def __exit__(self, *a):
        try:
            os.rmdir(self.path)
        except OSError:
            pass


# ------------------------------------------------------------------------------ the shared check
def check(action, token, target, args_sha256=None, store=None, session=None, cfg=None, h=None):
    """THE check every chokepoint calls. -> None when `action`'s token setting is off (nothing is
    required, behaviour is exactly as before); else the spent token's {id, factor, ...}. Raises
    TokenError, whose hints name how to mint one."""
    if not required(action, cfg):
        return None
    st = store or Store(h)
    if token is None or token == "":
        e = TokenError("token_required", "%s needs a single-use token (paranoid.action_tokens.%s "
                       "is on)" % (action, action), mint_hint(action, target, args_sha256))
        st._audit_refusal(action, None, target, e.code, args_sha256, session)
        raise e
    return st.spend(action, token, target, args_sha256, session)


# ------------------------------------------------------------------------------ minting
def _ask_text(action, target, args_sha256, ttl, n=None):
    t = _clean(target, 300)
    s = "gt single-use token: allow ONE %s of %s" % (action, t)
    if args_sha256:
        s += " (content/args fingerprint %s...)" % args_sha256[:12]
    return s + ", valid %d min" % max(1, round((ttl or DEFAULT_TTL) / 60.0))


def mint_with_presence(store, presence, action, target, args_sha256=None, ttl=None, session=None):
    """Ask for presence (presence(scope, reason) -> {allowed, code, message, hints, factor}), then
    mint. A refusal or a failure to ask leaves no token and one audit line."""
    if action not in ACTION_NAMES:
        raise TokenError("bad_action", "unknown action %r" % (action,),
                         ["actions: " + ", ".join(ACTION_NAMES)])
    reason = _ask_text(action, target, args_sha256, ttl)
    try:
        ans = presence(mint_scope(action), reason)
    except Exception as e:                               # noqa: BLE001 - never a pass
        _refuse_mint(store, action, target, "no_approval")
        raise TokenError("no_approval", "no approval was given (%s); no token was made"
                         % type(e).__name__)
    if not isinstance(ans, dict) or not ans.get("allowed"):
        code = "denied" if isinstance(ans, dict) and ans.get("code") == "denied" else "no_approval"
        _refuse_mint(store, action, target, code)
        msg = (ans or {}).get("message") if isinstance(ans, dict) else None
        raise TokenError(code, "not approved: %s; no token was made" % (msg or "no confirmation"),
                         (ans.get("hints") if isinstance(ans, dict) else None) or [])
    return store.mint(action, target, args_sha256, ttl, ans.get("factor") or "unknown", session)


def _refuse_mint(store, action, target, code):
    try:
        store._append({"event": "mint", "decision": "refused", "code": code, "action": action,
                       "target": _clean(target)})
    except OSError:
        pass


def mint_plan(store, presence, items, ttl=None, session=None):
    """One confirmation mints every token of an approved plan: items = [(action, target,
    args_sha256), ...]. Only while `plan_bound` is on. -> the public views."""
    if not required("plan_bound"):
        raise TokenError("plan_bound_off", "minting a plan's tokens together is off "
                         "(paranoid.action_tokens.plan_bound)",
                         ["turn it on: python3 ~/.claude/golden-thread/hooks/gt_settings.py set "
                          "paranoid.action_tokens.plan_bound on", "or mint one token at a time"])
    items = [tuple(i) + (None,) * (3 - len(i)) for i in items]
    if not items or len(items) > 100:
        raise TokenError("bad_plan", "a plan lists 1 to 100 actions")
    for a, t, h in items:
        if a not in ACTION_NAMES:
            raise TokenError("bad_action", "unknown action %r in the plan" % (a,))
    shown = "; ".join("%s %s" % (a, _clean(t, 80)) for a, t, _h in items[:5])
    reason = "gt single-use tokens for an approved plan: %d actions (%s%s)" % (
        len(items), shown, "; ..." if len(items) > 5 else "")
    try:
        ans = presence(mint_scope("plan_bound"), reason)
    except Exception as e:                               # noqa: BLE001
        raise TokenError("no_approval", "no approval was given (%s); no token was made"
                         % type(e).__name__)
    if not isinstance(ans, dict) or not ans.get("allowed"):
        raise TokenError("denied", "not approved: %s; no token was made"
                         % ((ans or {}).get("message") if isinstance(ans, dict) else None
                            or "no confirmation"))
    return [store.mint(a, t, h, ttl, ans.get("factor") or "unknown", session)
            for a, t, h in items]


def _typed_confirm(reason):
    """No gt unlock: type a phrase at a real terminal. -> (ok, message). Weaker than Touch ID, and
    it says so: it proves a terminal is open and someone is typing, not that it is you."""
    if os.environ.get("GT_INTEGRITY_NO_TTY") == "1":
        return False, "no terminal is available to this process (GT_INTEGRITY_NO_TTY is set)"
    text = ("\n%s\nCaveat: gt unlock is not set up, so this is a typed confirmation, not Touch ID.\n"
            "Type %s to continue (anything else cancels): " % (reason, CONFIRM))
    try:
        if os.name == "nt":
            if not gt_tty.has_terminal(sys.stdin):
                return False, "this needs a terminal; this process has none"
            sys.stderr.write(text)
            sys.stderr.flush()
            answer = sys.stdin.readline()
        else:
            import select
            fd = os.open("/dev/tty", os.O_RDWR | getattr(os, "O_BINARY", 0))
            try:
                os.write(fd, text.encode("utf-8"))
                buf, deadline = b"", time.monotonic() + 180
                while b"\n" not in buf and time.monotonic() < deadline:
                    r, _w, _x = select.select([fd], [], [], 1.0)
                    if r:
                        chunk = os.read(fd, 256)
                        if not chunk:
                            break
                        buf += chunk
                answer = buf.decode("utf-8", "replace")
            finally:
                os.close(fd)
    except OSError:
        return False, "this needs a terminal; this process has none"
    return (True, "") if answer.strip() == CONFIRM else (False, "the confirmation was not given")


def cli_presence(h=None):
    """Presence for the mint command: gt unlock (Touch ID, Hello, a code) when it is on, else a
    typed confirmation at a terminal. The permission-prompt route the brokers use does not exist
    here: nothing has shown a prompt to anyone."""
    def ask(scope, reason):
        try:
            import gt_unlock_policy as P
            unlock_on = P.enabled_fast()
        except Exception:                                # noqa: BLE001
            unlock_on = False
        if unlock_on:
            import gt_io_policy as pol
            import gt_unlock_client as c
            ans = c.tty_answer if gt_tty.has_terminal(sys.stdin) else None
            v = c.check(scope, request=True, reason=reason, answer=ans)
            ok = bool(v.get("allowed"))
            _state, name = pol.factor_state(h)
            return {"allowed": ok, "code": v.get("code"), "message": v.get("message"),
                    "hints": v.get("hints") or [], "factor": (name or "gt unlock") if ok else None}
        ok, msg = _typed_confirm(reason)
        return {"allowed": ok, "code": "typed", "message": msg, "hints": [],
                "factor": "typed_confirmation" if ok else None}
    return ask


def auto_presence():
    """Presence for a target on the allow list: no prompt (the broker would not ask either), and
    the token still exists so the read is counted and recorded."""
    def ask(scope, reason):
        return {"allowed": True, "code": "allow_list", "message": "", "hints": [],
                "factor": "allow_list"}
    return ask


def _allow_listed(action, target, h=None):
    try:
        import gt_io_policy as pol
        if action in ("disk_read", "disk_list"):
            return pol.classify(target, h=h)["class"] == "allow"
        if action == "net_fetch":
            return pol.classify_url(target, h=h)["class"] == "allow"
    except Exception:                                    # noqa: BLE001
        return False
    return False


def _git_tree(repo):
    import subprocess
    p = subprocess.run(["git", "-C", repo, "write-tree"], capture_output=True, text=True)
    return p.stdout.strip() if p.returncode == 0 else None


def signed_by_gt(repo):
    """Does gt_sign sign this repository's commits? Then the signing step is the git_commit
    chokepoint (it sees the exact tree) and the pre-commit hook leaves the token to it."""
    import subprocess
    try:
        import gt_sign
        want = gt_sign._wrapper_path()
    except Exception:                                    # noqa: BLE001
        return False

    def cfg(k):
        p = subprocess.run(["git", "-C", repo, "config", "--get", k], capture_output=True, text=True)
        return p.stdout.strip() if p.returncode == 0 else ""
    return cfg("commit.gpgsign") == "true" and cfg("gpg.ssh.program") == want


def _normal_target(action, target, h=None):
    if action in ("disk_read", "disk_list", "disk_write"):
        try:
            import gt_io_policy as pol
            return pol.real_path(target, h)
        except Exception:                                # noqa: BLE001
            return target
    if action == "net_fetch":
        return canon_url(target)
    return target


# ------------------------------------------------------------------------------ the command line
def _print_error(e):
    sys.stdout.write("refused: %s (%s)\n" % (e.message, e.code))
    for h in e.hints:
        sys.stdout.write("  %s\n" % h)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="gt_action_token.py", description=(
        "Single-use action tokens (paranoid.action_tokens). Run `mint` yourself, in a terminal: "
        "the assistant has no way to, and is given only the id this prints."))
    sub = ap.add_subparsers(dest="cmd")
    m = sub.add_parser("mint", help="make one token (asks for presence)")
    m.add_argument("action", help="one of: " + ", ".join(ACTION_NAMES))
    m.add_argument("target", help="the path, URL, connection/op, repo ... the token is for")
    m.add_argument("--args-sha256", help="fingerprint of the content or arguments to bind")
    m.add_argument("--content-file", help="disk_write: a file whose text is the exact content")
    m.add_argument("--ttl", type=int, default=DEFAULT_TTL, help="seconds, at most %d" % MAX_TTL)
    m.add_argument("--session", help="bind the token to one session id")
    m.add_argument("--refs", help="git_push: the remote refs, comma separated "
                                  "(target is then the repository path)")
    m.add_argument("--tree", help="git_commit: the tree hash (default: the repository's staged tree; "
                                  "target is then the repository path)")
    p = sub.add_parser("mint-plan", help="make an approved plan's tokens under one confirmation")
    p.add_argument("plan", help="JSON file: a list of {action, target, args_sha256?}")
    p.add_argument("--ttl", type=int, default=DEFAULT_TTL)
    p.add_argument("--session")
    sub.add_parser("list", help="the unspent tokens (never their signatures)")
    sub.add_parser("verify", help="check the audit log's chain")
    sub.add_parser("status", help="which actions need a token")
    g = sub.add_parser("gate", help="spend the token in $%s for an action (for scripts: exit 0 when "
                                    "allowed or not required, 1 when refused)" % ENV_VAR)
    g.add_argument("action")
    g.add_argument("target")
    g.add_argument("--args-sha256")
    cg = sub.add_parser("git-commit-gate", help="the vault pre-commit hook's check (exit 0 allowed "
                                                "or not required; 1 refused)")
    cg.add_argument("--repo", default=".")
    f = sub.add_parser("fingerprint", help="print the fingerprint a vault_write token binds for one "
                                           "queued write (same as the vault tools compute)")
    f.add_argument("--op", required=True)
    fs = f.add_mutually_exclusive_group(required=True)
    fs.add_argument("--content")
    fs.add_argument("--content-file")
    f.add_argument("--section")
    f.add_argument("--key")
    ns = ap.parse_args(argv)
    st = Store()
    if ns.cmd == "mint":
        try:
            h = ns.args_sha256
            target = _normal_target(ns.action, ns.target)
            if ns.action == "git_push" and ns.refs:
                target = git_push_target(ns.target, [r for r in ns.refs.split(",") if r])
            elif ns.action == "git_commit" and (ns.tree or os.path.isdir(ns.target)):
                tree = ns.tree or _git_tree(ns.target)
                if not tree:
                    raise TokenError("bad_target", "cannot read the staged tree of %s" % ns.target)
                target = git_commit_target(ns.target, tree)
            if ns.content_file:
                with open(ns.content_file, encoding="utf-8") as fh:
                    h = sha256_text(fh.read())
            presence = auto_presence() if _allow_listed(ns.action, ns.target) else cli_presence()
            t = mint_with_presence(st, presence, ns.action, target, h, ns.ttl, ns.session)
        except TokenError as e:
            _print_error(e)
            return 1
        except OSError as e:
            sys.stdout.write("refused: %s\n" % (e.strerror or type(e).__name__))
            return 1
        sys.stdout.write("token %s\n  action  %s\n  target  %s\n  expires in %d s, works once\n"
                         "Give the assistant this id as the `token` argument. It is useless for "
                         "anything else.\n" % (t["id"], t["action"], _clean(t["target"], 200),
                                               int(t["expires_at"] - st.now())))
        return 0
    if ns.cmd == "gate":
        try:
            check(ns.action, token_from_env(), ns.target, ns.args_sha256)
        except TokenError as e:
            sys.stderr.write(refusal_text(e, via_env=True))
            return 1
        return 0
    if ns.cmd == "git-commit-gate":
        try:
            repo = _real(ns.repo)
            if not required("git_commit") or signed_by_gt(repo):
                return 0
            tree = _git_tree(repo)
            if not tree:
                raise TokenError("bad_target", "cannot read the staged tree, so the commit's "
                                 "token cannot be checked")
            check("git_commit", token_from_env(), git_commit_target(repo, tree))
        except TokenError as e:
            sys.stderr.write("COMMIT REFUSED -- git_commit needs a single-use token\n"
                             + refusal_text(e, via_env=True))
            return 1
        return 0
    if ns.cmd == "fingerprint":
        try:
            content = ns.content if ns.content is not None else open(
                ns.content_file, encoding="utf-8").read()
        except OSError as e:
            sys.stdout.write("refused: cannot read the content file (%s)\n" % type(e).__name__)
            return 1
        d = {"op": ns.op, "content": content}
        if ns.section:
            d["section"] = ns.section
        if ns.key:
            d["key"] = ns.key
        sys.stdout.write(args_hash(d) + "\n")
        return 0
    if ns.cmd == "mint-plan":
        try:
            with open(ns.plan, encoding="utf-8") as fh:
                data = json.load(fh)
            items = [(i["action"], _normal_target(i["action"], i["target"]),
                      i.get("args_sha256")) for i in data]
            out = mint_plan(st, cli_presence(), items, ns.ttl, ns.session)
        except TokenError as e:
            _print_error(e)
            return 1
        except (OSError, ValueError, KeyError, TypeError) as e:
            sys.stdout.write("refused: cannot read the plan (%s)\n" % type(e).__name__)
            return 1
        for t in out:
            sys.stdout.write("token %s  %s  %s\n" % (t["id"], t["action"], _clean(t["target"], 120)))
        return 0
    if ns.cmd == "list":
        rows = st.live()
        for t in rows:
            sys.stdout.write("%s  %-12s  %s  (%d s left)\n" % (
                t["id"], t["action"], _clean(t["target"], 120), int(t["expires_at"] - st.now())))
        if not rows:
            sys.stdout.write("no unspent tokens\n")
        return 0
    if ns.cmd == "verify":
        ok, n, why = st.audit_verify()
        sys.stdout.write("audit chain: %s (%d records)%s\n" % ("intact" if ok else "BROKEN", n,
                                                                "" if ok else ": " + why))
        return 0 if ok else 1
    if ns.cmd == "status":
        cfg = GP.read_config()
        for a in ACTION_NAMES:
            info = GP.ACTIONS_BY_NAME[a]
            sys.stdout.write("%-15s %-3s  %s%s\n" % (
                a, "on" if required(a, cfg) else "off", info["covers"],
                "" if info["wired"] else "   (not enforced yet)"))
        return 0
    ap.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
