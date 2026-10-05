# Live check of `lotr connect` against a real MCP server (owner, by hand)

> The verified guide is `dev/oauth-live-check-linear.md`; use that one. This file is the older, wider survey.

Everything the test suite can prove is proved against `dev/fake_oauth_mcp.py`. What it cannot prove
is how a REAL authorization server behaves. This is the ten-minute check. It needs you, a plain
checkout of this branch (no install), a terminal that is YOURS (not Claude Code's), a browser on the
same machine, and a Linear account (a free workspace is enough).

It uses a throwaway gateway home and a throwaway secret store, so nothing of your real LOTR setup is
touched and the cleanup at the end is `rm -rf`.

## Which server: Linear (recommended)

Verified 2026-10-04 from the servers' PUBLIC, unauthenticated metadata only (`dev/oauth_diag.py`, no
login, no token):

| Server (endpoint) | Registration | PKCE | Refresh | `iss` | Revocation | Scopes / hosts | Verdict |
|---|---|---|---|---|---|---|---|
| **Linear** `https://mcp.linear.app/mcp` | dynamic (`/register`) **and** metadata documents | S256 only | yes | yes (RFC 9207) | yes (`/token`) | `read write` (use `--scope read`); one host `mcp.linear.app` | **use this one** |
| Atlassian (Jira) `https://mcp.atlassian.com/v1/mcp` | dynamic (`/v1/register`) | plain + S256 | yes | no | yes (`/v1/token`) | none advertised; publishes NO protected-resource metadata, so the fallback path runs (a note says so); one host | works, but exercises the legacy path and a site-wide Atlassian consent; second choice |
| Notion `https://mcp.notion.com/mcp` | dynamic **and** metadata documents | plain + S256 | yes | no | yes | one scope `default`; one host | fine, but it grants a whole workspace |
| Sentry `https://mcp.sentry.dev/mcp` | dynamic **and** metadata documents | S256 only | yes | yes | yes (`/oauth/token`) | `org:read project:write team:write event:write alerts:write` (broad: five scopes, four are write); one host | works; consent is wide |
| GitHub `https://api.githubcopilot.com/mcp/` | **none** (no `registration_endpoint`) | S256 | yes | yes | **none** | many repo-wide scopes; hosts `api.githubcopilot.com` AND `github.com` | needs `--client-id` of an OAuth app you create; not automatic; no revocation |

Why Linear: automatic registration, the strictest PKCE (S256 only), the RFC 9207 `iss` check gets
exercised, revocation works so `disconnect` can be verified, one host, and `--scope read` limits the
consent to read-only (Linear's own `read` scope), so the sign-in is as harmless as a sign-in gets.

## 0. Set up (once)

```bash
cd <your checkout>/golden-thread-plugin/golden-thread-lotr/<version>/scripts   # version dir: 0.3.0 on the 0.20.1 branch
export LOTR_STORE_DIR="$HOME/lotr-live-store"     # the refresh token lands here (mode 600), nowhere else
H="$HOME/lotr-live-home"
python3 lotr.py --home "$H" init --zone personal --mode local
```

You should see JSON with `"ok": true` and a `wrote` list. Nothing is listening and nothing signed in.

If gt unlock is ON on this machine the refresh token is sealed through the authority instead of
written to `$LOTR_STORE_DIR`; you will be asked for your factor once. That is fine and is what the
sealed path should do; step 6 then checks `gt_unlock.py seal list` instead of the store folder.

## 1. Read-only look at the server (no login)

```bash
python3 ../../../dev/oauth_diag.py https://mcp.linear.app/mcp
```

(`dev/oauth_diag.py` is in the plugin's `dev/` folder, three levels up from `scripts`.) You should see
three requests (a 401, the protected-resource document, the authorization-server document), a
`discovery` block with issuer `https://mcp.linear.app`, `scopes ["read","write"]`,
`iss_param_supported true`, `pkce_s256 true`, and `"verdict": "registration: dynamic; metadata
documents supported"`. Nothing in it is secret.

## 2. Sign in (the one step that needs you)

```bash
python3 lotr.py --home "$H" connect linear --url https://mcp.linear.app/mcp --scope read \
    --identity "me @ linear.app" --description "Linear (OAuth live check)"
```

You should see, before any browser opens, a summary on your terminal:

```
About to sign in to:
  server      https://mcp.linear.app/mcp
  hosts       mcp.linear.app (pinned: LOTR will only talk to these)
  issuer      https://mcp.linear.app
  permissions read
  client      a new client registered automatically (dynamic client registration)
Open the browser and sign in? [y/N]
```

Check that host and permission are what you expect, answer `y`. Your browser opens Linear's consent page
for a client called `gt-lotr` asking for read access; approve. The tab says "Signed in. You can return to
the terminal." and the command prints JSON: `"added": "linear@personal"`, `"tools": <n>` (n > 0),
`"issuer": "https://mcp.linear.app"`, `"scopes": ["read"]`, `"client_id_source": "dcr"`,
`"pinned_hosts": ["mcp.linear.app"]`, `"refresh_token": "L1 (a mode-600 file ...)"` (or `L2` with unlock).
If you answer `n` nothing is registered anywhere (`oauth_declined`).

If it says `oauth_not_a_terminal`, you ran it from inside Claude Code: open a normal terminal window.
On a headless machine the URL is printed instead of opening: open it in a browser on the same machine (or
forward the printed port) and re-run with `--redirect-port <that port>`.

## 3. Use it (a read)

The call plane goes through the daemon. In a second terminal (leave it open):

```bash
export LOTR_STORE_DIR="$HOME/lotr-live-store"
cd <same scripts dir>; python3 -I lotrd.py --home "$HOME/lotr-live-home"
```

Back in the first terminal:

```bash
python3 lotr.py --home "$H" status
python3 lotr.py --home "$H" find "teams" --connection linear@personal
python3 lotr.py --home "$H" read linear@personal list_teams      # use a read tool that `find` listed
```

You should see `status` with `linear@personal`, credential `"level": "L1"` and an `oauth` block
(`refresh_token: "stored as store:"`, `access_cached: false` before the first call); `find` lists Linear's
tools with tiers (anything that creates, updates, deletes or sends shows `consent`, by design on OAuth
connections); the read returns `"ok": true` with your teams. Run `status` again: `access_cached: true`.

## 4. Forced token refresh

The access token lives only in the daemon's memory, so stopping the daemon makes the next call refresh:

```bash
shasum -a 256 "$LOTR_STORE_DIR"/lotr-oauth-*-rt          # note the hash
# in the daemon terminal: Ctrl-C, then start it again exactly as in step 3
python3 lotr.py --home "$H" read linear@personal list_teams
shasum -a 256 "$LOTR_STORE_DIR"/lotr-oauth-*-rt
```

You should see the read succeed without any browser. If Linear rotates refresh tokens the second hash
differs (the rotated token was re-stored before use); if it does not rotate the hashes match, which is
fine. `status` shows `refresh_unpersisted: false`.

## 5. Lapse and recovery (optional, 2 minutes)

In Linear: Settings > Security & access > Authorized applications > revoke `gt-lotr`. Then:

```bash
python3 lotr.py --home "$H" read linear@personal list_teams
```

You should see `"ok": false`, `"code": "needs_login"` and the hint `run: lotr login linear@personal`
(with `--zone Z` if your zone is not `personal`). Then `python3 lotr.py --home "$H" login linear` (it prints
the same summary and asks again) and the read works. `login` reports `old_token_revoked`.

## 6. Disconnect and leftover-state check

```bash
python3 lotr.py --home "$H" disconnect linear
```

You should see `"revoked": true`, `"secrets": [{"ref": "store:lotr-oauth-...", "deleted": true}]` and no
notes. Now prove nothing is left:

```bash
ls -la "$LOTR_STORE_DIR"                                   # empty
python3 lotr.py --home "$H" connections                    # "connections": []
grep -c "lotr-oauth" "$H/registry.json"                    # 0
pgrep -fl lotrd.py                                         # the daemon you started; stop it (Ctrl-C)
lsof -nP -iTCP -sTCP:LISTEN 2>/dev/null | grep -i "127.0.0.1" | grep -i python   # no stray listener
# with unlock on:  python3 ~/.claude/golden-thread/hooks/gt_unlock.py seal list    # no lotr-oauth-* name
```

In Linear the app is gone from Authorized applications (it is, after step 5 or after `disconnect`'s
revocation). Cleanup of the throwaway state:

```bash
rm -rf "$HOME/lotr-live-home" "$HOME/lotr-live-store"
```

## If anything fails: run this and send the output

```bash
python3 ../../../dev/oauth_diag.py https://mcp.linear.app/mcp --register \
    --home "$HOME/lotr-live-home" --name linear
```

It prints a redacted structure only: every request's method, URL without query, status, content type and
size; the `WWW-Authenticate` parameters; the discovery facts; the registration result (status, whether
the answer was acceptable, the first six characters of the client id); and, with `--home/--name`, the
registered connection's shape (ref SCHEMES only, level, pinned hosts, whether the store file exists and
its mode). It never prints a token, code, state, verifier or secret, never starts a login, and `--register`
only creates a client record at the server (harmless, and the same thing `connect` does).

Also useful, and also secret-free: the JSON error `lotr` printed (`code`, `message`, `hints`).

## What to report back

Anything that differs from a "You should see": a refused redirect URI (try `--redirect-port 53682`),
`needs_login` right after a successful login (a rotation or audience problem), `oauth_audience_mismatch`
(`--skip-audience-check`, and tell us the issuer), a missing refresh token (try `--offline-access`),
`revoked: false`, or a consent screen asking for more than the summary said.
