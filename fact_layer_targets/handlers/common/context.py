"""Tenant-scope context for the domain read tools (AgentCore MCP target contract).

An AgentCore MCP Lambda target receives ONLY the tool-arguments map — there is no
`_fact_ctx` side channel, and the request interceptor does NOT inject tenant
identity / a tenant_secret_arn / agent_id / session_id (see the Fact Layer's
`ARCHITECTURE.md` in the `aws` repo, which is authoritative for the generic
contract). These domain read tools follow the same proven pattern:

  * `tenant_id` is read from the TOOL ARGUMENTS — the requested DATA SCOPE (tenant
    selector), NOT authenticated identity. Missing/empty fails closed. Any legacy
    `_fact_ctx` on the event is ignored and can NEVER override the tool argument.
    Authorizing that the caller may select this scope is the Gateway governance
    layer's job (reference same-tenant profile: Cedar tenant-equality). The SQL
    tenant predicate in each handler is DATA SCOPING, not caller authorization.
  * the domain DB credential ARN comes from deployment configuration
    (`DOMAIN_READS_SECRET_ARN`) — a single secret for THIS repo's transactional
    store (orders/bets), resolved by ARN at runtime. Fails closed if unset.

These remain intentionally DOMAIN-SPECIFIC targets owned by this repo; they are
not, and must not become, generic Fact Layer tools.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

# Legacy keys that must NOT be treated as authenticated context anymore; if a
# caller or a stale interceptor puts one on the event it is ignored — it can never
# override the actual tool argument.
_LEGACY_CTX_KEYS = ("_fact_ctx", "context", "bedrockAgentCoreContext")
# Some target wirings nest the arguments; accept either shape.
_ARG_WRAPPERS = ("input", "arguments", "body")


@dataclass(frozen=True)
class ToolContext:
    tenant_id: str            # requested data scope (tenant selector), not identity
    tenant_secret_arn: str    # domain-store credential, from DOMAIN_READS_SECRET_ARN


def _tool_args(event: dict) -> dict[str, Any]:
    """Return the tool-argument map, stripping any legacy context keys."""
    if not isinstance(event, dict):
        return {}
    for k in _ARG_WRAPPERS:
        if isinstance(event.get(k), dict):
            return dict(event[k])
    return {k: v for k, v in event.items() if k not in _LEGACY_CTX_KEYS}


def extract(event: dict) -> tuple[ToolContext, dict[str, Any]]:
    """Return (ToolContext, tool_args) from an MCP target event. Fails closed.

    `tenant_id` is the requested data scope taken from the tool argument; a
    missing/empty value raises PermissionError.
    """
    args = _tool_args(event)

    raw_tenant = args.get("tenant_id")
    if raw_tenant is None or not str(raw_tenant).strip():
        raise PermissionError(
            "tenant_id argument is required and must be non-empty "
            "(the Gateway-authorized requested data scope)"
        )
    tenant_id = str(raw_tenant).strip()

    secret_arn = os.environ.get("DOMAIN_READS_SECRET_ARN")
    if not secret_arn:
        raise ValueError("DOMAIN_READS_SECRET_ARN is not configured")

    return ToolContext(tenant_id=tenant_id, tenant_secret_arn=secret_arn), args
