# lex-align

Enterprise legal and architectural alignment for AI coding agents. lex-align is a compliance layer that turns soft-prompted package rules into strictly-enforced programmatic constraints.

Merging *Lex* (law) with *Align* (safe, steerable AI), lex-align prevents AI-induced architectural drift and legal liabilities by enforcing a centrally-managed package registry and license policy. Approved packages flow through the golden path; banned, deprecated, or license-incompatible packages are hard-blocked before the agent can install them.

Designed for use with [Claude Code](https://claude.ai/code). Hooks into the agent's session lifecycle so enforcement happens automatically at session start and on every `pyproject.toml` edit.

---

## What it enforces

Every package an AI agent tries to add to `pyproject.toml` is evaluated against the enterprise registry and license policy. The possible outcomes:

| Registry status | Behavior |
|---|---|
| **preferred** | Allowed. An accepted ADR is auto-written to record the choice. |
| **approved** (neutral) | Allowed. The agent is instructed to run `lex-align propose` to document why. |
| **deprecated** | Hard-blocked. The agent is told to use the registry-specified replacement. |
| **version-constrained** | Allowed only for versions satisfying the constraint; otherwise hard-blocked. |
| **banned** | Hard-blocked with the registry-provided reason. |
| *unrecognized* | License is fetched from PyPI and checked against the license policy. |

License outcomes for unrecognized packages:

| License class | Behavior |
|---|---|
| Permissive (MIT, Apache-2.0, BSD-3-Clause, …) | Auto-approved. |
| Weak copyleft (LGPL) | Hard-blocked. |
| Strong/network copyleft (GPL, AGPL) | Hard-blocked. |
| Unknown | Hard-blocked by default (configurable). |

Hard blocks exit the `PreToolUse` hook non-zero — the agent's `Edit` / `Write` call fails before any bytes hit `pyproject.toml`.

---

## Requirements

- Python 3.11+
- [Claude Code](https://claude.ai/code) (for hook integration)
- Optional: an enterprise registry JSON file (see *The Enterprise Registry* below)

---

## Installation

```bash
uv add --dev lex-align
# or
pip install lex-align
```

Verify:

```bash
lex-align --help
```

---

## Initializing a project

```bash
lex-align init
lex-align init --registry path/to/registry.json
```

`init` does three things:

1. Creates `.lex-align/` with `decisions/` and `sessions/` subdirectories.
2. Records the registry file location (if provided) in `.lex-align/config.json`.
3. Writes hooks into `.claude/settings.json` so Claude Code automatically injects the architecture brief at session start and enforces the registry on `pyproject.toml` edits.

It also adds `.lex-align/sessions/` and `.lex-align/license-cache.json` to `.gitignore` so raw session logs and transient license lookups stay local while decisions travel with the repo.

**Commit the generated files:**

```bash
git add .lex-align/ .claude/settings.json .gitignore
git commit -m "Initialize lex-align"
```

The store starts empty — `init` does not seed observed entries from existing dependencies. To bootstrap a project that already has dependencies, run `lex-align compliance` (see *Cold-start compliance check* below).

---

## Cold-start compliance check

Existing projects accumulate dependencies long before lex-align is added. The compliance check evaluates every entry already in `pyproject.toml` against the registry and license policy, then seeds the store accordingly:

```bash
lex-align compliance            # analyze + write
lex-align compliance --dry-run  # analyze only
```

| Outcome | Behavior |
|---|---|
| **preferred** / **version-constrained** / unknown w/ permissive license | Accepted ADR is auto-written. |
| **approved** (neutral) | Observed entry is created. The agent must promote it with rationale. |
| **deprecated** / **banned** / version-violated / blocked license | Blocker. Nothing is written for any package while a blocker exists. |
| Already covered by an accepted ADR | Skipped. The check is idempotent across re-runs. |

Exit codes: `0` = passing, `1` = observed entries await promotion, `2` = blockers present.

When observed entries remain, the command prints a ready-to-paste prompt that instructs an AI agent to analyze the codebase and run `lex-align promote <id>` for each one with derived rationale. Once every dependency has an accepted ADR and no blockers remain, compliance passes and you can rely on hook enforcement for new dependencies going forward.

---

## The enterprise registry

Platform teams author the registry in YAML and compile it to JSON. A minimal example:

```yaml
# enterprise-lexalign-registry.yml
version: "1.2"
global_policies:
  auto_approve_licenses: [MIT, Apache-2.0, BSD-3-Clause]
  hard_ban_licenses: [AGPL-3.0, GPL-3.0, LGPL-3.0]
  unknown_license_policy: block  # block | warn | allow

packages:
  httpx:
    status: preferred
    reason: Standard async HTTP client.
  requests:
    status: deprecated
    replacement: httpx
    reason: Migrating all services to async patterns.
  pyqt5:
    status: banned
    reason: GPL-licensed; no commercial license held.
  cryptography:
    status: version-constrained
    min_version: "42.0.0"
    reason: CVE-2023-50782 affects earlier versions.
```

Compile to the optimized JSON the agent consumes:

```bash
python tools/compile_registry.py enterprise-lexalign-registry.yml registry.json
```

Registry resolution order for the CLI and hooks:

1. `--registry <path>` flag (CLI commands).
2. `LEXALIGN_REGISTRY_FILE` environment variable.
3. Path recorded in `.lex-align/config.json` at `init` time.
4. `.lex-align/registry.json` as a convention default.

If no registry is configured, lex-align falls back to license-only enforcement.

### Inspecting the registry

```bash
lex-align registry show
lex-align registry check httpx
lex-align registry check cryptography --version 41.0.0
```

---

## Agent interface

### Automatic (via hooks)

- **Session start** — injects an architecture brief listing accepted and observed decisions and the active registry. Reconciles the store against `pyproject.toml`.
- **Before editing `pyproject.toml`** — evaluates every added or bumped dependency against the registry and license policy. Allows, prompts for an ADR, or hard-blocks with a reason.
- **After editing `pyproject.toml`** — reconciles; creates observed entries for any new dependency that wasn't covered by an auto-ADR.
- **Before editing Python files** — surfaces observed entries for dependencies imported in the file being edited (once per entry per session).

### Manual commands

```bash
lex-align compliance
lex-align compliance --dry-run
lex-align plan "add a background job queue for sending emails"
lex-align propose --dependency redis --title "Use Redis for session storage" \
  --context "..." --decision "..." --consequences "..." --yes
lex-align promote ADR-0003 --context "..." --yes
lex-align show ADR-0047
lex-align history src/auth/
lex-align check-constraint blue-green-deploys
lex-align rebuild-index
lex-align report
lex-align doctor [--repair]
lex-align uninstall
```

`propose` refuses to create an ADR for a package whose registry status is `banned` or `deprecated` — the registry is authoritative.

---

## Decision statuses

| Status | Meaning |
|--------|---------|
| `accepted` | Currently in force, with rationale. Shown in the brief. |
| `observed` | Currently in force, but rationale was not captured. Shown in the brief, segregated. Eligible for promotion. |
| `superseded` | Was in force, replaced by another decision. Excluded from the brief; retrievable via `show` or `history`. |
| `rejected` | Proposed but never accepted, or a blocked attempt preserved for audit. |

Every decision carries a `provenance` field recording how it was created: `reconciliation`, `manual`, `registry_preferred`, `registry_approved`, or `license_auto_approve`. Observed entries additionally retain the legacy `observed_via` tag where relevant.

Auto-written ADRs (from enforcement) also record:

- `license` — the normalized license token observed at decision time.
- `registry_version` — which registry snapshot sanctioned the choice.
- `version_constraint` — the registry-imposed version pin, if any.

---

## Reports

```bash
lex-align report
lex-align report --since "2 weeks ago"
```

The report includes an **Enforcement** section breaking down blocks (banned / deprecated / license), preferred auto-approvals, and license-cache hit rate — the primary diagnostic for whether the enforcement layer is doing its job.

Session logs live in `.lex-align/sessions/` and are gitignored. They contain invocation metadata only, not decision content.

---

## What gets committed

| Path | Committed |
|------|-----------|
| `.lex-align/decisions/` | Yes |
| `.lex-align/index.json` | Yes |
| `.lex-align/config.json` | Yes |
| `.lex-align/sessions/` | No (gitignored) |
| `.lex-align/license-cache.json` | No (gitignored) |
| `.claude/settings.json` | Yes |

Decision files contain rationale, alternatives, and constraints. Treat them with the same sensitivity as source code — they are part of the repository's permanent git history.

lex-align does not transmit any data externally other than the PyPI license lookup for unknown packages. No telemetry, no central collection. Run `lex-align privacy` to view the full privacy notice.
