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
    "identity": "<you> @ github.com",
    "zone": "personal",
    "kind": "http",
    "profile": "github",
    "description": "GitHub repos, issues and PRs as <you>",
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
    "id": "laptop",
    "machine": "My laptop",
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
   createForward`) or `/$batch` → consent. Since 0.5.0 (built-in connector review) also: the other
   GitHub merges (`/repos/o/r/merges`, `/merge-upstream`), mail in a child folder
   (`…/mailFolders/A/childFolders/B/messages/ID/reply`), an event `forward` or `cancel` on any
   calendar path, a new group conversation or a group thread `reply` / `forward`, any path ending
   `/permanentDelete`, and Jira `…/issue/KEY/notify` and `/bulk/issues/delete`. Other destructive
   endpoints on a generic connection stay write unless the owner adds `policy.consent` globs.
5c. Read-only POSTs (no explicit op tier), by profile only: Jira (`jira-v2`, `jira-v3`)
   `POST /rest/api/{2,3,latest}/search`, `/search/jql`, `/search/approximate-count`; Graph
   `POST /search/query`, `/{me|users/X}/calendar/getSchedule`, `/findMeetingTimes`,
   `/getMemberGroups`, `/checkMemberGroups`, `/getMemberObjects` → read. On any other profile,
   and for every other POST, the method rule applies (write).
6. GraphQL: `graphql_query` is lexed whole (comments and strings removed); read only if it holds exactly one operation, a `query` or the `{...}` shorthand, plus fragment definitions. Mutation, subscription, several operations, or anything unparsable → write, except a document naming a `mergePullRequest` / `enablePullRequestAutoMerge` / `mergeBranch` (0.5.0) mutation, or a `delete…` mutation field such as `deleteIssue` or `deleteRef` (0.5.0) → consent. An explicit `body` is classified in place of `query`.
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
         [--allow-insecure-metadata HOST]   (0.5.0, repeatable; see below)
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
                       "net": {"allow_private": bool, "allow_insecure_localhost": bool,
                               "insecure_metadata_hosts": [host, ...]   (0.5.0; absent when none)},
                       "audience_check": bool, "max_access_cache_s": 3600, "spec": "2026-07-28"}}

`token_ref` is the REFRESH token's ref, `null` when the server issued none. The ACCESS token is not
stored anywhere. Every endpoint host is pinned in `network.hosts`; validation refuses a literal
token or secret, a non-https URL (plain http only with `net.allow_insecure_localhost`), an unpinned
host, a confidential client without `client_secret_ref`, and `refresh_cmd` on an oauth connection.

### Flow (what `lotrlib/oauth.py` does, in order)

1. **Discovery.** Unauthenticated `initialize` POST -> must be 401. `WWW-Authenticate: Bearer
   resource_metadata="…"` (same origin as the endpoint) else the RFC 9728 well-known URIs (path,
   then root). Protected-resource `resource` must equal the endpoint (canonical form), or (0.5.0) be a
   less specific URI of it: same origin, no query, a whole-segment path prefix (Slack names
   `https://mcp.slack.com` for `/mcp`). That identifier is recorded as `auth.oauth.resource_id`, sent as
   `resource` and accepted as a JWT `aud`; the token stays bound to the endpoint (`auth.oauth.resource`).
   Another origin, port or scheme, a sibling or a more specific path is still `oauth_resource_mismatch`. With no
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
4. **Authorization.** Loopback listener on 127.0.0.1 (plus ::1 on the same port when the redirect names `localhost`; never a wider address), ephemeral port (or `--redirect-port`),
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
| TLS downgrade of the protected-resource metadata (0.5.0) | plain http only for the one document, only for a host the owner vouched for with `--allow-insecure-metadata`, only from the endpoint's own host; the address guard, redirects and every other URL (issuer, token, authorize, registration, revocation) are unchanged |
| Mix-up / forged callback | recorded issuer vs `iss` (RFC 9207 table), exact state, exact redirect URI, one-shot loopback with Host check |
| Code interception | PKCE S256, AS must advertise it, verifier never leaves the process |
| Token for another server | `resource` in both requests; JWT `aud` check; the MCP server validates audience (spec) |
| Token theft at rest | refresh token sealed (L2) or mode-600 (L1, said plainly); access token memory only; never in argv, environment, logs, audit lines, errors or results (tests scan for issued token strings) |
| Refresh-token replay after rotation | rotation persisted before use, one refresh at a time, reuse -> `needs_login` |
| Another seat riding a cached token | access tokens cached per seat; a new seat must read the refresh secret (so unseal under its own grant) |
| Hostile AS error text | only a plain error code is quoted, never `error_description` |

### What it does not do

No device-code, client_credentials, private_key_jwt or DPoP; no automatic step-up; stdio servers only through add-stdio (0.5.0);
no hosting of a Client ID Metadata Document (you give it a URL you host); no deletion of DCR client
records at the AS; it does not read or sign out Claude Code's own copy of a login (`claude mcp
remove` does that); `connect` needs a person with a browser on the machine (or a forwarded port).
The cached access token outlives the unseal that minted it, for at most `max_access_cache_s`
(default 3600, shortened by the server's `expires_in`): a deliberate trade-off against prompting on
every call, recorded in SECURITY.md.

### 0.5.0: `--allow-insecure-metadata HOST` (gt 0.21.4; request 2026-10-05-lotr-allow-insecure-metadata-host)

**Why.** A server may advertise its RFC 9728 protected-resource metadata with an `http://` URL (the
`resource_metadata` parameter of its 401 challenge, and a `resource` that is `http://` too). LOTR
refuses to follow it, `oauth_insecure_url`: a plain-http fetch is a TLS downgrade and an SSRF vector.
The real fix is the server advertising https. Until it does, the owner can vouch for one host.

**What it is.** `lotr connect NAME --url U --allow-insecure-metadata HOST`, repeatable. HOST is a bare
host name (letters, digits, hyphen, dots; no scheme, port, path, wildcard or user info; a bad one is a
usage error before any network). It is kept in `auth.oauth.net.insecure_metadata_hosts` (sorted,
lowercase, absent when empty, so older blocks read and write the same). `NetPolicy.from_block` reads
it, so `lotr login` (which discovers again) and the token lifecycle (refresh) reuse it with nothing
asked a second time. `lotr status` carries `oauth.insecure_metadata_hosts` and `--table` prints a
`! plain-http metadata allowed for HOST` line under the connection; `connect` returns the list and,
for a host that is not the endpoint's own, a note that it has no effect. The registry re-checks the
list on load (a list of bare host names), because the file is editable.

**What it relaxes, exactly**, for a vouched host and the protected-resource metadata document only
(`oauth.METADATA_WHATS`: the `resource_metadata` challenge URL and the well-known fetch):
(a) the URL may be plain http; (b) the metadata-origin rule accepts the plain-http form of the
endpoint's OWN host in place of the same origin: same host, and the same port with http's 80 standing
for https's 443; (c) a `resource` in that document that names the endpoint's host by an `http://` URL
is read as the same server, comparing it as https; the https endpoint stays the token's resource and
what is sent as `resource`, and the http identifier is never recorded (the registry would refuse an
http `resource_id`). A confirmation note says the exemption was used.

**What it does not relax.** The MCP endpoint (https); the issuer in `authorization_servers`; the
authorization server's own metadata URL; the authorization, token, registration and revocation
endpoints (https, and the registry refuses an http one); a redirect (same origin including the
scheme, so http to https is refused); the address guard (a private address still needs
`--allow-private-network`); the cross-origin rule (metadata from another host is refused even when
that host is vouched too); the JWT audience check (its own flag, `--skip-audience-check`).

**Not done.** A server that advertises an internal resource name (a cluster-local host that does not
resolve to the public one) is a different problem and still stops at `oauth_resource_mismatch`.
A template cannot carry the flag yet. Not run against a live server (self-verified against scripted
and local servers only).

**Tests**: `tests/test_lotr_insecure_metadata.py`.

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
  consent-tier operation.) A read or consent prefix counts in snake_case (`get_issue`) and, since
  0.5.0, in camelCase too (`getJiraIssue`, `listJiraProjects`, `deleteJiraIssue`: the verb in lower
  case followed by an upper-case letter), because hosted servers such as Atlassian's name tools that
  way; `getter` is not a read prefix. A name with neither (`fetch`, `issue_read`, `atlassianUserInfo`)
  is still a write until a `policy.read` glob says otherwise.

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
- **Not changed.** A connection's `tools/call` error text is still the server's own (untrusted data).
  (The engine used to hold its lock across a downstream call; see "Calls run side by side" below.)

### Calls run side by side (owner 2026-10-25)

A model call can last minutes, so no lock is held for the wait on a downstream. `server._EngineHolder`
locks only to build the engine; `Engine.call` does its bookkeeping under the engine's RLock in three
short steps and makes the request with no lock held:

1. under the lock: reload, look up, classify, gate, policy, take the cursor;
2. no lock: the consent dialog (a person's answer may take minutes; a consent window is read, never
   changed, here);
3. under the lock: audit-writable check, limiter slot, claim of the op reference (`RefStore.claim`: a
   reference checked in step 1 is spent exactly once even when callers race), build the connection object;
4. no lock: the downstream request (`_run_plan`), then the slot is released in a `finally`;
5. under the lock: swap the result's cursor for an opaque one. Audit append is one `O_APPEND` write and
   `_note_health` takes the lock, so audit lines and the health counters stay whole under threads.

`status(check=True)` runs its probes with no lock held and builds the rows after. `limits.max_concurrent`
N now admits N calls in flight (the N+1th is refused `rate_limited_locally`, not queued). Stdio: the
process table has its own lock held only to read or write the table; each server has a request lock (one
request at a time on one server, others in parallel) and each connection a spawn lock, so a server that is
starting blocks nothing else. A request in flight keeps its server from the idle reaper.

### Stdio command approval is enforced (phase 112; owner 2026-10-25)

The engine passes its home to every stdio connection (calls and `status --check` probes), so a command
whose hash (connection id plus argv) is not in `approved_commands.json` is refused `command_not_approved`
and never started. One-time migration, run when an engine first starts on a home: when no
`approvals_migrated.json` marker exists and no approvals file exists, every stdio connection already in the
registry is recorded as approved, one audit line `approvals_migrated: N` is written, and `lotr status`
shows `approvals_migrated: N` (a key in the JSON, a line under `--table`; not told to a hub client). The
marker is written either way (exclusive create), so deleting the approvals file does not trust everything a
second time; an install that already had an approvals file, or a gateway with no connections yet, trusts
nothing and only writes the marker. After the marker, any command not approved (a connection added by hand
to the registry, or a changed argv) is refused.

`lotr add-stdio --yes` is not a person: with gt unlock off and the real LOTR home it needs the typed code at
the person's terminal (`_unlock_off_gate`); with unlock on nothing changes. `setup` gates its own `--yes`
the same way and `setup` / `app-connect` pass an internal `person_asked` flag to add-stdio. `connect`,
`login` and `disconnect` take the same typed-code gate after the terminal check. A scratch home is exempt.

### Locality and provider, served everywhere

`locality` (`local` | `remote`, default `remote`) and the new optional `provider` (a free label, 1 to 64
characters from letters, digits and `. _ : / + -`, validated in `registry.py` and `conn_templates.py`
through `upstream.provider_problem`; no shipped template sets it) are returned by:

* `find("")` catalog rows: `locality` on EVERY row (it was only present as `local`), `provider` when set;
* `describe`: `locality` always, `provider` when set;
* `status`: `locality` when set (as before), `provider` when set; `lotr status --table` prints
  `provider NAME` on the row.

Only fields are added; every existing key and shape is unchanged.

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
  `--redirect-host localhost|127.0.0.1` names the redirect host (the listener binds 127.0.0.1, and also ::1 on the same port for `localhost`; a busy ::1 port refuses the sign-in).
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
  `mergePullRequest`, `DELETE`; 0.5.0 adds the rest of rule 5b and GraphQL `mergeBranch` / `delete…`). Other destructive endpoints on a generic connection are write
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


## 0.4.1 additions (gt 0.21.0)

- `templates/connections/*.json` + `scripts/lotrlib/conn_templates.py`: secret-free connect-arg
  prefill descriptors read live from the plugin (`SHIPPED_CONNECTIONS`, as `engine.SHIPPED_RECIPES`)
  and `~/.config/gt-lotr/<zone>/connections/` (later wins by name). `lotr connect NAME
  --from-template T`: explicit flags win, `--url` is optional when the template supplies it
  (`missing_url` otherwise), and discovery may not pin a host the template's `network.hosts` does
  not vouch for (`template_host_mismatch`, nothing registered). A credential-shaped key anywhere in
  a template is `template_has_secret`. Shipped: the generic `example.json` only; no vendor-specific
  template ships here.
- `agent_split` (module setting, `split` | `combined`) and `scripts/lotrlib/agent_split.py`:
  `combined` swaps the server-instructions routing for one read/write agent (`agents/gateway.md`)
  and is in effect only at lockdown `insecure`.
- module.json `lockdown`: `{"<level>": {"allow": [...], "deny": [...]}}`, cumulative by level;
  find and call_read from trust-local, call_write at insecure, call_consent never.

## 0.5.0: template authoring and MCP over stdio

**What existed before.** Connection templates (0.4.1) were kind `mcp-oauth` only, applied by
`lotr connect --from-template`. `mcp` connections were `transport: http` only (`add-mcp` bearer ref,
`connect` OAuth); the registry refused every other transport. 0.5.0 adds template kinds `http` and
`mcp-stdio`, the `lotr template guide|new|check|list` authoring commands, and `transport: stdio`.

**Template kinds** (`lotrlib/conn_templates.py`). `http`: `connect.{base_url, profile, auth_scheme,
auth_header, auth_user, credential_ref, trust, identity, description, policy}` + `network.hosts`;
applied by `add-http --from-template`. `mcp-stdio`: `connect.{command (argv), env_refs {NAME: ref},
cwd, timeouts, trust, identity, description, policy}`; applied by `add-stdio --from-template`.
Refs only: `credential_ref` / `env_refs` values must look like `scheme:target`; the credential-key
scan is unchanged. `--from-template` with the wrong kind is `template_kind_mismatch` naming the
right command. Explicit flags win. A template with a `TODO` value cannot be applied.

**Authoring** (`lotrlib/template_tool.py`). `template guide` prints the field reference (generated
beside two examples that the tests validate); `template new NAME [--kind] [--from-doc FILE|-]
[--out] [--force]` writes `connections/NAME.json` with hints read from the doc (first https API URL,
bearer / basic / `X-...-Key` header / OAuth mentions) and `TODO` for the rest; `template check`
returns `{ok, problems, warnings, todo}` (exit 1 unless valid and TODO-free); `template list`.
No network is touched; a URL's text is saved to a file by the caller first.

**stdio transport** (`lotrlib/conn_stdio.py`, `registry._validate_mcp_stdio`). Entry: `kind mcp`,
`transport stdio`, `command` (argv, no shell, resolved with `shutil.which` on the child's PATH),
`env` {NAME: secret ref} resolved at process start, `cwd` absolute, `timeouts {start_s 30,
call_s 120, idle_s 600}` each 1..3600, `auth` none, no `endpoint`, `network.hosts` empty. One
process per connection id, started lazily, shared by every connection object in the hosting
process (lotrd / lotr_mcp), restarted when command/env/cwd change, when dead, or idle past
`idle_s`; killed on a request timeout; stopped at exit. Newline-delimited JSON-RPC; stray stdout
lines ignored; server-to-client requests answered -32601; a line over 8 MiB kills it. The child
gets PATH, HOME, temp, locale and Windows system variables plus the env refs, nothing else.
stderr: 8 KiB in-memory tail, env values masked, never echoed or put in errors (`status` shows
`process.stderr_tail` only when the process is not running). A process that died mid-request is
restarted and retried once for `tools/list` and read-tier tools; write and consent are never
replayed. Tiers, strict tools, policy globs, consent dialogs, unlock and hub client rules apply
unchanged because the engine's gate runs before the connection is built. Exposure note: the
registry (mode 600, owner-written) names a command that runs as the user, the same class as
`refresh_cmd`; nothing a caller sends reaches argv or env. Not done: Windows process-group kill of
grandchildren, `roots`/`sampling`, per-connection log retrieval command.

## 0.5.0: archetypes, research run, setup run, template catalog

Goal (owner, 2026-10-14): enough shared information to set up almost any MCP with little research, and
from the user's side "connect Linear" -> paste a key or sign in -> done. Templates are releasable
separately from gt.

* **Schema** (`conn_templates.py`): kind `mcp-http` (`url`, `auth_scheme` bearer|header|none,
  `auth_header`, `credential_ref`; registry `auth.scheme` `header` with `auth.header`, sent by
  `conn_mcp` instead of Authorization); `mcp-oauth` `client_registration` auto|preregistered with
  `client_secret_required`, `app_setup_url`, `redirect_port`, and `policy`; `mcp-stdio` `env` (plain,
  `secretcheck.looks_like_secret`, names that change what runs refused), `runtime`
  (node|uv|docker|binary|python, `runtimes.py`), `params` (`{name}` placeholders, kind path|string, never a
  secret), `bridge` ("sse"); top level `archetype`, `docs`, `smoke`. Registry `kind mcp` stdio entries gain
  `env_plain`, `runtime`, `bridge`.
* **Archetypes** (shipped in `templates/connections/`): stdio-npx, stdio-uvx, stdio-docker, stdio-binary,
  mcp-oauth, mcp-oauth-preregistered, mcp-http-key, http-rest, sse-bridge. TODO-marked; `lotr setup` and
  the catalog both refuse a template with a TODO left.
* **Fact sheet** (`facts.py`): per fact `id, field, value, claim, source (https), retrieved, state,
  evidence`; states unverified|self-verified|independently-verified (the last needs verified_by/on);
  load-bearing template fields must each be backed by a fact whose `value` equals the template's.
* **Setup run** (`lotr setup`, `setup_run.py`): template check -> runtime check -> params -> confirm the
  program -> credential refs present (hidden-prompt write to `store:` only from a person's terminal) ->
  `add-http | add-mcp | add-stdio | connect` through the real parser -> smoke (in-process Engine: status,
  catalog, one read-tier call; the result is never printed). Exit 0 done / 3 needs the user / 1 failed.
* **Catalog** (`catalog.py`, `lotr template update`): `lotr-templates/` at the repo root; trust model in
  the module docstring and SECURITY.md 9.2. `dev/lotr_catalog.py build|check` maintains `catalog.json`.
  Since gt 0.21.4 a byte-for-byte copy ships in `templates/catalog/` (`lotr_catalog.py build|bundle`
  writes it, `check` fails on any difference); `conn_templates.BUNDLED_CATALOG` reads it, hash-checked,
  and the higher catalog_version of bundled and fetched wins. The catalog has NO signature (the hashes
  come from the same place as the files); a fetched template that names a program runs it once the
  person gives `--yes`, so `lotr setup` shows the command first.
* **Bridge verification** (self-verified 2026-10-14): `mcp-remote` README (usage `npx mcp-remote <url>`,
  `--transport sse-only`, tokens in `~/.mcp-auth`, `MCP_REMOTE_CONFIG_DIR`) and the npm registry entry
  (bin `mcp-remote`, MIT, 0.14.3 at the time; published from the punkpeye repository, original author
  geelen). Not run against a real SSE server.
* **Catalog gaps closed (2026-10-14)**: `mcp-oauth` `auth_params`, an allow-list of extra
  authorization-request parameters (`access_type` offline|online, `prompt` from none/login/consent/
  select_account, `include_granted_scopes` true|false), sent after LOTR's own, kept in
  `auth.oauth.auth_params` for `lotr login` and re-checked on load; `mcp-http` `auth_scheme` `prefix` with
  `auth_prefix` (one scheme word then a space or one `name=`: `"Token token="`), registry `auth.scheme`
  `bearer` with `auth.prefix`, sent as `Authorization: <prefix><key>`; stdio `{param}` placeholders in plain
  `env` values (never `env_refs`; a substituted value that looks like a secret is `param_invalid`); a
  `NAME.facts.json` beside templates is not a template (`conn_templates.template_files`).
* **Built-in connector review (2026-10-14)**: the REST profiles stay as they are (no REST templates);
  their hosted MCP servers are catalog templates. Consent twins and GraphQL consent widened (rule 5b, 6);
  camelCase read/consent prefixes for MCP tool names; `lotr setup jira|github|m365` with no template names
  both routes (`setup_run.builtin_hints`).
* **Tests**: `tests/test_lotr_templates_setup.py`, `tests/test_lotr_catalog.py`,
  `tests/test_lotr_builtin_connectors.py`, `tests/test_lotr_template_gaps.py`.

## 0.5.0: `lotr pull`, the MCP servers the machine already has (gt 0.21.4)

Request 2026-10-05-lotr-admin-pull-downstream-mcp-connections. The machine already knows its MCP
servers: Claude Code keeps them in `~/.claude.json` and `~/.claude/settings.json`, and several already
hold a token on disk. `lotr pull` turns "run `connect` once per endpoint" into one reviewed list.
Code: `lotrlib/pull.py`; CLI `cmd_pull` in `lotr.py`. Admin plane (refused in mode client, needs an
initialised home).

    lotr pull [--table] [--no-probe] [--allow-private-network] [--from FILE ...]
    lotr pull --import NAME [--import NAME ...] [--dry-run] [--yes]

**The listing is read-only.** It reads, for each source, the `mcpServers` object (and, in
`~/.claude.json`, each project's `mcpServers` as scope `local:<path>`), plus `--from FILE` (for
instance a project's `.mcp.json`). An entry is reduced to names the moment it is read: the URL without
user info, query or fragment, header NAMES, environment variable NAMES, the command line (scanned; an
argument that looks like a credential blocks the command and is not shown). No value from a config or a
token file is kept, printed, logged or put in a command. The same server in two places is one
candidate; one name on two different servers is two (`jira@zone`, `jira-2@zone`). Output is JSON (or
`--table`, lines that never wrap except the commands, which are meant to be copied).

Per candidate: `name` (made safe to print), `id`, `transport`, `scopes`, `url`, `host`, `pinned_hosts`,
`header_names` or `command` and `env_names`, and for an http server `token` {`state` file |
file_not_private | file_unusable | none, `file`, `ref`, `modified`, `needs_auth`} and `oauth_metadata`
{`state` advertised | not_found | unreachable | refused | not_probed, `issuers`, `resource_scheme`};
`status`, `template`, `commands`, `import`, `notes`. Top level: `sources`, `candidates`,
`unmatched_token_files`, `problems`, `summary`, `import_ready` (one `pull --import NAME ...` naming each
ready server, or null) and `next`.

**The token a client keeps.** `~/.claude/<name>-mcp-tokens.json`, matched to a server by name
(case and punctuation folded). `state` is `file` only for a regular file (never a symlink), owned by the
user, with no group or other permission bits, whose JSON has a top-level `access_token` (or
`accessToken`) string. `ref` is then `file:/abs/path#access_token`, the reference
`secrets.resolve` already reads (and re-checks at use). The match is by NAME alone, and registering
sends that token to that endpoint, so the summary and the note say so and show both side by side. `~/.claude/mcp-needs-auth-cache.json` marks
`needs_auth` (read tolerantly; its shape was not verified against a live Claude Code).

**The OAuth metadata probe.** One unauthenticated GET per https candidate of
`/.well-known/oauth-protected-resource` (path form, then root), through `oauth.fetch`: https only
(plain http only to this machine), address-checked, bounded, same-origin redirects, no credential.
`--no-probe` contacts nothing. A private address is `refused` (the hint names `--allow-private-network`,
which is carried into the printed `connect`). A document whose `resource` is `http://` adds a note naming
`--allow-insecure-metadata HOST`; the printed command never carries it unasked.

**Status and the command for each.**

| status | when | `commands` |
|---|---|---|
| `ready_to_import` | http, a private token file | `add-mcp ID --endpoint U --identity S --auth-ref file:...#access_token`; `import` is `pull --import NAME` |
| `needs_sign_in` | http, no token | `connect ID --url U` (or `setup T --id ID` for a matching template); when the server publishes no OAuth metadata, `add-mcp` with no credential first and `connect` second |
| `needs_key` | http with one header (or an `mcp-http` template) | `secret put KEY`, then `add-mcp ... --auth-ref store:KEY [--auth-header NAME]`; the key is never copied |
| `needs_setup` | stdio | a matching template: `setup T --id ID [--param k=v]`; else `secret put` per variable, then `add-stdio ID --command "..." --env NAME=store:KEY` |
| `already_connected` | the registry holds the endpoint or argv | none |
| `unsupported` | `sse`, another transport, plain http off this machine | none; the note says the way forward |
| `blocked` | a URL with user info or query, `${VAR}`, more than one header, a credential-looking argument, `PATH`-class environment | none; the note says why |

A template is matched by exact URL (a trailing slash ignored) for http, and for stdio by program name,
arguments and `{placeholders}` (a package's version ignored, `--yes` is `-y`); the owner's own template
wins over the catalog. That is how a known quirk (`skip_audience_check`, a pinned authorization server,
`auth_params`) is pre-filled without `pull` naming any employer or host. `connect` and `secret put` are
for the owner's own terminal; every command carries `--home` or `--zone` when `pull` was given one.

**`--import NAME` registers, and only on a yes.** One `--import` per name the owner typed; there is no
"all", and a name that is unknown, ambiguous, or not `ready_to_import` is refused (`pull_unknown_candidate`,
`pull_ambiguous`, `pull_not_importable`, the last with the commands that would do it). It prints a
summary (the reference, never the token) and asks `[y/N]` at a terminal, or takes `--yes`; with neither
it stops (`import_needs_confirmation`), and a no is `import_declined`. `--dry-run` shows the plan
(`would_register`) and touches nothing. The registration is `add-mcp` with `--auth-ref`: the registry
holds the reference, the first `tools/list` is made with the token read in memory, and a token the server
refuses registers nothing. LOTR does not renew a token it did not mint: with no `--refresh-cmd` an expired
one is `auth_failed` (the note says so).

**Not done.** No `--all`; no import of a keyed or stdio server (their commands are printed); no read of
Claude Code's own credential store; plugin-provided servers are not in these files; an `mcp-http` or
`mcp-oauth` template cannot yet carry `--allow-insecure-metadata`. The formats of
`mcp-needs-auth-cache.json` and of the `*-mcp-tokens.json` sidecars were not checked against a live
machine (self-verified against fixtures shaped like the request describes them).

**Tests**: `tests/test_lotr_pull.py`.

## 0.5.0: the capability index and contracts (gt 0.21.4)

Request `2026-10-08-lotr-acs-capability-index-and-contracts`, from the owner's comparison with the Agent
Capability Standard (ACS v1.0.0; a specification, no code). LOTR already had ACS's core (a fixed tool
surface, operations found on demand, payloads kept out of the main context by specialist agents). This
section is the rest of what the comparison named, as the seven acceptance criteria of the request.

**No fifth tool.** `describe` is `find(connection, op)` and a reference is run by passing it as the `op`
of a call. The tool list is still `find`, `call_read`, `call_write`, `call_consent`; the three call tools'
schemas are unchanged (`required` is still `connection` and `op`); `find` gains two optional properties,
`op` and `role`. So no agent's `tools:` line, no `module.json` lockdown allow rule and no user's
permission rule needs to change, and a reader that holds only `find` and `call_read` can describe and
run by reference. Both MCP doors get it from `lotrlib/mcp_dispatch.py`: `request_for("find", {...})`
returns `("describe", params)` when `op` is present (then `connection` is required, else `bad_request`),
and the daemon's methods are now `find`, `describe`, `call`, `status`, `catalog`, `ping` (`POST
/v1/describe` exists too). `lotr describe CONN OP [--role R]` is the CLI form.

### 1. find stays within a token budget (`lotrlib/contracts.py`)

Tokens are an estimate everywhere here: characters of compact JSON / 4 (the savings ledger's own).

| constant | value | meaning |
|---|---|---|
| `ITEM_TOKENS` | 50 | one operation's entry, every field but `params` |
| `FIND_BUDGET_TOKENS` | 1200 | a whole find result; `limits.find_budget_tokens` in gateway.json (50 to 1,000,000) |
| `MIN_OUTCOME_CHARS` | 24 | a summary clipped below this is no outcome: the entry is dropped |
| `SUMMARY_MAX_CHARS` | 90 | the longest outcome a SHIPPED op or recipe may carry (the lint, below) |

An op or recipe entry is `{connection, op, kind, summary, tier, risk}` (+ `stale`, and `params` with
`detail: schema`). `tier` is `read | write | consent`; `risk` is the same fact in ACS's words, `read |
write | destructive` (consent = destructive, which is what needs a person's confirmation). The tier is
the one the call would have with no arguments, so an op shows it even when the profile does not name
one; a GraphQL op, whose tier depends on the document, lists as `write`. A summary that does not fit
50 tokens is clipped at a word boundary with an ellipsis, never below `MIN_OUTCOME_CHARS`; an entry whose
id, tier and risk alone leave no room for that is left out and counted in `notes` ("N operations could
not be summarized within 50 tokens"). Within budget the order is the index's (BM25) and nothing else
changes. Over budget the connections are ordered by their best entry, each connection's entries stay
together, and the list is cut where the budget runs out ("N more matches left out to stay within the
1200-token budget"). The cut counts what is LEFT after the caller's view is applied, and `limit` is
applied after it too: an op the caller may not see never uses up a slot. `find("")`, the connection
catalog, is unchanged.

### 2. outcome wording (`contracts.lint_summary`, `lint_shipped`)

A summary says what the call achieves. The lint flags the unambiguous API shapes: an HTTP method with a
path, a URL, a lower-case host name, a path of two segments, a template placeholder (`{x}`, `{{x}}`), a
query string, search syntax (`is:pr`), code syntax (`a = b`, `f()`), and wire vocabulary (endpoint,
REST, HTTP, JSON, XML, GraphQL, OData, SOAP, payload, request body), plus more than one line, empty, and
over 90 characters for an op or recipe. `lint_shipped()` runs it over every curated op summary, every
recipe's summary, every connection template's `summary` and `connect.description` (a description lands in
the connection catalog the model reads) and the bundled catalog's templates; a scaffold that still says
TODO is skipped. `tests/test_lotr_acs.py` runs it as the gate. `lotr template check` shows the same
finding as a warning on the user's own template. Six shipped summaries were rewritten to pass (the
GitHub issue search and GraphQL ops, both Jira searches, and two recipes) and one template description
(`connections/example.json`, which named a host); their search words moved to `tags`.

### 3. describe (`Engine.describe`)

`find(connection, op)` returns the planning contract of one op or recipe: `outcome` (the full
one-line summary, to 200 characters), `tier`, `risk`, `effects` (what it does to the world),
`confirmation` (who must confirm: the owner for consent, and "a single-use token the user mints" when
`paranoid.action_tokens.lotr_<tier>` is on), `inputs` (`[{name, required, description}]`, required
first; for an MCP tool the server's own `required` list, for a built-in profile the path placeholders, for
a recipe the params with no default), `call_with` (the tool that carries it), `ref`, `expires_in_s`,
`single_use`. It never contains a method, path, base URL, host, header, credential reference, wire
schema or a recipe's steps (a test serialises the contract and looks for each). A raw op
(`GET /path`) is described under the string the caller gave.

### 4. visibility before planning (`lotrlib/visibility.py`)

What a caller could not use is left out of `find`; `describe` answers for it exactly as for an op that
does not exist (`unknown_op`, with hints that name only visible ops); and the rules run again at call
time on the operation a reference names. One rule, in order: the owner's `policy.deny`; `policy.check_client`
(client allow list, tier ceiling, revoked; for the local caller gateway.json `local`); a hub client on an
OAuth sign-in may only read; the caller's declared `role`; gt unlock's authority hard-refusing the scope
`lotr:<connection>:<tier>` (`mcp_only`, `denied`; `locked` and `step_up` stay visible because the person
can unlock, and the shim does it on the call; asked with `request=False`, so finding things out never
prompts). The unknown-connection hint and the unknown-op hint are trimmed to what the caller may see.

- **agent split and lockdown** enter through `role` (`find(role=...)`, `lotr find --role`): `reader` sees
  `read`; `writer` sees `write` and `consent`; `combined` sees all three only where `agent_split`
  combined is IN EFFECT, which needs lockdown `insecure` (`lotrlib/agent_split.py`); at any other level or
  with `split` a combined agent sees no operations and is told why. A role narrows and never widens, and
  the gateway cannot tell which agent is really asking: it keeps a reader's context clear of tools it
  cannot call, and the tool tiers still refuse at call time. This is not a boundary (the 0.4.0 caveat
  stands). The shipped agents do not pass it yet (see "Not done").
- **persona** is not a source. A persona changes what gt offers, never what it enforces (gt ADR-18;
  `lotrlib/persona.py` says the same of LOTR; the pack schema accepts no field that could forbid an op).
  A test pins that setting a persona changes nothing in `find`.
- **lockdown** has no per-level operation refusal in the gateway (the levels set Claude Code's allow
  rules; `module.json` `lockdown.*.deny` is empty). Its reach here is `combined` above and the paranoid
  tokens in `confirmation`.

### 5. references (`lotrlib/refs.py`)

`gtr_` + 32 characters, issued by describe, bound to `{connection, op, tool, tier, client, seat}`,
single use, five minutes (`limits.ref_ttl_s`, held to 1..900). Redeemed in the `op` slot of
call_read / call_write / call_consent, with the same `connection`; a string in the `op` slot is a
reference only if it matches `^gtr_[A-Za-z0-9_-]{32}$` (so a downstream tool named `gtr_x` is still
callable). The engine holds the store in memory with the cursors, as one class, `RefStore`, used for
both (`Engine._cursors` is the cursor store's own dict). It is the action-token mechanism's shape, not a
second one: an id that names a record held elsewhere; checks in the tokens' order (`unknown`, `spent`,
`expired`, `wrong_target`, `wrong_session`, the codes `gt_action_token` already uses); spent by use, not
burned by a refusal; the tokens' lifetime and ceiling (`gt_action_token.DEFAULT_TTL` 300, `MAX_TTL` 900).
It checks at the tokens' chokepoint: the reference is resolved to its op before `action_token.check`, so
a token minted for `connection/op` is checked against the real op. **A reference is not a token and does
not stand in for one:** it selects, it authorises nothing. With `paranoid.action_tokens.lotr_*` on, the
user-minted token is still required, and policy, tier, client, unlock and consent all still run on the
operation. Spent only after all of them agree, immediately before the downstream call. The audit line
records `op` and `ref` (twelve hex digits of a hash; the id itself is never written).

### 6. errors (`lotrlib/redact.py`, `Engine._audit`)

The engine keeps the full error and writes it to the audit line as `detail` (message and hints on one
line, at most 400 characters, withheld entirely if it holds credential-shaped text); the admin plane
(the CLI, `/v1`) still shows it. The two MCP doors pass every error through `public_error` before the
model reads it: a code the gateway wrote (policy, tier, caller, cursor, reference, token, consent,
unlock) keeps its message and hints minus any URL or secret reference; every other code (a downstream's
HTTP status, a network error, a sign-in or secret problem, a code not known yet) becomes one fixed
sentence per family and its hints are dropped, except a sign-in command (`run: lotr login ...`). Unknown
means generic. The audit log's old promise (no arguments) stands; its `detail` can carry a downstream's
words, which is the point, and the file is mode 600 on a private directory.

### 7. the savings ledger (`lotrlib/ledger.py`, gt-usage 0.1.10)

Each find with a query appends one `lotr-find` line to `~/.claude/golden-thread/usage/savings.jsonl`:
`actual_eq` is the tokens of what the model received (results and notes), `counterfactual_eq` the tokens
of every operation of the same connections as a full tool definition (an MCP connection's cached tool
definitions as they are; a built-in profile rendered the same way), `state` `estimated`. Counts only: no
query, id or argument. Written only if the `usage` folder exists and `savings_ledger` is not `off`; a
failed write is dropped. `gt_savings.py` has the row `LOTR find budget (vs full schemas)`, a `design` row
until a line exists. Measured on this build's fixture (three connections, eight results): 377 tokens
against 2,811.

### Not done, on purpose

- Becoming ACS-conformant (Levels 1 to 3), federation across trust domains, changing MCP servers.
- The shipped agents do not yet pass `role` or use references; their text is pinned by tests and the
  default flow (find, call by name) is unchanged. `describe` is there for them when someone wants it.
- `find` results are not rendered as compact JSON on the wire (the envelope is still indent 1), so the
  ledger's figure is a little under what the model reads.
- The server `instructions` catalog is not filtered per hub client (it never was); it lists connections,
  not operations.

### Tests

`tests/test_lotr_acs.py`: the seven criteria against the real engine, both MCP doors (the stdio shim in
process, the hub's HTTP server) and the real `lotrd`, `lotr_mcp.py` and `lotr` as processes against a fake
GitHub; the pure pieces by unit tests. Two assertions in `tests/test_lotr_frontends.py` changed because
the behaviour did, on purpose: `find`'s property set grew by `op` and `role`, and a hub client enrolled
at `write` no longer sees a consent operation in `find` (the call is still refused with `tier_ceiling`).
`test_lotr_mcp_output_schema`, `test_lotr_e2e`, `test_lotr_cursor`, `test_lotr_action_token`,
`test_lotr_agents`, `test_lotr_agent_split` and `test_savings_ledger` pass unchanged.

## 0.5.0: MCP over HTTP, the hub's `/mcp` front door (gt 0.21.4)

**What existed before.** LOTR had two MCP-shaped things that never met: the stdio shim
(`lotr_mcp.py`, four tools, one per Claude Code session) and the hub's HTTP server (`/v1/<method>`, a
proprietary five-method protocol with Bearer + `X-GT-Client`). A plain MCP-over-HTTP client
(any MCP connector) had nowhere to connect: `/mcp` was a 404. 0.5.0 adds `POST /mcp` to the
hub's existing HTTP server, same port, no new daemon.

**Switches** (`gateway.json`, `listen`, both bool, both default `false`, validated by
`config.validate` / `config.check_listen`; a string such as `"false"`, a number or `null` is
`settings_invalid`, and `lotrd` stops on it rather than read it as true; changing either needs a
`lotrd` restart; `lotr init --mode hub` writes both as `false` and says where to turn one on):

| key | false (default) | true |
|---|---|---|
| `listen.mcp_http` | `/mcp` is a 404, like any unknown route; every `/v1/...` route is unchanged | `POST /mcp` speaks MCP |
| `listen.mcp_http_trust_loopback` | `/mcp` needs Bearer + `X-GT-Client` | a request from this machine with neither header may name an enrolled client as `?cid=<id>` (needs `mcp_http`) |

`serve_http(..., mcp_http=False, mcp_http_trust_loopback=False)` takes both as **keyword-only** arguments
and only the boolean `True` turns either on (the second lock behind `lotrd`'s check).

**The shared surface** (`lotrlib/mcp_dispatch.py`, new; pure, no I/O): `PROTOCOLS`, `DEFAULT_PROTOCOL`,
`SERVER_INFO`, `FALLBACK_INSTRUCTIONS`, `TOOLS`, `TOOL_NAMES`, `OUTPUT_SCHEMAS`, `tools_for(protocol)`,
`negotiate_protocol`, `make_initialize_response(asked, instructions)`, `request_for(name, arguments)`
(a tools/call -> `("find"|"call", params)` or `bad_request`), `error_result(exc)`,
`envelope(result)` (`{content, structuredContent, isError}`), and `handle_message(msg, protocol,
initialize, tools_call)`, the one JSON-RPC router. `lotr_mcp.py` imports all of it and re-exports every
name it used to define (`lotr_mcp.TOOLS`, `tools_for`, `OUTPUT_SCHEMAS`, ...), keeping the stdio
loop, the daemon client, the unlock handling and the Claude Code routing text (`ROUTING`,
`ROUTING_COMBINED`). The stdio shim's output is byte-identical before and after (self-verified
2026-10-15: the 0.5.0 `lotr_mcp.py` before the extraction against after, one scripted session of
mixed, malformed and edge-case messages (34 reply lines), four protocol versions, daemon up and down,
stdout compared byte for byte).

**The route** (`server._HttpHandler._post_mcp`). One JSON-RPC 2.0 message per POST, answered as
`application/json`; no SSE stream, no `Mcp-Session-Id`, no batches.

| request | answer |
|---|---|
| `mcp_http` off, any method | 404 `not_found` (the `/v1` 404 body) |
| `GET` / `DELETE` / `PUT` ... `/mcp`, door on | 405 `method_not_allowed`, `Allow: POST` (MCP's "no stream here") |
| no / bad credentials | 401 `unauthorized`; revoked client 403 `client_revoked` (the very bodies `/v1` sends) |
| body over 1 MB, bad `Content-Length` | 413 / 400 `{"error": ...}` as `/v1` |
| body not JSON | 400 with JSON-RPC `-32700` (id null) |
| a JSON array (batch), a non-object, a message with no method and no result/error | 400 with JSON-RPC `-32600` |
| a notification (`notifications/*`), or a response to us | 202, empty body |
| a request | 200 with the JSON-RPC response (an unknown method is a 200 carrying `-32601`) |

Methods: `initialize`, `ping`, `tools/list`, `tools/call`; routing, error codes and messages are the
stdio shim's (`handle_message`). `initialize` answers the client's `protocolVersion` if it is in
`PROTOCOLS`, else `2025-06-18`; `capabilities {"tools": {"listChanged": false}}`; `serverInfo`
`{"name": "gt-lotr", "version": <release>}`; `instructions` = the engine's `catalog` text (at most 1,800
characters asked for, 2,048 kept) or `FALLBACK_INSTRUCTIONS` when the engine cannot say. The Claude
Code routing rule (`ROUTING`, which names the `gt-lotr:*` plugin agents) is **not** appended: a plain
MCP client has no such agents. HTTP carries no session, so the protocol for `tools/list` (outputSchema
from 2025-06-18 on) is read from each request's `MCP-Protocol-Version` header; missing or unknown means
the default, as for a stdio client that never initialized. `tools/list` is exactly the stdio list for
that protocol, field for field (tested against the real stdio shim).

**`tools/call`** runs `mcp_dispatch.request_for` then `server.dispatch(holder, "find" | "call", params,
client_id, remote=True)`: the same dispatcher as `/v1`, as the authenticated client. Policy (allow
globs, tier ceiling), strict tools, the OAuth-unattended rule, audit lines and consent are the engine's
and unchanged: `call_consent` is advertised, a client enrolled below `consent` gets the engine's
`tier_ceiling` as a tool error (`isError` true), never a hang, and a consent-tier operation by a client
that may do it raises the hub's own confirmation (`local.confirm`) exactly as `/v1/call` does. There is
no remote confirmation. Malformed arguments are a `bad_request` tool result before the engine is
touched; a `GatewayError` keeps its code, message and hints; any other exception is reduced to its type
name. The catalog text in `initialize` is not filtered per client (nor is `/v1/catalog`).

**Authentication.** Bearer first, exactly `/v1`'s (`_authenticate_bearer`, shared): `Authorization:
Bearer <secret>` + `X-GT-Client`, `Registry.authenticate` on every request, 401 / 403 as above. The
loopback fallback is tried only when `mcp_http_trust_loopback` is on **and** the request carries neither
`Authorization` nor `X-GT-Client` (a failed Bearer is never rescued by `?cid=`), and only if all hold:
the TCP peer is a loopback address; no `Forwarded`, `X-Forwarded-For`, `X-Forwarded-Host`,
`X-Forwarded-Proto` or `X-Real-IP` header (a proxy stands in the way); no `Origin` header (a browser's
mark: a web page could otherwise ask a secretless door for tool calls); the `Host` is a loopback name
(DNS rebinding keeps the attacker's own); exactly one `cid`, shaped like a client id, that the
registry lists. Every refusal before the registry lookup is the same 401 as "no credentials", so a
non-local caller cannot tell the fallback exists; an unknown `cid` is 401, a revoked one 403. The
client's own allow list and tier ceiling then apply. **A secret is never read from a URL**: a
`?secret=` or any other query key is ignored (the request is then unauthenticated), and nothing is
logged (`log_message` is silent). Not done: an Origin allow-list for a connector that is itself a
browser page (it must send the Bearer headers instead).

**Exposure.** The existing guard stands: a non-loopback `listen.host` without TLS is refused
(`insecure_listen`), so Bearer secrets never cross a network in plaintext. Turning `mcp_http` on adds a
second authenticated route to a port that already serves `/v1`; it adds no secret, no process and no
port. `mcp_http_trust_loopback` removes the secret for programs on the hub machine, so every program
of every user on it can act as that enrolled client: enrol such a client at the lowest tier it needs
(`--max-tier read` or `write`), and do not enable it behind a reverse proxy on the same machine
(the proxy headers and Host checks stop the common cases; they are not a substitute for not doing it).

**Out of scope, as asked**: SSE streaming and server-initiated messages, sessions, JSON-RPC batches,
and remote consent or Touch ID.

**Tests**: `tests/test_lotr_frontends.py` `McpHttpTests` (the route against fake engine and registry),
`McpHttpConfigTests` (the switches), `McpHttpDaemonTests` (a hub `lotrd` with the real engine and
registry); the stdio shim by `McpShimTests`, `test_lotr_mcp_output_schema`, `test_lotr_agent_split`,
`test_lotr_agents` unchanged.

## 0.5.1: `lotr onboard`, from an MCP to a working thin agent (gt 0.22.0)

Plan phases 36 to 43 of the 0.22.0 build. The owner's aim: someone who does not know which template fits, or what to
ask for, can connect an MCP and get an agent that keeps heavy results out of the main conversation.

**Commands.** `lotr onboard NAME --url URL | --package SPEC` (probe, template, fact sheet, then `lotr setup`);
`lotr onboard discover ID` (manifest); `lotr onboard agent ID [--yes] [--force]` (agent file and guard registration);
`lotr onboard compare ID [--last N]`; `lotr onboard tune ID [--direct-tokens N] [--never-summarise OP] [--summarise OP]`.
The words `discover agent compare tune` are reserved and cannot be template names. The script an agent runs is
`scripts/lotr_agent_call.py CONNECTION OP [--args JSON] [--select PATHS]`.

**Probe (`lotrlib/onboard_probe.py`).** At most three requests, all through `oauth.fetch` (the owner's network policy:
https, or loopback only when the owner allows it; no private or metadata address; no proxy; no redirect). 401 with
OAuth discovery and a registration endpoint is `mcp-oauth`; without one `mcp-oauth-preregistered`; a bearer challenge
with no OAuth metadata is `mcp-http-key`, confirmed by ONE request carrying the fixed fake credential
`gt-probe-not-a-credential` (which matches no credential rule); a JSON-RPC answer with no credential is `mcp-http-key`
with `auth_scheme none`; an SSE endpoint event is `sse-bridge`. Phase 36 measured that an OAuth server answers the same
with or without a credential, so none is sent there. A launcher line is classified from its text alone and never run.
The result holds status codes and header names, never a header value.

**Manifest (`onboard_discover.py`).** The operations come from the registry's cached MCP tool list through
`profiles.mcp_profile`, so tiers are the gateway's own. Per operation: name, tier, argument names, types and required
flags, and two hashes (description, schema) for the pin. No description text is kept; the descriptions go to
`gt_sandbox_reader.py` and only a count returns. Names outside `[A-Za-z0-9_.-]{1,64}` or shaped like a credential are
dropped and counted; more than 200 operations are cut with the count. If the reader is absent the manifest says
`not-scanned`, never clean.

**The call script and router (`lotr_agent_call.py`, `onboard_route.py`).** Refuses before sending anything: an
operation not in the manifest, a non-read tier, an unknown or mistyped or missing argument. Sends through the gateway
(`call_read`), keeps the data, cursor and notes of the answer, and routes by size (characters / 4, `estimated`): up to
`onboard_direct_tokens` (default 1500; module setting, or `tune` per connection) it prints the result; above it, the
result is written whole to `onboard/records/ID/` (mode 600) and `gt_sandbox_reader.summarise` (a headless model with no
tools, `onboard_summary_model`, default haiku) returns `{summary, facts, truncated}`. If the summariser cannot run, a
short preview and the reason are printed, never the large result. A credential-shaped string withholds the result. The
model is told the operation and argument names, never values. The last 10 stored results are kept.

**The agent (`onboard_agent.py`, `hooks/guard_lotr_agent.py`).** A fixed template filled from the manifest; `tools:
Bash` only. Phase 36 measured that an agent file cannot confine its own Bash (a `tools:` pattern and a hook in the
agent's frontmatter both failed), so gt's PreToolUse guard does, keyed on `agent_type` starting `lotr-` and on
`onboard/agents.json`. It allows only the script with that agent's connection, and fails closed for those agents.
Verified live once (Claude Code 2.1.306, Haiku): the script ran, `touch` was denied.

**Not here.** A generated writer agent; anything that reads, sends or stores a credential; running a package during the
probe; rewriting an agent when a server's tools change (a pin mismatch only warns; `agent --yes` regenerates).

**Tests.** `test_lotr_onboard_probe`, `_flow`, `_discover`, `_agent`, `_compare`, `_e2e` (the real daemon and scripts
against a fake hosted MCP), `test_lotr_agent_call`, `test_guard_lotr_agent`, and `Summarise` in `test_gt_sandbox_reader`.

## 0.5.1 validation fixes (gt 0.22.0)

* **`local.confirm` is read once, at start.** The engine reads `gateway.json` `local.confirm` when the
  daemon starts and ignores a later edit (a hot reload still applies everything else), so a process that
  can write the file cannot switch the consent prompt off on a running daemon; a person restarts the
  daemon to change it. A start with `confirm: none` writes an audit line (`tool` `start`, `verdict`
  `confirm_none`) and `status` shows `confirm` and a note (`lotr status --table` prints it). The limit:
  anything that can write `gateway.json` AND restart the daemon as the same user can still turn the
  prompt off; the file is owner-only (mode 600), not tamper-proof against its owner's own account.
* **Result shaping withholds every shape in `gt_secret_shapes.HIGH_CONFIDENCE`** (`shaping.SHARED_SHAPES`,
  a copy kept in step by `tests/test_lotr_shapes_and_tiers.py`), plus a connection-URL password and a
  credential-named field with a literal value (`"password":"..."`, `password=...`, a dict key).
* **MCP tool tiers.** The destructive-word list gained nuke, obliterate, truncate, format, shred,
  terminate and similar, so `get_nuke_everything` is consent. A connection may declare
  `read_only_tools: [names]` (owner-edited in `registry.json`): a listed name is a read; every other name
  is rated by the same rules (a risky word is consent, an unplaceable name stays write).

## 0.5.1: a downstream LLM behind the gateway (gt 0.22.0, owner 2026-10-25)

Controls for routing a model request over the gateway: an HTTP model server, a hosted API behind
`http-rest`, or an MCP tool that answers a chat request. All of them are additive: with none of the new
fields or arguments in use nothing changes, except the one BREAKING change below. The code is
`lotrlib/upstream.py`; `conn_http.py`, `engine.py`, `client.py`, `server.py`, `shaping.py`,
`registry.py`, `conn_templates.py` and `redact.py` use it. The gateway never sleeps or retries on its
own: it reports, the caller decides.

### Error codes and the error dict

An error is still `{"ok": false, "error": {...}}`. The error dict is `{code, message, hints}` plus, for the
codes below only, two optional plain numbers:

| code | when | `upstream_status` | `retry_after_s` |
|---|---|---|---|
| `upstream_rate_limited` | the provider answered 429, or (MCP, best effort) a tool result with `isError` whose text reads like a rate limit | the HTTP status (429); absent for MCP | seconds, from `Retry-After`; absent when the header is missing or junk, and always absent for MCP |
| `upstream_error` | the provider answered 5xx (except 504) | the status | from `Retry-After` when present (a 503 often has one) |
| `upstream_payment_required` | the provider answered 402 (a hosted LLM out of credit or over its key limit); retrying does not help | the status (402) | absent |
| `upstream_timeout` | the provider answered 408 or 504 | the status | from `Retry-After` when present |
| `rate_limited_locally` | the gateway's own per-connection `limits` refused the call; nothing reached the provider | absent | seconds until a slot frees (at least 1) |

The mapping is a table in `upstream.py` (`STATUS_CODES` by exact status, `STATUS_CLASSES` by first digit),
so a new status or provider is one row. Any other status keeps `http_<status>` and no extra field.
`retry_after_s` is a whole number of seconds, 0 to 3600 (`upstream.RETRY_AFTER_MAX_S`): `Retry-After` in
delta-seconds or IMF-fixdate form is read, a date in the past is 0, a larger value is clamped, anything
else is ignored. The model doors (`lotrlib/redact.public_error`) replace the provider's words with a fixed
sentence per code and keep only these two numbers, and only when they are numbers.

**BREAKING (gt-lotr 0.5.1):** an upstream 5xx used to be `http_5xx`; it is now `upstream_error` (408 and
504 `upstream_timeout`; 429 was `http_429`; 402 was `http_402` and is now `upstream_payment_required`). A caller that matches `http_5` must also match the new codes.
`upstream_status` carries the number the old code spelled.

**MCP is best effort.** There is no standard rate-limit signal on MCP, so only a tool result with
`isError` is read, and only the tool's own text (not the connection or tool name) against
`upstream.RATE_LIMIT_PHRASES`: "rate limit", "rate-limit", "ratelimit", "too many requests", "quota", or
the bare number 429. A tool that says anything else stays `downstream_error`. No `retry_after_s` is ever
given for MCP.

**Health.** A rate limit, a 5xx, a timeout status and a local limit say the provider is busy or failing,
not that the connection is down: none of them is a failure in `lotr status` (state stays `connected`). Each
is audited as its own verdict (`error:upstream_rate_limited`, ...) with `status` = the upstream status and
`retry_after_s` when known.

### Per-call timeout

* `connect.timeouts.call_s` (template) / `timeouts: {call_s}` (registry), http connections only, 1 to 3600
  seconds, validated on add and on load. Default stays 20.
* `request("call", {..., "timeout_s": N})` carries a per-call value to the connection. The engine holds it
  to `[1, ceiling]`; the ceiling is `gateway.json` `limits.max_call_timeout_s` (default 300, at most 3600).
  A caller can lower its timeout or raise it up to the ceiling, never past it; a non-number is
  `bad_request`. It reaches http connections and stdio MCP connections (one request, held to
  [1, 3600] like an http one; a timed-out stdio request still stops its process). An http-transport MCP
  connection keeps its own `timeouts.call_s`.
* The client waits `timeout_s` (held to 3600) plus a 10 s grace, and never less than its own timeout, so the
  gateway's own error arrives before the socket gives up. The Windows named-pipe door is unchanged.

### Local limits (off by default)

`limits: {max_calls_per_min, max_concurrent}` on a connection (either kind; whole numbers 1 to 1,000,000).
Checked in `Engine.call` after the policy and consent steps and before the reference is spent and before the
upstream call. A refused call is `rate_limited_locally`, audited, and uses no slot. Per-client limits are
not built: `policy.check_client` is an allow list and a tier ceiling with no per-client state, so a limit per
client is a separate piece of work.

### Truncation

When shaping cut anything (a list paged to `default_page`, or the result over `max_result_chars`) the call
envelope gains `truncated: true` and `original_chars` (the serialized length after projection and noise
removal, before any cut). An uncut reply has neither field. `request("call", {..., "max_result_chars": N})`
sets the cap for that call, up to `limits.max_result_chars_ceiling` (default 200000, and never below the
configured cap); a larger request is held to it with a note. 0, a negative, a boolean or a non-integer is
`bad_request`.

### Model-op blast radius

`llm: {"tools": "none" | "read" | "write"}` on a connection (registry and template `connect.llm`) declares
what a model op can trigger. The effective tier of an op on it is the higher of the op's own tier and the
declared one (`none` adds nothing; `deny` stays `deny`); `find`, `describe`, `call` and the tool split all use
the effective tier. It is shown as `llm_tools` in `find` results and `status`, and by `lotr status --table`
(`llm tools: write`, `30 calls/min`, `call timeout 90s`). No shipped template declares it.
