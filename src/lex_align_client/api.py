"""HTTP client for the lex-align server.

Synchronous; the CLI is one-shot, so the simplicity of `httpx.Client` is worth
more than the throughput of an async client. Failure semantics:

  * connection error and `fail_open=true`  → return a synthetic ALLOWED
    verdict with `transport_error=True` so the caller can warn.
  * connection error and `fail_open=false` → raise ServerUnreachable.
  * server 4xx/5xx → raise ServerError(detail).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional

import httpx

from .config import ClientConfig


PROJECT_HEADER = "X-LexAlign-Project"


class ServerError(RuntimeError):
    def __init__(self, status_code: int, detail: str):
        super().__init__(f"{status_code}: {detail}")
        self.status_code = status_code
        self.detail = detail


class ServerUnreachable(RuntimeError):
    pass


@dataclass
class Verdict:
    verdict: str
    reason: str
    package: str
    version: Optional[str]
    resolved_version: Optional[str]
    registry_status: Optional[str]
    replacement: Optional[str]
    version_constraint: Optional[str]
    license: Optional[str]
    cve_ids: list[str]
    max_cvss: Optional[float]
    is_requestable: bool
    needs_rationale: bool
    transport_error: bool = False

    @classmethod
    def from_response(cls, data: dict) -> "Verdict":
        return cls(
            verdict=data.get("verdict", "ALLOWED"),
            reason=data.get("reason", ""),
            package=data.get("package", ""),
            version=data.get("version"),
            resolved_version=data.get("resolved_version"),
            registry_status=data.get("registry_status"),
            replacement=data.get("replacement"),
            version_constraint=data.get("version_constraint"),
            license=data.get("license"),
            cve_ids=list(data.get("cve_ids") or []),
            max_cvss=data.get("max_cvss"),
            is_requestable=bool(data.get("is_requestable", False)),
            needs_rationale=bool(data.get("needs_rationale", False)),
        )

    @property
    def denied(self) -> bool:
        return self.verdict == "DENIED"

    def to_dict(self) -> dict:
        return {
            "verdict": self.verdict,
            "reason": self.reason,
            "package": self.package,
            "version": self.version,
            "resolved_version": self.resolved_version,
            "registry_status": self.registry_status,
            "replacement": self.replacement,
            "version_constraint": self.version_constraint,
            "license": self.license,
            "cve_ids": self.cve_ids,
            "max_cvss": self.max_cvss,
            "is_requestable": self.is_requestable,
            "needs_rationale": self.needs_rationale,
            "transport_error": self.transport_error,
        }


class LexAlignClient:
    def __init__(self, config: ClientConfig, http_client: httpx.Client | None = None):
        self.config = config
        self._http = http_client or httpx.Client(timeout=5.0)
        self._owns_client = http_client is None

    def __enter__(self) -> "LexAlignClient":
        return self

    def __exit__(self, *_exc) -> None:
        if self._owns_client:
            self._http.close()

    # ── headers ────────────────────────────────────────────────────────────

    def _headers(self) -> dict[str, str]:
        h = {PROJECT_HEADER: self.config.project}
        if self.config.mode == "org":
            token = os.environ.get(self.config.api_key_env_var)
            if token:
                h["Authorization"] = f"Bearer {token}"
        return h

    # ── public API ─────────────────────────────────────────────────────────

    def check(self, package: str, version: Optional[str] = None) -> Verdict:
        params: dict[str, str] = {"package": package}
        if version:
            params["version"] = version
        try:
            resp = self._http.get(
                f"{self.config.server_url}/api/v1/evaluate",
                params=params,
                headers=self._headers(),
            )
        except httpx.HTTPError as exc:
            if self.config.fail_open:
                return Verdict(
                    verdict="ALLOWED",
                    reason=(
                        f"lex-align server unreachable ({exc.__class__.__name__}); "
                        "fail_open=true — allowing without enforcement."
                    ),
                    package=package, version=version, resolved_version=None,
                    registry_status=None, replacement=None, version_constraint=None,
                    license=None, cve_ids=[], max_cvss=None,
                    is_requestable=False, needs_rationale=False,
                    transport_error=True,
                )
            raise ServerUnreachable(str(exc)) from exc
        if resp.status_code != 200:
            raise ServerError(resp.status_code, _detail(resp))
        return Verdict.from_response(resp.json())

    def request_approval(self, package: str, rationale: str) -> dict:
        try:
            resp = self._http.post(
                f"{self.config.server_url}/api/v1/approval-requests",
                json={"package": package, "rationale": rationale},
                headers=self._headers(),
            )
        except httpx.HTTPError as exc:
            raise ServerUnreachable(str(exc)) from exc
        if resp.status_code not in (200, 201, 202):
            raise ServerError(resp.status_code, _detail(resp))
        return resp.json()

    def health(self) -> dict:
        resp = self._http.get(
            f"{self.config.server_url}/api/v1/health",
            headers={PROJECT_HEADER: self.config.project},
        )
        resp.raise_for_status()
        return resp.json()


def _detail(resp: httpx.Response) -> str:
    try:
        body = resp.json()
        if isinstance(body, dict) and "detail" in body:
            return str(body["detail"])
    except ValueError:
        pass
    return resp.text[:200]
