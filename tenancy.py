"""
Tenancy + agent identity resolution for the governed-fact-layer port.

Every fact written to (or read from) the AgentCore fact layer is scoped to a
`tenant_id` — the requested DATA SCOPE. In the reference governance profile the
Gateway authorizes the call by comparing that tenant_id (the tool argument)
LITERALLY against the caller's JWT `tenant_id` claim before it reaches TiDB. This
module is the single place this repo resolves *which* tenant a given invocation
is acting as — from an explicit argument, then the environment.

Design rule (task 9): **never hardcode a single tenant_id anywhere.** If a
caller cannot supply one and `DEMO_TENANT_ID` is unset, we refuse to run rather
than silently defaulting to one tenant and leaking another tenant's data into
an un-scoped query.

The two demo tenants (task 1 / task 11) are `demo-bank-alpha` and
`demo-bank-beta`. `demo-bank-alpha` carries the primary seeded fraud/betting
data; `demo-bank-beta` is a second tenant used to demonstrate a live
cross-tenant Cedar deny. These names are advisory constants only — the *active*
tenant always comes from configuration, not from these literals.
"""

from __future__ import annotations

import os
import uuid

# Advisory demo tenant names. NOT a default — see resolve_tenant_id().
DEMO_TENANT_ALPHA = "demo-bank-alpha"
DEMO_TENANT_BETA = "demo-bank-beta"
DEMO_TENANTS = (DEMO_TENANT_ALPHA, DEMO_TENANT_BETA)

_TENANT_ENV = "DEMO_TENANT_ID"
_AGENT_ID_ENV = "FACT_LAYER_AGENT_ID"


def resolve_tenant_id(tenant_id: str | None = None) -> str:
    """Resolve the active tenant_id: explicit arg > $DEMO_TENANT_ID > error.

    Raises ValueError when neither is available. This is deliberate: an
    un-scoped domain query or fact write is a tenant-isolation hole, so we fail
    closed rather than guess.
    """
    tid = tenant_id or os.getenv(_TENANT_ENV)
    if not tid:
        raise ValueError(
            "tenant_id not provided and $DEMO_TENANT_ID is unset. "
            "Refusing to run un-scoped — set DEMO_TENANT_ID (e.g. "
            f"'{DEMO_TENANT_ALPHA}') or pass tenant_id explicitly. "
            "No tenant is hardcoded by design (see tenancy.py)."
        )
    return str(tid)


def resolve_agent_id(agent_id: str | None = None) -> str:
    """Resolve the acting agent identity for fact provenance.

    NOTE: an MCP Lambda target receives only the tool arguments — there is no
    interceptor-injected identity and no _fact_ctx side channel (see the fact
    layer's ARCHITECTURE.md). agent_id is therefore provenance INTENT that this
    caller records; propagating a verified caller identity to the target is a
    separate future design, not a current guarantee.
    """
    return agent_id or os.getenv(_AGENT_ID_ENV) or "ag_fraud_detection.agent"


def new_session_id() -> str:
    """A fresh session id (uuid4 str) for one investigation lifecycle."""
    return str(uuid.uuid4())
