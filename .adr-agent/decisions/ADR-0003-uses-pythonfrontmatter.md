---
alternatives:
- name: Manual parsing (split on ---)
  outcome: not-chosen
  reason: brittle against frontmatter variations such as blank lines, trailing dashes,
    and nested YAML values; the library handles these edge cases robustly
  reversible: costly
- name: ruamel.yaml direct
  outcome: not-chosen
  reason: would require manual body/header splitting on top of YAML parsing; python-frontmatter
    provides exactly the abstraction needed without extra glue code
  reversible: cheap
confidence: high
created: '2026-04-21'
id: ADR-0003
observed_via: seed
scope:
  paths:
  - src/adr_agent/store.py
  tags:
  - python_frontmatter
  - storage
  - markdown
status: accepted
title: Uses python_frontmatter
---

## Context

Decision files are Markdown documents with YAML frontmatter — a convention used by many static-site generators and ADR tools, and one that keeps decisions readable in GitHub PR diffs without special tooling. The store needs to roundtrip these files: read YAML metadata and Markdown body separately, modify metadata fields (status, alternatives, confidence, etc.), and write back without corrupting the structure.

## Decision

Use `python-frontmatter>=1.1` to parse and serialize decision files. `frontmatter.load()` is used in `store._read()` to split metadata from body, `frontmatter.Post` constructs the combined object, and `frontmatter.dumps()` serializes it back to disk.

## Consequences

The library correctly handles the `---`-delimited frontmatter and body structure including edge cases. Decision files remain plain Markdown that any text editor, GitHub, or static-site renderer can display. The `frontmatter.Post` API cleanly separates the metadata dict from the body string, which maps directly onto the `Decision` dataclass. The library brings PyYAML as a transitive dependency (which is explicitly pinned in `pyproject.toml` as ADR-0004).
