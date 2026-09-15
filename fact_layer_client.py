"""
fact_layer_client — MCP caller for the AgentCore Governed Fact Layer.

This repo is a CALLER of a separately-deployed, standalone AgentCore service
(the "governed fact layer") that lives in a sibling repo. Adjudication, hashing,
tamper-evident audit, per-tenant Cedar isolation, and server-side embedding all
live THERE, behind an AgentCore Gateway. This module does exactly one thing:
authenticate as a tenant and speak MCP to that Gateway.

The auth/call pattern is reused verbatim from the fact layer's own thin test
caller (aws/test_fact_layer.py): a Cognito bearer token via boto3
`initiate_auth` (USER_PASSWORD_AUTH), then an MCP `ClientSession` over
`streamablehttp_client` with an `Authorization: Bearer <token>` header. Nothing
here re-implements storage, adjudication, or hashing.

Exposes three thin wrappers over the Gateway tools:
  * record_fact(...)          -> the governed write path (adjudicated server-side)
  * vector_search(...)        -> tenant-scoped semantic recall (embedded server-side)
  * query_tenant_metrics(...) -> enumerated named metrics only

===========================================================================
CANONICAL SUBJECT-KEY CONVENTION  (task 2 — read before calling record_fact)
===========================================================================
A fact's identity is (tenant_id, subject, predicate). Two assertions collide on
the same current-truth row IFF they share all three. That collision is what
lets record_fact's adjudication corroborate / supersede / dispute instead of
piling up duplicates — so the subject key IS the identity decision. Get it wrong
and either (a) the same real-world fact splits across many subjects (no
corroboration, no supersede) or (b) unrelated facts collide.

Two hard constraints from the deployed fact layer shape these choices:

  1. PREDICATE MUST BE REGISTERED. record_fact rejects any predicate not in the
     fact layer's `predicate_rule` table. That table (seeded in the aws repo,
     which we must not modify) currently holds only: status (scalar),
     last_seen (monotonic), known_aliases (set), notes (freetext). A scalar
     `status` gives us precisely the corroborate/supersede/dispute semantics we
     want for a verdict, so verdicts map to predicate **"status"**. The task's
     intended domain names ("fraud_verdict", "liability_status") are exposed as
     env-overridable constants below (FRAUD_PREDICATE / BETTING_PREDICATE) for
     when an operator adds those rows to predicate_rule; they default to
     "status" so the port runs against the fact layer as it ships today.

  2. VECTOR_SEARCH EMBEDS THE SUBJECT STRING, NOT THE VALUE. Semantic recall
     (Tier 5) matches a trigger against `subject_vec` = embed(subject). So the
     subject must carry the semantic tokens you want to recall on. Opaque ids
     recall poorly; that is why catalog subjects are descriptive slugs, not
     hashes.

Subject grammar:   "<domain>:<kind>:<identifier>"
  domain  : "fraud" | "betting"          (keeps the shared "status" predicate
                                          from cross-contaminating a fraud vs a
                                          betting verdict on the same numeric id)
  kind    : entity-scoped  -> customer | ip | device | session | card | address
            signature      -> a sorted composite, kind rendered as the joined keys
            catalog        -> pattern
  identifier : the id, the composite "k1:v1|k2:v2" (keys sorted), or a slug

PER-PATTERN MAPPING (enumerated from adapters/fraud & adapters/betting):

  Fraud — LIVE verdicts (compound_resolution about a real investigated entity):
    velocity burst / coordinated multi-account ....... signature  fraud:ip:<ip>
    high-value first-time / ATO session-hijack ....... entity     fraud:customer:<id>
    credential-stuffing ATO .......................... entity     fraud:customer:<id>
    cross-border / freight-forwarding redirect ....... entity     fraud:customer:<id>
    refund abuse (serial returner, triangulation) .... entity     fraud:customer:<id>
    synthetic identity — thin-file ................... entity     fraud:customer:<id>
    synthetic identity — address factory ............. signature  fraud:address:<slug>
    device-fingerprint reuse ......................... signature  fraud:device:<fp>
    headless-browser automation ...................... signature  fraud:device:<fp>
    off-hours burst .................................. signature  fraud:ip:<ip>
    chargeback double-dip ............................ entity     fraud:customer:<id>
    gift-card laundering ............................. entity     fraud:customer:<id>

  Betting — LIVE verdicts:
    arbitrage bot (single IP, opposing bets) ......... signature  betting:ip:<ip>
    multi-accounting bonus abuse (shared session) .... signature  betting:session:<sess>
    insider sharp money .............................. entity     betting:customer:<id>

  Catalog patterns (SEED_CATALOG, cold-start canonical shapes, no live entity):
    every fraud/betting seed ......................... catalog    <domain>:pattern:<slug>
    (slug = kebab-case of the pattern's leading phrase, so a trigger naming the
     pattern recalls it via subject-embedding similarity)

Helpers below build these keys; use them rather than hand-formatting subjects.
"""

from __future__ import annotations

import asyncio
import base64
import ipaddress
import json
import os
import re

import boto3
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client


# ---------------------------------------------------------------------------
# Predicate + source constants (env-overridable)
# ---------------------------------------------------------------------------
# Default to "status" because that is what the deployed predicate_rule accepts.
# Override once fraud_verdict / liability_status are registered in predicate_rule.
FRAUD_PREDICATE = os.getenv("FACT_FRAUD_PREDICATE", "status")
BETTING_PREDICATE = os.getenv("FACT_BETTING_PREDICATE", "status")

# Source identity for provenance + the authority gate. These must match rows in
# the fact layer's source_rank table (seeded there): system_of_record=100,
# verified_integration=80, user_assertion=60, agent_inference=40. An unranked
# source is treated as rank 0 (lowest authority) by the adjudicator.
SOURCE_AGENT = os.getenv("FACT_SOURCE_AGENT", "agent_inference")            # rank 40
SOURCE_HUMAN = os.getenv("FACT_SOURCE_HUMAN", "user_assertion")            # rank 60
SOURCE_SEED = os.getenv("FACT_SOURCE_SEED", "verified_integration")       # rank 80
SOURCE_SYSTEM = os.getenv("FACT_SOURCE_SYSTEM", "system_of_record")       # rank 100


# ---------------------------------------------------------------------------
# Subject-key builders (see the convention block above)
# ---------------------------------------------------------------------------
def _slug(text: str, max_len: int = 60) -> str:
    """Stable kebab-case slug carrying the semantic tokens of a phrase."""
    lead = text.split(":", 1)[0].strip().lower()
    lead = re.sub(r"[^a-z0-9]+", "-", lead).strip("-")
    return lead[:max_len] or "pattern"


def _looks_like_ip(value: str) -> bool:
    try:
        ipaddress.ip_address(value)
        return True
    except ValueError:
        return False


def entity_subject(domain: str, entity_ref: str) -> str:
    """Entity-scoped subject from a raw entity_ref (customer_id or IP).

    Mirrors the adapters' own entity_ref inference: an all-digit ref is a
    customer_id; a dotted/colon quad is an IP; anything else is an opaque
    entity id.
    """
    ref = str(entity_ref).strip()
    if ref.isdigit():
        kind = "customer"
    elif _looks_like_ip(ref):
        kind = "ip"
    else:
        kind = "entity"
    return f"{domain}:{kind}:{ref}"


def signature_subject(domain: str, **attrs: str) -> str:
    """Signature-scoped subject: a canonical, key-sorted composite.

    e.g. signature_subject("fraud", device="fp-abc", ip="1.2.3.4")
         -> "fraud:device:fp-abc|ip:1.2.3.4"
    Keys are sorted so the same signature always renders identically and
    collides on the same fact identity.
    """
    parts = [f"{k}:{v}" for k, v in sorted(attrs.items()) if v is not None]
    kind = "|".join(parts)
    return f"{domain}:{kind}"


def catalog_subject(domain: str, content: str) -> str:
    """Catalog subject for a cold-start seed pattern: <domain>:pattern:<slug>."""
    return f"{domain}:pattern:{_slug(content)}"


# ---------------------------------------------------------------------------
# Gateway configuration (from env — no deployment state exists to read from yet)
# ---------------------------------------------------------------------------
def _gateway_url() -> str:
    url = os.getenv("FACT_LAYER_GATEWAY_URL")
    if not url:
        raise ValueError(
            "FACT_LAYER_GATEWAY_URL is unset. Point it at the fact layer's "
            "Gateway MCP endpoint (the AGENTCORE_GATEWAY_<name>_URL emitted by "
            "the fact layer's deploy)."
        )
    return url


def _tenant_credentials(tenant_id: str) -> tuple[str, str, str]:
    """Resolve (client_id, username, password) for authenticating AS a tenant.

    The fact layer injects tenant_id into the request context from the JWT
    `tenant_id` claim, so authenticating as a tenant means using a Cognito user
    whose token carries that claim. Two resolution paths:

      1. FACT_LAYER_TENANT_CREDENTIALS — JSON map keyed by tenant_id:
         {"demo-bank-alpha": {"client_id": "...", "username": "...",
                               "password": "..."}, "demo-bank-beta": {...}}
      2. Fallback single-tenant env: COGNITO_CLIENT_ID / COGNITO_USERNAME /
         COGNITO_PASSWORD (matches aws/test_fact_layer.py). Only valid when the
         requested tenant matches that user's claim.
    """
    raw = os.getenv("FACT_LAYER_TENANT_CREDENTIALS")
    if raw:
        creds = json.loads(raw).get(tenant_id)
        if creds:
            client_id = creds.get("client_id") or os.getenv("COGNITO_CLIENT_ID")
            return client_id, creds["username"], creds["password"]
    client_id = os.getenv("COGNITO_CLIENT_ID")
    username = os.getenv("COGNITO_USERNAME")
    password = os.getenv("COGNITO_PASSWORD")
    if not (client_id and username and password):
        raise ValueError(
            f"No Cognito credentials for tenant '{tenant_id}'. Set "
            "FACT_LAYER_TENANT_CREDENTIALS (per-tenant JSON map) or the "
            "single-tenant COGNITO_CLIENT_ID / COGNITO_USERNAME / "
            "COGNITO_PASSWORD env vars."
        )
    return client_id, username, password


def _get_token(tenant_id: str) -> str:
    """Cognito bearer token whose tenant_id claim == tenant_id (per config)."""
    client_id, username, password = _tenant_credentials(tenant_id)
    cognito = boto3.client("cognito-idp", region_name=os.getenv("AWS_REGION"))
    resp = cognito.initiate_auth(
        AuthFlow="USER_PASSWORD_AUTH",
        ClientId=client_id,
        AuthParameters={"USERNAME": username, "PASSWORD": password},
    )
    return resp["AuthenticationResult"]["AccessToken"]


def decode_claims(token: str) -> dict:
    """Local, unverified JWT payload decode — for a claims sanity print only."""
    payload_b64 = token.split(".")[1]
    padded = payload_b64 + "=" * (-len(payload_b64) % 4)
    return json.loads(base64.urlsafe_b64decode(padded))


# ---------------------------------------------------------------------------
# MCP call plumbing
# ---------------------------------------------------------------------------
async def _acall_tool(gateway_url: str, token: str, tool_name: str, arguments: dict):
    headers = {"Authorization": f"Bearer {token}"}
    async with streamablehttp_client(gateway_url, headers=headers) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            return await session.call_tool(tool_name, arguments=arguments)


def _parse_result(result) -> dict:
    """Coerce an MCP CallToolResult into a plain dict.

    The Gateway returns the handler's JSON as a text content block; parse it.
    An MCP-level tool error (isError) is surfaced as {"error": ...} so callers
    never mistake a deny/handler error for a successful write.
    """
    if getattr(result, "isError", False):
        text = _first_text(result)
        return {"error": text or "tool call returned isError", "is_error": True}
    text = _first_text(result)
    if text is None:
        return {"error": "empty tool result"}
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return {"raw": text}


def _first_text(result):
    for block in getattr(result, "content", []) or []:
        t = getattr(block, "text", None)
        if t is not None:
            return t
    return None


def _call(tenant_id: str, tool_name: str, arguments: dict) -> dict:
    """Synchronous one-shot MCP tool call (auth + call + parse)."""
    token = _get_token(tenant_id)
    result = asyncio.run(_acall_tool(_gateway_url(), token, tool_name, arguments))
    return _parse_result(result)


# ---------------------------------------------------------------------------
# Public tool wrappers
# ---------------------------------------------------------------------------
def record_fact(*, tenant_id: str, subject: str, predicate: str, value,
                source: str, confidence: float,
                agent_id: str | None = None,
                session_id: str | None = None) -> dict:
    """Record a fact through the governed write path.

    tenant_id is sent as the tool argument AND is the identity the token
    authenticates as (Cedar compares them).

    agent_id / session_id ARE threaded into this client (task 9) but are
    deliberately NOT placed in the MCP tool arguments: the fact layer derives
    provenance (agent_id, session_id, tool_turn) from the interceptor-injected
    request context, never from the caller's tool input (see
    aws/handlers/common/context.py), and record_fact's inputSchema does not
    declare them — so sending them would at best be ignored and at worst be
    rejected by a strict Gateway validator. They are accepted here so call sites
    pass a complete identity and so provenance is available for local logging;
    the deployed interceptor remains authoritative.

    Write control (minimum confidence) is enforced SERVER-SIDE in the fact
    layer's record_fact; this caller does not pre-gate confidence.
    """
    args = {
        "tenant_id": tenant_id,
        "subject": subject,
        "predicate": predicate,
        "value": value,
        "source": source,
        "confidence": confidence,
    }
    return _call(tenant_id, "record_fact", args)


def vector_search(*, tenant_id: str, query_text: str, top_k: int = 5) -> dict:
    """Tenant-scoped semantic recall. query_text is embedded server-side."""
    return _call(tenant_id, "vector_search", {
        "tenant_id": tenant_id,
        "query_text": query_text,
        "top_k": top_k,
    })


def query_tenant_metrics(*, tenant_id: str, metric: str) -> dict:
    """One enumerated, tenant-scoped metric (fact_count, dispute_rate, ...)."""
    return _call(tenant_id, "query_tenant_metrics", {
        "tenant_id": tenant_id,
        "metric": metric,
    })
