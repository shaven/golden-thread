# gt-lotr 0.1.0 — build spec (interfaces every module codes against)

Design: vault `Projects/golden-thread/mcp-gateway/design.md`, ADR-1..4. This file pins the
**interfaces** so modules can be written in parallel. Change an interface here first.

## Ground rules

- **Stdlib only, Python >= 3.9.** It must run on this Mac (3.9.6) and on a Linux hub. No
  third-party imports anywhere in `scripts/`.
- **A secret value never reaches stdout, stderr, a log, an exception message, a result or
  argv.** Exceptions name the *ref*, never the value. Tests assert this.
- Package: `scripts/lotrlib/` (`__init__.py` empty). Entry scripts sit in `scripts/` and put
  their own directory on `sys.path`.
- All JSON on disk is UTF-8 with `indent=2`. Writes go through `lotrlib.util.atomic_write`.
- Times: `lotrlib.util.now_iso()` (UTC, seconds, `Z`).
- Errors cross module boundaries as `lotrlib.errors.GatewayError(code, message, hints=None)`.

## Layout on disk

```
$LOTR_HOME                      default ~/.config/gt-lotr/<zone>   (zone from --zone or "personal")
  gateway.json                  settings: placement and front-end config (admin plane)
  registry.json                 connections + clients (admin plane; the authority)
  recipes/*.json                owner recipes (in addition to the shipped ones)
  state/audit.jsonl             append-only call log
  state/index.json              cached index (rebuildable)
  lotrd.sock                    unix socket, mode 600 (local / hub-admin)
```

### gateway.json

```json
{
  "schema": 1,
  "zone": "personal",
  "mode": "local",
  "socket": null,
  "listen": {"host": "127.0.0.1", "port": 8765, "tls_cert": null, "tls_key": null},
  "hub": {"url": null, "client_id": null, "credential_ref": null, "timeout_s": 8},
  "local_connections": [],
  "offline": "fail-closed",
  "limits": {"max_result_chars": 24000, "default_page": 20}
}
```

`mode`:
- `local`: the daemon serves the unix socket.
- `hub`: the daemon serves the unix socket (admin and local use) **and** HTTP on `listen` for
  enrolled clients.
- `client`: no daemon. The CLI and MCP shim talk HTTP to `hub.url`.
- `hybrid`: a local daemon serves `local_connections` and forwards everything else to the hub.
  **0.1.0 implements local, hub and client. Hybrid is parsed and refused** with
  `GatewayError("not_implemented")`.

### registry.json

```json
{
  "schema": 1,
  "zone": "personal",
  "connections": [{
    "id": "github@personal",
    "identity": "shaven @ github.com",
    "zone": "personal",
    "kind": "http",
    "profile": "github",
    "description": "GitHub repos, issues and PRs as shaven",
    "base_url": "https://api.github.com",
    "auth": {"scheme": "bearer", "token_ref": "keychain:gt-lotr/github-personal", "user": null, "header": null},
    "headers": {},
    "network": {"hosts": ["api.github.com"]},
    "trust": "T0",
    "policy": {"deny": [], "consent": [], "write": [], "read": []},
    "enabled": true
  }],
  "clients": [{
    "id": "mbp-shaven",
    "machine": "MacBook Pro",
    "zone": "personal",
    "secret_sha256": "<hex>",
    "allow": ["*"],
    "max_tier": "write",
    "revoked": null
  }]
}
```

- `auth.scheme`: `bearer` | `basic` (`user` + token as password) | `header` (raw token in header
  `auth.header`) | `none`.
- `policy` lists are **globs** (`fnmatch`) matched against both the op *name* and its
  `"METHOD /path"` form.

## Module interfaces

### lotrlib/util.py, lotrlib/errors.py (lead writes)

```python
class GatewayError(Exception):
    def __init__(self, code: str, message: str, hints: list = None): ...
    def to_dict(self) -> dict  # {"code","message","hints"}
def atomic_write(path, text: str, mode: int = 0o600) -> None
def now_iso() -> str
def read_json(path) -> dict          # raises GatewayError("bad_json", ...) naming the path
def lotr_home(zone: str = None) -> Path   # $LOTR_HOME or ~/.config/gt-lotr/<zone>
```

### lotrlib/secrets.py (agent A)

```python
def resolve(ref: str, *, store_dir: Path = None) -> str
```

Ref schemes:
- `file:/abs/path`: the file must not be group- or world-readable (`mode & 0o077 == 0`), or
  `GatewayError("secret_perms")`. Strips one trailing newline.
- `store:<name>`: `file:` under `store_dir` (default `$LOTR_STORE_DIR` or `~/.secrets`).
- `keychain:<service>/<account>`: macOS `security find-generic-password -s S -a A -w`. Run with
  argv only, stdout captured, never echoed. Not found → `GatewayError("secret_missing")`.
- `env:<NAME>`: allowed **only** when `LOTR_ALLOW_ENV_SECRETS=1` (tests). Otherwise
  `GatewayError("secret_scheme_refused")`.

Every error names the ref, never the value. `describe(ref) -> dict` returns
`{scheme, target, present: bool}` without reading the value where possible (for `file:`, stat
only).

### lotrlib/registry.py (agent A)

```python
class Registry:
    @classmethod
    def load(cls, path) -> "Registry"       # validates; GatewayError("registry_invalid", msg naming field)
    zone: str
    connections: dict[str, dict]            # id -> entry (enabled ones only via .active())
    clients: dict[str, dict]
    def active(self) -> list[dict]
    def connection(self, cid) -> dict       # GatewayError("unknown_connection", hints=[closest ids])
    def client(self, client_id) -> dict      # GatewayError("unknown_client")
    def authenticate(self, client_id, secret: str) -> dict   # sha256 compare (hmac.compare_digest); revoked -> "client_revoked"
    def add_connection(self, entry) -> None  # validate + refuse duplicate id + refuse zone != registry zone
    def enroll(self, client_id, machine, allow, max_tier) -> str   # returns NEW secret; stores only sha256
    def revoke(self, client_id) -> None
    def save(self, path) -> None             # atomic, mode 600
def validate_connection(entry, zone) -> None
```

Validation:
- `id` matches `^[a-z0-9][a-z0-9._-]*@[a-z0-9][a-z0-9._-]*$`.
- `zone` equals the registry zone (**zones never mix**).
- `kind == "http"` (0.1.0).
- `base_url` is https, **or** http to `127.0.0.1`/`localhost` (for tests).
- `base_url` host is in `network.hosts`.
- `trust` is in `T0/T1/T2`.
- `auth.token_ref` is present unless scheme is `none`, and is **never a literal secret**: it
  must match `^(file|store|keychain|env):`.

### lotrlib/policy.py (agent A)

```python
TIERS = ("read", "write", "consent")
def classify(conn: dict, op: dict) -> str
    # op = {"name": str|None, "method": "GET", "path": "/x", "tier": optional profile override,
    #       "graphql_query": optional str}
    # returns "deny" | "consent" | "write" | "read"
def allowed_through(tool: str, tier: str) -> bool
    # tool in {"call_read","call_write","call_consent"}:
    # call_read: read only; call_write: read|write; call_consent: read|write|consent
def check_client(client: dict, conn_id: str, tier: str) -> None
    # GatewayError("client_not_allowed") if conn not in allow (supports "*" and fnmatch);
    # GatewayError("tier_ceiling") if tier above max_tier
```

`classify` order:
1. `policy.deny` → deny.
2. `policy.consent` → consent.
3. `policy.write` → write.
4. `policy.read` → read.
5. Profile override `op["tier"]`.
6. GraphQL: `graphql_query` is lexed whole (comments and strings removed); read only if it holds exactly one operation, a `query` or the `{...}` shorthand, plus fragment definitions. Mutation, subscription, several operations, or anything unparsable → write. An explicit `body` is classified in place of `query`.
7. Method: GET/HEAD → read; anything else → write.

Unknown → write.

### lotrlib/audit.py (agent A)

```python
def record(home: Path, **fields) -> None   # appends one JSON line to state/audit.jsonl (mode 600)
```

Fields: `ts, client, connection, identity, op, tier, tool, verdict, status, args_sha256`.
**Never raw args.**

### lotrlib/profiles.py (agent B)

```python
PROFILES: dict[str, dict]   # "github", "jira-v3", "jira-v2", "graph", "generic"
def get(name) -> dict       # GatewayError("unknown_profile")
def resolve_op(profile: dict, op: str) -> dict
```

`resolve_op` returns `{"name","method","path","summary","params","query_defaults","tier"?,
"tags","graphql": bool}`. `op` is either a curated op name (e.g. `"list_pulls"`) or a raw
`"METHOD /path/{param}"` string; a raw op returns `name=None`. Unknown curated name →
`GatewayError("unknown_op", hints=[closest names])`.

Each profile is:

```python
{"name","title","auth_hint","pagination": "link-header"|"jira-token"|"jira-startat"|"odata"|None,
 "noise_keys": [...],               # dropped by shaping
 "ops": [ {name, method, path, summary, params: {pname: description}, query_defaults: {...},
           tier?: "consent"|"write"|"read", tags: [...]} ]}
```

Curated ops are 8–15 per profile:
- **github:** list_pulls, get_pull, list_issues, get_issue, search_issues, create_issue,
  comment_issue, merge_pull (tier consent), get_repo, list_workflow_runs, graphql
  (`POST /graphql`).
- **jira-v3:** myself, search (`GET /rest/api/3/search/jql`, query_defaults
  `maxResults=20, fields=summary,status,assignee,priority,updated`), get_issue (`fields` default
  as above), get_transitions, transition_issue, add_comment, create_issue.
- **jira-v2:** the same names on `/rest/api/2/...` (search = `GET /rest/api/2/search`).
- **graph:** me, list_messages (`/me/messages`, `$select=subject,from,receivedDateTime,isRead`,
  `$top=20`), get_message, send_mail (consent), list_events (`/me/calendarView`), list_drive_root,
  search_drive.
- **generic:** no ops; raw only.

### lotrlib/index.py (agent B)

```python
class Index:
    def __init__(self, docs: list[dict]): ...   # doc = {"key","connection","op","kind","summary","text","tier"?,"stale"?}
    def search(self, query: str, *, connection: str = None, limit: int = 8) -> list[dict]
def build_docs(connections: list[dict], recipes: list[dict]) -> list[dict]
```

- **Search** is BM25 (k1=1.2, b=0.75), tokens lowercased and split on non-alphanumerics, with
  `_`, `.`, `/` and `{}` also splitting.
- **Docs:** one per (connection × curated op), one per (connection × applicable recipe), one per
  connection (kind `"connection"`, text = id + identity + description + profile title).
- **Empty query** → every connection doc (the catalog), up to `limit=200`.
- **Ranking:** recipes get a ×1.5 boost; stale docs ×0.3.

### lotrlib/recipes.py (agent B)

```python
def load_recipes(dirs: list[Path]) -> list[dict]     # later dirs override same name
def applicable(recipe, conn) -> bool                 # conn["profile"] in recipe["profiles"]
def expand(recipe, conn, args: dict) -> list[dict]   # -> steps [{"op": str, "args": dict}] with {{param}} filled
```

Recipe file:

```json
{"name": "jira.my_open_issues", "summary": "...", "tier": "read",
 "params": {"project": {"description": "...", "default": null}},
 "profiles": {"jira-v3": {"steps": [{"op": "search", "args": {"jql": "assignee = currentUser() AND statusCategory != Done{{#project}} AND project = {{project}}{{/project}} ORDER BY updated DESC"}}]},
              "jira-v2": {"steps": [...]}},
 "select": "issues[].key,issues[].fields.summary,issues[].fields.status.name"}
```

- Templating: `{{name}}`, plus the section `{{#name}}…{{/name}}`, which is omitted when the arg is
  null or empty.
- 0.1.0 runs a recipe's steps in order and returns the **last** step's result (shaped by the
  recipe's `select`).
- Ship 4 recipes in `templates/recipes/`: `github.my_open_prs`, `github.pr_status`,
  `jira.my_open_issues`, `m365.today` (graph list_events for today).

### lotrlib/shaping.py (agent C)

```python
def project(data, select: str | None)
def shape(data, *, noise_keys=(), select=None, max_chars=24000, default_page=20) -> tuple[object, list[str]]
def scan_credentials(obj) -> list[dict]    # [{"path": "a.b[2]", "rule": "github_pat", "length": 40}]
```

- **`project`:** comma list of dotted paths. `a[].b` maps over a list. A top-level list applies
  each path to its elements. Missing → omitted.
- **`shape`:**
  - drops `noise_keys` and any key ending `_url` (except `html_url`), plus `node_id`,
    `avatar_url`, `_links`, `self`;
  - truncates lists longer than `default_page`, with a note naming how many were cut;
  - caps the serialized size at `max_chars`, with a note on how to narrow (`select` / `limit`).
  - Returns `(data, notes)`.
- **`scan_credentials`** covers:
  - GitHub tokens (`ghp_ gho_ ghu_ ghs_ ghr_ github_pat_`)
  - Slack `xox[abpors]-`
  - AWS `AKIA[0-9A-Z]{16}`
  - PEM private key headers
  - JWTs `eyJ…\.eyJ…\.`
  - `Bearer <20+>`
  - Atlassian `ATATT`

  It **never returns the matched text.**

### lotrlib/conn_http.py (agent C)

```python
class HttpConnection:
    def __init__(self, conn: dict, profile: dict, *, secret_resolver=secrets.resolve, timeout=20): ...
    def call(self, op: dict, args: dict, *, cursor: str = None) -> dict
        # returns {"status": int, "data": parsed JSON or text, "next_cursor": str|None}
```

- **Path params** are filled from args, URL-quoted. Missing → `GatewayError("missing_param")`.
- **Remaining args:** to the query for GET/HEAD/DELETE (merged over `query_defaults`), and to the
  JSON body otherwise.
- **`args["query"]` / `args["body"]`**, if present, are used verbatim. Graph's `$select` keys
  pass through unchanged.
- **Auth header** is added from `secret_resolver(token_ref)` at request time and never stored on
  `self`.
- **Host check:** every request's host must be in `network.hosts`, or
  `GatewayError("host_not_allowed")`, including the next-page URL.
- **Pagination:**
  - The next URL comes from the GitHub `Link rel=next`, Graph `@odata.nextLink`, or Jira
    `nextPageToken` (re-request with `nextPageToken`) / `startAt + maxResults < total`.
  - The cursor is `base64url(json{"u": url})`. On use, the host and the `base_url` path prefix
    are validated.
- **HTTP errors:** >= 400 → `GatewayError("http_<status>", message from body (truncated to 300
  chars, credential-scanned), hints)`.
- **Drift hints:** a 404 on a curated op gets the hint "operation may have moved; run
  lotr find again".
- **User-Agent:** `gt-lotr/0.1.0`.

### lotrlib/engine.py, lotrlib/config.py (lead writes)

```python
class Engine:
    def __init__(self, home: Path): ...       # loads gateway.json + registry.json, recipes, index; reload() on mtime change
    def find(self, query="", connection=None, limit=8, detail="summary", client_id=None) -> dict
    def call(self, tool, connection, op, args=None, select=None, cursor=None, client_id=None) -> dict
    def status(self) -> dict
    def catalog_text(self, max_chars=1800) -> str   # MCP server instructions
```

Every `call` result is an envelope:

```json
{"ok": true, "connection": "...", "identity": "...", "client": "...", "op": "...", "tier": "read",
 "data": ..., "next_cursor": null, "notes": [], "untrusted": true}
```

or `{"ok": false, "error": {"code","message","hints"}}`. If `scan_credentials` finds anything,
`data` is replaced by `null` and `withheld: [{"path","rule","length"}]`.

### lotrlib/server.py, lotrlib/client.py (agent D)

```python
def serve_unix(engine_factory, sock_path, *, stop_event=None) -> None
def serve_http(engine_factory, host, port, *, registry_getter, tls_cert=None, tls_key=None, stop_event=None) -> None
class Client:
    @classmethod
    def from_home(cls, home: Path) -> "Client"
    def request(self, method: str, params: dict) -> dict
```

- **Unix socket protocol:** one JSON object per line. Request `{"id", "method", "params"}` →
  response `{"id", "result"}` or `{"id", "error": {...}}`.
  - Methods: `find`, `call`, `status`, `catalog`, `ping`.
  - The socket is chmod 600. The client id is `"local"`.
- **HTTP (hub):** `POST /v1/<method>` with JSON params.
  - Headers: `Authorization: Bearer <secret>`, `X-GT-Client: <client_id>`.
  - Authenticated via `Registry.authenticate`. 401 when absent, 403 when revoked.
  - **No admin methods over HTTP** (enroll, add, revoke and reload are absent, so 404).
  - One engine per process, guarded by a lock.
- **`Client.from_home`:**
  - mode `local`/`hub` → unix socket.
  - mode `client` → HTTP to `hub.url` with the bearer from `secrets.resolve(hub.credential_ref)`.
- **A down daemon** gives `GatewayError("daemon_unreachable", hints=["start it: lotrd --home …"])`,
  never a traceback.

### scripts/lotr.py (CLI), scripts/lotrd.py (daemon), scripts/lotr_mcp.py (MCP stdio shim) — agent D

**CLI** (`lotr [--home H | --zone Z] <cmd>`). Stdout is JSON; exit 0 on ok, 1 on a gateway error,
2 on usage.

Call plane:
- `find [QUERY] [--connection C] [--limit N] [--schema]`
- `read|write|consent CONN OP [--args JSON] [k=v ...] [--select S] [--cursor X]`
- `status`
- `catalog`

Admin plane (operates on files directly; refused in mode `client`):
- `init --zone Z --mode M`
- `add-http ID --profile P --base-url U --identity S --auth bearer|basic|header|none [--token-ref R] [--user U] [--header H] [--description D] [--trust T]`
- `enroll CLIENT --machine M [--allow ...] [--max-tier T] --secret-out PATH`
  - Writes the new secret to `PATH`, mode 600, refusing if `PATH` exists.
  - **Never prints it.**
- `revoke CLIENT`
- `connections`

**`lotrd [--home H]`:** runs `serve_unix`, plus `serve_http` when mode is `hub`. Reloads the
registry when its mtime changes.

**`lotr_mcp.py [--home H]`:** an MCP server over stdio, JSON-RPC 2.0, newline-delimited.
- `initialize`:
  - Answers with the client's `protocolVersion` if it is in
    `{"2025-11-25","2025-06-18","2025-03-26"}`, else `"2025-06-18"`.
  - capabilities `{"tools": {"listChanged": false}}`, serverInfo `{"name":"gt-lotr","version":"0.1.0"}`.
  - `instructions` = `catalog` from the daemon (fallback: a static line).
- `tools/list`: exactly 4 tools, `find`, `call_read`, `call_write` and `call_consent`.
  - Each has `inputSchema` and `_meta: {"anthropic/alwaysLoad": true}`.
  - Annotations: `find` and `call_read` are `readOnlyHint: true`; `call_write` is
    `destructiveHint: false`; `call_consent` is `destructiveHint: true` and also has
    `_meta["anthropic/requiresUserInteraction"] = true`.
  - From 0.3.0 (gt 0.20.0), when the negotiated protocol is 2025-06-18 or later, each tool also
    has an `outputSchema` describing its envelope (`ok` required; the error object; for calls
    `data`, `next_cursor`, `notes`, `withheld`, with `data` untyped). A 2025-03-26 client gets
    the list without it, since that protocol has no `outputSchema`.
- `tools/call`: forwards to the daemon via `Client`. The result is
  `{"content":[{"type":"text","text": json}], "structuredContent": envelope, "isError": not ok}`.
  An envelope without a boolean `ok` gets one, so every result conforms to the declared schema.
- `ping`, and `notifications/*` (ignored). Unknown method → -32601.
- **The shim never exits on a daemon error**; it returns a tool error instead.

## Local security (ADR-5), added 2026-10-01

- `config.check_private(home)`: the home is 700, and `gateway.json`/`registry.json` are 600, else
  `GatewayError("insecure_perms")`. It is called by `load_settings`.
- `gateway.json` `local`: `{"allow": ["*"], "max_tier": "consent", "confirm": "auto"}`. The engine
  treats local callers as client `local` with this allow list and ceiling.
- `server.peer_uid(sock)`: the kernel peer uid, or None. `_UnixHandler` refuses anything not
  `os.getuid()` (`peer_refused`).
- `server.check_private_dir(dir)`: the socket directory is owned and 700.
- `serve_http`: a non-loopback host without TLS raises `insecure_listen`.
- `lotrlib/confirm.py`: `confirm(mode, text, dialog=None)` and `describe(...)`. The engine calls it for
  every consent-tier op, whoever the caller. `Engine(..., dialog=callable)` is for tests.

## 0.3.0: the gt unlock consumer and the Windows front door (gt 0.20.0)

Zero behaviour change while gt unlock is off (its default): every 0.2.0 test runs unchanged.

### lotrlib/unlock.py

Finds gt core's client at run time: `GT_HOOKS_DIR` (honoured only while the real home's unlock is
off, like `GT_UNLOCK_HOME` in gt_unlock_client), else `~/.claude/golden-thread/hooks`, where gt
installs `gt_unlock_client.py` and `gt_ipc.py`. Nothing is imported until unlock is asked about.

| Function | Contract |
|---|---|
| `enabled()` | the client's `enabled()`; without the client, the policy files alone (admin floor, `policy.json`); any doubt is ON |
| `check(scope, subject, request=, reason=, answer=)` | grant id, or None when off / open; raises `GatewayError(<authority code>)` (`locked`, `mcp_only`, `step_up`, `failed_closed`, `unreachable`, ...) |
| `call(method, params)` | one authority request (`consent`, `secret`, `register_shim`, `unlock`) |
| `kernel_peer(sock)` | `{pid, start}` of a unix-socket peer via `gt_ipc.unix_peer`, only while on |

On and the client unloadable: `unlock_unavailable` (fail closed).

### Front doors

- **Unix socket** (unchanged checks: private dir, 600, peer uid). While unlock is on, every request
  also carries the caller's kernel identity `{pid, start}` to the engine as `subject`; an
  unidentifiable peer is refused (`peer_refused`). `find` / `status` / `catalog` are checked as
  scope `lotr:catalog:read` (connection ids always contain `@`, so it names none).
- **Named pipe** (native Windows): `server.serve_pipe(factory, home)` on
  `gt_ipc.default_address(home, "lotrd")`, same JSON-lines protocol. DACL `D:P(A;;GA;;;<user SID>)`,
  `PIPE_REJECT_REMOTE_CLIENTS`, `FILE_FLAG_FIRST_PIPE_INSTANCE` (a held name is `pipe_squatted`);
  each client's SID comes from its impersonated token. `Client.from_home` connects with
  `gt_ipc.connect`, which checks the server's owner SID before sending. gateway.json `socket` does
  not apply there.
- **Mode bits on Windows:** `config.check_private`, the audit log's chmod and `file:`/`store:`
  600 checks are skipped (st_mode cannot express an ACL). The home lives in the user's profile,
  whose ACL (user, SYSTEM, Administrators) draws the same boundary as 700.

### Engine

`call(..., subject=None)`. For a LOCAL caller while unlock is on: after the tier is assigned and
`wrong_tool` is checked, and before `policy.check_client`, scope `lotr:<connection>:<tier>` is
checked with `subject` and `request=False`. A local caller with no subject is `peer_unidentified`.
Hub HTTP clients (subject None) keep their bearer + revocation model, unchanged.

Consent: with the authority's `consent_requires_factor: "platform"`, the authority's `consent`
method (subject, `op_hash` = sha256 of `tool|connection|op|args_sha256`, text = `confirm.describe`)
is the confirmation; a failed factor is `consent_denied` (never a fallback to the dialog). Mode
`none` runs the 0.2.0 `local.confirm` path. New `local.confirm` value `biometric`: require the
platform route; refuse (`consent_refused`) when it is unavailable.

Audit: `FIELDS` gains `grant` (null when off / open / refused before a grant), and local lines
carry the caller's `pid`. Refusals are audited too.

### Secrets

New brokered schemes `sealed:`, `sops:`, `op:`, `bw:`, `vault:`, `wincred:`: resolved only by the
authority's `secret` method (`{ref, subject}`), which requires a grant for `gt:secrets` and audits
the resolve with its id. `describe()` reports scheme + level from `gt_unlock_brokers.describe(ref)`
when installed. `keychain:` refs are labelled **L1** (and `lotr status` labels `file:`/`store:` L1).

### MCP shim

At startup, while unlock is on: `register_shim` (failure logged to stderr, not fatal). A result
with code `locked`: one `unlock` request for itself (reason names the tool, connection, op; the
authority raises the prompts), then one retry. Never a loop.

### CLI

`enroll` / `revoke`: while unlock is on, scope `gt:hub:enroll` with `request=True` (step-up)
before anything is written. `add-mcp --refresh-cmd` is split with Windows rules on Windows.
