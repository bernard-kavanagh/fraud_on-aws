# Create this file in your app/ or root directory on your EC2 instance.
# Prerequisites:
# pip install mysql-connector-python sentence-transformers python-dotenv boto3 mcp
import mysql.connector
import json
import math
import os
from sentence_transformers import SentenceTransformer
from mysql.connector import Error

# Governed fact layer (AgentCore Gateway) — confirmed fraud/betting verdicts and
# their semantic recall now live in the standalone fact layer, reached over MCP.
# This repo is a caller; adjudication/hashing/audit/embedding are all server-side.
#
# Imported LAZILY: fact_layer_client pulls in boto3 + mcp, which are only needed
# for the fraud/betting fact paths. The product/policy/review demos and the
# dashboards import agent_tools too and must not require those deps — so the
# import happens on first use, not at module load.
from tenancy import resolve_tenant_id, resolve_agent_id


def _fact_layer():
    """Lazy accessor for the fact-layer MCP client (see note above)."""
    import fact_layer_client
    return fact_layer_client

# --- CONFIGURATION ---
# Best Practice: Load Secrets from .env
from dotenv import load_dotenv
load_dotenv()

DB_CONFIG = {
    'host': os.getenv('TIDB_HOST'),
    'port': int(os.getenv('TIDB_PORT', 4000)),
    'user': os.getenv('TIDB_USER'),
    'password': os.getenv('TIDB_PASSWORD'),
    'database': os.getenv('TIDB_DATABASE', 'agentcore_fraud'),
    'ssl_ca': os.getenv('TIDB_SSL_CA'),
    'autocommit': True
}

# --- MODEL ROUTING CONSTANTS ---
# Single source of truth for model selection. Override via .env when testing
# new model versions (e.g. Opus on the explore path for complex fraud).
MODEL_SHORTCUT = os.getenv("TIDB_AGENT_MODEL_SHORTCUT", "claude-haiku-4-5")
MODEL_EXPLORE  = os.getenv("TIDB_AGENT_MODEL_EXPLORE",  "claude-sonnet-4-6")
MODEL_SUMMARY  = os.getenv("TIDB_AGENT_MODEL_SUMMARY",  "claude-haiku-4-5")

# --- COGNITIVE FOUNDATION TUNING ---
# Routing gate thresholds. Raise similarity gate to reduce shortcut rate.
# Lower confidence gate to allow more patterns to shortcut (not recommended).
ROUTING_CONFIDENCE_GATE  = float(os.getenv("ROUTING_CONFIDENCE_GATE",  "0.85"))
ROUTING_SIMILARITY_GATE  = float(os.getenv("ROUTING_SIMILARITY_GATE",  "0.55"))

# Write control (minimum confidence) is NO LONGER enforced here — it moved
# SERVER-SIDE into the fact layer's record_fact (handlers/common/write_control.py).
# Deduplication is retired entirely: canonical subject keys (see
# fact_layer_client.py) make repeat assertions about the same fact collide on
# one (tenant_id, subject, predicate) row and be adjudicated by record_fact,
# so cosine-distance dedup is the wrong mechanism once identity is canonical.

# The local sentence-transformers model is retained ONLY for this repo's own
# demo vector search (sales_knowledge / products / reviews — 384-dim, this
# repo's TiDB). Fraud/betting fact recall is embedded server-side by the fact
# layer (Titan Text Embeddings V2, 1024-dim) and does NOT use this model.
# Load the Embedding Model (Global to avoid reloading per request)
# This runs locally on your EC2 instance.
_model = None

def get_model():
    """Lazy load the model to avoid crashes/delays on import."""
    global _model
    if _model is None:
        print("🧠 Loading Embedding Model for Vector Tools...")
        _model = SentenceTransformer('all-MiniLM-L6-v2')
    return _model

def get_db_connection():
    """Helper to get a fresh connection."""
    return mysql.connector.connect(**DB_CONFIG)

# --- TOOL 1: THE ANALYTICAL ENGINE (SQL) ---
def execute_sql(query: str):
    """
    Executes a standard SQL query.
    Use this for: "Total revenue", "Count orders", "Check inventory".
    """
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)
        
        # Safety: Prevent accidental destruction during demos unless intended
        if "DROP" in query.upper() or "DELETE" in query.upper():
            return "❌ SAFETY BLOCK: Destructive queries are blocked in this demo mode."

        cursor.execute(query)
        results = cursor.fetchall()
        
        if not results:
            return "No results found."
            
        # Limit context window usage by truncating massive results
        return json.dumps(results[:10], default=str)

    except Error as e:
        return f"❌ SQL Error: {e}"
    finally:
        if conn and conn.is_connected():
            conn.close()

# --- TOOL 2: THE SEMANTIC ENGINE (Vector Search) ---
def vector_search(user_query: str, target_table: str = 'sales_knowledge',
                  tenant_id: str = None):
    """
    Performs a semantic search using TiDB Vectors (this repo's own demo data:
    policies / catalog / reviews). Fraud/betting FACT recall is NOT this
    function — that is fact_layer_client.vector_search (server-side embedding).

    Args:
        user_query: The natural language question (e.g. "Can I return a laptop?")
        target_table: 'sales_knowledge' (Policies), 'products' (Catalog) or 'reviews'
        tenant_id: tenant scope; resolved from arg or $DEMO_TENANT_ID (never hardcoded)
    """
    tenant_id = resolve_tenant_id(tenant_id)
    conn = None
    try:
        # 1. Convert text to Vector (Local Inference)
        query_embedding = get_model().encode(user_query).tolist()
        query_vec_str = str(query_embedding)

        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)

        # 2. Construct TiDB Vector Query (tenant-scoped)
        # We use VEC_COSINE_DISTANCE for semantic similarity [Source 93]
        if target_table == 'sales_knowledge':
            sql = """
                SELECT content, category,
                       VEC_COSINE_DISTANCE(embedding, %s) as distance
                FROM sales_knowledge
                WHERE tenant_id = %s
                ORDER BY distance ASC
                LIMIT 3;
            """
            params = (query_vec_str, tenant_id)

        elif target_table == 'products':
            sql = """
                SELECT name, price, description, category,
                       VEC_COSINE_DISTANCE(embedding, %s) as distance
                FROM products
                WHERE tenant_id = %s
                ORDER BY distance ASC
                LIMIT 3;
            """
            params = (query_vec_str, tenant_id)
        elif target_table == 'reviews':
            sql = """
                SELECT r.review_text, r.rating, r.sentiment_label, r.sentiment_score,
                       c.name as customer,
                       VEC_COSINE_DISTANCE(r.embedding, %s) as distance
                FROM reviews r
                JOIN customers c ON r.customer_id = c.customer_id
                WHERE r.tenant_id = %s AND c.tenant_id = %s
                ORDER BY distance ASC
                LIMIT 5;
            """
            params = (query_vec_str, tenant_id, tenant_id)
        else:
            return "❌ Error: Invalid target table for vector search."

        # 3. Execute
        cursor.execute(sql, params)
        results = cursor.fetchall()

        # Format for the LLM
        response = []
        for row in results:
            if target_table == 'reviews':
                response.append(
                    f"[{row['sentiment_label'].upper()} | ⭐{row['rating']}/5 | {row['customer']}] "
                    f"\"{row['review_text']}\" (Confidence: {1 - row['distance']:.2f})"
                )
            else:
                response.append(f"Found: {row.get('content') or row.get('name')} (Confidence: {1 - row['distance']:.2f})")
            
        return "\n".join(response)

    except Error as e:
        return f"❌ Vector DB Error: {e}"
    finally:
        if conn and conn.is_connected():
            conn.close()

# --- TOOL 3: THE WRITE-BACK ENGINE (Fraud Action) ---
def flag_order(order_id: int, reason: str, tenant_id: str = None):
    """
    Flags an order as suspicious, demonstrating agentic write-back.
    Tenant-scoped: an order is only flaggable within its own tenant.
    """
    tenant_id = resolve_tenant_id(tenant_id)
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()

        sql = """
            UPDATE orders
            SET status = 'flagged', flagged_reason = %s
            WHERE order_id = %s AND tenant_id = %s
        """
        cursor.execute(sql, (reason, order_id, tenant_id))
        conn.commit()
        if cursor.rowcount == 0:
            return f"⚠️ Order {order_id} not found for tenant {tenant_id}."
        return f"✅ Order {order_id} flagged successfully."
    except Error as e:
        return f"❌ SQL Error: {e}"
    finally:
        if conn and conn.is_connected():
            conn.close()

def adjust_odds(event_id: int, overloaded_selection: str, tenant_id: str = None):
    """
    Rebalances a betting market by adjusting odds.
    Reduces the overloaded selection's odds by 12% to make it less attractive,
    and increases the opposing side by 8% to draw money across.
    Used when liability concentration exceeds threshold — keeps the market open.
    Tenant-scoped.
    """
    tenant_id = resolve_tenant_id(tenant_id)
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()

        cursor.execute(
            "SELECT home_odds, away_odds FROM betting_events WHERE event_id = %s AND tenant_id = %s",
            (event_id, tenant_id)
        )
        row = cursor.fetchone()
        if not row:
            return f"❌ Event {event_id} not found for tenant {tenant_id}."

        home_odds = float(row[0])
        away_odds = float(row[1])

        if overloaded_selection == 'home':
            new_home = round(home_odds * 0.88, 3)
            new_away = round(away_odds * 1.08, 3)
            summary = f"Home {home_odds} → {new_home} | Away {away_odds} → {new_away}"
        else:
            new_home = round(home_odds * 1.08, 3)
            new_away = round(away_odds * 0.88, 3)
            summary = f"Away {away_odds} → {new_away} | Home {home_odds} → {new_home}"

        cursor.execute(
            "UPDATE betting_events SET home_odds = %s, away_odds = %s WHERE event_id = %s AND tenant_id = %s",
            (new_home, new_away, event_id, tenant_id)
        )
        conn.commit()
        return f"✅ Odds adjusted for event {event_id}. {summary}"
    except Exception as e:
        return f"❌ Error adjusting odds: {e}"
    finally:
        if conn and conn.is_connected():
            conn.close()

def flag_bettor(ip_address: str, tenant_id: str = None):
    """
    Flags all accepted bets from a suspicious IP address as 'flagged'.
    Removes them from the active pool and holds for manual review.
    Demonstrates fraud detection write-back for the sports betting demo.
    Tenant-scoped.
    """
    tenant_id = resolve_tenant_id(tenant_id)
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE bets SET status = 'flagged' WHERE ip_address = %s AND status = 'accepted' AND tenant_id = %s",
            (ip_address, tenant_id)
        )
        affected = cursor.rowcount
        conn.commit()
        return f"✅ IP {ip_address} flagged. {affected} bets held for review."
    except Error as e:
        return f"❌ SQL Error: {e}"
    finally:
        if conn and conn.is_connected():
            conn.close()

def get_suspicious_orders(tenant_id: str = None):
    """
    Uses TiFlash/HTAP to find recent suspicious transaction patterns.
    Examples:
      - Velocity bursts (same IP, many orders in short time)
      - Unusually high value for new accounts
    Tenant-scoped throughout (including the velocity sub-query).
    """
    tenant_id = resolve_tenant_id(tenant_id)
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)

        # HTAP Query: Find IPs with 3+ orders in the last 24h OR single orders > $5000.
        # Limits to 'pending' to avoid re-reviewing. Every clause is tenant-scoped.
        sql = """
            SELECT o.order_id, c.name as customer, o.ip_address, o.amount, o.country, o.order_date
            FROM orders o
            JOIN customers c ON o.customer_id = c.customer_id AND c.tenant_id = o.tenant_id
            WHERE o.tenant_id = %s
            AND o.status = 'pending'
            AND (
                o.amount > 3000
                OR o.ip_address IN (
                    SELECT ip_address FROM orders
                    WHERE tenant_id = %s AND status = 'pending'
                      AND order_date >= NOW() - INTERVAL 1 DAY
                    GROUP BY ip_address HAVING COUNT(*) >= 3
                )
            )
            ORDER BY o.order_date DESC
            LIMIT 5;
        """
        cursor.execute(sql, (tenant_id, tenant_id))
        results = cursor.fetchall()
        
        if not results:
            return "No suspicious orders found."
            
        return json.dumps(results, default=str)
    except Error as e:
        return f"❌ SQL Error: {e}"
    finally:
        if conn and conn.is_connected():
            conn.close()

# --- TOOL 4: REVIEW ANALYTICS (HTAP + ML on Operational Data) ---
def get_review_analytics(tenant_id: str = None):
    """
    Runs HTAP aggregate queries on the reviews table via TiFlash.
    Returns sentiment distribution, per-product ratings, and recent negative reviews.
    This demonstrates real-time ML analytics on operational data — no ETL required.
    Tenant-scoped throughout.
    """
    tenant_id = resolve_tenant_id(tenant_id)
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)

        # Overall sentiment + rating summary (TiFlash columnar scan)
        summary_sql = """
            SELECT /*+ read_from_storage(tiflash[reviews]) */
                COUNT(*)                                                        AS total_reviews,
                ROUND(AVG(rating), 2)                                           AS avg_rating,
                SUM(CASE WHEN sentiment_label = 'positive' THEN 1 ELSE 0 END)  AS positive_count,
                SUM(CASE WHEN sentiment_label = 'neutral'  THEN 1 ELSE 0 END)  AS neutral_count,
                SUM(CASE WHEN sentiment_label = 'negative' THEN 1 ELSE 0 END)  AS negative_count,
                ROUND(AVG(sentiment_score), 3)                                  AS avg_sentiment_score
            FROM reviews
            WHERE tenant_id = %s
        """
        cursor.execute(summary_sql, (tenant_id,))
        summary = cursor.fetchone()

        # Per-product breakdown — surfaces underperforming products
        product_sql = """
            SELECT /*+ read_from_storage(tiflash[reviews]) */
                p.name                                                              AS product_name,
                COUNT(r.review_id)                                                  AS review_count,
                ROUND(AVG(r.rating), 2)                                             AS avg_rating,
                ROUND(AVG(r.sentiment_score), 3)                                    AS avg_sentiment,
                SUM(CASE WHEN r.sentiment_label = 'negative' THEN 1 ELSE 0 END)    AS negative_count
            FROM reviews r
            JOIN products p ON r.product_id = p.product_id AND p.tenant_id = r.tenant_id
            WHERE r.tenant_id = %s AND r.review_type = 'product'
            GROUP BY r.product_id, p.name
            ORDER BY avg_rating DESC
            LIMIT 10
        """
        cursor.execute(product_sql, (tenant_id,))
        product_ratings = cursor.fetchall()

        # Recent negative reviews — churn risk signals
        negative_sql = """
            SELECT r.review_text, r.rating, r.sentiment_score,
                   c.name AS customer, p.name AS product, r.created_at
            FROM reviews r
            JOIN customers c ON r.customer_id = c.customer_id AND c.tenant_id = r.tenant_id
            LEFT JOIN products p ON r.product_id = p.product_id AND p.tenant_id = r.tenant_id
            WHERE r.tenant_id = %s AND r.sentiment_label = 'negative'
            ORDER BY r.created_at DESC
            LIMIT 5
        """
        cursor.execute(negative_sql, (tenant_id,))
        negative_reviews = cursor.fetchall()

        # Sentiment trend over last 7 days
        trend_sql = """
            SELECT /*+ read_from_storage(tiflash[reviews]) */
                DATE(created_at)                    AS review_date,
                ROUND(AVG(sentiment_score), 3)      AS daily_sentiment,
                COUNT(*)                            AS review_count
            FROM reviews
            WHERE tenant_id = %s AND created_at >= NOW() - INTERVAL 7 DAY
            GROUP BY DATE(created_at)
            ORDER BY review_date ASC
        """
        cursor.execute(trend_sql, (tenant_id,))
        trend = cursor.fetchall()

        return json.dumps({
            "summary": summary,
            "product_ratings": product_ratings,
            "recent_negative_reviews": negative_reviews,
            "sentiment_trend_7d": trend
        }, default=str)

    except Error as e:
        return f"❌ SQL Error: {e}"
    finally:
        if conn and conn.is_connected():
            conn.close()


# --- COGNITIVE FOUNDATION: CONTEXT ASSEMBLY ---
#
# assemble_context() is the Thesis 05 function: the platform decides what
# the model sees BEFORE the model sees its first token. Pure SQL. Zero LLM
# calls. Hard token budget. Priority-ordered sources.
#
# Five tiers (fraud adapter):
#   T1  entity profile             ~80 tokens     (customers + risk band)
#   T2  recent transactions        ~100-300       (orders by entity/IP)
#   T3  active investigation       ~100-200       (latest agent_reasoning row)
#   T4  prior investigations       ~200-500       (agent_reasoning history for entity)
#   T5  domain semantic memory     cap 500        (fraud_memory via vector similarity)
#
# Returns (system_context, sources, top_match, vector_matches) — the last two
# feed the routing layer (Stage 3).

CONTEXT_BUDGET_TOKENS = 3600
TIER_CAPS = {
    "t1_entity": 80,
    "t2_recent": 300,
    "t3_active": 200,
    "t4_prior":  500,
    "t5_semantic": 500,
}

def _approx_tokens(text: str) -> int:
    """Cheap token estimate: ~4 chars per token. Good enough for budget gating."""
    return max(1, len(text) // 4)

def _truncate_to_tokens(text: str, max_tokens: int) -> str:
    max_chars = max_tokens * 4
    return text if len(text) <= max_chars else text[:max_chars] + "…"


# Fact-layer status -> routing confidence proxy. fact_current has no per-fact
# confidence field (the decay analog is a deferred design pass), so the governed
# status is the interim confidence signal for route_investigation:
#   active     -> authoritative (cleared the server-side write-control floor)
#   disputed   -> downweighted below the routing gate (equal-authority conflict)
#   superseded/retracted -> excluded (no longer current truth)
_STATUS_CONFIDENCE = {"active": 0.90, "disputed": 0.50}


def _fact_matches_to_routing(results: list) -> list:
    """Adapt fact-layer vector_search results into route_investigation's shape.

    route_investigation expects dicts with confidence / similarity / pattern_id;
    here pattern_id is the governed subject string (the fact identity), and
    confidence is the status-derived proxy above.
    """
    matches = []
    for r in results:
        status = r.get("status") or "active"
        if status in ("superseded", "retracted"):
            continue
        distance = float(r.get("distance", 1.0))
        matches.append({
            "pattern_id": r.get("subject"),          # governed subject = identity
            "subject_id": r.get("subject_id"),
            "confidence": _STATUS_CONFIDENCE.get(status, 0.0),
            "similarity": max(0.0, 1.0 - distance),
            "content": r.get("current_value"),
            "status": status,
        })
    return matches

def assemble_context(entity_ref: str = None, session_id: str = None,
                     trigger_text: str = None, adapter=None,
                     tenant_id: str = None, domain: str = "fraud"):
    """
    Build the agent's prompt context from the substrate. Pure SQL for Tiers 1-4;
    Tier 5 is now a governed-fact-layer semantic recall (server-side embedding).

    Tiers 1, 2, 4 are domain-specific and delegated to the adapter (Thesis 11)
    and are tenant-scoped. Tier 3 (active checkpoint) is substrate-generic
    session state. Tier 5 (semantic memory) is now the AgentCore fact layer:
    fact_layer_client.vector_search, tenant-scoped, embedded server-side —
    NOT the local fraud_memory table.

    Args:
        entity_ref:   customer_id (as str) or ip_address — the focus of the investigation
        session_id:   current agent session, used to find the active reasoning row
        trigger_text: natural-language description of what triggered this run,
                      used as the Tier 5 query text (embedded server-side)
        adapter:      domain adapter module exposing tier_1_entity, tier_2_recent,
                      tier_4_prior. Defaults to adapters.fraud.
        tenant_id:    tenant scope; resolved from arg or $DEMO_TENANT_ID (never hardcoded)
        domain:       "fraud" or "betting" — used only for Tier 5 observability labels

    Returns:
        dict with:
          system_context  — concatenated prompt under the budget
          sources         — per-tier provenance + token cost (for observability)
          top_match       — the highest-ranked Tier 5 fact match, or None
          vector_matches  — list of Tier 5 fact matches (for routing inspection)
    """
    if adapter is None:
        from adapters import fraud as adapter

    tenant_id = resolve_tenant_id(tenant_id)
    sources = {}
    blocks = []
    budget_used = 0
    top_match = None
    vector_matches = []

    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)

        # ----- TIER 1: entity profile + risk band (adapter, tenant-scoped) -----
        t1_text, t1_status = adapter.tier_1_entity(cursor, entity_ref, tenant_id)
        if t1_text:
            t1_text = _truncate_to_tokens(t1_text, TIER_CAPS["t1_entity"])
            t1_cost = _approx_tokens(t1_text)
            if budget_used + t1_cost <= CONTEXT_BUDGET_TOKENS:
                blocks.append(t1_text)
                budget_used += t1_cost
                sources["t1_entity"] = {"tokens": t1_cost, "status": t1_status}
        else:
            sources["t1_entity"] = {"tokens": 0, "status": t1_status}

        # ----- TIER 2: recent transactions for this entity (adapter, tenant-scoped) -----
        t2_lines, t2_status = adapter.tier_2_recent(cursor, entity_ref, tenant_id)
        if t2_lines:
            t2_text = "[T2 recent] " + " | ".join(t2_lines)
            t2_text = _truncate_to_tokens(t2_text, TIER_CAPS["t2_recent"])
            t2_cost = _approx_tokens(t2_text)
            if budget_used + t2_cost <= CONTEXT_BUDGET_TOKENS:
                blocks.append(t2_text)
                budget_used += t2_cost
                sources["t2_recent"] = {"tokens": t2_cost, "count": len(t2_lines), "status": t2_status}
        else:
            sources["t2_recent"] = {"tokens": 0, "status": t2_status}

        # ----- TIER 3: active investigation checkpoint (substrate) -----
        # session_id is a unique UUID bound to one tenant, so this is already
        # single-tenant; the agent_sessions join + tenant_id filter makes the
        # scope explicit (defense in depth) and consistent with Tier 4.
        if session_id:
            cursor.execute(
                """SELECT ar.observation, ar.hypothesis, ar.confidence
                   FROM agent_reasoning ar
                   JOIN agent_sessions s ON s.session_id = ar.session_id
                   WHERE ar.session_id = %s AND s.tenant_id = %s
                   ORDER BY ar.created_at DESC LIMIT 1""",
                (session_id, tenant_id)
            )
            row = cursor.fetchone()
            if row:
                t3_text = (
                    f"[T3 active] obs={row['observation']} | "
                    f"hyp={row['hypothesis']} | conf={row['confidence']}"
                )
                t3_text = _truncate_to_tokens(t3_text, TIER_CAPS["t3_active"])
                t3_cost = _approx_tokens(t3_text)
                if budget_used + t3_cost <= CONTEXT_BUDGET_TOKENS:
                    blocks.append(t3_text)
                    budget_used += t3_cost
                    sources["t3_active"] = {"tokens": t3_cost, "status": "ok"}

        # ----- TIER 4: prior investigations for this entity (adapter) -----
        t4_lines, t4_status = adapter.tier_4_prior(cursor, entity_ref, tenant_id)
        if t4_lines:
            t4_text = "[T4 prior] " + " | ".join(t4_lines)
            t4_text = _truncate_to_tokens(t4_text, TIER_CAPS["t4_prior"])
            t4_cost = _approx_tokens(t4_text)
            if budget_used + t4_cost <= CONTEXT_BUDGET_TOKENS:
                blocks.append(t4_text)
                budget_used += t4_cost
                sources["t4_prior"] = {"tokens": t4_cost, "count": len(t4_lines), "status": t4_status}
        else:
            sources["t4_prior"] = {"tokens": 0, "status": t4_status}

        # ----- TIER 5: domain semantic memory (governed fact layer) -----
        # Server-side embedded recall over the fact layer, tenant-scoped. Returns
        # nearest fact subjects with their current value + status. Note: the fact
        # layer's fact_current has NO per-fact confidence column (that decay analog
        # is a deferred design pass), so routing confidence is derived from the
        # governed `status`: an 'active' governed fact cleared the write-control
        # floor and is authoritative; 'disputed' is downweighted; superseded/
        # retracted are excluded. Similarity = 1 - cosine distance.
        if trigger_text:
            try:
                fl = _fact_layer().vector_search(
                    tenant_id=tenant_id, query_text=trigger_text, top_k=3
                )
                if fl.get("error"):
                    sources["t5_semantic"] = {"tokens": 0, "status": f"degraded:{fl['error']}"}
                else:
                    vector_matches = _fact_matches_to_routing(fl.get("results", []))
                    if vector_matches:
                        # Highest confidence-proxy match feeds the routing layer.
                        top_match = max(vector_matches, key=lambda r: float(r['confidence']))

                        t5_lines = []
                        for r in vector_matches:
                            snippet = str(r.get('content') or '')[:80]
                            t5_lines.append(
                                f"subject={r['pattern_id']} status={r['status']} "
                                f"conf~{r['confidence']:.2f} sim={float(r['similarity']):.2f} "
                                f":: {snippet}"
                            )
                        t5_text = "[T5 fact-layer] " + " | ".join(t5_lines)
                        t5_text = _truncate_to_tokens(t5_text, TIER_CAPS["t5_semantic"])
                        t5_cost = _approx_tokens(t5_text)
                        if budget_used + t5_cost <= CONTEXT_BUDGET_TOKENS:
                            blocks.append(t5_text)
                            budget_used += t5_cost
                            sources["t5_semantic"] = {
                                "tokens": t5_cost,
                                "count": len(vector_matches),
                                "status": "ok",
                                "tier": "fact_layer",
                            }
                    else:
                        sources["t5_semantic"] = {"tokens": 0, "status": "no_fact_matches"}
            except Exception as e:
                sources["t5_semantic"] = {"tokens": 0, "status": f"degraded:{e}"}

    finally:
        if conn and conn.is_connected():
            conn.close()

    return {
        "system_context": "\n".join(blocks),
        "sources": sources,
        "budget_used": budget_used,
        "budget_total": CONTEXT_BUDGET_TOKENS,
        "top_match": top_match,
        "vector_matches": vector_matches,
    }


# --- COGNITIVE FOUNDATION: CUSTODIAL DUTIES ---
#
# The five duties are what turn a vector table into memory (Thesis 03).
# Shipping deduplication first as the demo-visible duty; reconciliation and
# confidence decay follow.

# consolidate_fraud_memory() — RETIRED (task 8).
#
# Cosine-similarity deduplication of fraud_memory rows is deleted. It was the
# wrong mechanism once fact identity is canonical rather than fuzzy-matched:
# with canonical subject keys (fact_layer_client.py), two paraphrased assertions
# about the same fact now resolve to the SAME (tenant_id, subject, predicate)
# identity in the governed fact layer and are handled by record_fact's
# adjudication (corroborate / supersede / dispute) — no embedding-distance merge
# needed. See MEMORY_MAINTENANCE_POC.md (Deduplication entry).


def reinforce_pattern(pattern_id: int) -> bool:
    """
    LEGACY (disconnected by this port). Operated on the local fraud_memory table,
    which the port no longer writes to — semantic memory is now the governed fact
    layer, and its fact_current has no per-fact confidence to reinforce/decay
    against (that analog is a deferred design pass, out of scope). Retained only
    so decay_fraud_memory/compact_fraud_memory below still import cleanly; not
    called on the live investigation path.

    Mark a fraud_memory pattern as reinforced.

    Called by the cognitive loop when a pattern wins the routing gate — the
    pattern was useful, so `last_reinforced_at` bumps and `evidence_count`
    increments. This is the reinforcement signal that Gap 2 of the maintenance
    audit identified: without it, decay treats actively-used patterns the same
    as forgotten ones, so a pattern that drives 100 shortcuts a day would fade
    while a never-matched pattern stays at its initial confidence.

    Returns True if a row was updated (active pattern), False otherwise.
    Best-effort — failures are swallowed by the caller; investigations never
    block on reinforcement.
    """
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            """UPDATE fraud_memory
               SET evidence_count    = evidence_count + 1,
                   last_reinforced_at = NOW()
               WHERE pattern_id = %s AND superseded_by IS NULL""",
            (pattern_id,),
        )
        conn.commit()
        return cursor.rowcount > 0
    except Error:
        return False
    finally:
        if conn and conn.is_connected():
            conn.close()


# reconcile_fraud_memory() — DELETED (task 6).
#
# Reconciliation is now LIVE and handled by the governed fact layer's built-in
# adjudication (aws/handlers/common/adjudication.py), invoked on every
# record_fact write. Single-mode only: a contradiction from a strictly
# higher-authority source auto-supersedes (prior event retained, hash-chained,
# visible in the fact_event audit trail); an equal/lower-authority contradiction
# resolves to `disputed`. There is no local reconciliation stub, no queue, no
# human-review UI, and no evidence-weighted mode — a human-in-loop queue remains
# an explicit POC-phase decision for a real customer conversation, not built
# here. See MEMORY_MAINTENANCE_POC.md (Reconciliation entry).


def decay_fraud_memory(half_life_days: int = 30, dry_run: bool = False,
                       grace_period_days: int = 7, floor: float = 0.30):
    """
    Custodial duty 4 — Confidence Decay. LEGACY (disconnected by this port).

    Operates on the local fraud_memory table, which the port no longer writes to.
    A confidence-decay analog for the governed fact model (fact_current has no
    per-fact confidence field) is a separate design pass, explicitly out of scope
    here. Kept for reference / potential local-tier use; not wired into the app.

    Reduces the confidence of fraud_memory patterns that have not been
    reinforced recently. Reinforcement historically happened via:
      - reinforce_pattern()       when a pattern won the routing gate

    Exponential half-life math:
        new = old * exp( -ln(2) * days_unreinforced / half_life_days )

    Examples (half_life_days = 30):
      - 30 days unreinforced → confidence halves    (0.90 → 0.45)
      - 60 days unreinforced → confidence quarters  (0.90 → 0.225)
      - 90 days unreinforced → ~12.5% of original   (0.90 → 0.113)

    Patterns dropping below ROUTING_CONFIDENCE_GATE stop driving SHORTCUTs.
    Patterns dropping below `floor` (default 0.30) are candidates for
    compaction — they're skipped by future decay passes to avoid further
    erosion of an already-dead pattern.

    Args:
        half_life_days:    time for confidence to halve. Domain-tunable.
        dry_run:           True → preview only, no writes. Always use first.
        grace_period_days: skip patterns reinforced in the last N days.
                           Prevents penalising fresh writes.
        floor:             skip patterns already below this confidence. They've
                           already lost the routing gate; further decay is
                           waste.

    Returns:
        JSON: dry_run, half_life_days, totals (active, eligible, skipped),
              decayed (list of per-pattern preview: pattern_id, days_unreinforced,
              old_confidence, new_confidence, will_lose_routing, content_snippet).
    """
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)

        cursor.execute(
            """SELECT pattern_id, content, confidence, last_reinforced_at,
                      DATEDIFF(NOW(), last_reinforced_at) AS days_unreinforced
               FROM fraud_memory
               WHERE superseded_by IS NULL"""
        )
        rows = cursor.fetchall()

        decayed = []
        skipped_grace = 0
        skipped_floor = 0

        for r in rows:
            days = int(r['days_unreinforced'] or 0)
            old = float(r['confidence'])

            if days < grace_period_days:
                skipped_grace += 1
                continue
            if old < floor:
                skipped_floor += 1
                continue

            decay_factor = math.exp(-math.log(2) * days / half_life_days)
            new = round(old * decay_factor, 3)

            decayed.append({
                "pattern_id": r['pattern_id'],
                "days_unreinforced": days,
                "old_confidence": old,
                "new_confidence": new,
                "will_lose_routing": new < ROUTING_CONFIDENCE_GATE,
                "content_snippet": (r['content'][:60] + "…") if len(r['content']) > 60 else r['content'],
            })

        if not dry_run and decayed:
            for item in decayed:
                cursor.execute(
                    "UPDATE fraud_memory SET confidence = %s WHERE pattern_id = %s",
                    (item['new_confidence'], item['pattern_id']),
                )
            conn.commit()

        return json.dumps({
            "dry_run": dry_run,
            "half_life_days": half_life_days,
            "grace_period_days": grace_period_days,
            "floor": floor,
            "total_active": len(rows),
            "eligible_decayed": len(decayed),
            "skipped_grace_period": skipped_grace,
            "skipped_below_floor": skipped_floor,
            "patterns": decayed,
        }, default=str)
    except Error as e:
        return json.dumps({"status": "error", "error": str(e)})
    finally:
        if conn and conn.is_connected():
            conn.close()


def compact_fraud_memory():
    """
    Custodial duty 5 — Compaction.

    Removes or archives fraud_memory rows that are:
      - superseded (superseded_by IS NOT NULL) and older than 90 days
      - decayed below confidence 0.50 and unreinforced for > 60 days
      - exact duplicates missed by the deduplication threshold

    Compaction keeps the semantic memory store lean so vector search latency
    does not grow unboundedly as the system accumulates patterns.

    Intended to run as a scheduled job (weekly or monthly), not inline with
    the investigation loop.

    Not yet implemented.
    """
    raise NotImplementedError("compact_fraud_memory() not yet implemented.")


# --- COGNITIVE FOUNDATION: ROUTING LAYER ---
#
# Thesis 06 — the substrate decides which model runs, not the model itself.
#
# After assemble_context(), inspect the top fraud_memory match. If it passes
# BOTH gates, route the investigation to the shortcut path: cheaper model,
# tighter round budget, pre-classify skipped (the substrate already classified
# the trigger by retrieving its match). Otherwise, explore path.

ROUTE_SHORTCUT = {
    "path": "SHORTCUT",
    "model": MODEL_SHORTCUT,
    "max_tool_rounds": 3,
}
ROUTE_EXPLORE = {
    "path": "EXPLORE",
    "model": MODEL_EXPLORE,
    "max_tool_rounds": 15,
}

def route_investigation(vector_matches, confidence_gate=None, similarity_gate=None):
    """
    Decide which model + round budget runs the investigation loop.

    Canonical pattern (AGENT_LIFECYCLE.md §1 STEP 2): scan Tier 5 matches
    for ANY entry passing both gates. Earlier versions of this function
    inspected a single pre-selected top_match — a higher-similarity match
    with marginally lower confidence could be wrongly excluded. The scan
    fixes that.

    Accepts either:
      - a list of vector_matches dicts (canonical, preferred)
      - a single dict (legacy compatibility — wraps as a one-element list)
      - None / empty list (cold start — explore path)

    Gate precedence (highest first):
      1. confidence_gate / similarity_gate kwargs (caller override)
      2. ROUTING_CONFIDENCE_GATE / ROUTING_SIMILARITY_GATE (env or module default)
    Adapter-level overrides are resolved by the caller (run_investigation in
    cognitive_loop.py) before invoking this function — that keeps the function
    pure and adapter-agnostic.

    If multiple rows pass both gates, prefer the one with the highest
    combined confidence × similarity (deterministic, surfaced in reason).

    Returns: path, model, max_tool_rounds, reason, gates, matched_pattern_id.
    """
    cg = ROUTING_CONFIDENCE_GATE if confidence_gate is None else float(confidence_gate)
    sg = ROUTING_SIMILARITY_GATE if similarity_gate is None else float(similarity_gate)

    if vector_matches is None:
        candidates = []
    elif isinstance(vector_matches, dict):
        candidates = [vector_matches]
    else:
        candidates = list(vector_matches)

    if not candidates:
        return {
            **ROUTE_EXPLORE,
            "reason": "no fact-layer matches returned by assemble_context",
            "gates": {"confidence": None, "similarity": None,
                      "confidence_gate": cg, "similarity_gate": sg},
            "scanned": 0,
            "matched_pattern_id": None,
        }

    passing = []
    for m in candidates:
        conf = float(m.get("confidence") or 0)
        sim = float(m.get("similarity") or 0)
        if conf >= cg and sim >= sg:
            passing.append((conf * sim, conf, sim, m))

    if passing:
        # Deterministic best: highest (conf × sim), tie-broken by similarity.
        passing.sort(key=lambda t: (t[0], t[2]), reverse=True)
        best_score, best_conf, best_sim, best = passing[0]
        return {
            **ROUTE_SHORTCUT,
            "reason": (
                f"{len(passing)}/{len(candidates)} fact-layer matches passed both gates "
                f"(conf≥{cg}, sim≥{sg}); chose subject {best.get('pattern_id')} "
                f"(conf={best_conf:.2f}, sim={best_sim:.2f}, score={best_score:.3f}) "
                f"— substrate-driven shortcut"
            ),
            "gates": {
                "confidence": best_conf, "similarity": best_sim,
                "confidence_gate": cg, "similarity_gate": sg,
            },
            "scanned": len(candidates),
            "passed": len(passing),
            "matched_pattern_id": best.get("pattern_id"),
        }

    # Surface the closest miss for observability — operator sees which row
    # was nearest the gate and why it failed.
    closest = max(
        candidates,
        key=lambda m: (float(m.get("confidence") or 0) >= cg,
                       float(m.get("similarity") or 0)),
    )
    c_conf = float(closest.get("confidence") or 0)
    c_sim = float(closest.get("similarity") or 0)
    failing = []
    if c_conf < cg:
        failing.append(f"conf={c_conf:.2f}<{cg}")
    if c_sim < sg:
        failing.append(f"sim={c_sim:.2f}<{sg}")
    return {
        **ROUTE_EXPLORE,
        "reason": (
            f"scanned {len(candidates)} matches; none passed both gates. "
            f"Closest: subject {closest.get('pattern_id')} (" + ", ".join(failing) + ")"
        ),
        "gates": {
            "confidence": c_conf, "similarity": c_sim,
            "confidence_gate": cg, "similarity_gate": sg,
        },
        "scanned": len(candidates),
        "passed": 0,
        "matched_pattern_id": None,
    }


# --- COGNITIVE FOUNDATION: SEMANTIC MEMORY OPERATIONS ---

def recall_similar_fraud(query_text: str, scope: str = None, entity_ref: str = None,
                         k: int = 5, tenant_id: str = None):
    """
    Retrieve confirmed fraud/betting verdicts from the governed fact layer via
    tenant-scoped, server-side-embedded semantic recall (Titan V2, 1024-dim).

    This replaces the local fraud_memory vector query. The fact layer's
    vector_search is tenant-scoped only (no scope/entity_ref filter server-side);
    `scope`/`entity_ref` are accepted for call-site compatibility and used to
    focus the query text, but the authoritative filter is the tenant claim.

    assemble_context() already surfaces the top matches in Tier 5;
    recall_similar_fraud() is the on-demand version the agent loop can call for a
    broader pull.
    """
    tenant_id = resolve_tenant_id(tenant_id)
    # entity_ref sharpens the semantic query toward the entity of interest.
    q = f"{query_text} {entity_ref}".strip() if entity_ref else query_text
    try:
        fl = _fact_layer().vector_search(tenant_id=tenant_id, query_text=q, top_k=k)
        if fl.get("error"):
            return f"❌ Recall Error (fact layer): {fl['error']}"
        results = fl.get("results", [])
        if not results:
            return "No similar fraud patterns in the governed fact layer."
        return json.dumps([
            {
                "subject": r.get("subject"),
                "predicate": r.get("predicate"),
                "current_value": r.get("current_value"),
                "status": r.get("status"),
                "similarity": round(max(0.0, 1.0 - float(r.get("distance", 1.0))), 4),
            }
            for r in results
        ], default=str)
    except Exception as e:
        return f"❌ Recall Error: {e}"


_DEFAULT_VERDICT = {"fraud": "confirmed_fraud", "betting": "liability_flagged"}


def compound_resolution(content: str, confidence: float = 0.85,
                        scope: str = "global", entity_ref: str = None,
                        tenant_id: str = None, agent_id: str = None,
                        session_id: str = None, domain: str = "fraud",
                        verdict: str = None, source: str = None,
                        evidence: list = None, assessor_type: str = "agent",
                        human: bool = False):
    """
    Persist a confirmed verdict to the governed fact layer via record_fact, as a
    full Evidence -> Assessment -> Resolution write.

    The model proposes a resolution; this maps it onto a governed fact and calls
    record_fact, which embeds server-side, runs predicate-specific authority
    adjudication, appends a hash-chained event, attaches evidence references, and
    upserts current truth in one ACID transaction.

    Mapping (see fact_layer_client.py):
      subject       : entity_subject(domain, entity_ref) for an entity verdict;
                      catalog_subject(domain, content) for a scope='global' pattern.
      predicate     : FRAUD_PREDICATE / BETTING_PREDICATE (fraud_status / liability_status,
                      which carry predicate-specific authority in the fact layer).
      value         : the scalar verdict label so agreements corroborate and
                      disagreements adjudicate (cleared vs confirmed, etc).
      source        : agent identity by default (agent_inference); a human review
                      uses human_investigator (top authority) when human=True or
                      source is given.
      evidence      : references back to the fraud domain (transaction_id, alert_id,
                      investigation_id, ...) — {evidence_type, evidence_ref, ...}.
      context_summary: the banded description -> folded into the semantic embedding.
      confidence    : passed through; write-control floor enforced SERVER-SIDE.
      idempotency_key: derived so a retried resolution does not double-write.
    """
    tenant_id = resolve_tenant_id(tenant_id)
    agent_id = resolve_agent_id(agent_id)
    fl = _fact_layer()

    predicate = fl.FRAUD_PREDICATE if domain == "fraud" else fl.BETTING_PREDICATE
    entity_type = None
    if entity_ref:
        subject = fl.entity_subject(domain, entity_ref)
        value = verdict or _DEFAULT_VERDICT.get(domain, "confirmed")
        # Derive a canonical entity_type from the subject shape (customer/ip/...).
        entity_type = subject.split(":", 2)[1] if subject.count(":") >= 2 else "entity"
    else:
        subject = fl.catalog_subject(domain, content)
        value = verdict or content
        entity_type = "pattern"

    # A human resolution outranks the agent's inference for fraud_status.
    src = source or (fl.SOURCE_HUMAN if human else fl.SOURCE_AGENT)
    resolved_assessor = "human" if (human or src == fl.SOURCE_HUMAN) else assessor_type
    # Stable idempotency key so a retried identical resolution replays, not doubles.
    idem = f"{session_id or 'nosession'}:{subject}:{value}"

    try:
        res = fl.record_fact(
            tenant_id=tenant_id, subject=subject, predicate=predicate,
            value=value, source=src, confidence=confidence,
            agent_id=agent_id, session_id=session_id,
            entity_type=entity_type, canonical_key=(entity_ref or subject),
            assessor_type=resolved_assessor, evidence=evidence,
            context_summary=content, idempotency_key=idem,
        )
        if res.get("error"):
            return f"❌ record_fact rejected: {res['error']}"
        replay = " (idempotent replay)" if res.get("idempotent_replay") else ""
        return (
            f"✅ fact recorded — subject={subject} predicate={predicate} "
            f"outcome={res.get('outcome')} status={res.get('status')} "
            f"policy={res.get('policy_version')} event_id={res.get('event_id')}"
            f"{replay}"
            + (f" competing={res['competing_event_ids']}"
               if res.get("competing_event_ids") else "")
        )
    except Exception as e:
        return f"❌ Compound Error (fact layer): {e}"


def explain_fact(entity_ref: str, domain: str = "fraud", tenant_id: str = None):
    """Ask the governed fact layer WHY the current verdict for an entity holds.

    Returns the full provenance (resolution, authority, supporting vs contrary
    evidence, assessments, history) so the agent can answer "why do we believe
    this?" rather than just restating a verdict. Read-only, tenant-scoped.
    """
    tenant_id = resolve_tenant_id(tenant_id)
    fl = _fact_layer()
    predicate = fl.FRAUD_PREDICATE if domain == "fraud" else fl.BETTING_PREDICATE
    subject = fl.entity_subject(domain, entity_ref)
    try:
        res = fl.explain_fact(tenant_id=tenant_id, subject=subject, predicate=predicate)
        if res.get("error"):
            return f"No governed fact for {subject}/{predicate}: {res['error']}"
        return json.dumps(res, default=str)
    except Exception as e:
        return f"❌ explain_fact error (fact layer): {e}"


def seed_fraud_memory_from_adapter(adapter=None, tenant_id: str = None,
                                   domain: str = "fraud"):
    """
    Load an adapter's SEED_CATALOG into the governed fact layer for a tenant.

    Mirrors the EV adapter's outage_catalog seeding pattern: a fresh cluster
    skips the warm-up curve by bringing canonical patterns to invocation 1, so
    Tier 5 recall (fact_layer vector_search) can match from day one.

    Each catalog entry is recorded as a governed fact with a catalog subject
    (<domain>:pattern:<slug>) and source=verified_integration (a ranked seed
    source). Idempotency is now the fact layer's job: re-seeding the same
    subject+value corroborates rather than duplicating current truth — there is
    no local cosine-distance dedup anymore (retired, task 8).
    """
    if adapter is None:
        from adapters import fraud as adapter

    tenant_id = resolve_tenant_id(tenant_id)
    seed_source = _fact_layer().SOURCE_SEED
    seeded = []
    for entry in getattr(adapter, "SEED_CATALOG", []):
        result = compound_resolution(
            content=entry["content"],
            confidence=entry.get("confidence", 0.85),
            scope=entry.get("scope", "global"),
            entity_ref=entry.get("entity_ref"),
            tenant_id=tenant_id,
            domain=domain,
            source=seed_source,
        )
        seeded.append(result)
    return json.dumps({"tenant_id": tenant_id, "domain": domain, "seeded": seeded},
                      default=str)


def write_reasoning_checkpoint(session_id: str, observation: str, hypothesis: str,
                               evidence_refs: list = None, confidence: float = 0.0,
                               resolution: str = None):
    """
    Write a structured episodic checkpoint to agent_reasoning.

    NOT a transcript. Each row is the agent's distilled state at a decision
    point. Stage 5 (slim summary) reads the latest row and produces the
    investigation report from these five fields — without replaying the
    loop conversation.
    """
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            """INSERT INTO agent_reasoning
                 (session_id, observation, hypothesis, evidence_refs, confidence, resolution)
               VALUES (%s, %s, %s, %s, %s, %s)""",
            (session_id, observation, hypothesis,
             json.dumps(evidence_refs or []), confidence, resolution)
        )
        new_id = cursor.lastrowid
        conn.commit()
        return f"✅ agent_reasoning checkpoint {new_id} written."
    except Error as e:
        return f"❌ Checkpoint Error: {e}"
    finally:
        if conn and conn.is_connected():
            conn.close()


# --- TOOL 5: THE STATE MACHINE (Memory) ---
def create_session(session_id: str, tenant_id: str, user_id: str = "guest",
                   metadata: dict = None):
    """
    Initializes a new session in TiDB to satisfy Foreign Key constraints.

    tenant_id is REQUIRED: it is the workflow data scope every downstream
    episodic/workflow read filters on (Tier 3 active checkpoint, Tier 4 prior
    investigations, the slim-summary read). It is application DATA SCOPE — NOT the
    authenticated principal identity — and it must be threaded in from the
    already-established investigation tenant, never inferred from a customer id.
    An empty/blank tenant fails closed (ValueError) rather than writing an
    un-scoped session that Tier 4 could later leak across tenants.

    Convention for cognitive-foundation investigations:
      - Set user_id = str(entity_ref) (customer_id or ip_address) when starting
        an entity-focused investigation. Tier 4 in assemble_context joins on
        agent_sessions.user_id AND agent_sessions.tenant_id; sessions created with
        'guest' or a UI-level label will silently return no prior investigations
        for that entity.

    Audit-trail conventions (metadata JSON):
      - source              : where the session was created from. Examples:
                              "agent_ui.bootstrap" (Streamlit UI start),
                              "agent_ui.investigation" (Admin fraud invest),
                              "betting_investigation.cli", "run_agent.cli".
      - parent_session_id   : OPTIONAL. When a session spawns a child session
                              (e.g. UI bootstrap spawns an entity-scoped
                              investigation), the child writes the parent's
                              session_id here. Enables full lineage queries:
                                SELECT session_id, JSON_EXTRACT(metadata, '$.source'),
                                       JSON_EXTRACT(metadata, '$.parent_session_id')
                                FROM agent_sessions WHERE session_id = X;

    Both fields are metadata-only (no schema change). Elevate to columns if
    you want indexed lineage queries; the data is already there.
    """
    # Fail closed: workflow context must never be created un-scoped.
    if not tenant_id or not str(tenant_id).strip():
        raise ValueError(
            "create_session requires an explicit non-empty tenant_id (workflow "
            "data scope). Refusing to create an un-scoped session — Tier 4 prior-"
            "investigation recall filters on it and would otherwise cross tenants."
        )
    tenant_id = str(tenant_id).strip()

    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()

        sql = """
            INSERT INTO agent_sessions (session_id, tenant_id, user_id, metadata)
            VALUES (%s, %s, %s, %s)
        """
        # Honest metadata. Caller-supplied; we don't fabricate a source.
        meta = json.dumps(metadata or {})

        cursor.execute(sql, (session_id, tenant_id, user_id, meta))
        conn.commit()
        print(f"✅ Session {session_id} created (tenant={tenant_id}).")

    except Error as e:
        print(f"❌ Session Creation Failed: {e}")
    finally:
        if conn and conn.is_connected():
            conn.close()

def log_interaction(session_id: str, role: str, content: str, tool_used: str = None):
    """
    Saves the Agent's 'Thoughts' to TiDB for RCA (Root Cause Analysis).
    """
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        
        sql = """
            INSERT INTO chat_history (session_id, role, content, metadata)
            VALUES (%s, %s, %s, %s)
        """
        meta = json.dumps({"tool": tool_used}) if tool_used else None
        
        cursor.execute(sql, (session_id, role, content, meta))
        conn.commit()
        return "✅ Memory Saved."

    except Error as e:
        print(f"Logging Failed: {e}")
    finally:
        if conn and conn.is_connected():
            conn.close()

# --- TEST HARNESS (Run this to verify before deploying) ---
# Requires DEMO_TENANT_ID set, plus fact-layer env (FACT_LAYER_GATEWAY_URL +
# Cognito creds) for the recall/seed/Tier-5 checks that hit the Gateway.
if __name__ == "__main__":
    from tenancy import DEMO_TENANT_ALPHA
    tid = os.getenv("DEMO_TENANT_ID", DEMO_TENANT_ALPHA)

    print("🧪 Testing Vector Tool (Policy, tenant-scoped)...")
    print(vector_search("What is the return policy for gaming laptops?",
                        "sales_knowledge", tenant_id=tid))

    print("\n🧪 Testing Vector Tool (Products, tenant-scoped)...")
    print(vector_search("Something specifically for video editing",
                        "products", tenant_id=tid))

    print("\n🧪 Testing seed_fraud_memory_from_adapter() -> fact layer...")
    print(seed_fraud_memory_from_adapter(tenant_id=tid, domain="fraud"))

    print("\n🧪 Testing recall_similar_fraud() via fact layer...")
    print(recall_similar_fraud("velocity burst same IP multiple orders card testing",
                               tenant_id=tid))

    print("\n🧪 Testing route_investigation() — cold (None matches)...")
    print(route_investigation(None))

    print("\n🧪 Testing route_investigation() — warm (synthetic passing match)...")
    print(route_investigation([{
        "pattern_id": "fraud:pattern:velocity-burst", "confidence": 0.90, "similarity": 0.72
    }]))

    print("\n🧪 Testing assemble_context() — cold start (Tier 5 = fact layer)...")
    ctx = assemble_context(trigger_text="suspicious orders velocity burst", tenant_id=tid)
    print(f"  budget_used={ctx['budget_used']}/{ctx['budget_total']}")
    print(f"  sources={ctx['sources']}")
    print(f"  top_match={'found' if ctx['top_match'] else 'none'}")
    print(f"  vector_matches={len(ctx['vector_matches'])} rows")

    print("\n🧪 Testing compound_resolution() -> record_fact (entity verdict)...")
    print(compound_resolution("velocity burst confirmed on this IP",
                              confidence=0.90, entity_ref="185.15.54.22",
                              tenant_id=tid, domain="fraud"))
