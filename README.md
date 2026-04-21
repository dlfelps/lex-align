# adr-agent

Per-repository architectural memory for AI agents. Records architectural decisions so that AI agents (and humans) can see what has been decided, what was considered and rejected, and why — across sessions.

Designed for use with [Claude Code](https://claude.ai/code). Hooks into Claude's session lifecycle to inject context automatically at session start, prompt at dependency changes, and remind at session end.

---

## Requirements

- Python 3.11+
- [Claude Code](https://claude.ai/code) (for hook integration)
- An `ANTHROPIC_API_KEY` environment variable (used when writing new decisions)

---

## Installation

The package is not yet on PyPI. Install from the prebuilt wheel in the `dist/` directory.

**With uv (recommended):**

```bash
uv pip install path/to/adr_agent-0.1.0-py3-none-any.whl
```

**With pip:**

```bash
pip install path/to/adr_agent-0.1.0-py3-none-any.whl
```

Contributors cloning the repository later just need `uv sync` — adr-agent is already in the lockfile.

Verify the install:

```bash
adr-agent --help
```

---

## Initializing a project

Run `init` once at the repository root:

```bash
adr-agent init
```

On first use you will see a one-time privacy notice and a confirmation prompt. Pass `--yes` to skip it in automated contexts.

`init` does three things:

1. Creates `.adr-agent/` with `decisions/` and `sessions/` subdirectories.
2. Seeds observed entries from any existing runtime dependencies in `pyproject.toml` so the store has content from day one.
3. Writes hooks into `.claude/settings.json` so Claude Code automatically injects architectural context at session start, prompts on dependency changes, and reminds on session end.

It also adds `.adr-agent/sessions/` to `.gitignore` so raw session logs stay local while decisions travel with the repo.

**Commit the generated files:**

```bash
git add .adr-agent/ .claude/settings.json .gitignore
git commit -m "Initialize adr-agent"
```

### First-Run Audit (existing codebases)

If your project already has dependencies, `init` will seed them as *observed* entries — they exist in the store but have no captured rationale. To backfill rationale immediately, give this prompt to Claude:

> "I have just initialized adr-agent in this repository. Review the list of OBSERVED dependencies provided in the architecture brief. For each central dependency (e.g., the web framework, database client, or CLI library): 1. Research why it was likely chosen over common alternatives by examining the code, imports, and documentation. 2. Analyze the pros and cons of this choice in the context of this specific project. 3. Execute `adr-agent promote <id>` to convert these into ACCEPTED entries. Include the rationale and at least one alternative considered in the promotion flow. If you cannot find evidence for why a dependency was chosen, leave it as 'Observed' to maintain store integrity."

You can display this prompt again at any time:

```bash
adr-agent first-run-audit
```

---

## Using adr-agent

### How Claude uses it automatically

Once initialized, Claude Code runs these actions automatically via hooks:

- **Session start** — injects an architecture brief listing accepted and observed decisions, then reconciles the store against `pyproject.toml`.
- **Before editing `pyproject.toml`** — surfaces relevant existing decisions if dependencies are changing; suggests `adr-agent propose` if the change isn't covered.
- **After editing `pyproject.toml`** — runs reconciliation; creates observed entries for any new uncovered dependencies.
- **Before editing Python files** — surfaces observed entries for dependencies imported in the file being edited (once per entry per session).
- **Session end** — reminds about any dependency changes that were made without a matching `propose` call.

### Manual commands

#### Get context before starting a task

```bash
adr-agent plan "add a background job queue for sending emails"
```

Returns relevant accepted decisions, observed entries that may be affected, alternatives previously evaluated, and active constraints. Run this before starting any non-trivial task.

#### Record a new decision

```bash
adr-agent propose
```

Walks through an interactive prompt: title, rationale, confidence, scope tags, paths, constraints, alternatives, and supersessions. Uses the Anthropic API to generate the Context / Decision / Consequences prose body. Writes an accepted decision file to `.adr-agent/decisions/`.

Options for automated or triggered contexts:

```bash
adr-agent propose --dependency redis --relevant-adrs ADR-0019,ADR-0047 --path src/cache/
```

#### Promote an observed entry to accepted

```bash
adr-agent promote ADR-0003
```

Walks through the same fields as `propose`, pre-filled with what the observed entry already has. Use this when you have context explaining why a dependency was originally chosen.

#### View a decision

```bash
adr-agent show ADR-0047
```

Prints the full decision including frontmatter, alternatives, and prose body.

#### View decision history for a path or tag

```bash
adr-agent history src/auth/
adr-agent history session
```

Returns all decisions that have governed a file path or tag, in chronological order, including superseded ones.

#### Find decisions that depend on a constraint

```bash
adr-agent check-constraint blue-green-deploys
```

Lists every decision and alternative that references the named constraint tag. Useful when a constraint changes and you want to know what decisions may need revisiting.

#### Rebuild the search index

```bash
adr-agent rebuild-index
```

Reconstructs `.adr-agent/index.json` from all decision files. Run this after manual file edits or if `plan` returns unexpected results.

#### Check hook health

```bash
adr-agent doctor
adr-agent doctor --repair
```

Reports whether the hooks in `.claude/settings.json` are correctly configured. `--repair` rewrites them if not.

#### Remove hooks

```bash
adr-agent uninstall
```

Removes adr-agent hooks from `.claude/settings.json`. Decision files are preserved.

---

## Decision statuses

| Status | Meaning |
|--------|---------|
| `accepted` | Currently in force, with rationale. Shown in the brief. |
| `observed` | Currently in force, but rationale was not captured. Shown in the brief, segregated. Eligible for promotion. |
| `superseded` | Was in force, replaced by another decision. Excluded from the brief; retrievable by `show` or `history`. |
| `rejected` | Proposed but never accepted. Preserved as a record. |

Observed entries carry an `observed_via` field: `seed` (created at `init`), `reconciliation` (auto-created when a dependency appeared without a `propose` call), or `manual`.

---

## Generating reports

```bash
adr-agent report
adr-agent report --since "2 weeks ago"
adr-agent report --since "2026-01-01"
```

Produces a summary of activity and store integrity:

```
Retrieval (72 voluntary queries)
  show          41    most-viewed: ADR-0047, ADR-0024, ADR-0032
  plan          18    most-queried topics: job queue, session storage, async
  history        9
  check-constraint 4

Writes (13 records created)
  propose        6
  promote        7    observed → accepted

Integrity
  Reconciliation events: 3
    via session start:  1
    via post-edit hook: 2
  Promotion opportunities: 12 (edit-time prompts fired)
  Promotions resulting:   5  (42% of opportunities)

Observed entries: 14 total
  via seed:          11 (from initial adoption)
  via reconciliation:  3 (created when propose was skipped)
  via manual:          0
```

The **Integrity** section is the key diagnostic:

- **Reconciliation count** — how often the system had to backstop a missed `propose` call. Each event is a moment where rationale was available but not captured.
- **Promotion ratio** — how effectively the pull mechanisms are converting observed entries into accepted ones.
- **Observed entries by source** — a healthy store shows seed counts shrinking (promotions happening) while reconciliation counts stay low (write discipline holding).

Session logs are stored locally in `.adr-agent/sessions/` and are gitignored. They contain invocation metadata only, not decision content.

---

## What gets committed

| Path | Committed |
|------|-----------|
| `.adr-agent/decisions/` | Yes |
| `.adr-agent/index.json` | Yes |
| `.adr-agent/sessions/` | No (gitignored) |
| `.claude/settings.json` | Yes |

Decision files contain rationale, alternatives, and constraints. Treat them with the same sensitivity as source code — they are part of the repository's permanent git history.

adr-agent does not transmit any data externally. No telemetry, no central collection. Run `adr-agent privacy` to view the full privacy notice.
