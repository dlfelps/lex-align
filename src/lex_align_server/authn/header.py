"""Trust headers from an upstream auth gateway.

This is the recommended backend for organizations: oauth2-proxy,
Pomerium, Authelia, Cloudflare Access, ingress-nginx with auth_request,
Envoy ext_authz — any of them can authenticate against your existing
SSO and forward the result as headers. lex-align then consumes them.

**Critical**: only trust the headers when the request reaches the server
through a known proxy. ``trusted_proxies`` is a list of CIDR blocks; any
request from outside those blocks is rejected with HTTP 401 *even if*
the headers are present, because an attacker can otherwise spoof them
by hitting the server directly. The default of ``127.0.0.1/32`` assumes
the proxy is co-located.
"""

from __future__ import annotations

import ipaddress
from typing import Iterable

from fastapi import Request

from .base import AuthError, Authenticator, Identity


class HeaderAuthenticator(Authenticator):
    def __init__(
        self,
        *,
        user_header: str,
        email_header: str | None,
        groups_header: str | None,
        groups_separator: str,
        trusted_proxies: Iterable[str],
    ):
        self.user_header = user_header
        self.email_header = email_header
        self.groups_header = groups_header
        self.groups_separator = groups_separator
        self.trusted_networks = tuple(
            ipaddress.ip_network(cidr.strip(), strict=False)
            for cidr in trusted_proxies
            if cidr.strip()
        )
        # An operator who explicitly sets 0.0.0.0/0 (or ::/0) is opting into
        # "trust everything" — including ASGI clients whose host isn't an IP
        # at all (e.g. starlette TestClient sets "testclient"). Without this
        # special case, the wildcard wouldn't actually match unix-socket /
        # in-process clients.
        self.trust_all = any(
            net.prefixlen == 0 for net in self.trusted_networks
        )

    async def authenticate(self, request: Request) -> Identity:
        client_ip = self._client_ip(request)
        if not self._is_trusted(client_ip):
            # Fail closed: the headers cannot be trusted from this client,
            # so don't even look at them.
            raise AuthError(
                f"Request from {client_ip} not in trusted_proxies; "
                "header auth requires the request to come through a "
                "configured upstream proxy."
            )

        user = (request.headers.get(self.user_header) or "").strip()
        if not user:
            raise AuthError(
                f"Missing {self.user_header} header from upstream proxy."
            )

        email = None
        if self.email_header:
            email = (request.headers.get(self.email_header) or "").strip() or None

        groups: tuple[str, ...] = ()
        if self.groups_header:
            raw = request.headers.get(self.groups_header) or ""
            groups = tuple(
                g.strip() for g in raw.split(self.groups_separator) if g.strip()
            )

        return Identity(id=user, email=email, groups=groups, raw={"source": "header"})

    @staticmethod
    def _client_ip(request: Request) -> str:
        if request.client and request.client.host:
            return request.client.host
        return ""

    def _is_trusted(self, ip_str: str) -> bool:
        if self.trust_all:
            return True
        if not ip_str:
            return False
        try:
            ip = ipaddress.ip_address(ip_str)
        except ValueError:
            return False
        return any(ip in net for net in self.trusted_networks)
