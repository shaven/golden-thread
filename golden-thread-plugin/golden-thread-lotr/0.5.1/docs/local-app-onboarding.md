# Give a local app the gt vault (lotr app-connect)

For an app on this machine that speaks MCP and nothing else. One command, run in **your own terminal** (not inside Claude Code):

    lotr app-connect myapp --vault ~/path/to/vault

It registers the vault connection (`vault@<zone>`, from the `vault-mcp` template), enrols `myapp` as a hub client and writes its secret to `<home>/clients/myapp.secret` (mode 600, folder 700). It prints the endpoint, the two header names and that file's **path**. It never prints the secret.

Point the app at:

- URL `http://127.0.0.1:<port>/mcp`
- `Authorization: Bearer <the contents of the secret file>`
- `X-GT-Client: myapp`

Check it: `lotr app-connect myapp --verify` calls the hub with the file's secret and prints the tool count or the failure (401 means the file and the enrolled client disagree: `--rotate`).

## Why a header and not `?secret=` in the URL
A URL is logged, cached and shown. A secret never rests in one (Core rule 9), and the MCP spec does not allow tokens in the URL.

## Least privilege
The app may reach the vault connection only, at write tier. `--allow CONNECTION` (repeatable) and `--max-tier read|write|consent` widen that on purpose.

## What is still yours to do
- Turn on the front door: `listen.mcp_http: true` in `gateway.json`, then restart `lotrd`. `app-connect` tells you when it is off.
- Turn the vault tools on: `gt_settings.py set vault_mcp on`.
- Consent-tier operations still ask you. An OAuth connection needs `--l1` for unattended use, and `presence.gate_writes` if you want hub writes only while you are at the machine (SECURITY.md 9.3a).

## Undo
`lotr app-disconnect myapp` revokes the client, deletes the file, and removes the vault connection only when no other live client can still reach it. `lotr app-connect myapp --rotate` swaps the secret.
