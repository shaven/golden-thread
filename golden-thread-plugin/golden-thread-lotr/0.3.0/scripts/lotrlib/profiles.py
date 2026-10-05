"""Curated operation tables, one per API profile.

A profile names a family of APIs the gateway knows how to talk to. Its curated ops are the
handful of calls worth finding by name; anything else is reachable raw as "METHOD /path".
The summaries are what search ranks, so they carry the words a model would use to ask.

Paths are relative to the connection's base_url:
  github   https://api.github.com                 (REST v3 + GraphQL at /graphql)
  jira-v3  https://<site>.atlassian.net           (Jira Cloud REST v3)
  jira-v2  https://<jira-host>[/context]          (Jira Data Center / Server REST v2)
  graph    https://graph.microsoft.com/v1.0       (Microsoft Graph v1.0)
  generic  anything; raw ops only
"""
import copy
import difflib
import re
import unicodedata
from urllib.parse import parse_qsl

from .errors import GatewayError

METHODS = ("GET", "HEAD", "POST", "PUT", "PATCH", "DELETE")
_PLACEHOLDER = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")

_JIRA_FIELDS = "summary,status,assignee,priority,updated"


def _op(name, method, path, summary, params=None, query_defaults=None, tags=(), tier=None):
    d = {"name": name, "method": method, "path": path, "summary": summary,
         "params": dict(params or {}), "query_defaults": dict(query_defaults or {}),
         "tags": list(tags)}
    if tier:
        d["tier"] = tier
    return d


# -- GitHub REST v3 -------------------------------------------------------------------------

_OWNER = {"owner": "repository owner (user or organisation login)",
          "repo": "repository name"}

_GITHUB_OPS = [
    _op("list_pulls", "GET", "/repos/{owner}/{repo}/pulls",
        "List pull requests (PRs) in a GitHub repository, open by default",
        dict(_OWNER, state="open | closed | all (default open)",
             head="filter by head branch as user:ref-name",
             base="filter by base branch name",
             sort="created | updated | popularity | long-running",
             direction="asc | desc",
             per_page="page size (max 100)"),
        {"per_page": 20}, ("github", "pr", "pull request", "code review")),
    _op("get_pull", "GET", "/repos/{owner}/{repo}/pulls/{pull_number}",
        "Get one pull request (PR): state, mergeable, draft, head and base branch, review status",
        dict(_OWNER, pull_number="pull request number"),
        {}, ("github", "pr", "pull request", "status")),
    _op("list_issues", "GET", "/repos/{owner}/{repo}/issues",
        "List issues in a GitHub repository (also returns pull requests), open by default",
        dict(_OWNER, state="open | closed | all", labels="comma-separated label names",
             assignee="login, none or *", creator="login of the issue author",
             since="ISO 8601 timestamp; only issues updated at or after",
             sort="created | updated | comments", direction="asc | desc",
             per_page="page size (max 100)"),
        {"per_page": 20}, ("github", "issue", "bug", "ticket")),
    _op("get_issue", "GET", "/repos/{owner}/{repo}/issues/{issue_number}",
        "Get one GitHub issue: title, body, state, labels, assignees",
        dict(_OWNER, issue_number="issue number"),
        {}, ("github", "issue", "ticket")),
    _op("search_issues", "GET", "/search/issues",
        "Search GitHub issues and pull requests across all repos with a query "
        "(is:pr is:open author:@me assignee:@me review-requested:@me repo:owner/name)",
        {"q": "search query, e.g. 'is:pr is:open author:@me'",
         "sort": "comments | reactions | created | updated", "order": "asc | desc",
         "per_page": "page size (max 100)"},
        {"per_page": 20}, ("github", "search", "issue", "pr", "pull request", "mine", "assigned")),
    _op("create_issue", "POST", "/repos/{owner}/{repo}/issues",
        "Create (open, file) a new issue in a GitHub repository",
        dict(_OWNER, title="issue title", body="issue body (Markdown)",
             labels="list of label names", assignees="list of logins",
             milestone="milestone number"),
        {}, ("github", "issue", "new", "file a bug")),
    _op("comment_issue", "POST", "/repos/{owner}/{repo}/issues/{issue_number}/comments",
        "Add a comment to a GitHub issue or pull request conversation",
        dict(_OWNER, issue_number="issue or pull request number", body="comment text (Markdown)"),
        {}, ("github", "comment", "reply", "issue", "pr")),
    _op("merge_pull", "PUT", "/repos/{owner}/{repo}/pulls/{pull_number}/merge",
        "Merge a pull request (PR) into its base branch",
        dict(_OWNER, pull_number="pull request number",
             commit_title="title for the merge commit", commit_message="extra detail",
             sha="head SHA that must match for the merge to proceed",
             merge_method="merge | squash | rebase"),
        {}, ("github", "pr", "pull request", "merge"), tier="consent"),
    _op("get_repo", "GET", "/repos/{owner}/{repo}",
        "Get a GitHub repository: description, default branch, visibility, open issue count",
        dict(_OWNER), {}, ("github", "repository", "repo")),
    _op("list_workflow_runs", "GET", "/repos/{owner}/{repo}/actions/runs",
        "List GitHub Actions workflow runs (CI builds, checks) for a repository, newest first",
        dict(_OWNER, branch="branch name", event="push | pull_request | ...",
             status="completed | in_progress | queued | success | failure | ...",
             actor="login that triggered the run", head_sha="commit SHA",
             per_page="page size (max 100)"),
        {"per_page": 20}, ("github", "actions", "ci", "build", "checks", "workflow")),
    _op("graphql", "POST", "/graphql",
        "Run a GitHub GraphQL query (a mutation is classified as a write)",
        {"query": "GraphQL document", "variables": "object of GraphQL variables"},
        {}, ("github", "graphql", "query")),
]


# -- Jira -----------------------------------------------------------------------------------

def _jira_ops(v):
    root = f"/rest/api/{v}"
    key = {"issueIdOrKey": "issue key (e.g. PROJ-123) or numeric id"}
    if v == 3:
        search = _op("search", "GET", f"{root}/search/jql",
                     "Search Jira issues with JQL (assignee, project, status, sprint); "
                     "e.g. assignee = currentUser()",
                     {"jql": "JQL query, e.g. 'assignee = currentUser() AND statusCategory != Done'",
                      "fields": "comma-separated fields to return",
                      "maxResults": "page size",
                      "nextPageToken": "page token from the previous response"},
                     {"maxResults": 20, "fields": _JIRA_FIELDS},
                     ("jira", "issue", "search", "jql", "ticket"))
        comment_body = "comment body in Atlassian Document Format (ADF) object"
    else:
        search = _op("search", "GET", f"{root}/search",
                     "Search Jira issues with JQL (assignee, project, status, sprint); "
                     "e.g. assignee = currentUser()",
                     {"jql": "JQL query, e.g. 'assignee = currentUser() AND statusCategory != Done'",
                      "fields": "comma-separated fields to return",
                      "maxResults": "page size", "startAt": "index of the first result"},
                     {"maxResults": 20, "fields": _JIRA_FIELDS},
                     ("jira", "issue", "search", "jql", "ticket"))
        comment_body = "comment text (wiki markup string)"
    return [
        _op("myself", "GET", f"{root}/myself",
            "Who am I in Jira: the current user's account, name, email and time zone",
            {}, {}, ("jira", "user", "whoami", "me")),
        search,
        _op("get_issue", "GET", f"{root}/issue/{{issueIdOrKey}}",
            "Get one Jira issue (ticket) by key: summary, status, assignee, priority",
            dict(key, fields="comma-separated fields to return (*all for everything)",
                 expand="e.g. renderedFields,transitions"),
            {"fields": _JIRA_FIELDS}, ("jira", "issue", "ticket")),
        _op("get_transitions", "GET", f"{root}/issue/{{issueIdOrKey}}/transitions",
            "List the workflow transitions (status changes) available on a Jira issue",
            dict(key), {}, ("jira", "workflow", "status", "transition")),
        _op("transition_issue", "POST", f"{root}/issue/{{issueIdOrKey}}/transitions",
            "Move a Jira issue to another status (transition it, e.g. to In Progress or Done)",
            dict(key, transition="object {\"id\": \"<transition id>\"} from get_transitions",
                 fields="fields to set during the transition",
                 update="update operations to apply during the transition"),
            {}, ("jira", "workflow", "status", "transition", "close", "resolve")),
        _op("add_comment", "POST", f"{root}/issue/{{issueIdOrKey}}/comment",
            "Add a comment to a Jira issue",
            dict(key, body=comment_body), {}, ("jira", "comment", "reply")),
        _op("create_issue", "POST", f"{root}/issue",
            "Create a new Jira issue (ticket, bug, task, story)",
            {"fields": "object: project {key}, summary, issuetype {name}, description, ...",
             "update": "optional update operations"},
            {}, ("jira", "issue", "new", "ticket", "bug")),
    ]


# -- Microsoft Graph v1.0 -------------------------------------------------------------------

_ODATA = {"$select": "comma-separated properties to return", "$top": "page size",
          "$filter": "OData filter expression", "$orderby": "OData sort, e.g. 'receivedDateTime desc'"}

_GRAPH_OPS = [
    _op("me", "GET", "/me",
        "Who am I in Microsoft 365: signed-in user's name, email, job title",
        {"$select": _ODATA["$select"]},
        {"$select": "displayName,mail,userPrincipalName,jobTitle,id"},
        ("m365", "microsoft", "user", "whoami", "profile")),
    _op("list_messages", "GET", "/me/messages",
        "List Outlook email messages in the mailbox (inbox mail, unread, from, subject)",
        dict(_ODATA, **{"$search": "KQL search string, e.g. '\"from:alice\"' (cannot combine with $orderby)"}),
        {"$select": "subject,from,receivedDateTime,isRead", "$top": 20},
        ("m365", "outlook", "email", "mail", "inbox")),
    _op("get_message", "GET", "/me/messages/{message_id}",
        "Read one Outlook email message: subject, sender, recipients, body",
        {"message_id": "message id from list_messages", "$select": _ODATA["$select"]},
        {"$select": "subject,from,toRecipients,ccRecipients,receivedDateTime,isRead,body"},
        ("m365", "outlook", "email", "mail", "read")),
    _op("send_mail", "POST", "/me/sendMail",
        "Send an Outlook email message as the signed-in user",
        {"message": "object: subject, body {contentType, content}, toRecipients "
                    "[{emailAddress {address}}], ccRecipients",
         "saveToSentItems": "true (default) | false"},
        {}, ("m365", "outlook", "email", "mail", "send"), tier="consent"),
    _op("list_events", "GET", "/me/calendarView",
        "List Outlook calendar events and meetings in a time window (today, this week, agenda)",
        dict(_ODATA, startDateTime="window start, ISO 8601 (required), e.g. 2026-10-01T00:00:00Z",
             endDateTime="window end, ISO 8601 (required)"),
        {"$select": "subject,start,end,location,organizer,isAllDay,isCancelled",
         "$top": 20, "$orderby": "start/dateTime"},
        ("m365", "outlook", "calendar", "events", "meetings", "schedule")),
    _op("list_drive_root", "GET", "/me/drive/root/children",
        "List files and folders at the top of the user's OneDrive",
        dict(_ODATA),
        {"$select": "id,name,size,lastModifiedDateTime,webUrl,folder,file", "$top": 20},
        ("m365", "onedrive", "files", "drive", "documents")),
    _op("search_drive", "GET", "/me/drive/root/search(q='{q}')",
        "Search the user's OneDrive files and documents by name or content",
        {"q": "search text (double any single quote)", "$select": _ODATA["$select"],
         "$top": _ODATA["$top"]},
        {"$select": "id,name,size,lastModifiedDateTime,webUrl,parentReference", "$top": 20},
        ("m365", "onedrive", "files", "drive", "documents", "search")),
]


PROFILES = {
    "github": {
        "name": "github", "title": "GitHub REST API v3",
        "auth_hint": "bearer: a personal access token (fine-grained preferred) or `gh auth token`",
        "pagination": "link-header",
        "noise_keys": ["node_id", "gravatar_id", "reactions", "performed_via_github_app",
                       "author_association", "site_admin", "user_view_type",
                       "active_lock_reason", "sub_issues_summary", "issue_dependencies_summary"],
        "ops": _GITHUB_OPS,
    },
    "jira-v3": {
        "name": "jira-v3", "title": "Jira Cloud REST API v3",
        "auth_hint": "basic: Atlassian account email as user, API token as password",
        "pagination": "jira-token",
        "noise_keys": ["expand", "avatarUrls", "iconUrl", "renderedFields", "names", "schema",
                       "editmeta", "operations", "versionedRepresentations"],
        "ops": _jira_ops(3),
    },
    "jira-v2": {
        "name": "jira-v2", "title": "Jira Data Center REST API v2",
        "auth_hint": "bearer: a personal access token (Data Center / Server 8.14+); basic also works",
        "pagination": "jira-startat",
        "noise_keys": ["expand", "avatarUrls", "iconUrl", "renderedFields", "names", "schema",
                       "editmeta", "operations", "versionedRepresentations"],
        "ops": _jira_ops(2),
    },
    "graph": {
        "name": "graph", "title": "Microsoft Graph v1.0 (Microsoft 365)",
        "auth_hint": "bearer: a delegated access token for graph.microsoft.com",
        "pagination": "odata",
        "noise_keys": ["@odata.context", "@odata.etag", "@odata.type", "changeKey", "iCalUId",
                       "transactionId", "parentFolderId", "conversationIndex",
                       "internetMessageHeaders"],
        "ops": _GRAPH_OPS,
    },
    "generic": {
        "name": "generic", "title": "Generic HTTP JSON API",
        "auth_hint": "any scheme; only raw 'METHOD /path' ops",
        "pagination": None,
        "noise_keys": [],
        "ops": [],
    },
}


# -- MCP downstream (0.2.0) --------------------------------------------------------------------
# An `mcp` connection has no hand-written profile: its ops are the downstream server's own tools,
# cached at `add-mcp` (connection["tools"]). The tier comes from the tool's MCP annotations when
# it has them, else from its name, else it is a write -- the gateway decides, never the caller.
MCP_PROFILE = "mcp"
_MCP_READ = ("get_", "list_", "search", "find_", "read_", "fetch_", "query", "describe_", "show_")
_MCP_CONSENT = ("delete_", "remove_", "send_", "merge_", "drop_", "destroy_", "purge_")


# OAuth connections (0.3.0): annotations are HINTS a server controls, so they may only raise a
# tier here. A name containing any of these WORDS is consent, whatever the hints say. The word is
# matched as a SUBSTRING of the lowercased name, for every name: deletefile, get_deleteall,
# list_sendmail and searchdeleteall are all consent. The only exception is the short allow-list
# _SAFE_READ_WORDS (settings, assets, presets, closest, ...), which may contain a risky word
# without making the name a write. A readOnlyHint counts only for a name that begins with a read
# prefix and has no such word. This is a deny-list and so
# incomplete by nature: the real fix is the 0.21 allow-list-by-token approach; until then an owner
# pins what matters with `policy.consent` globs on the connection.
_MCP_RISKY = frozenset((
    "delete", "remove", "send", "write", "create", "update", "merge", "post", "drop", "grant",
    "revoke", "run", "exec", "execute", "publish", "wipe", "upload", "cancel", "transfer", "share",
    "approve", "deploy", "reset", "set", "close", "archive", "purge", "clear", "apply", "destroy",
    "kill", "invite", "assign", "move", "rename", "restore", "enable", "disable", "install",
    "uninstall", "trigger", "submit", "commit", "push", "force",
    # third review (3): the verbs a deny-list must not miss
    "erase", "unlink", "edit", "modify", "add_comment", "addcomment", "reply", "replace", "empty"))


# Read-only words that CONTAIN a risky word (settings holds "set", running holds "run"). A risky
# word counts unless it lies wholly inside one of these spans; a risky word that straddles or sits
# beside them still counts, so `resetsettings`, `settingsdelete` and `presetdrop` are consent.
_SAFE_READ_WORDS = ("setting", "asset", "dataset", "preset", "closest", "postmortem", "running",
                    "runtime", "credit")


def fold_name(name):
    """A tool name as a reader sees it, for tiering: NFKC (fullwidth letters become ASCII) with every
    format character (zero-width space and joiners, soft hyphen, word joiner, BOM) removed, lowercased.
    Third review (3)."""
    text = unicodedata.normalize("NFKC", str(name or ""))
    return "".join(c for c in text if unicodedata.category(c) != "Cf").lower()


def _risky_name(name):
    low = fold_name(name)
    safe = [(m.start(), m.end()) for w in _SAFE_READ_WORDS
            for m in re.finditer(re.escape(w), low)]
    for r in _MCP_RISKY:
        for m in re.finditer("(?=%s)" % re.escape(r), low):      # every occurrence, overlapping too
            a, b = m.start(), m.start() + len(r)              # the lookahead itself has no width
            if not any(x <= a and b <= y for x, y in safe):
                return True
    return False


def mcp_tier(tool, strict=False):
    ann = tool.get("annotations") if isinstance(tool.get("annotations"), dict) else {}
    name = fold_name(tool.get("name"))
    if strict:
        if ann.get("destructiveHint") is True or _risky_name(tool.get("name")) \
                or name.startswith(_MCP_CONSENT):
            return "consent"
        return "read" if name.startswith(_MCP_READ) else "write"
    if ann.get("destructiveHint") is True:
        return "consent"
    if ann.get("readOnlyHint") is True:
        return "read"
    if name.startswith(_MCP_CONSENT):
        return "consent"
    if name.startswith(_MCP_READ):
        return "read"
    return "write"


def mcp_profile(conn):
    oauth_strict = (conn.get("auth") or {}).get("scheme") == "oauth"
    ops = []
    for t in conn.get("tools") or []:
        if not isinstance(t, dict) or not t.get("name"):
            continue
        props = ((t.get("inputSchema") or {}).get("properties") or {})
        ops.append(_op(t["name"], "MCP", t["name"],
                       (t.get("description") or t["name"]).strip().split("\n")[0][:200],
                       params={k: (v or {}).get("description", "") if isinstance(v, dict) else ""
                               for k, v in props.items()},
                       tags=["mcp"], tier=mcp_tier(t, strict=oauth_strict)))
    return {"name": MCP_PROFILE, "title": "MCP endpoint", "ops": ops, "noise_keys": ()}


def for_connection(conn):
    """The profile a connection's ops come from: its cached MCP tools, or a named profile."""
    if conn.get("kind") == "mcp":
        return mcp_profile(conn)
    return get(conn.get("profile"))


def get(name):
    """The profile named `name` (a copy, so callers cannot edit the tables)."""
    if name not in PROFILES:
        raise GatewayError("unknown_profile", f"no profile named {name!r}",
                           difflib.get_close_matches(str(name), list(PROFILES), n=3, cutoff=0.4)
                           or sorted(PROFILES))
    return copy.deepcopy(PROFILES[name])


def placeholders(path):
    return _PLACEHOLDER.findall(path)


def _template_regex(path):
    parts = _PLACEHOLDER.split(path)
    # split() alternates literal, name, literal, ...
    out = []
    for i, p in enumerate(parts):
        out.append("[^/]+" if i % 2 else re.escape(p))
    return re.compile("^" + "".join(out) + "$")


def _resolved(op, graphql):
    d = copy.deepcopy(op)
    d.setdefault("tags", [])
    d["graphql"] = bool(graphql)
    return d


def _raw(profile, op):
    parts = op.split(None, 1)
    if len(parts) != 2:
        raise GatewayError("bad_op", f"raw op must be 'METHOD /path', got {op!r}")
    method, path = parts[0].upper(), parts[1].strip()
    if method not in METHODS:
        raise GatewayError("bad_op", f"method {parts[0]!r} is not one of {', '.join(METHODS)}")
    if not path.startswith("/") or path.startswith("//") or "://" in path or any(c.isspace() for c in path):
        raise GatewayError("bad_op", f"raw path must be a relative path starting with '/', got {path!r}")
    query = {}
    if "?" in path:
        path, qs = path.split("?", 1)
        query = dict(parse_qsl(qs, keep_blank_values=True))
    if ".." in path.split("/"):
        raise GatewayError("bad_op", "raw path may not contain '..' segments")
    d = {"name": None, "method": method, "path": path, "summary": f"raw {method} {path}",
         "params": {p: "path parameter" for p in placeholders(path)},
         "query_defaults": query, "tags": ["raw"],
         "graphql": profile.get("name") == "github" and method == "POST" and path == "/graphql"}
    # A raw call that hits a curated endpoint keeps the curated tier, so "PUT .../merge"
    # spelled raw is still a consent operation.
    for c in profile.get("ops", []):
        if c["method"] == method and c.get("tier") and _template_regex(c["path"]).match(path):
            d["tier"] = c["tier"]
            break
    return d


def resolve_op(profile, op):
    """Resolve a curated op name or a raw 'METHOD /path' string against `profile`."""
    if not isinstance(op, str) or not op.strip():
        raise GatewayError("unknown_op", "op must be a curated op name or 'METHOD /path'",
                           [c["name"] for c in profile.get("ops", [])])
    op = op.strip()
    if " " in op or "\t" in op or op.startswith("/"):
        if op.startswith("/"):
            raise GatewayError("bad_op", f"raw op needs a method: 'GET {op}'")
        return _raw(profile, op)
    for c in profile.get("ops", []):
        if c["name"] == op:
            return _resolved(c, c["name"] == "graphql" and profile.get("name") == "github")
    names = [c["name"] for c in profile.get("ops", [])]
    hints = difflib.get_close_matches(op, names, n=3, cutoff=0.5)
    if not names:
        hints = [f"profile {profile.get('name')} has no curated ops; call it raw, e.g. 'GET /path'"]
    elif not hints:
        hints = names
    raise GatewayError("unknown_op", f"{op!r} is not an operation of profile {profile.get('name')}",
                       hints)
