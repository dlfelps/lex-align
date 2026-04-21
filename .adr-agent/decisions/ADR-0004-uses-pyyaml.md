---
alternatives:
- name: Implicit transitive dep (no explicit pin)
  outcome: not-chosen
  reason: leaving version resolution to python-frontmatter's own constraints could
    allow pip/uv to silently resolve an older version with the yaml.load() security
    issue
  reversible: cheap
- name: ruamel.yaml as python-frontmatter backend
  outcome: not-chosen
  reason: requires configuring a custom frontmatter handler; PyYAML is the default
    and well-tested backend for python-frontmatter with no configuration needed
  reversible: cheap
confidence: medium
created: '2026-04-21'
id: ADR-0004
observed_via: seed
scope:
  paths:
  - pyproject.toml
  tags:
  - pyyaml
  - storage
status: accepted
title: Uses pyyaml
---

## Context

`python-frontmatter` requires a YAML backend to serialize and deserialize decision file metadata. PyYAML is its default backend. There is no direct `import yaml` in the adr-agent source — PyYAML is consumed exclusively through python-frontmatter's internals. However, older PyYAML versions (pre-6.0) had a security issue where `yaml.load()` without a Loader argument executed arbitrary Python. PyYAML 6.0 made safe loading the default.

## Decision

Explicitly declare `pyyaml>=6.0` as a direct dependency in `pyproject.toml` even though it is consumed only transitively. This pins the minimum version and makes the dependency graph auditable.

## Consequences

The explicit pin ensures PyYAML 6.0+ (safe load by default) is always resolved, removing the security concern from older versions. The dependency is visible in lock files and auditing tools rather than hidden as an implicit transitive. The entry in `pyproject.toml` looks redundant without this context, but the pin is intentional and load-bearing from a security standpoint.
