# Project Status & Comparison

## How lex-align compares

Most existing tools catch problems *after* a PR is open. `lex-align` is
narrower in scope (Python only) but enforces policy *before* the bytes
hit disk, with a closed-enum verdict that AI agents can branch on.

| Feature                       | Dependabot | Snyk              | FOSSA   | lex-align   |
|-------------------------------|:----------:|:-----------------:|:-------:|:-----------:|
| CVE checking                  | ✅         | ✅                | ✅      | ✅          |
| License compliance            | ❌         | ✅                | ✅      | ✅          |
| Approved registry enforcement | ❌         | ❌                | ❌      | ✅          |
| Pre-commit interception       | ❌         | ⚠️ via plugin[^1] | ❌      | ✅          |
| AI agent integration          | ❌         | ❌                | ❌      | ✅          |
| Language support              | 20+        | 10+               | 20+     | Python only |
| Cost                          | Free       | Freemium          | Paid    | Free        |
| Self-hosted                   | ✅         | Partial           | Partial | ✅          |

[^1]: Snyk can be wired into pre-commit hooks via the
[`pre-commit`](https://pre-commit.com) framework (community-maintained hooks
exist) or through custom shell wrappers. It is not a first-party,
out-of-the-box feature.

The middle rows — approved-registry enforcement, pre-commit
interception, and AI-agent integration — are where `lex-align`
differs in kind, not just degree: an approved-registry gate, an
edit-time `PreToolUse` intercept, and an auto-written `CLAUDE.md` so
agents pre-flight every dep without being asked.

**Language support is a real limitation.** `lex-align` is Python-only.
Organisations running polyglot stacks (Node, Go, Java, Rust, …) will
need a separate tool — or multiple tools — for those ecosystems.
Dependabot and FOSSA cover 20+ package managers out of the box; Snyk
covers 10+. If your codebase is mixed-language, treat the language-support
row as the deciding factor before evaluating anything else.

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
| **5.** Pluggable approval proposers + hot-reload | :material-check-circle: shipped |
| **6.** Single-user workflow (`quickstart`, `audit`, `status`, auto-enqueue) | :material-check-circle: shipped |

Approvals now flow through a pluggable *proposer*: opens a PR (GitHub
backend), commits to a local repo (local-git), writes YAML directly
(local-file), or just logs (log-only / evaluation). The server
hot-reloads on merge or YAML write — no restarts.

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
