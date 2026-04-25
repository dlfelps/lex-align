#!/usr/bin/env python3
"""Compile a human-authored enterprise registry YAML file into the optimized
JSON consumed by the lex-align hook.

Usage:
    python tools/compile_registry.py <input.yml> <output.json>

Intended to run in CI. Exits 0 on success, non-zero with a clear message on
schema errors so broken registry edits fail the pipeline before merge.

The schema validator lives in `lex_align_server.registry_schema` so the
dashboard's YAML-import endpoint and this CLI agree on what is valid.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import yaml

from lex_align_server.registry_schema import (
    GLOBAL_POLICY_ALLOWED_FIELDS,
    PACKAGE_ALLOWED_FIELDS,
    VALID_STATUSES,
    VALID_UNKNOWN_POLICIES,
    ValidationError,
    validate_registry,
)


# Re-exported for backwards compatibility with anything that imported
# these constants from this script directly.
__all__ = [
    "GLOBAL_POLICY_ALLOWED_FIELDS",
    "PACKAGE_ALLOWED_FIELDS",
    "VALID_STATUSES",
    "VALID_UNKNOWN_POLICIES",
    "ValidationError",
    "validate_registry",
    "main",
]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("input", help="Path to YAML registry source")
    parser.add_argument("output", help="Path to write compiled JSON registry")
    args = parser.parse_args()

    input_path = Path(args.input)
    output_path = Path(args.output)

    if not input_path.exists():
        print(f"Input file not found: {input_path}", file=sys.stderr)
        return 2

    try:
        doc = yaml.safe_load(input_path.read_text())
    except yaml.YAMLError as exc:
        print(f"YAML parse error: {exc}", file=sys.stderr)
        return 2

    try:
        compiled = validate_registry(doc)
    except ValidationError as exc:
        print(f"Registry validation failed: {exc}", file=sys.stderr)
        return 2

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(compiled, indent=2, sort_keys=True) + "\n")
    print(
        f"Compiled {len(compiled['packages'])} package rules from {input_path} → {output_path}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
