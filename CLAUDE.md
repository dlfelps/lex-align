## lex-align

Before starting any non-trivial task that may touch dependencies, run:
  uv run lex-align plan "<task description>"
This surfaces relevant decisions, previously-evaluated alternatives, active
constraints, and any registry guidance that applies to the task.

When modifying pyproject.toml dependencies:
- The PreToolUse hook will evaluate every added or bumped package against the
  enterprise registry and license policy. It may hard-block the edit.
- For `approved` (neutral) packages, run `uv run lex-align propose` to document
  the architectural need before the edit lands.
- For `preferred` packages, the hook auto-writes an accepted ADR; no action needed.
- For `deprecated`, `banned`, or license-blocked packages, use the registry-named
  replacement or choose a different package.

When you encounter an OBSERVED entry for a dependency you are actively using:
- Run `uv run lex-align promote <id>`. You likely have enough context from the
  current task. If you genuinely don't, leave it observed.

Run `uv run lex-align show <id>` before touching code governed by a decision.
Do not repeat evaluation work already recorded in alternatives.

IMPORTANT for AI agents: NEVER run `lex-align propose` or `lex-align promote`
without supplying all required flags and `--yes`. These commands have interactive
prompts that will hang a non-interactive session. Always use flags to provide
every field. Run `lex-align propose --help` or `lex-align promote --help` to see
the required flags.
