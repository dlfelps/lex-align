"""Registry-YAML schema validation, shared by the CLI compiler and the
dashboard's import endpoint.

This module is intentionally pure: no I/O, no CLI argparse, no FastAPI
dependencies. The two entry points that consume it (the
`tools/compile_registry.py` CLI and `lex_align_server.api.v1.registry`)
both depend on the same validator so a registry YAML accepted by the
dashboard is guaranteed to compile in CI, and vice versa.
"""

from __future__ import annotations

from typing import Any


VALID_STATUSES = {
    "preferred", "approved", "deprecated", "version-constrained", "banned",
}
VALID_UNKNOWN_POLICIES = {"block", "warn", "allow"}
PACKAGE_ALLOWED_FIELDS = {
    "status", "reason", "replacement", "min_version", "max_version",
}
GLOBAL_POLICY_ALLOWED_FIELDS = {
    "auto_approve_licenses",
    "hard_ban_licenses",
    "require_human_review_licenses",
    "unknown_license_policy",
    "cve_threshold",
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

    cve_threshold = gp.get("cve_threshold", 0.9)
    if not isinstance(cve_threshold, (int, float)) or isinstance(cve_threshold, bool):
        raise ValidationError("cve_threshold must be a number between 0 and 1")
    cve_threshold = float(cve_threshold)
    if not (0.0 <= cve_threshold <= 1.0):
        raise ValidationError("cve_threshold must be between 0 and 1")

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
            "cve_threshold": cve_threshold,
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
