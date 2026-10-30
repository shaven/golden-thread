# LOTR template catalog: changelog

The catalog is released on its own schedule, separately from gt. Each version lists what a
`lotr template update` will change. Needs gt-lotr 0.5.0 or newer (catalog 2 onward: 0.5.1).

## Signing the catalog (owner steps, once)

Catalog signing is built but off: it turns on when the public key is committed. Nothing here
contains key material.

1. Generate a dedicated key: `ssh-keygen -t ed25519 -C "gt signing" -f ~/gt-signing`.
2. Keep the private half in your secrets store; delete the loose file after each use.
3. Copy `~/gt-signing.pub` to `golden-thread-lotr/0.5.1/templates/catalog/catalog-signing.pub`.
4. Run `python3 dev/lotr_catalog.py sign --key ~/gt-signing`.
5. Run `python3 dev/lotr_catalog.py check`, then commit the `.pub`, `lotr-templates/catalog.json.sig`
   and the bundled copy of the `.sig`. Run `sign` again after every `build`.

Full list, with release-tag signing: SECURITY.md section 9.2.

## Catalog 6 -- 2026-10-27

Needs gt-lotr 0.5.1 or newer.

- vault-mcp: the documentation and source links in the template and its fact sheet name a placeholder
  account (`example-account`) instead of a real one, so the catalog carries no account-specific link.
  The template's behaviour is unchanged.

## Catalog 4 -- 2026-10-27

Needs gt-lotr 0.5.1 or newer.

- openai-compatible: a hosted OpenAI-compatible LLM (OpenRouter by default; any host that serves
  /v1/chat/completions) over https with your own key, held by reference. Chat completions are a
  write, the model list is a read and doubles as a key check. The connection is remote and its
  model cannot act, so the farm may send to it with a `transport: lotr` row. A provider's 402 comes
  back as upstream_payment_required, 429 as upstream_rate_limited. Prices, model ids and the host's
  privacy settings are the owner's and are not stated as facts.

## Catalog 3 -- 2026-10-25

Needs gt-lotr 0.5.1 or newer.

- twilio: the REST API over basic auth; reading a message is a read, sending a message, placing a
  call and every DELETE ask first. The Account SID and token are not in the file.
- tailscale: API v2 with an access token; devices, the policy and key expiry are reads, every
  change asks first. Base URL and paths are unverified in the fact sheet.
- google-gmail, google-calendar, google-docs, google-sheets: Google's hosted Workspace MCP servers
  with your own OAuth client, asking only for read-only scopes. Tasks has no hosted server on
  Google's page.
- google-tasks: the Google Tasks REST API with an OAuth access token held by reference (Google has no
  hosted Tasks server on its MCP page). Task lists and tasks are reads, adding or completing a task is a
  write, every DELETE and clearing completed tasks ask first. The token is short-lived and is not refreshed.
  The bearer header, token lifetime and the missing hosted server are unverified in the fact sheet.
- unifi: UniFi's hosted Site Manager API (api.ui.com) with an API key in the X-API-Key header;
  hosts, sites and devices are reads, every change asks first. It does not reach a controller on a
  private address: that is unifi-controller. Base URL, header and paths are unverified in the fact
  sheet.
- unifi-controller: the UniFi Network integration API on your own controller at a private address,
  over https with a certificate from your own private CA (`lotr trust-ca add`, then
  `add-http --trust-ca`); plain http and a factory self-signed certificate stay refused. The key goes
  in the X-API-KEY header by reference; sites, devices and clients are reads, every change asks first.
  The base URL is a placeholder you always replace. Path, header and reads are unverified in the fact
  sheet.

## Catalog 2 -- 2026-10-24

Needs gt-lotr 0.5.1 or newer (every entry now says so: a template parameter can name a resolver).

- vault-mcp: gt's own vault MCP server over stdio, for an app that speaks MCP and nothing else.
  Needs no key. Vault writes go through gt's write queue; `vault_queue_write` is write-tier. The
  `scripts_dir` parameter fills itself with the newest installed gt.
- llama-server: a llama.cpp server on this machine, no key, with a `GET /health` check and a `local`
  label. A server on another machine is reached through an SSH tunnel to loopback.

## Catalog 1 -- 2026-10-14

First catalog. Sixteen templates, all MCP servers, each with a fact sheet that gives the source
page and the verification state of every fact. None is independently verified yet; read `open` in
each sheet.

- linear, sentry, notion, gitlab: hosted MCP servers with browser sign-in (mcp-oauth).
- atlassian: the Atlassian Rovo MCP server (Confluence and Jira), browser sign-in (mcp-oauth).
  Transitions and comments are writes; four lookups are reads.
- pagerduty, google-drive, slack, m365: hosted servers that need an OAuth app you create first
  (mcp-oauth-preregistered). google-drive asks for offline access so it keeps a refresh token;
  slack needs PKCE turned on in your Slack app; m365 is Microsoft Work IQ and needs a tenant admin.
- github: GitHub's hosted MCP server with a token (mcp-http-key).
- playwright: Microsoft's @playwright/mcp, run with npx (stdio-npx).
- filesystem, git, fetch: the MCP project's reference servers (stdio; filesystem and git ask for a
  folder or repository, fetch asks for confirmation on every fetch).
- sqlite (mcp-sqlite) and postgres (Google's MCP Toolbox): local database servers. Arbitrary SQL
  asks first.
