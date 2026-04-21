---
alternatives:
- name: openai SDK
  outcome: not-chosen
  reason: adr-agent targets the Claude Code ecosystem where ANTHROPIC_API_KEY is already
    ambient; requiring an OpenAI key would add friction for every user
  reversible: cheap
- name: litellm
  outcome: not-chosen
  reason: provider-agnostic routing adds dependency weight for a tool that only ever
    targets one provider
  reversible: cheap
- name: No LLM (manual prose only)
  outcome: not-chosen
  reason: prose generation from brief rationale is a core value-add; removing it
    would require engineers to write full ADR prose themselves
  reversible: cheap
confidence: high
created: '2026-04-21'
id: ADR-0001
observed_via: seed
scope:
  paths:
  - src/adr_agent/llm.py
  tags:
  - anthropic
  - llm
status: accepted
title: Uses anthropic
---

## Context

adr-agent is purpose-built for Claude Code, Anthropic's AI coding assistant. All users of adr-agent already have `ANTHROPIC_API_KEY` available in their environment because Claude Code requires it, so there is zero additional setup cost. The tool needs to generate structured ADR prose (Context, Decision, Consequences sections) from brief engineer-provided rationale during the `propose` and `promote` workflows.

## Decision

Use the official Anthropic Python SDK (`anthropic>=0.40`) to call `claude-haiku-4-5-20251001` for lightweight ADR prose generation. The SDK is initialized lazily in `llm.py` using the ambient `ANTHROPIC_API_KEY`.

## Consequences

Users get zero-config LLM access — no additional key setup is required beyond what Claude Code already needs. `claude-haiku` is low-cost and fast for the ~1024-token prose generation task. The tool has a hard dependency on Anthropic's API; prose generation will fail in offline environments or if the key is absent. Switching to another provider would require replacing `llm.py` but the interface is isolated enough to make that change cheap.
