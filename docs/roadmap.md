# Project Status & Comparison

## How lex-align compares

Most existing tools catch problems *after* a PR is open. `lex-align` is
narrower in scope (Python only) but enforces policy *before* the bytes
hit disk, with a closed-enum verdict that AI agents can branch on.

| Feature                       | Dependabot | Snyk     | FOSSA   | lex-align   |
|-------------------------------|:----------:|:--------:|:-------:|:-----------:|
| CVE checking                  | ✅         | ✅       | ✅      | ✅          |
| License compliance            | ❌         | ✅       | ✅      | ✅          |
| Approved registry enforcement | ❌         | ❌       | ❌      | ✅          |
| Pre-commit interception       | ❌         | ❌       | ❌      | ✅          |
| AI agent integration          | ❌         | ❌       | ❌      | ✅          |
| Language support              | 20+        | 10+      | 20+     | Python only |
| Cost                          | Free       | Freemium | Paid    | Free        |
| Self-hosted                   | ✅         | Partial  | Partial | ✅          |

The bottom three rows are where `lex-align` differs in kind, not just
degree: an approved-registry gate, an edit-time `PreToolUse` intercept,
and an auto-written `CLAUDE.md` so agents pre-flight every dep without
being asked.

---

## Project status

Honest about scope. `lex-align` does one thing: dependency policy
enforcement for Python projects, single-user by default.

| Phase | Status |
|---|---|
| **1.** Server core (registry, license, CVE, audit, evaluate) | :material-check-circle: shipped |
| **2.** Thin client (init, check, request-approval, pre-commit, Claude hooks) | :material-check-circle: shipped |
| **3.** Approval workflow UI + reporting endpoints + agent identity | :material-check-circle: shipped |
| **4.** Pluggable org-mode auth | :material-check-circle: shipped |
| **4.** Pluggable approval proposers + hot-reload | :material-check-circle: shipped |

Approvals now flow through a pluggable *proposer*: opens a PR (GitHub
backend), commits to a local repo (local-git), writes YAML directly
(local-file), or just logs (log-only / evaluation). The server
hot-reloads on merge or YAML write — no restarts. See
[Approvals & Reloads](git-backed-approvals.md) for the full flow.

The dashboard's pending queue gained an "implicit candidates" section
that surfaces packages your audit log has seen but no one filed an
approval request for, with a `reason` badge per row so you can tell
at a glance why something is showing up.

**Scope limits to be explicit about:**

- Python and `pyproject.toml` only. Other package ecosystems are not
  on the roadmap.
- The server only talks to PyPI (license metadata) and OSV (CVE feed).
  The audit log lives on the server's host. Nothing is sent to a third
  party.
