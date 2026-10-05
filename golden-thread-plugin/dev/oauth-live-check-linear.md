# Live OAuth check against Linear (zero setup, ten minutes)

This checks gt-lotr's own OAuth against a REAL server. Linear needs no console setup, no client to
create and no secret to store: the client registers itself (dynamic client registration), the sign-in
is read-only (`--scope read`), and the whole thing is deleted at the end with `rm -rf`.

You need: a terminal of your own (NOT Claude Code's), a browser on the same machine, Python 3.8+, and
a Linear account. No Linear account: sign up at https://linear.app (free, about two minutes, any
email, create a workspace and one issue so there is something to read).

What was verified before you run it (2026-10-04, from Linear's PUBLIC metadata only, no login): the
server answers 401 with `resource_metadata`; its authorization server offers dynamic registration and
metadata documents, PKCE `S256` only, refresh tokens, RFC 9207 `iss`, revocation, and one host
(`mcp.linear.app`). The same flow was run end to end against a fake server of that shape: consent
prompt, sign-in, read, restart + refresh with rotation, disconnect with revocation, nothing left over.

## 0. Get the code (no install)

```bash
SHA=<the commit you were given>
git -C ~/Code.noindex/golden-thread worktree add --detach ~/lotr-live-check "$SHA"
cd ~/lotr-live-check/golden-thread-plugin/golden-thread-lotr/0.3.0/scripts
export LOTR_STORE_DIR="$HOME/lotr-live-store"        # the refresh token lands here, mode 600, nowhere else
H="$HOME/lotr-live-home"
python3 lotr.py --home "$H" init --zone personal --mode local
```

You should see JSON with `"ok": true` and a `wrote` list. (If gt unlock is ON on this machine the
refresh token is sealed instead and you will be asked for your factor once; that is the sealed path
working. Then step 6 checks `gt_unlock.py seal list`.)

## 1. Look at the server (no login, nothing secret)

```bash
python3 ../../../dev/oauth_diag.py https://mcp.linear.app/mcp
```

You should see three requests (401, the protected-resource document, the authorization-server
document) and `"verdict": "registration: dynamic; metadata documents supported"`, issuer
`https://mcp.linear.app`, scopes `["read","write"]`, `iss_param_supported: true`.

## 2. Sign in

```bash
python3 lotr.py --home "$H" connect linear --url https://mcp.linear.app/mcp --scope read \
    --identity "me @ linear.app"
```

You should see, before any browser opens:

```
About to sign in to:
  server      https://mcp.linear.app/mcp
  hosts       mcp.linear.app (pinned: LOTR will only talk to these)
  issuer      https://mcp.linear.app
  permissions read
  client      a new client registered automatically (dynamic client registration)
Open the browser and sign in? [y/N]
```

Answer `y`. Linear's consent page for a client named `gt-lotr` asks for read access: approve. The tab
says "Signed in. You can return to the terminal." and the command prints JSON with
`"added": "linear@personal"`, `"tools": <n>` (n > 0), `"scopes": ["read"]`,
`"client_id_source": "dcr"`, `"pinned_hosts": ["mcp.linear.app"]` and `"refresh_token": "L1 (a
mode-600 file ...)"`. Answering `n` registers nothing anywhere (`oauth_declined`).

`oauth_not_a_terminal` means you ran it inside Claude Code: use a normal terminal window. On a
machine with no display the URL is printed instead: open it in a browser on the same machine (or
forward the printed port) and re-run with `--redirect-port <that port>`.

## 3. Read through the daemon

Second terminal, leave it running:

```bash
export LOTR_STORE_DIR="$HOME/lotr-live-store"
cd ~/lotr-live-check/golden-thread-plugin/golden-thread-lotr/0.3.0/scripts
python3 -I lotrd.py --home "$HOME/lotr-live-home"
```

First terminal:

```bash
python3 lotr.py --home "$H" status
python3 lotr.py --home "$H" find "teams" --connection linear@personal
python3 lotr.py --home "$H" read linear@personal list_teams      # a read tool that `find` listed
```

You should see: `status` shows `linear@personal`, credential level `L1`, `oauth.access_cached: false`
before the first call; `find` lists Linear's tools (any that create, update, delete or send show tier
`consent`: by design on OAuth connections); the read returns `"ok": true` with your teams; `status`
now shows `access_cached: true`.

## 4. Forced refresh

```bash
shasum -a 256 "$LOTR_STORE_DIR"/lotr-oauth-*-rt          # note the hash
# daemon terminal: Ctrl-C, then start it again exactly as in step 3
python3 lotr.py --home "$H" read linear@personal list_teams
shasum -a 256 "$LOTR_STORE_DIR"/lotr-oauth-*-rt
```

The read succeeds with no browser (the cold daemon refreshed). If Linear rotates refresh tokens the
second hash differs (the new token was stored before use); if not they match. Both are fine.

## 5. Lapse and recovery (optional)

In Linear: Settings > Security & access > Authorized applications > revoke `gt-lotr`. Then:

```bash
python3 lotr.py --home "$H" read linear@personal list_teams
```

You should see `"ok": false`, `"code": "needs_login"`, hint `run: lotr login linear@personal`. Then
`python3 lotr.py --home "$H" login linear` (same summary, same prompt) and the read works again.

## 6. Disconnect and prove nothing is left

```bash
python3 lotr.py --home "$H" disconnect linear
```

You should see `"revoked": true`, `"secrets": [{"ref": "store:lotr-oauth-...", "deleted": true}]`
and empty `notes`. Then:

```bash
ls -la "$LOTR_STORE_DIR"                         # empty
python3 lotr.py --home "$H" connections          # "connections": []
grep -c lotr-oauth "$H/registry.json"            # 0
pgrep -fl lotrd.py                               # only the daemon you started: Ctrl-C it
lsof -nP -iTCP -sTCP:LISTEN 2>/dev/null | grep 127.0.0.1 | grep -i python    # nothing
```

Where to check by hand in Linear: Settings > Security & access > Authorized applications: `gt-lotr`
must be gone (if `disconnect` said `revoked: true` it is; otherwise revoke it there).

Cleanup:

```bash
rm -rf "$HOME/lotr-live-home" "$HOME/lotr-live-store"
git -C ~/Code.noindex/golden-thread worktree remove --force ~/lotr-live-check
```

## If anything fails

Run this and send the output. It prints a redacted structure only (request methods, URLs without
query, status codes, content types, sizes, the `WWW-Authenticate` parameters, discovery facts, the
registration result with the first six characters of the client id, and the registered connection's
shape: ref SCHEMES only, level, pinned hosts, whether the store file exists and its mode). It never
prints a token, code, state, verifier or secret and never starts a login:

```bash
python3 ../../../dev/oauth_diag.py https://mcp.linear.app/mcp --register \
    --home "$HOME/lotr-live-home" --name linear
```

Also send the JSON error `lotr` printed (`code`, `message`, `hints`; no secret is ever in it).

Things worth reporting even if it "worked": a consent screen asking for more than the summary said;
`needs_login` right after a successful sign-in; `revoked: false`; a refused redirect URI (try
`--redirect-port 53682`); the browser tab not saying "Signed in".
