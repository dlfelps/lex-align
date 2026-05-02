---
title: Dashboards
---

# Dashboards

`lex-align-server` ships four dashboards. They render server-side from
templates baked into the Python package and call the read-only JSON API
for their data, so they work against any deployment without needing a
separate frontend build.

| Path | Audience | Goal |
| --- | --- | --- |
| `/dashboard/registry` | Platform / dev experience | Triage approval requests, edit the registry, export YAML. |
| `/dashboard/legal` | Legal / OSS compliance | License-compliance posture across all evaluations. |
| `/dashboard/security` | AppSec / supply-chain | Vulnerability posture across all evaluations. |
| `/dashboard/agents` | Operators | Which agent identities are doing what. |

Every page accepts a `?project=…` filter scoped to the
`X-LexAlign-Project` value clients send.

---

## Registry workshop — `/dashboard/registry`

The interactive page. It loads the live registry, surfaces what's
waiting to be triaged, and routes any change through the configured
[proposer](git-backed-approvals.md) so the YAML in your registry repo
stays the source of truth.

Key pieces:

- **Pending approval requests** — packages someone explicitly filed a
  `request-approval` for.
- **Implicit candidates** — packages the audit log has seen but no one
  filed an approval for, classified by why they're surfacing
  (`provisional-no-rationale`, `repeatedly-denied`, `pre-screened`).
- **Global policies editor** — CVSS threshold, auto-approve / hard-ban
  license lists, unknown-license policy.
- **Add to registry…** — opens the proposer flow (PR for `github`,
  commit for `local-git`, direct write for `local-file`).
- **Export YAML** — last-resort manual save. Useful when running with
  `log_only`.

<!-- TODO: image — screenshot of the registry workshop with both pending
panels populated and a package being edited -->
*[image placeholder: registry workshop showing pending approval and
implicit-candidates panels above the package table, with the edit
modal open]*

---

## Legal compliance — `/dashboard/legal`

The legal dashboard answers **"what licenses are in our supply chain,
and how is the policy classifying them?"** It's framed for whoever owns
OSS-licensing risk; the policy itself lives in the registry workshop.

It pulls from `/api/v1/reports/legal`, which returns:

| Field | What it shows |
| --- | --- |
| `total_denials` | Count of `DENIED` audit rows whose denial category is `license`. |
| `recent` | The 100 most recent license-driven denials. |
| `license_breakdown` | Every audit row in scope grouped by normalised license, split into `allowed` / `provisional` / `denied`. |
| `unknown_license` | The same shape, restricted to rows that normalised to `UNKNOWN`. |
| `top_projects` | Projects ranked by number of license-driven denials. |

Page sections:

- **KPI strip** — total evaluations with a license, license-driven
  denials, unknown-license rows, and the count of distinct licenses
  ever seen.
- **License breakdown table** — every license sorted by frequency, with
  a stacked bar showing the verdict mix at a glance. Copyleft licenses
  (GPL, AGPL, LGPL) get a red tag; permissive ones (MIT, BSD, Apache,
  ISC) get a green tag; `UNKNOWN` gets an amber tag.
- **Unknown-license policy panel** — how the configured
  `unknown_license_policy` is performing in production. If you're seeing
  a lot of provisional rows that never get followed up on, that's a
  signal the policy is too lax.
- **Top projects** — which repos are pulling the most non-compliant
  packages.
- **Recent denials** — the existing 100-row tail with project, package,
  license, reason, and the agent identity that triggered it.

<!-- TODO: image — screenshot of the legal dashboard with KPIs,
breakdown table, and stacked bars visible -->
*[image placeholder: legal dashboard showing license-breakdown table
with stacked verdict-mix bars, plus the unknown-license panel and top
projects table]*

### Reading the breakdown

* **A copyleft row that's mostly `denied`** is the policy doing its job
  — investigate only if the count is climbing.
* **A copyleft row with `allowed` rows** means it slipped past the
  policy. Most often that's because the license isn't on
  `hard_ban_licenses` and the package matched a registry `preferred` /
  `approved` rule. Either ban the license globally or downgrade the
  package's registry status.
* **High `UNKNOWN` count with `provisional` verdicts** means PyPI
  metadata isn't giving us a normalisable license string for those
  packages. Either chase upstream metadata or tighten
  `unknown_license_policy` from `pending_approval` to `block`.

---

## Security posture — `/dashboard/security`

The security dashboard answers **"are we letting known vulnerabilities
into the codebase, and are any already-approved packages turning hot?"**
It pulls from `/api/v1/reports/security`, which extends the base denial
report with:

| Field | What it shows |
| --- | --- |
| `total_denials` | Count of `DENIED` audit rows whose denial category is `cve`. |
| `recent` | The 100 most recent CVE-driven denials. |
| `severity_distribution` | CVE-denied rows bucketed by max CVSS (`critical` ≥ 9.0, `high` ≥ 7.0, `medium` ≥ 4.0, `low` &gt; 0, `unknown`). |
| `top_packages` | Packages with the most CVE-driven denials, carrying their highest-seen CVSS and up to five CVE ids. |
| `top_cves` | The CVE identifiers showing up most often. |
| `hot_registry_packages` | Registry-allowed packages whose audit rows in the last 30 days include a CVE-driven denial or provisional. |

Page sections:

- **KPI strip** — total CVE denials and per-severity counts, sized so a
  spike in critical is impossible to miss.
- **"Already-approved packages with new CVE activity"** — the
  highest-signal cell. Anything in the registry as `preferred`,
  `approved`, or `version-constrained` whose audit rows in the last 30
  days went through a CVE denial or provisional verdict shows up here.
  This is exactly the case the [`pre-commit` hook in CLAUDE.md](../CLAUDE.md)
  is documented to catch — except the dashboard finds it before someone
  tries to commit.
- **Top packages by CVE denials** — sorted by max CVSS first, then by
  denial count, with their CVE ids inline.
- **Severity distribution panel** — a simple counts-by-bucket table.
- **Top CVE identifiers panel** — which CVE ids drive the most blocks,
  and which packages they hit.
- **Recent CVE denials** — the chronological tail.

<!-- TODO: image — screenshot of the security dashboard with the "hot
packages" panel populated and a critical-severity KPI lit up -->
*[image placeholder: security dashboard showing severity KPIs across
the top, the red "already-approved packages" panel below, and the
top-packages and top-CVEs tables underneath]*

### Acting on a "hot" registry package

When something appears in **already-approved packages with new CVE
activity**:

1. Check the CVE ids and the affected version range in OSV.
2. If you're on a safe version, pin it: open the registry workshop,
   set the package to `version-constrained` with a `min_version`, and
   propose the change.
3. If no safe version exists yet, set it to `banned` (with a `reason`
   pointing at the CVE id) until upstream ships a fix.
4. Either path goes through the same proposer flow as everything else
   in the registry workshop, so the rationale is captured in the audit
   trail.

---

## Agent activity — `/dashboard/agents`

A simple read-only roll-up of `/api/v1/reports/agents`. Groups every
audit row by `(agent_model, agent_version)` so operators can answer
"which Claude version made that request?". The
`X-LexAlign-Agent-Model` and `X-LexAlign-Agent-Version` headers the
client sends propagate into every audit row; rows without them collapse
into a single `unknown` bucket.

<!-- TODO: image — screenshot of the agents dashboard -->
*[image placeholder: agents dashboard showing the per-agent aggregate
table above the recent-evaluations table]*

---

## Direct API access

Every dashboard is a thin renderer over the JSON API; you can hit the
endpoints directly with `curl` for scripting:

```bash
curl -s http://localhost:8000/api/v1/reports/legal | jq .
curl -s http://localhost:8000/api/v1/reports/security?project=demo | jq .
curl -s http://localhost:8000/api/v1/reports/agents | jq .
```

The schema additions on the legal and security endpoints are additive;
older clients that only consumed `total_denials` and `recent` keep
working.
