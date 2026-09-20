"""
Betting domain adapter.

Second proof of Thesis 11 — the pattern is generic, the domain is a plugin.
Same substrate (assemble_context, routing, custodial duties, agent loop) is
reused as-is; only the tier callables, banding, and seed catalog change.

Defines:
  - tier_1_entity:   bettor profile + (placeholder) risk band
  - tier_2_recent:   recent bets for entity/IP
  - tier_4_prior:    prior betting investigations for the entity
  - risk_band:       placeholder banding rule (betting has no VIP tier today)
  - SEED_CATALOG:    three canonical betting fraud patterns

Session convention (same as fraud adapter): create_session() must be called
with user_id=str(entity_ref) for tier_4_prior to surface prior runs.
"""

from mysql.connector import Error


# ---------------------------------------------------------------------------
# ROUTING CALIBRATION (per-adapter overrides)
# ---------------------------------------------------------------------------
# Betting triggers tend to be longer and more specific ("opposing home/away
# bets within seconds from one IP") than fintech ones, so the EV-style 0.55
# similarity gate is honest here. Tighter than fraud's 0.45 — the betting
# patterns are domain-narrow and we don't want a fraud-style velocity trigger
# accidentally shortcutting through the arbitrage pattern.
# ---------------------------------------------------------------------------
SIMILARITY_GATE = 0.55
CONFIDENCE_GATE = 0.85


# ---------------------------------------------------------------------------
# SCHEMA HINT (composed into the agent's system prompt)
# ---------------------------------------------------------------------------
# Listed to save the agent from burning tool rounds on DESCRIBE queries
# during a betting investigation. Customer table is shared with fraud;
# betting-specific tables are bets / betting_events / customer_bonuses /
# odds_history.
# ---------------------------------------------------------------------------
SCHEMA_HINT = """\
Available tables (betting adapter, sports book). EVERY domain table is
tenant-scoped by a tenant_id column; all reads are already filtered to your
tenant (execute_sql is retired).
  customers(tenant_id, customer_id, name, email, email_domain, region, vip_status,
            signup_date, timezone)  -- shared with fraud adapter
  bets(tenant_id, bet_id, customer_id, event_id, selection, stake, odds,
       potential_payout, status, ip_address, placed_at)
  betting_events(tenant_id, event_id, sport, home_team, away_team, league, event_time,
                 status, home_odds, away_odds, draw_odds)
  customer_bonuses(tenant_id, bonus_id, customer_id, bonus_type, claimed_at,
                   qualifying_session)
  odds_history(tenant_id, history_id, event_id, recorded_at, home_odds, away_odds,
               draw_odds, suspended)

Semantic memory is now the GOVERNED FACT LAYER (AgentCore Gateway): use
recall_similar_fraud for tenant-scoped semantic recall and compound_resolution to
persist a confirmed liability/verdict (adjudicated + audited server-side). Local
episodic checkpoints still live in:
  agent_reasoning(reasoning_id, session_id, observation, hypothesis,
                  evidence_refs JSON, confidence, resolution, created_at)
"""


def risk_band(stake_history_count: int = 0) -> str:
    """
    Placeholder banding for betting. Bettors with substantial accepted-bet
    history are treated as STANDARD; new accounts are FRESH (cold profile —
    no history to validate sharp money or arbitrage shapes against).
    """
    return "STANDARD" if stake_history_count >= 20 else "FRESH"


def tier_1_entity(cursor, entity_ref: str, tenant_id: str) -> tuple[str, str]:
    """Build the Tier 1 bettor-profile block (tenant-scoped)."""
    if not entity_ref:
        return "", "degraded_no_entity"

    # Try as customer_id first
    try:
        cursor.execute(
            "SELECT customer_id, name, region FROM customers "
            "WHERE customer_id = %s AND tenant_id = %s LIMIT 1",
            (int(entity_ref), tenant_id),
        )
        row = cursor.fetchone()
        if row:
            cursor.execute(
                "SELECT COUNT(*) AS n FROM bets "
                "WHERE customer_id = %s AND status = 'accepted' AND tenant_id = %s",
                (int(entity_ref), tenant_id),
            )
            count_row = cursor.fetchone()
            n = int(count_row['n']) if count_row else 0
            band = risk_band(n)
            return (
                f"[T1 bettor] customer_id={row['customer_id']} name={row['name']} "
                f"region={row['region']} accepted_bets={n} risk_band={band}",
                "ok",
            )
    except (ValueError, Error):
        pass

    # Fall back to IP scope
    try:
        cursor.execute(
            "SELECT COUNT(*) AS n FROM bets WHERE ip_address = %s AND tenant_id = %s",
            (entity_ref, tenant_id),
        )
        row = cursor.fetchone()
        if row:
            return (
                f"[T1 bettor] ip_address={entity_ref} historic_bets={row['n']} risk_band=IP_SCOPE",
                "ok",
            )
    except Error:
        pass

    return "", "degraded_lookup_failed"


def tier_2_recent(cursor, entity_ref: str, tenant_id: str) -> tuple[list[str], str]:
    """Build the Tier 2 recent-bets block (tenant-scoped)."""
    if not entity_ref:
        return [], "degraded_no_entity"

    lines = []
    try:
        cursor.execute(
            """SELECT bet_id, event_id, selection, stake, odds, status, placed_at
               FROM bets
               WHERE customer_id = %s AND tenant_id = %s
               ORDER BY placed_at DESC LIMIT 5""",
            (int(entity_ref), tenant_id),
        )
        for r in cursor.fetchall():
            lines.append(
                f"bet={r['bet_id']} event={r['event_id']} sel={r['selection']} "
                f"stake=${r['stake']} odds={r['odds']} status={r['status']} at={r['placed_at']}"
            )
    except (ValueError, Error):
        try:
            cursor.execute(
                """SELECT bet_id, event_id, selection, stake, odds, status, placed_at
                   FROM bets
                   WHERE ip_address = %s AND tenant_id = %s
                   ORDER BY placed_at DESC LIMIT 5""",
                (entity_ref, tenant_id),
            )
            for r in cursor.fetchall():
                lines.append(
                    f"bet={r['bet_id']} event={r['event_id']} sel={r['selection']} "
                    f"stake=${r['stake']} odds={r['odds']} status={r['status']} at={r['placed_at']}"
                )
        except Error:
            return [], "degraded_lookup_failed"

    return lines, "ok" if lines else "no_recent_activity"


def tier_4_prior(cursor, entity_ref: str, tenant_id: str = None) -> tuple[list[str], str]:
    """Build the Tier 4 prior-investigations block — same shape as fraud.

    Reads episodic/workflow tables keyed by user_id=entity_ref, which is NOT
    unique across tenants, so the query MUST also filter on
    agent_sessions.tenant_id to prevent cross-tenant leakage. Fails closed when
    the tenant scope is missing.
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
# SEED CATALOG — Betting (3 canonical patterns)
# ---------------------------------------------------------------------------
# Schema-grounded against the Phase A extensions:
#   - arbitrage bot:        bets, betting_events
#   - bonus abuse:          customer_bonuses
#   - insider sharp money:  bets, odds_history
# ---------------------------------------------------------------------------

SEED_CATALOG = [
    {
        "scope": "global",
        "content": (
            "Arbitrage bot — cross-market: single IP placing opposing bets on the same "
            "event across multiple selections (home + draw, or home + away) within "
            "seconds of each other, with stakes sized to guarantee a small positive "
            "return regardless of outcome. Distinguishable from legitimate hedging by "
            "the sub-5-second placement window and the mathematical stake ratio."
        ),
        "confidence": 0.93,
    },
    {
        "scope": "global",
        "content": (
            "Multi-accounting — bonus abuse: 3+ accounts sharing an IP or device "
            "fingerprint, each claiming a first-deposit bonus, with deposits and "
            "bonus-qualifying bets placed within the same session. The accounts are "
            "distinct but the session fingerprint connects them. Pattern is consistent "
            "with deliberate bonus farming rather than family use of a shared network."
        ),
        "confidence": 0.91,
    },
    {
        "scope": "global",
        "content": (
            "Insider knowledge — sharp money: sudden high-stake bet ($5,000+) on a "
            "low-liquidity market placed 10-30 minutes before a significant odds "
            "movement or line suspension, with no prior betting history on that sport "
            "or league. Time proximity to the market move combined with the first-time "
            "sport profile is the distinguishing signal from normal sharp betting."
        ),
        "confidence": 0.86,
    },
]
