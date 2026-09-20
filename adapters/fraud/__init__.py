"""
Fraud domain adapter.

Defines the fraud-specific parts of the cognitive foundation:
  - tier_1_entity:    customer profile + risk band
  - tier_2_recent:    recent transactions for entity/IP
  - tier_4_prior:     prior fraud investigations for the entity
  - risk_band:        banding rule (VIP_TRUSTED / STANDARD / IP_SCOPE)
  - SEED_CATALOG:     16 curated e-commerce fraud patterns

Tier 3 (active investigation) and Tier 5 (semantic memory recall) are
substrate-generic — they live in agent_tools.assemble_context and are
NOT adapter concerns. Do not add T3 or T5 here.

Session convention (required for tier_4_prior):
  create_session(session_id, tenant_id, user_id=str(entity_ref))
  tenant_id is required (workflow data scope) and user_id must be the
  customer_id (as string) or ip_address — NOT "guest". tier_4_prior filters on
  agent_sessions.user_id = entity_ref AND agent_sessions.tenant_id; if user_id
  is a default value prior investigations return empty, and a mismatched tenant
  is correctly excluded (no cross-tenant recall).

Sports betting patterns live in adapters/betting/SEED_CATALOG (separate
adapter on the same substrate — Thesis 11).
"""

from mysql.connector import Error


# ---------------------------------------------------------------------------
# ROUTING CALIBRATION (per-adapter overrides)
# ---------------------------------------------------------------------------
# Fintech triggers are short ("any fraudulent orders?", "investigate IP X")
# and embed at lower similarity to long banded catalog descriptions. The EV
# adapter's 0.55 default loses calibration here — too many otherwise-good
# matches sit at 0.45-0.55 and miss the shortcut path.
#
# Precedence (handled by cognitive_loop.run_investigation):
#   ROUTING_SIMILARITY_GATE env var > this adapter default > module default
# ---------------------------------------------------------------------------
SIMILARITY_GATE = 0.45
CONFIDENCE_GATE = 0.85  # match the substrate default; adapters can override.


# ---------------------------------------------------------------------------
# SCHEMA HINT (composed into the agent's system prompt)
# ---------------------------------------------------------------------------
# Tells the model what tables and columns exist so it does not burn tool
# rounds on DESCRIBE queries. Kept compact — the goal is to inform, not to
# replace SQL knowledge. The agent can still inspect schema on demand.
# ---------------------------------------------------------------------------
SCHEMA_HINT = """\
Available tables (fraud adapter, e-commerce). EVERY domain table is tenant-scoped
by a tenant_id column; all reads are already filtered to your tenant — you do not
issue SQL yourself (execute_sql is retired).
  customers(tenant_id, customer_id, name, email, email_domain, region, vip_status,
            signup_date, timezone, shipping_address JSON)
  orders(tenant_id, order_id, customer_id, product_id, quantity, amount, ip_address,
         country, billing_country, shipping_country, shipping_address,
         device_id, user_agent, checkout_seconds, delivery_expedited,
         status ENUM('pending','flagged','cleared','fraudulent'),
         flagged_reason, order_date)
  products(tenant_id, product_id, name, category, price, description,
           is_digital_giftcard)
  login_attempts(tenant_id, attempt_id, customer_id, ip_address, country, success,
                 attempted_at)
  refunds(tenant_id, refund_id, order_id, customer_id, reason, refund_method,
          refunded_at)
  chargebacks(tenant_id, chargeback_id, customer_id, card_last4, filed_at,
              delivery_confirmed_at)
  freight_forwarders(tenant_id, forwarder_id, address_pattern, country, known_since)

Semantic memory is now the GOVERNED FACT LAYER (AgentCore Gateway), not a local
table: use recall_similar_fraud for tenant-scoped semantic recall of prior
verdicts, and compound_resolution to persist a confirmed verdict (the fact layer
adjudicates contradictions and keeps a tamper-evident audit trail server-side).
Local episodic checkpoints still live in:
  agent_reasoning(reasoning_id, session_id, observation, hypothesis,
                  evidence_refs JSON, confidence, resolution, created_at)
"""


def risk_band(vip_status: bool) -> str:
    """Banding rule — keeps risk classification declarative and adapter-owned."""
    return "VIP_TRUSTED" if vip_status else "STANDARD"


def tier_1_entity(cursor, entity_ref: str, tenant_id: str) -> tuple[str, str]:
    """
    Build the Tier 1 entity-profile block for the fraud adapter (tenant-scoped).

    Returns (text_block, status_tag). Empty text_block + 'degraded' tag
    means the substrate should degrade gracefully (Thesis: degraded state
    must be visible, not invisible).
    """
    if not entity_ref:
        return "", "degraded_no_entity"

    try:
        cursor.execute(
            "SELECT customer_id, name, region, vip_status FROM customers "
            "WHERE customer_id = %s AND tenant_id = %s LIMIT 1",
            (int(entity_ref), tenant_id),
        )
        row = cursor.fetchone()
        if row:
            band = risk_band(row['vip_status'])
            return (
                f"[T1 entity] customer_id={row['customer_id']} name={row['name']} "
                f"region={row['region']} risk_band={band}",
                "ok",
            )
    except (ValueError, Error):
        pass

    # Fallback: treat entity_ref as IP and surface order history aggregate
    try:
        cursor.execute(
            "SELECT COUNT(*) AS n FROM orders WHERE ip_address = %s AND tenant_id = %s",
            (entity_ref, tenant_id),
        )
        row = cursor.fetchone()
        if row:
            return (
                f"[T1 entity] ip_address={entity_ref} historic_orders={row['n']} risk_band=IP_SCOPE",
                "ok",
            )
    except Error:
        pass

    return "", "degraded_lookup_failed"


def tier_2_recent(cursor, entity_ref: str, tenant_id: str) -> tuple[list[str], str]:
    """Build the Tier 2 recent-transactions block (tenant-scoped)."""
    if not entity_ref:
        return [], "degraded_no_entity"

    lines = []
    try:
        cursor.execute(
            """SELECT order_id, amount, country, status, order_date
               FROM orders
               WHERE customer_id = %s AND tenant_id = %s
               ORDER BY order_date DESC LIMIT 5""",
            (int(entity_ref), tenant_id),
        )
        for r in cursor.fetchall():
            lines.append(
                f"order={r['order_id']} amount=${r['amount']} country={r['country']} "
                f"status={r['status']} at={r['order_date']}"
            )
    except (ValueError, Error):
        try:
            cursor.execute(
                """SELECT order_id, amount, country, status, order_date
                   FROM orders
                   WHERE ip_address = %s AND tenant_id = %s
                   ORDER BY order_date DESC LIMIT 5""",
                (entity_ref, tenant_id),
            )
            for r in cursor.fetchall():
                lines.append(
                    f"order={r['order_id']} amount=${r['amount']} country={r['country']} "
                    f"status={r['status']} at={r['order_date']}"
                )
        except Error:
            return [], "degraded_lookup_failed"

    return lines, "ok" if lines else "no_recent_activity"


def tier_4_prior(cursor, entity_ref: str, tenant_id: str = None) -> tuple[list[str], str]:
    """Build the Tier 4 prior-investigations block.

    Reads the episodic/workflow tables (agent_reasoning / agent_sessions). These
    are keyed by user_id=entity_ref, which is NOT unique across tenants (two
    tenants can each have a customer_id 4), so the query MUST also filter on
    agent_sessions.tenant_id — otherwise one tenant's prior investigation would
    surface for another tenant using the same entity id. Fails closed if the
    tenant scope is missing.
    """
    if not entity_ref:
        return [], "degraded_no_entity"
    if not tenant_id:
        return [], "degraded_no_tenant"

    try:
        cursor.execute(
            """SELECT ar.hypothesis, ar.resolution, ar.confidence
               FROM agent_reasoning ar
               JOIN agent_sessions s ON s.session_id = ar.session_id
               WHERE s.user_id = %s AND s.tenant_id = %s
               ORDER BY ar.created_at DESC LIMIT 3""",
            (str(entity_ref), str(tenant_id)),
        )
        rows = cursor.fetchall()
        if not rows:
            return [], "no_prior_investigations"
        return (
            [
                f"hyp={r['hypothesis']} → res={r['resolution']} (conf={r['confidence']})"
                for r in rows
            ],
            "ok",
        )
    except Error:
        return [], "degraded_lookup_failed"


# ---------------------------------------------------------------------------
# SEED CATALOG — Fraud (16 e-commerce patterns)
# ---------------------------------------------------------------------------
# Curated patterns loaded into the governed FACT LAYER at deploy time (via
# seed_fraud_memory_from_adapter -> record_fact) so the routing layer can
# shortcut from invocation 1, rather than waiting for the ~15-25 invocation
# warm-up curve. Mirrors the EV adapter's outage_catalog pattern.
#
# All entries here are global-scope canonical shapes. Entity-scoped patterns
# (per customer_id or ip_address) accumulate automatically through normal
# compound_resolution() calls during live operation — do not pre-seed those.
#
# Confidence calibration guide:
#   0.95+  near-certain — seen hundreds of times, very low false-positive rate
#   0.90   high — well-established pattern, occasional edge cases
#   0.85   threshold — minimum for shortcut routing; include only confirmed shapes
#
# When adding patterns:
#   1. Write in semantically-banded language (what was observed, not jargon).
#   2. Include the signal chain: what precedes it, what it leads to.
#   3. Assign confidence based on real-world FP rate, not intuition.
#   4. Run seed_fraud_memory_from_adapter() after adding — it is idempotent.
#
# Schema dependencies (Phase A schema extension):
#   - customers.email_domain, .timezone, .shipping_address
#   - orders.billing_country, .shipping_country, .shipping_address,
#     .device_id, .user_agent, .checkout_seconds, .delivery_expedited
#   - products.is_digital_giftcard
#   - login_attempts, refunds, chargebacks, freight_forwarders tables
#
# Sports betting patterns live in adapters/betting/SEED_CATALOG, not here.
# ---------------------------------------------------------------------------

SEED_CATALOG = [

    # --- VELOCITY / COORDINATED ---

    {
        "scope": "global",
        "content": (
            "Velocity burst: 3+ orders from the same IP within 24h, with at least one "
            "order over $1,000 and shipping to a high-risk country. Pattern is "
            "consistent with card-testing followed by a high-value pull."
        ),
        "confidence": 0.92,
    },
    {
        "scope": "global",
        "content": (
            "High-value first-time purchase: single order over $3,000 from a customer "
            "with no prior order history. Often signals stolen-card abuse or account "
            "takeover."
        ),
        "confidence": 0.88,
    },
    {
        "scope": "global",
        "content": (
            "Coordinated multi-account: distinct customer_ids sharing one IP placing "
            "similar orders within the same hour. Pattern is consistent with botnet "
            "activity or loyalty-program abuse."
        ),
        "confidence": 0.90,
    },

    # --- ACCOUNT TAKEOVER (ATO) ---

    {
        "scope": "global",
        "content": (
            "Account takeover — credential stuffing: successful login from a new "
            "country or IP within 2h of a failed login burst (5+ failures from "
            "distinct IPs), followed immediately by a high-value order or address "
            "change. The login-failure cluster is the ATO precursor; the order is "
            "the extraction event."
        ),
        "confidence": 0.93,
    },
    {
        "scope": "global",
        "content": (
            "Account takeover — session hijack: established customer with 6+ months "
            "of order history suddenly places a high-value order ($2,000+) with a new "
            "shipping address in a different country, no prior orders to that region. "
            "No login-failure precursor — likely session token theft rather than "
            "credential stuffing."
        ),
        "confidence": 0.87,
    },

    # --- CROSS-BORDER / SHIPPING MISMATCH ---

    {
        "scope": "global",
        "content": (
            "Cross-border shipping mismatch: billing country does not match shipping "
            "country, and the shipping destination is on a high-risk country list, "
            "for an order over $500 placed by a customer with fewer than 3 prior "
            "orders. Mismatch alone is weak signal; combined with low order history "
            "and high-risk destination, false-positive rate drops significantly."
        ),
        "confidence": 0.86,
    },
    {
        "scope": "global",
        "content": (
            "Freight-forwarding redirect: shipping address is a known freight-forwarder "
            "or package-consolidation service in the US or EU, combined with an order "
            "of easily-resaleable electronics or gift cards over $800. These addresses "
            "are used to launder physical goods internationally; the forwarder obscures "
            "the final destination."
        ),
        "confidence": 0.89,
    },

    # --- REFUND / RETURN ABUSE ---

    {
        "scope": "global",
        "content": (
            "Refund abuse — serial returner: customer has a refund rate above 60% "
            "across 5+ completed orders in 90 days, with refunds consistently "
            "claimed on high-value items citing 'item not received' or 'not as "
            "described'. Pattern is consistent with wardrobing or deliberate refund "
            "farming rather than legitimate dissatisfaction."
        ),
        "confidence": 0.85,
    },
    {
        "scope": "global",
        "content": (
            "Refund abuse — triangulation: order placed with a stolen card, item "
            "delivered to a legitimate victim address, then the fraudster contacts "
            "support claiming non-delivery and receives a refund to a different "
            "payment method. Signal: refund-to-different-method within 7 days of "
            "delivery confirmation, combined with a first-time customer."
        ),
        "confidence": 0.91,
    },

    # --- SYNTHETIC IDENTITY ---

    {
        "scope": "global",
        "content": (
            "Synthetic identity — thin-file high-value: customer account created "
            "within the last 30 days, no order history, email domain is a free "
            "provider (gmail/yahoo/hotmail), shipping and billing addresses added "
            "within 24h of account creation, first order immediately high-value "
            "($1,500+). The thin file combined with immediate high-value intent "
            "distinguishes this from a legitimate new customer."
        ),
        "confidence": 0.88,
    },
    {
        "scope": "global",
        "content": (
            "Synthetic identity — address factory: multiple customer accounts created "
            "within a 48h window sharing the same physical address or address "
            "components (same street number, same postcode), each with distinct email "
            "addresses. Consistent with bulk account creation using address-generation "
            "tools; precedes coordinated multi-account fraud."
        ),
        "confidence": 0.90,
    },

    # --- DEVICE / SESSION SIGNALS ---

    {
        "scope": "global",
        "content": (
            "Device fingerprint reuse across accounts: the same browser fingerprint "
            "or device_id appears across 3+ distinct customer accounts within 7 days, "
            "with at least one of those accounts placing a flagged or high-risk order. "
            "Fingerprint reuse is the connecting signal that ties otherwise-independent "
            "accounts to a single operator."
        ),
        "confidence": 0.91,
    },
    {
        "scope": "global",
        "content": (
            "Headless browser / automation signature: order placed with a user-agent "
            "consistent with a headless browser (Puppeteer, Playwright, PhantomJS) or "
            "with no user-agent, combined with checkout completion time under 8 seconds "
            "from cart creation. Human checkout averages 45-90 seconds; sub-8s "
            "completion is near-certain automation."
        ),
        "confidence": 0.94,
    },

    # --- TIME-OF-DAY / BEHAVIOURAL ---

    {
        "scope": "global",
        "content": (
            "Off-hours burst: 5+ orders across distinct accounts placed between "
            "02:00-05:00 local time for the billing country, all within a 90-minute "
            "window, with at least 3 shipping to the same city. Off-hours clustering "
            "is consistent with automated fraud runs timed to avoid real-time "
            "review queues."
        ),
        "confidence": 0.87,
    },

    # --- CHARGEBACK CORRELATION ---

    {
        "scope": "global",
        "content": (
            "Chargeback precursor — double-dip: customer has 2+ chargebacks in the "
            "last 6 months, each on a different card, with the chargeback filed within "
            "48h of delivery confirmation. New order now placed on a third card. "
            "The multi-card + rapid-chargeback pattern is a strong predictor of "
            "intentional friendly fraud rather than legitimate disputes."
        ),
        "confidence": 0.90,
    },

    # --- GIFT CARD / DIGITAL GOODS ---

    {
        "scope": "global",
        "content": (
            "Gift card laundering: order consists entirely of digital gift cards "
            "totalling over $500, placed by a first-time customer or an account "
            "with no prior digital-goods purchases, with expedited delivery selected. "
            "Digital gift cards are the preferred monetisation vector for stolen "
            "cards — instant delivery, no shipping address required, highly liquid."
        ),
        "confidence": 0.92,
    },
]
