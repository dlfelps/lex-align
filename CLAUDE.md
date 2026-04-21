## adr-agent

Before starting any non-trivial task, run:
  uv run adr-agent plan "<task description>"
This surfaces relevant decisions, previously-rejected alternatives, and active
constraints. Do this before writing code, not after.

When modifying pyproject.toml dependencies:
- Run `uv run adr-agent propose` BEFORE making the change if it represents a new decision.
- If you added or removed a dependency without proposing first, run
  `uv run adr-agent promote <id>` to capture rationale while you still have context.

When you encounter an OBSERVED entry for a dependency you are actively using:
- Run `uv run adr-agent promote <id>`. You likely have enough context from the current
  task. If you genuinely don't, leave it observed.

Run `uv run adr-agent show <id>` before touching code governed by a decision. Do not
repeat evaluation work already recorded in alternatives.
