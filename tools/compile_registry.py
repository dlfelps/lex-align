#!/usr/bin/env python3
"""Compile a human-authored enterprise registry YAML file into the optimized
JSON consumed by the lex-align hook.

Usage:
    python tools/compile_registry.py <input.yml> <output.json>

Intended to run in CI. Exits 0 on success, non-zero with a clear message on
schema errors so broken registry edits fail the pipeline before merge.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import yaml


VALID_STATUSES = {
    "preferred", "approved", "deprecated", "version-constrained", "banned"
}
VALID_UNKNOWN_POLICIES = {"block", "warn", "allow"}
PACKAGE_ALLOWED_FIELDS = {
    "status", "reason", "replacement", "min_version", "max_version"
}
GLOBAL_POLICY_ALLOWED_FIELDS = {
    "auto_approve_licenses",
    "hard_ban_licenses",
    "require_human_review_licenses",
    "unknown_license_policy",
}


class ValidationError(Exception):
    pass


def _require_list_of_str(value: Any, field: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(x, str) for x in value):
        raise ValidationError(f"{field} must be a list of strings")
    return value


def validate_registry(doc: dict) -> dict:
    if not isinstance(doc, dict):
        raise ValidationError("top-level YAML must be a mapping")

    version = doc.get("version")
    if version is None:
        raise ValidationError("missing required field: version")
    if not isinstance(version, (str, int, float)):
        raise ValidationError("version must be a string or number")
    version_str = str(version)

    gp = doc.get("global_policies") or {}
    if not isinstance(gp, dict):
        raise ValidationError("global_policies must be a mapping")
    unknown_fields = set(gp) - GLOBAL_POLICY_ALLOWED_FIELDS
    if unknown_fields:
        raise ValidationError(f"global_policies has unknown fields: {sorted(unknown_fields)}")

    auto_approve = _require_list_of_str(gp.get("auto_approve_licenses"), "auto_approve_licenses")
    hard_ban = _require_list_of_str(gp.get("hard_ban_licenses"), "hard_ban_licenses")
    review = _require_list_of_str(
        gp.get("require_human_review_licenses"), "require_human_review_licenses"
    )
    unknown_policy = gp.get("unknown_license_policy", "block")
    if not isinstance(unknown_policy, str) or unknown_policy not in VALID_UNKNOWN_POLICIES:
        raise ValidationError(
            f"unknown_license_policy must be one of {sorted(VALID_UNKNOWN_POLICIES)}; "
            f"got {unknown_policy!r}"
        )

    packages = doc.get("packages") or {}
    if not isinstance(packages, dict):
        raise ValidationError("packages must be a mapping keyed by package name")

    compiled_packages: dict[str, dict] = {}
    for name, rule in packages.items():
        _validate_package(name, rule)
        compiled_packages[name] = _compile_package(rule)

    return {
        "version": version_str,
        "global_policies": {
            "auto_approve_licenses": auto_approve,
            "hard_ban_licenses": hard_ban,
            "require_human_review_licenses": review,
            "unknown_license_policy": unknown_policy,
        },
        "packages": compiled_packages,
    }


def _validate_package(name: str, rule: Any) -> None:
    if not isinstance(name, str) or not name.strip():
        raise ValidationError(f"package key must be a non-empty string; got {name!r}")
    if not isinstance(rule, dict):
        raise ValidationError(f"package `{name}` must be a mapping")

    unknown_fields = set(rule) - PACKAGE_ALLOWED_FIELDS
    if unknown_fields:
        raise ValidationError(
            f"package `{name}` has unknown fields: {sorted(unknown_fields)}"
        )

    status = rule.get("status")
    if status not in VALID_STATUSES:
        raise ValidationError(
            f"package `{name}` has invalid status {status!r}; "
            f"must be one of {sorted(VALID_STATUSES)}"
        )

    if status == "deprecated" and not rule.get("replacement"):
        raise ValidationError(f"package `{name}` is deprecated but has no `replacement`")

    if status == "version-constrained" and not (
        rule.get("min_version") or rule.get("max_version")
    ):
        raise ValidationError(
            f"package `{name}` is version-constrained but has neither "
            "min_version nor max_version"
        )

    for vfield in ("min_version", "max_version"):
        v = rule.get(vfield)
        if v is not None and not isinstance(v, str):
            raise ValidationError(f"package `{name}`.{vfield} must be a string")

    for sfield in ("reason", "replacement"):
        v = rule.get(sfield)
        if v is not None and not isinstance(v, str):
            raise ValidationError(f"package `{name}`.{sfield} must be a string")


def _compile_package(rule: dict) -> dict:
    out: dict = {"status": rule["status"]}
    for field in ("reason", "replacement", "min_version", "max_version"):
        if rule.get(field):
            out[field] = rule[field]
    return out


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
