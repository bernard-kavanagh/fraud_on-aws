"""Interceptor-injected request context + tool-arg extraction (domain read tools).

Mirror of the fact layer's own context contract (aws/handlers/common/context.py)
so these domain read Lambdas plug into the SAME Gateway / interceptor / Cedar
pipeline. Enforcement order is: request interceptor -> Cedar -> tool. By the time
a handler runs, Cedar has verified context.input.tenant_id == the caller's
tenant_id claim, and the interceptor has injected the verified identity.

The handler trusts the INTERCEPTOR-injected tenant_id for all DB work (never the
raw tool argument) and asserts the two agree as defense in depth.

Injected-context contract (what the interceptor puts on the event):
    event["_fact_ctx"] = {
        "tenant_id":         "<verified JWT tenant claim>",
        "agent_id":          "<caller identity>",
        "session_id":        "<session>",
        "tenant_secret_arn": "arn:aws:secretsmanager:...:secret:...",
    }
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

_CTX_KEYS = ("_fact_ctx", "context", "bedrockAgentCoreContext")
_ARG_KEYS = ("input", "arguments", "body")

# Fallback secret ARN for THIS repo's demo TiDB (the transactional store these
# read tools query). Used only if the interceptor did not inject a per-tenant one.
_DEFAULT_SECRET_ARN = os.environ.get("DOMAIN_READS_SECRET_ARN")


@dataclass(frozen=True)
class ToolContext:
    tenant_id: str
    agent_id: str
    session_id: str
    tenant_secret_arn: str


def _find_context(event: dict) -> dict:
    for k in _CTX_KEYS:
        if isinstance(event.get(k), dict):
            return event[k]
    return {}


def _find_args(event: dict) -> dict:
    for k in _ARG_KEYS:
        if isinstance(event.get(k), dict):
            return dict(event[k])
    return {k: v for k, v in event.items() if k not in _CTX_KEYS}


def extract(event: dict) -> tuple[ToolContext, dict[str, Any]]:
    """Return (verified ToolContext, tool_args). Raises if identity is absent."""
    raw = _find_context(event)
    tenant_id = raw.get("tenant_id")
    if not tenant_id:
        raise PermissionError(
            "no verified tenant_id in request context — the interceptor must "
            "inject the identity before the handler runs"
        )
    secret_arn = raw.get("tenant_secret_arn") or _DEFAULT_SECRET_ARN
    if not secret_arn:
        raise ValueError(
            "no tenant-scoped secret ARN in context and DOMAIN_READS_SECRET_ARN unset"
        )
    ctx = ToolContext(
        tenant_id=str(tenant_id),
        agent_id=str(raw.get("agent_id", "unknown")),
        session_id=str(raw.get("session_id", "unknown")),
        tenant_secret_arn=str(secret_arn),
    )
    args = _find_args(event)

    # Defense in depth: a tool-arg tenant_id must match the verified claim.
    arg_tenant = args.get("tenant_id")
    if arg_tenant is not None and str(arg_tenant) != ctx.tenant_id:
        raise PermissionError(
            f"tenant_id argument ({arg_tenant}) does not match verified claim "
            f"({ctx.tenant_id})"
        )
    return ctx, args
