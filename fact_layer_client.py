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

Exposes thin wrappers over the nine generic Fact Layer Gateway tools:
  * record_fact(...)          -> the governed write path (adjudicated server-side)
  * get_fact(...)             -> current resolved fact for (tenant, subject, predicate)
  * get_fact_history(...)     -> append-only event lineage
  * explain_fact(...)         -> full provenance / why the current fact holds
  * list_disputes(...)        -> facts in a disputed resolution
  * search_entities(...)      -> structured entity lookup
  * vector_search(...)        -> tenant-scoped semantic recall (embedded server-side)
  * query_tenant_metrics(...) -> enumerated named metrics only
  * retract_fact(...)         -> lifecycle retraction (append-only)

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
     fact layer's `predicate_rule` registry. The generic fact layer ships
     domain-neutral core predicates (status [scalar], last_seen [monotonic],
     known_aliases [set], notes [freetext]); THIS deployment additionally SEEDS
     domain predicates — `fraud_status` and `liability_status` — into that same
     registry (with predicate-specific authority; see the constants block below).
     Seeding domain vocabulary is deployment DATA: it does not make the generic
     fact layer fraud-specific — the tools and mechanism stay domain-neutral.
     Accordingly FRAUD_PREDICATE / BETTING_PREDICATE default to `fraud_status` /
     `liability_status` (env-overridable). Against a deployment that has NOT
     seeded them, override to a registered predicate such as `status`, which
     gives the same corroborate/supersede/dispute semantics.

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
# Domain predicates are now registered in the fact layer's predicate_rule AND
# carry PREDICATE-SPECIFIC authority (authority_policy): for fraud_status a human
# investigator/fraud_review outranks a model/agent, and a user assertion is LOW
# authority (so a customer's "I'm legitimate" is retained as contrary evidence,
# not an automatic dispute). Override via env only if you rename them.
FRAUD_PREDICATE = os.getenv("FACT_FRAUD_PREDICATE", "fraud_status")
BETTING_PREDICATE = os.getenv("FACT_BETTING_PREDICATE", "liability_status")

# Source identities for provenance + the predicate-specific authority gate. These
# must match sources in the fact layer's authority_policy. For fraud_status:
# human_investigator=100, fraud_review=95, fraud_model=70, agent_inference=55,
# user_assertion=20. An unranked source is treated as authority 0.
SOURCE_AGENT = os.getenv("FACT_SOURCE_AGENT", "agent_inference")            # fraud_status: 55
SOURCE_HUMAN = os.getenv("FACT_SOURCE_HUMAN", "human_investigator")        # fraud_status: 100
SOURCE_FRAUD_REVIEW = os.getenv("FACT_SOURCE_FRAUD_REVIEW", "fraud_review")  # fraud_status: 95
SOURCE_MODEL = os.getenv("FACT_SOURCE_MODEL", "fraud_model")               # fraud_status: 70
SOURCE_USER = os.getenv("FACT_SOURCE_USER", "user_assertion")             # fraud_status: 20 (low)
SOURCE_SEED = os.getenv("FACT_SOURCE_SEED", "verified_integration")
SOURCE_SYSTEM = os.getenv("FACT_SOURCE_SYSTEM", "system_of_record")


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

    In the reference governance profile the Gateway authorizes a call by
    comparing the `tenant_id` tool argument (the requested data scope) to the
    caller's JWT `tenant_id` claim — so authenticating for a tenant means using a
    Cognito user whose token carries that claim. Two resolution paths:

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
# Logical tool name -> AgentCore Gateway MCP tool name
# ---------------------------------------------------------------------------
# This module's public API and internal callers speak in LOGICAL tool names
# (vector_search, record_fact, ...). The live AgentCore Gateway namespaces each
# MCP tool as "<TargetName>___<tool>". Keep the two decoupled: one explicit map,
# resolved once immediately before session.call_tool(). If the Gateway ever
# renames a target, this table is the only edit site.
_GATEWAY_TOOL_NAMES = {
    "explain_fact": "ExplainFact___explain_fact",
    "get_fact_history": "GetFactHistory___get_fact_history",
    "get_fact": "GetFact___get_fact",
    "list_disputes": "ListDisputes___list_disputes",
    "query_tenant_metrics": "QueryTenantMetrics___query_tenant_metrics",
    "record_fact": "RecordFact___record_fact",
    "retract_fact": "RetractFact___retract_fact",
    "search_entities": "SearchEntities___search_entities",
    "vector_search": "VectorSearch___vector_search",
}


def _resolve_gateway_tool(logical_name: str) -> str:
    """Map a logical Fact Layer tool name to its Gateway MCP tool name.

    Fails closed: an unknown logical name is a programming error (typo or a tool
    added without a mapping), never a silent pass-through that would reach the
    Gateway as an unknown tool.
    """
    try:
        return _GATEWAY_TOOL_NAMES[logical_name]
    except KeyError:
        raise ValueError(
            f"Unknown Fact Layer tool '{logical_name}'. Known logical tools: "
            f"{', '.join(sorted(_GATEWAY_TOOL_NAMES))}."
        ) from None


# ---------------------------------------------------------------------------
# MCP call plumbing
# ---------------------------------------------------------------------------
async def _acall_tool(gateway_url: str, token: str, tool_name: str, arguments: dict):
    headers = {"Authorization": f"Bearer {token}"}
    # Resolve the logical name to the Gateway's namespaced tool name immediately
    # before the call, so the wire always carries the real MCP tool name.
    gateway_tool_name = _resolve_gateway_tool(tool_name)
    async with streamablehttp_client(gateway_url, headers=headers) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            return await session.call_tool(gateway_tool_name, arguments=arguments)


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
                session_id: str | None = None,
                entity_type: str | None = None,
                canonical_key: str | None = None,
                assessor_type: str | None = None,
                evidence: list | None = None,
                context_summary: str | None = None,
                valid_from: str | None = None,
                valid_to: str | None = None,
                idempotency_key: str | None = None) -> dict:
    """Record a fact (assessment/resolution) through the governed write path.

    tenant_id is the requested data scope, sent as the tool argument. In the
    reference governance profile the Gateway authorizes the call by comparing it
    to the caller's token `tenant_id` claim (Cedar tenant-equality).

    Evidence -> Assessment -> Resolution fields (all optional, forwarded to the
    fact layer's record_fact): entity_type/canonical_key bind the fact to a
    canonical entity; assessor_type records the KIND of assessor; evidence[] are
    references back to fraud-domain observations (never copies); context_summary
    enriches the semantic embedding; valid_from/valid_to are the real-world
    validity window; idempotency_key makes retries safe.

    agent_id / session_id are provenance INTENT only and are NOT propagated to the
    MCP target (an MCP Lambda target receives only the tool arguments; there is no
    interceptor-injected identity and no _fact_ctx side channel).
    Write control (minimum confidence) is enforced SERVER-SIDE.
    """
    args = {
        "tenant_id": tenant_id,
        "subject": subject,
        "predicate": predicate,
        "value": value,
        "source": source,
        "confidence": confidence,
    }
    # Forward optional E->A->R fields only when present (schema-optional).
    if entity_type is not None:
        args["entity_type"] = entity_type
    if canonical_key is not None:
        args["canonical_key"] = canonical_key
    if assessor_type is not None:
        args["assessor_type"] = assessor_type
    if evidence:
        args["evidence"] = evidence
    if context_summary is not None:
        args["context_summary"] = context_summary
    if valid_from is not None:
        args["valid_from"] = valid_from
    if valid_to is not None:
        args["valid_to"] = valid_to
    if idempotency_key is not None:
        args["idempotency_key"] = idempotency_key
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


# ---------------------------------------------------------------------------
# Governed read / lifecycle tools (E->A->R)
# ---------------------------------------------------------------------------
def explain_fact(*, tenant_id: str, subject: str, predicate: str) -> dict:
    """Why is the current fact what it is? Full provenance for agent consumption:
    resolution, winning authority, supporting vs contrary evidence, assessments,
    superseded/disputed prior assertions, and outcome history."""
    return _call(tenant_id, "explain_fact", {
        "tenant_id": tenant_id, "subject": subject, "predicate": predicate,
    })


def get_fact(*, tenant_id: str, subject: str, predicate: str) -> dict:
    """The current resolved value/status for (subject, predicate)."""
    return _call(tenant_id, "get_fact", {
        "tenant_id": tenant_id, "subject": subject, "predicate": predicate,
    })


def get_fact_history(*, tenant_id: str, subject: str, predicate: str,
                     limit: int = 500) -> dict:
    """The append-only event lineage for (subject, predicate)."""
    return _call(tenant_id, "get_fact_history", {
        "tenant_id": tenant_id, "subject": subject, "predicate": predicate,
        "limit": limit,
    })


def list_disputes(*, tenant_id: str, limit: int = 100) -> dict:
    """Facts currently in a disputed resolution for the tenant."""
    return _call(tenant_id, "list_disputes", {"tenant_id": tenant_id, "limit": limit})


def search_entities(*, tenant_id: str, query: str | None = None,
                    entity_type: str | None = None, limit: int = 50) -> dict:
    """Structured canonical-entity lookup (not semantic; use vector_search for that)."""
    args = {"tenant_id": tenant_id, "limit": limit}
    if query is not None:
        args["query"] = query
    if entity_type is not None:
        args["entity_type"] = entity_type
    return _call(tenant_id, "search_entities", args)


def retract_fact(*, tenant_id: str, subject: str, predicate: str, source: str,
                 reason: str | None = None, assessor_type: str | None = None) -> dict:
    """Explicitly retract the current fact (append-only op='retract')."""
    args = {"tenant_id": tenant_id, "subject": subject, "predicate": predicate,
            "source": source}
    if reason is not None:
        args["reason"] = reason
    if assessor_type is not None:
        args["assessor_type"] = assessor_type
    return _call(tenant_id, "retract_fact", args)
