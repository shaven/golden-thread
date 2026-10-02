"""Generate tests/fixtures/recall-bench: a synthetic vault (no private content) + questions.json.

Run once; the output is committed. Pages describe a fictional platform ("Larkspur") so nothing
here comes from a real vault.
"""
import json
import sys
from pathlib import Path

OUT = Path(sys.argv[1])

PAGES = {
    "Knowledge/Backup Rotation.md": ("Backup Rotation", "how nightly snapshots are kept and pruned", """
Larkspur keeps **seven daily** snapshots, **four weekly** and **twelve monthly**. The pruner runs at
02:40 and never deletes the newest weekly. Restores are tested on the first Sunday of each month by
restoring the orders database into a scratch host and comparing row counts."""),
    "Knowledge/DNS Zones.md": ("DNS Zones", "internal and public zones, who edits them", """
The internal zone is `lark.internal`, served by two resolvers. Public records live in the registrar;
TTL is 300 seconds for anything that may fail over. A record change needs a ticket, and the zone
file is linted before reload so a missing trailing dot cannot take a name down."""),
    "Knowledge/Build Cache.md": ("Build Cache", "why builds are fast and how to clear the cache", """
Compiled artifacts are cached by content hash in an object store bucket named `lark-build-cache`.
A poisoned entry is cleared with `cachectl evict <hash>`; clearing everything takes about forty
minutes of cold builds. Cache keys include the compiler version, so a toolchain bump is a miss."""),
    "Knowledge/Deploy Windows.md": ("Deploy Windows", "when production deploys are allowed", """
Production deploys happen Tuesday to Thursday between 10:00 and 15:00 local time. Fridays are
frozen. An emergency fix outside the window needs two approvers and a rollback plan written first."""),
    "Knowledge/Log Retention.md": ("Log Retention", "how long logs are kept and where", """
Application logs are kept 30 days hot and 400 days in cold storage. Audit logs are kept seven years
and are write-once. Debug logging is never enabled in production for longer than one hour."""),
    "Knowledge/TLS Certificates.md": ("TLS Certificates", "certificate renewal and the expiry alarm", """
Certificates renew automatically 21 days before expiry. The expiry alarm fires at 14 days, which
means renewal has failed twice. Wildcards are not used; each service has its own certificate."""),
    "Knowledge/Queue Workers.md": ("Queue Workers", "scaling the job queue consumers", """
Queue consumers scale between 2 and 24 replicas on queue depth. A job that fails five times goes to
the dead-letter queue, which is reviewed every morning. Consumers must be idempotent."""),
    "Knowledge/Feature Flags.md": ("Feature Flags", "how flags are created and retired", """
Every flag has an owner and an expiry date. A flag older than 90 days shows up in the weekly cleanup
report. Flags are evaluated server side; the client never sees the rule, only the result."""),
    "Projects/alpha/research.md": ("alpha research", "dated findings for the alpha search rebuild", """
2026-03-02: the old search index took 9 seconds to answer a cold query; the new one takes 180 ms.
2026-03-09: stemming hurt product codes, so codes are indexed verbatim beside the stemmed text."""),
    "Projects/alpha/decisions.md": ("alpha decisions", "ADRs for alpha", """
ADR-1: use a single shard until the index passes 40 GB. Rejected: sharding by tenant, because most
tenants are tiny. ADR-2: rebuild nightly rather than incrementally; incremental updates drifted."""),
    "Projects/alpha/design.md": ("alpha design", "how the alpha search service is built", """
Queries go through a gateway that applies the tenant filter before the index sees them. Results are
re-ranked by recency for support tickets only. The index rebuild writes to a new alias and swaps."""),
    "Projects/beta/research.md": ("beta research", "dated findings for the beta billing move", """
2026-04-11: invoices rounded half-up in one region and half-even in another; standardised on
half-even. 2026-04-18: the tax service times out above 400 line items, so invoices are split."""),
    "Projects/beta/decisions.md": ("beta decisions", "ADRs for beta billing", """
ADR-1: keep money as integer minor units, never floats. ADR-2: the billing run is idempotent per
customer per period, keyed on customer id and period start."""),
    "Projects/gamma/design.md": ("gamma design", "the gamma mobile sync design", """
The phone keeps a local journal and syncs deltas every 15 minutes or on reconnect. Conflicts are
resolved last-writer-wins per field, except for the notes field, which is merged line by line."""),
    "Projects/gamma/README.md": ("gamma", "the gamma mobile app project", """
Gamma is the field-technician app. Owner: the mobile team. Status: beta with 40 technicians.
Open question: whether offline maps fit in the 200 MB download budget."""),
    "global-memory/working-hours.md": ("working hours", "when the owner is available", """
The owner works 07:00 to 16:00 Central time and does not want production changes during market
hours. Weekend work is fine for maintenance windows that were agreed in advance."""),
    "global-memory/editor.md": ("editor preferences", "how code should be written here", """
Four-space indentation, no tabs, lines under 100 characters. Comments explain why, not what.
Tests come before the change they prove."""),
    "global-memory/naming.md": ("naming conventions", "how things are named across projects", """
Hosts are named after birds; services after their function, lowercase with hyphens. Branches are
`feat/<topic>`; release tags are `vX.Y.Z`."""),
}

QUESTIONS = [
    ("how many weekly backup snapshots do we keep", "Knowledge/Backup Rotation.md"),
    ("when are restores tested", "Knowledge/Backup Rotation.md"),
    ("what time does the snapshot pruner run", "Knowledge/Backup Rotation.md"),
    ("what is the internal dns zone called", "Knowledge/DNS Zones.md"),
    ("what TTL do failover records use", "Knowledge/DNS Zones.md"),
    ("how do I evict a poisoned build cache entry", "Knowledge/Build Cache.md"),
    ("why did a compiler upgrade make every build slow", "Knowledge/Build Cache.md"),
    ("can we deploy to production on a friday", "Knowledge/Deploy Windows.md"),
    ("what does an emergency deploy outside the window need", "Knowledge/Deploy Windows.md"),
    ("how long are audit logs kept", "Knowledge/Log Retention.md"),
    ("how long can debug logging stay on in production", "Knowledge/Log Retention.md"),
    ("when does the certificate expiry alarm fire", "Knowledge/TLS Certificates.md"),
    ("do we use wildcard certificates", "Knowledge/TLS Certificates.md"),
    ("what happens to a job that keeps failing", "Knowledge/Queue Workers.md"),
    ("how many queue consumer replicas at most", "Knowledge/Queue Workers.md"),
    ("when does a feature flag show up in the cleanup report", "Knowledge/Feature Flags.md"),
    ("does the client see feature flag rules", "Knowledge/Feature Flags.md"),
    ("how fast is the new alpha search index on a cold query", "Projects/alpha/research.md"),
    ("why are product codes indexed verbatim", "Projects/alpha/research.md"),
    ("why did alpha reject sharding by tenant", "Projects/alpha/decisions.md"),
    ("does the alpha index rebuild incrementally", "Projects/alpha/decisions.md"),
    ("where is the tenant filter applied in alpha search", "Projects/alpha/design.md"),
    ("which alpha results are re-ranked by recency", "Projects/alpha/design.md"),
    ("which rounding mode does beta billing use", "Projects/beta/research.md"),
    ("why are large invoices split in two", "Projects/beta/research.md"),
    ("how is money stored in beta billing", "Projects/beta/decisions.md"),
    ("what makes the billing run idempotent", "Projects/beta/decisions.md"),
    ("how often does the gamma phone sync", "Projects/gamma/design.md"),
    ("how are gamma sync conflicts resolved for notes", "Projects/gamma/design.md"),
    ("how many technicians are using gamma", "Projects/gamma/README.md"),
    ("do offline maps fit the gamma download budget", "Projects/gamma/README.md"),
    ("what hours does the owner work", "global-memory/working-hours.md"),
    ("may we make production changes during market hours", "global-memory/working-hours.md"),
    ("how many spaces of indentation", "global-memory/editor.md"),
    ("what are hosts named after", "global-memory/naming.md"),
    ("what is the branch naming scheme", "global-memory/naming.md"),
]

LEVEL = lambda p: ("knowledge" if p.startswith("Knowledge/") else
                   "global-memory" if p.startswith("global-memory/") else "project")

vault = OUT / "vault"
for rel, (title, summary, body) in PAGES.items():
    f = vault / rel
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text("# %s\n\n%s\n" % (title, body.strip()), encoding="utf-8")
idx = ["# Index", "", "Synthetic fixture vault for gt_bench recall (no private content).", ""]
for rel, (title, summary, _b) in sorted(PAGES.items()):
    if rel.startswith("Knowledge/"):
        idx.append("- [[%s]] — %s" % (Path(rel).stem, summary))
(vault / "index.md").write_text("\n".join(idx) + "\n", encoding="utf-8")
qs = [{"id": "q%02d" % i, "question": q, "expected": e, "level": LEVEL(e)}
      for i, (q, e) in enumerate(QUESTIONS, 1)]
(OUT / "questions.json").write_text(json.dumps({"version": 1, "questions": qs}, indent=2) + "\n",
                                    encoding="utf-8")
print("%d pages, %d questions" % (len(PAGES), len(qs)))
