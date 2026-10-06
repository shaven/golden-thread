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
    "strict_tools": true,
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
def allowed_through(tool: str, tier: str, strict: bool = True) -> bool
    # tool in {"call_read","call_write","call_consent"}:
    # strict (default): call_read: read; call_write: write; call_consent: consent -- own tier only
    # strict False:     call_read: read; call_write: read|write; call_consent: read|write|consent
def strict_tools(conn: dict) -> bool
    # the connection's `strict_tools` (registry.json); True unless it is exactly false
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
5b. Consent twins (no explicit op tier): `DELETE` on any connection; a non-GET raw path that
   normalises (query dropped, percent-decoded, lower-cased, `//` collapsed, trailing `/` dropped) to
   a GitHub pull merge (`/repos/o/r/pulls/N/merge`, `/repositories/ID/pulls/N/merge`), a Graph
   send (`/me|users/X/sendMail`, `.../messages/ID/send|forward|reply|replyAll|createReply|
   createForward`) or `/$batch` → consent. Other destructive endpoints on a generic connection stay
   write unless the owner adds `policy.consent` globs.
5c. Read-only POSTs (no explicit op tier), by profile only: Jira (`jira-v2`, `jira-v3`)
   `POST /rest/api/{2,3,latest}/search`, `/search/jql`, `/search/approximate-count`; Graph
   `POST /search/query`, `/{me|users/X}/calendar/getSchedule`, `/findMeetingTimes`,
   `/getMemberGroups`, `/checkMemberGroups`, `/getMemberObjects` → read. On any other profile,
   and for every other POST, the method rule applies (write).
6. GraphQL: `graphql_query` is lexed whole (comments and strings removed); read only if it holds exactly one operation, a `query` or the `{...}` shorthand, plus fragment definitions. Mutation, subscription, several operations, or anything unparsable → write, except a document naming a `mergePullRequest` / `enablePullRequestAutoMerge` mutation → consent. An explicit `body` is classified in place of `query`.
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
  - From 0.3.0 (gt 0.20.1), when the negotiated protocol is 2025-06-18 or later, each tool also
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

## 0.3.0: the gt unlock consumer and the Windows front door (gt 0.20.1)

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

## 0.3.0: native OAuth for remote MCP servers

**Spec implemented: MCP authorization, revision 2026-07-28** (modelcontextprotocol.io/specification/
2026-07-28/basic/authorization, fetched 2026-10-04; the module docstring of `lotrlib/oauth.py` says
the same). RFCs relied on: OAuth 2.1 draft-13, 9728 (protected-resource metadata), 8414 (+ OpenID
Connect discovery), 7591 (DCR), 8707 (resource indicators), 7636 (PKCE S256), 7009 (revocation),
9207 (`iss`), 8252 (loopback), and the Client ID Metadata Document draft-00. Until now an `mcp`
connection could only reuse another client's token by ref (`add-mcp`); it still can. `connect`
makes LOTR the OAuth client.

### Commands (admin plane, run by a person at a terminal on the machine holding the registry)

    lotr connect NAME --url HTTPS_MCP_ENDPOINT [--scope "s1 s2"] [--client-id ID]
         [--client-secret-file PATH | --client-secret-stdin] [--client-metadata-url URL]
         [--authorization-server ISSUER] [--redirect-port N] [--offline-access] [--l1]
         [--allow-insecure-localhost] [--allow-private-network] [--skip-audience-check]
         [--identity S] [--description D] [--trust T0|T1|T2]
    lotr login NAME [--scope "extra scopes"] [--redirect-port N]
    lotr disconnect NAME

`NAME` is `name@zone` or a bare name in the registry's zone. `connect` runs discovery, registers a
client, opens the browser (or prints the URL), waits on the loopback listener, exchanges the code,
stores the refresh token, lists the server's tools (so `find` works at once) and registers the
connection. `login` repeats the sign-in for an existing connection (scopes are only ever added,
the step-up rule of the spec). `disconnect` revokes at the server (RFC 7009, best effort and
reported), deletes the stored secrets and unregisters. All three refuse in `mode: client`.

### Registry shape

`kind: mcp`, `auth.scheme: "oauth"`:

    "auth": {"scheme": "oauth",
             "token_ref": "sealed:lotr-oauth-<name>-<hash>-rt" | "store:lotr-oauth-…-rt" | null,
             "oauth": {"issuer", "resource", "authorization_endpoint", "token_endpoint",
                       "revocation_endpoint"|null, "registration_endpoint"|null,
                       "client_id", "client_id_source": "preregistered"|"cimd"|"dcr",
                       "token_endpoint_auth_method": "none"|"client_secret_basic"|"client_secret_post",
                       "client_secret_ref": <ref>|null, "scopes": [...],
                       "net": {"allow_private": bool, "allow_insecure_localhost": bool},
                       "audience_check": bool, "max_access_cache_s": 3600, "spec": "2026-07-28"}}

`token_ref` is the REFRESH token's ref, `null` when the server issued none. The ACCESS token is not
stored anywhere. Every endpoint host is pinned in `network.hosts`; validation refuses a literal
token or secret, a non-https URL (plain http only with `net.allow_insecure_localhost`), an unpinned
host, a confidential client without `client_secret_ref`, and `refresh_cmd` on an oauth connection.

### Flow (what `lotrlib/oauth.py` does, in order)

1. **Discovery.** Unauthenticated `initialize` POST -> must be 401. `WWW-Authenticate: Bearer
   resource_metadata="…"` (same origin as the endpoint) else the RFC 9728 well-known URIs (path,
   then root). Protected-resource `resource` must equal the endpoint (canonical form). With no
   protected-resource metadata at all, the endpoint's own origin is used as the issuer and a note
   says so (pre-2025-06 servers). AS metadata: RFC 8414 then OpenID Connect locations, with and
   without path insertion; the `issuer` in the document must be identical to the one the URL was
   built from. Refused: no `S256` in `code_challenge_methods_supported`.
2. **Scope.** `--scope`, else the 401 challenge's `scope`, else `scopes_supported`, else none.
   `offline_access` only with `--offline-access`.
3. **Client.** Pre-registered `--client-id` (+ secret from a file or stdin, never argv); else a
   Client ID Metadata Document when `--client-metadata-url` is given AND the AS advertises
   `client_id_metadata_document_supported`; else DCR (`application_type: native`, `none` auth,
   redirect URI exactly the one about to be used; a response that alters it is refused); else
   refuse with the way forward. A DCR client is registered afresh at every `connect`/`login` (the
   loopback port changes), so an AS may collect unused client records.
4. **Authorization.** Loopback listener on 127.0.0.1 only, ephemeral port (or `--redirect-port`),
   path `/callback`, `state` 256 bits, PKCE S256, `resource`. The browser is opened with
   `webbrowser`; on a headless host (or if it fails) the URL is printed with the port. The
   listener accepts one callback and closes on every exit; wrong Host or path -> 404 and the wait
   goes on; wrong state, a contradicting or (when advertised) missing `iss`, an AS error, or the
   timeout (180 s) ends the attempt.
5. **Token exchange** with `resource` and `code_verifier`; Bearer only; `expires_in` honoured; a
   JWT access token whose `aud` does not name the resource is refused (defence in depth, off with
   `--skip-audience-check`).
6. **Lifecycle in lotrd.** `oauth.access_token(conn, Context)`: cached per connection AND per
   seat (kernel-identified caller; `-` while unlock is off); renewed 60 s before expiry or on a
   401 (once); one refresh at a time per connection; a rotated refresh token is persisted BEFORE
   the access token is returned (sealed: via the authority's `seal_put` for the calling subject
   with `request: false` -- a public-key seal, no prompt on macOS; store: by atomic rename); if
   persisting fails the token is held in memory for that seat, a note rides in the result and
   `lotr status` shows `refresh_unpersisted`. `invalid_grant` on refresh, or a 401 that survives a
   fresh token, is `needs_login`, with hint `run: lotr [--zone Z] login <id>`. A 403
   `insufficient_scope` is reported with `lotr login <id> --scope "<needed>"` (no automatic
   step-up).

### Where the refresh token rests (honest ratings)

- gt unlock **on**: `sealed:` (L2) -- sealed at `connect` through the authority (a person is at the
  terminal and may be asked a factor; `--l1` overrides), opened per grant or per `secrets_window_s`
  like any sealed ref, re-sealed on rotation without a prompt on macOS (Windows seals through
  Windows Hello, so a rotation there by a daemon with nobody present cannot be sealed and falls into
  the held-in-memory case above: the rotated token is kept in memory only and `lotr login` is needed
  after a restart).
- gt unlock **off**, or `--l1`: `store:` -- a mode-600 file the store wrote (L1: any process of
  this user can read it). On native Windows the 0600/0700 chmod calls are no-ops: the protection is
  the profile directory's ACL, and `lotr status` / `connect` say so. The macOS keychain is not written (its CLI takes the secret in argv).
- A client secret gets the same treatment under its own name.
- `lotr status` shows the level (`L2` via the authority, `L1` for store), `refresh_token:
  stored as sealed:|store:|none issued`, and `access_cached` / `refresh_unpersisted`, never a value.

### Authority interplay (unchanged rules)

Per-seat grants for `lotr:<conn>:<tier>`, `mcp_only`, step-up and consent are exactly as for any
connection; the engine's gate runs before the connection is built. `profiles.mcp_tier` still
assigns tiers (annotations are hints: the open 0.21 item). **An OAuth connection never qualifies
for an unattended write or consent**: a hub client calling it with `call_write` / `call_consent`
gets `oauth_unattended_refused`. **Unattended reads are supported only with an L1 `store:`
refresh token** (`connect --l1`; the honest cost of L1). A hub client's read on a connection with a
`sealed:` refresh token is refused (`oauth_unattended_refused`) BEFORE any request to the server:
the hub seat has no subject, so it cannot re-seal a rotation, and a refresh it could not store
would leave the stored token already-rotated (a later presentation then looks like a replay and a
server with reuse detection ends the sign-in). A `secret:<ref>` allow-list entry does not change
that. The local seat that holds the grant keeps working.

`login` is pinned to the stored issuer (Authorization Server Binding): if the server's
protected-resource metadata no longer lists it, `login` fails with `oauth_as_not_listed`; use
`disconnect` then `connect` to move to a new authorization server. `login` re-seals into `sealed:`
when gt unlock is now on and the stored token was L1 (the old file is deleted).

### Threat model

| Threat | Control |
|---|---|
| SSRF through discovery metadata (issuer, endpoints pointing inward) | every fetch: https only, address checked at connect time (private / loopback / link-local / CGNAT / metadata refused; metadata addresses refused even with `--allow-private-network`), same-origin redirects only, byte and time limits |
| Mix-up / forged callback | recorded issuer vs `iss` (RFC 9207 table), exact state, exact redirect URI, one-shot loopback with Host check |
| Code interception | PKCE S256, AS must advertise it, verifier never leaves the process |
| Token for another server | `resource` in both requests; JWT `aud` check; the MCP server validates audience (spec) |
| Token theft at rest | refresh token sealed (L2) or mode-600 (L1, said plainly); access token memory only; never in argv, environment, logs, audit lines, errors or results (tests scan for issued token strings) |
| Refresh-token replay after rotation | rotation persisted before use, one refresh at a time, reuse -> `needs_login` |
| Another seat riding a cached token | access tokens cached per seat; a new seat must read the refresh secret (so unseal under its own grant) |
| Hostile AS error text | only a plain error code is quoted, never `error_description` |

### What it does not do

No device-code, client_credentials, private_key_jwt or DPoP; no automatic step-up; no stdio servers;
no hosting of a Client ID Metadata Document (you give it a URL you host); no deletion of DCR client
records at the AS; it does not read or sign out Claude Code's own copy of a login (`claude mcp
remove` does that); `connect` needs a person with a browser on the machine (or a forwarded port).
The cached access token outlives the unseal that minted it, for at most `max_access_cache_s`
(default 3600, shortened by the server's `expires_in`): a deliberate trade-off against prompting on
every call, recorded in SECURITY.md.

### Hardening from the independent review (2026-10-04)

- **Server-controlled text is inert.** A `scope` from a 403 reaches a hint only when every token is a
  plain scope word (`A-Za-z0-9 : . _ / + = @ -`, at most 20 words); otherwise it is left out, never
  quoted in. Every other server-supplied string in a message (URLs, host names) goes through
  `oauth.clean` (control, space and non-ASCII characters become `?`) and `safe_url` strips query,
  fragment and userinfo. Only a plain error CODE is quoted from an AS, never its description.
- **Refresh-token reuse can end a sign-in.** Public clients get rotating refresh tokens, and a server
  that follows RFC 9700 revokes the whole grant when an already-rotated token is presented. So LOTR
  never presents one it knows is stale: held (unpersisted) tokens are kept per seat; while any seat holds
  one the stored copy is stale and every other seat gets `needs_login` without contacting the server;
  a caller that cannot re-store the rotation (a hub client with a `sealed:` ref) is refused up front with
  `oauth_unattended_refused`, before any request. When a grant does end, `lotr login <name>` signs in
  again and revokes the previous refresh token after the new one is saved.
- **Tiers.** On an OAuth connection annotations may only raise a tier: `destructiveHint` or a name
  containing a risky word (delete, remove, send, write, create, update, merge, post, drop, grant,
  revoke, run, exec, publish, upload, deploy, reset, set, close, purge, ... the list is `_MCP_RISKY`
  in `profiles.py`; third review added erase, unlink, edit, modify, add_comment, reply, replace, empty)
  is consent. Before tiering the name is folded: NFKC (fullwidth letters become ASCII) and every format
  character (zero-width space and joiners, soft hyphen, BOM) removed. The word is matched as a substring of the lowercased name, for every
  name, read prefix or not (`get_deleteall`, `list_sendmail`, `searchdeleteall` are consent). The one
  exception is a short allow-list of read-only words that contain a risky word (`_SAFE_READ_WORDS`:
  settings, assets, dataset, preset, closest, postmortem, running, runtime): a risky word counts
  unless it lies wholly inside one of them, so `list_assets`, `get_settings_page` and
  `describe_runtime` stay reads while `resetsettings` and `presetdrop` are consent. The cost is that
  some reads are consent (`list_commits`, which contains "commit"); the owner keeps them reads with a
  `policy.read` glob on the connection. A `readOnlyHint` counts only for a name that begins with a read
  prefix and has no risky word; an unknown name is write. (So `create_*` on an OAuth connection is a
  consent-tier operation.)

- **Local names are refused as the authorization endpoint (design, second review).** The owner's
  browser is sent to the authorization endpoint, so an endpoint whose host is `localhost` or ends in
  `.localhost` (any case, trailing dot allowed) is refused with `oauth_ssrf_refused`. A DNS name that
  merely starts with "localhost" and the owner's literal `127.0.0.1` test servers are not affected.
- **Who may sign in.** `connect`, `login` and `disconnect` refuse to run from inside Claude Code's
  process tree (or with `CLAUDECODE` set, or with no interactive shell above them), and while gt unlock
  is on require `gt:hub:enroll` with step-up. Honest limit: a same-user program can detach from the
  tree; the check stops a model's Bash tool, not a determined local process.
- **Runtime traffic.** An OAuth connection's calls to the MCP server use the same pinned, SSRF-checked
  connect as discovery (the owner's `net` flags apply), consult no proxy at all (`HTTP_PROXY`,
  `NO_PROXY` and the system proxy are ignored: connections are direct; non-OAuth `mcp` connections also
  bypass proxies), refuse redirects, and read at most 8 MiB for at most 120 s. Embedded IPv4 forms
  (IPv4-mapped, RFC 2765 translated `::ffff:0:a.b.c.d`, NAT64, 6to4, IPv4-compatible) are judged by the address inside; metadata addresses
  (169.254.169.254, 168.63.129.16, 192.0.0.192, fd00:ec2::254, 100.100.100.200) and multicast are never allowed.
- **Listener.** Each connection is read on its own thread (3 s), so idle sockets cannot stall the login;
  a request with the wrong state is a stray, not an abort (a forged callback cannot kill the owner's
  login); a state-valid answer from the wrong issuer, or an AS error, is terminal; one terminal verdict
  closes the socket.
- **Housekeeping.** The daemon forgets the cached tokens of a connection no longer in the registry on every
  reload; the per-connection access cache holds at most 64 seats and drops expired entries; a `connect`
  whose registry write fails deletes the secrets it wrote; `atomic_write` fsyncs before the rename.
- **Trust anchors.** TLS uses the platform trust store; `SSL_CERT_FILE` / `SSL_CERT_DIR` (and the
  system store) are therefore part of the trust boundary. On native Windows there are no mode bits: a
  `store:` file is protected by the profile directory's ACL, not by mode 600.
- **Not changed.** The engine holds its lock across a downstream call (pre-existing; a slow MCP server
  delays other callers), a connection's `tools/call` error text is still the server's own (untrusted data).

### Added after the adversarial test pass (2026-10-04)

- **Sealed secrets are bound to their record.** A `sealed:` refresh token or client secret is stored as
  `lotr-bound-v1.<sha256>.<value>`; the digest covers `issuer`, `token_endpoint`, `revocation_endpoint`,
  `client_id` and `resource`. lotrd and the CLI refuse (`oauth_binding_mismatch`, nothing is sent) when
  the record no longer matches or a sealed ref holds an unbound value. registry.json is editable by any
  process of the user; this keeps an edited token endpoint from being handed the sealed token. The
  L1 `store:` form of an OAuth secret is wrapped in the same envelope (second review): a plain copy
  of it under another name is refused by the bearer paths, which refuse any envelope. A value written
  before this is plain; it is read, and wrapped at its next refresh (`lotr status` says so until then).
  The L1 file is still readable by any process of the user, as it always was.
- **Rotation survives any store failure**: a GatewayError or a raw OSError while re-storing keeps the new
  token in memory for the seat (`refresh_unpersisted`).
- **Registry rules**: `endpoint` must equal `auth.oauth.resource` (canonical form; checked at load and again
  before every token use, `oauth_resource_mismatch`); `auth.token_ref` may only be `sealed:` or `store:`;
  the connection name is checked (`bad_connection_name`) before any network request; `login_nonce` in the
  block makes every sign-in a distinct cache key in lotrd.
- **Hints**: `--scope` may be given more than once, and a scope hint is printed as `--scope a --scope b`
  with no quoting, so it is inert in sh, cmd.exe and PowerShell alike.
- **Pre-registered clients (Google, Microsoft)**: with `--client-id`, the RFC 9728 well-known document is
  fetched first (Google answers `initialize` with 200 and challenges only tool calls); a listed issuer
  that is an origin plus `/` matches the same origin published without it (one direction only); a client
  secret from `--client-secret-file|-stdin|-prompt` is sent as `client_secret_post` when offered and
  stored as a ref; `--no-resource` omits the RFC 8707 parameter (remembered as `send_resource: false`);
  `--redirect-host localhost|127.0.0.1` names the redirect host (the listener binds 127.0.0.1 only).
  Microsoft, best effort and tested only against a fake: OpenID Connect discovery fallback, the
  `{tenantid}` issuer template (the authority's tenant segment is substituted), PKCE sent although not
  advertised (pre-registered clients only), `offline_access` added, `resource` withheld unless it matches
  the scope's audience.
- **Browser**: `webbrowser` is reached through one gate. `GT_LOTR_NO_BROWSER=1` always prints the URL
  instead; without it a browser opens only with a terminal on stdin and outside a test run, or with
  `--open-browser`. The launcher runs on its own thread.

### Tests

`tests/test_lotr_oauth_adversarial.py` (199 hostile-server tests), `tests/test_lotr_oauth.py` against `dev/fake_oauth_mcp.py` (in-process AS + protected MCP server,
rotating refresh with reuse detection). Real-server behaviour is verified by hand: `dev/oauth-live-check.md`.

## 0.4.0: shipped domain agents, readers and writers (gt 0.20.2)

Request `2026-10-04-shipped-lotr-domain-agents`, then the owner's reader/writer split
(2026-10-04): no single agent holds private data + untrusted content + an action channel. Eight
plugin agents in `agents/`, plain files (nothing generated), run as `gt-lotr:<name>`:

| Agent | Role | Profiles | Tools (`mcp__plugin_gt-lotr_gt-lotr__…`) | Intent | maxTurns |
|---|---|---|---|---|---|
| `jira` | reader | `jira-v3`, `jira-v2` | `find`, `call_read` | fast | 12 |
| `m365` | reader | `graph` | `find`, `call_read` | fast | 12 |
| `github` | reader | `github` | `find`, `call_read` | fast | 12 |
| `runner` | reader | anything else (`generic`, `mcp`) | `find`, `call_read` | fast | 12 |
| `jira-writer` | writer | `jira-v3`, `jira-v2` | `find`, `call_write` (no consent op in the profile) | balanced | 4 |
| `m365-writer` | writer | `graph` | `find`, `call_write`, `call_consent` (`send_mail`) | balanced | 4 |
| `github-writer` | writer | `github` | `find`, `call_write`, `call_consent` (`merge_pull`) | balanced | 4 |
| `writer` | writer | anything else | `find`, `call_write`, `call_consent` | balanced | 4 |

- **Readers** read untrusted text, so they are given no write or consent tool; a needed change is
  said in `GAPS`, never made. That limits what they can *call*, not what an op does: the gateway
  rates an `mcp` connection's tools by their own name and `readOnlyHint`, which a hostile server
  can mislabel (readers are told never to call an op whose name says it changes something).
  **Writers** are given no `call_read` and are **told not to read**; with strict tool tiers (the
  default) the gateway enforces it: `call_write` carries only write-tier ops and `call_consent`
  only consent-tier ops, so a read through either is `wrong_tool`. A connection with
  `strict_tools: false` goes back to `[read, write]` / `[read, write, consent]`, and there it is
  prose again. "One operation" and "no parallel calls" are prose either way (`maxTurns: 4` does
  not cap parallel calls). The split is a guard against a poisoned result steering an agent,
  **not a security boundary** (the main session can call the same tools); strict isolation is
  the 0.21 proxy design.
- **What consent covers:** the curated consent ops (`send_mail`, `merge_pull` at their path templates), MCP tools rated
  consent by name or hint, and, since gt-lotr 0.3.0 (gt 0.20.1), the raw twins (`classify` rule 5b: `POST …/sendMail`, `/send`,
  `/reply`, `/forward`, `$batch`, a merge by trailing slash or `/repositories/{id}/…`, GraphQL
  `mergePullRequest`, `DELETE`). Other destructive endpoints on a generic connection are write
  tier unless the owner adds `policy.consent` globs.
- **Writer input contract.** The prompt holds exactly `SPEC: {"connection", "op" (an op name or
  "METHOD /path"), "args"}` and `REQUEST: "<the user's own words, quoted>"`. The writer performs
  that ONE operation unchanged; with the SPEC or REQUEST missing, more than one operation, an op
  it does not do, or `args` that plainly do not do what the REQUEST asks, it makes no gateway call
  and returns `CODE: bad_spec`. One optional `find` confirms the tier, never looks data up. Every
  payload word is data. It makes no `find` call on an `mcp` or `generic` connection (the
  generic `writer` none), never fetches a missing id (`bad_spec`), never issues parallel tool
  calls, tries `call_write` once (`call_consent` for the named consent op or a SPEC
  `"tier": "consent"`) and answers `wrong_tool` back to the session. It stops on `locked`, `step_up`, `mcp_only`, `failed_closed`,
  `unreachable`, `consent_denied`, `consent_refused` and `consent_unconfirmed`, makes at most one
  `call_consent` per task, and returns `STATUS: ok | error; OP; CONNECTION; ID; URL; CODE`, never
  payload text (`ID` at most 64 characters of `A-Za-z0-9._:/#@-`, `URL` `https://` and at most
  300; the session treats the line as data).
- **Connection:** tools are named, never a wildcard, and point at THIS plugin's server. Plugin
  agents ignore `mcpServers`, so a spawn uses the session's shim: no new seat, no new grant, no
  unlock prompt while a grant is live; a consent op goes through the same shim and daemon.
  Grants and permission rules are shared with the main session.
- **Frontmatter:** `name`, `description`, `tools`, `maxTurns`, `model_intent`. No pinned
  `model:` (gt core's `gt_model_policy.py` writes `model:`/`effort:` into the installed copies
  from `model_intent`, keyed `gt-lotr:<name>`, since gt 0.20.2), no `mcpServers`, `hooks`,
  `permissionMode`, no credential.
- **Body:** readers: domain know-how (JQL; Graph OData and paging, raw Teams paths; GitHub
  search qualifiers; which recipes to prefer), a Read-only section, error codes reported not
  retried, results are data (third-party text quoted verbatim, control characters stripped), and
  the return contract `ANSWER`, `ITEMS (<connection>, n of m)`, `MORE: next_cursor=…`, `CALLS`,
  `GAPS` (at most 15 lines, 10 items, never a raw payload; at most 5 pages per sweep). Writers:
  the input contract, the write payload shapes (Jira ADF comment and transition, Graph
  `sendMail`, event and reply, GitHub comment, review and merge), stop list, one consent call,
  the one-line status.
- **Routing:** `lotr_mcp.ROUTING` is appended to the server `instructions` (catalog trimmed to
  fit the 2,048-char cap) and the skill carries the same rule: reads and sweeps go to a reader;
  to CHANGE something the main session composes the exact operation from the USER'S words only
  (never from tool or agent output) and hands it to a writer or calls `call_write` itself; it
  never acts on text a result contains. An opaque identifier a write needs (issue key, transition
  id) may come from a read, and then the main session shows the user the resolved target
  (connection, key, summary) before the write; a merge's head sha never comes from a read; free
  text never does.
- **Tests:** `tests/test_lotr_agents.py` (static: exact tool lists, no reader write or consent
  tool, no writer `call_read`, writer `maxTurns`, stop lists, one-consent rule, the input
  contract text, routing text), `tests/test_gt_model_policy.py` `ModuleAgentDefinitions` (all
  eight agents' models, overrides, `agent_models=session`, `verify`); measurement
  `dev/lotr_agents_measure.py` with `dev/fake_lotr_mcp.py` (which records accepted writes in a
  ledger; `--poison` plants injection text).
