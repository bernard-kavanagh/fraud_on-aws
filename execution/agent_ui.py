import os

# FIX 1: Prevent tokenizer parallelism crashes (common on Mac + Streamlit)
os.environ["TOKENIZERS_PARALLELISM"] = "false"

import streamlit as st
import sys
import os
import uuid
import json
import time
import re

# Add parent directory to path to import agent_tools
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import anthropic

from agent_tools import execute_sql, vector_search, log_interaction, create_session, flag_order, get_suspicious_orders, get_review_analytics, seed_fraud_memory_from_adapter
from cognitive_loop import run_investigation
from tenancy import DEMO_TENANTS, DEMO_TENANT_ALPHA

# --- CONFIGURATION ---
st.set_page_config(page_title="TiDB Unified Agent", layout="wide")

# --- SESSION STATE ---
if 'session_id' not in st.session_state:
    st.session_state['session_id'] = str(uuid.uuid4())
    create_session(
        st.session_state['session_id'],
        user_id="demo_user_ui",
        metadata={"source": "agent_ui.bootstrap",
                  "tenant_id": os.getenv("DEMO_TENANT_ID", DEMO_TENANT_ALPHA)},
    )
    st.session_state['messages'] = []
    st.session_state['chain_of_thought'] = []

# --- SIDEBAR: CHAIN OF THOUGHT ---
st.sidebar.title("🧠 Agent Memory")
st.sidebar.caption(f"Session ID: {st.session_state['session_id']}")

# Tenant selector (task 9) — every domain read/write and every governed-fact
# call below is scoped to this tenant. Switching tenants demonstrates isolation
# (and, against the fact layer, a live cross-tenant Cedar deny if a token's
# tenant_id claim disagrees).
tenant_id = st.sidebar.selectbox("🏦 Tenant", DEMO_TENANTS)
st.sidebar.caption(f"Acting as tenant: `{tenant_id}`")

# Role Selector
user_role = st.sidebar.selectbox(
    "User Role",
    ("Customer (Bernard)", "Admin")
)

# Auto-clear chat when role changes — avoids stale context bleeding between views
if 'active_role' not in st.session_state:
    st.session_state['active_role'] = user_role
if st.session_state['active_role'] != user_role:
    st.session_state['active_role'] = user_role
    st.session_state['messages'] = []
    st.session_state['chain_of_thought'] = []
    st.rerun()

if st.sidebar.button("Clear Memory"):
    st.session_state['messages'] = []
    st.session_state['chain_of_thought'] = []
    st.rerun()

# Seed the governed fact layer from the adapter's SEED_CATALOG — gives a cold
# tenant the same shortcut behaviour as a warmed cluster on day one. Seeds the
# CURRENTLY-SELECTED tenant (canonical catalog subjects; the fact layer embeds
# server-side and adjudicates duplicates).
if st.sidebar.button("🌱 Seed fact layer (adapter catalog)"):
    try:
        seed_result = seed_fraud_memory_from_adapter(tenant_id=tenant_id, domain="fraud")
        parsed = json.loads(seed_result)
        st.sidebar.success(
            f"Seeded {len(parsed.get('seeded', []))} catalog patterns "
            f"into the fact layer for `{tenant_id}`."
        )
        st.session_state['chain_of_thought'].append({
            "step": "🌱 Fact-layer seed",
            "content": f"Recorded canonical catalog patterns as governed facts for {tenant_id}.",
            "tool": json.dumps(parsed.get("seeded", []), default=str, indent=2)[:1200],
        })
    except Exception as e:
        st.sidebar.error(f"Seed failed: {e}")

# NOTE (port): the local custodial-duty buttons (Deduplication, Confidence
# Decay) are retired here. Deduplication is gone — canonical subject keys mean
# repeat assertions collide on one governed fact identity and are adjudicated by
# record_fact (no cosine-distance merge). Confidence decay has no analog yet on
# the fact model (fact_current has no per-fact confidence field) — a deferred
# design pass, explicitly out of scope for this port. Reconciliation is now LIVE
# and automatic inside record_fact (auto-supersede-by-authority or dispute).
st.sidebar.caption(
    "Reconciliation: LIVE (auto supersede/dispute in record_fact). "
    "Dedup: retired (canonical subject keys). "
    "Decay: deferred design pass."
)

st.sidebar.divider()
st.sidebar.subheader("Recent Thoughts")

# Display Chain of Thought in Sidebar
for thought in st.session_state['chain_of_thought']:
    with st.sidebar.expander(f"{thought['step']}", expanded=True):
        st.write(thought['content'])
        if thought.get('tool'):
            st.code(thought['tool'], language='json')

# --- MAIN CHAT INTERFACE ---
st.title("🤖 TiDB Unified Agent")
st.caption(f"Powered by TiDB Serverless (HTAP + Vector) | Mode: {user_role}")

# Display Chat History
for message in st.session_state['messages']:
    with st.chat_message(message["role"]):
        st.markdown(message["content"])

# User Input
if prompt := st.chat_input("Ask about orders, products, or policies..."):
    # 1. Add User Message to Chat
    st.session_state['messages'].append({"role": "user", "content": prompt})
    with st.chat_message("user"):
        st.markdown(prompt)
        
    # Log to DB
    log_interaction(st.session_state['session_id'], 'user', prompt)
    
    # 2. Agent Processing (The Loop)
    with st.chat_message("assistant"):
        message_placeholder = st.empty()
        full_response = ""
        
        # --- INTENT: FLAG ORDER ---
        # Detect "flag order <id>" before running the normal SQL/vector pipeline
        flag_match = re.search(r'\b(\d+)\b', prompt)
        if 'flag' in prompt.lower() and flag_match:
            order_id = int(flag_match.group(1))
            reason = prompt

            st.session_state['chain_of_thought'].append({
                "step": "🚩 Flagging Order",
                "content": f"Calling flag_order({order_id})...",
                "tool": f"flag_order(order_id={order_id}, reason='{reason}')"
            })

            result = flag_order(order_id, reason, tenant_id=tenant_id)
            message_placeholder.markdown(result)
            st.session_state['messages'].append({"role": "assistant", "content": result})
            log_interaction(st.session_state['session_id'], 'assistant', result, tool_used='flag_order')
            st.rerun()

        # --- INTENT: FRAUD INVESTIGATION (cognitive foundation loop) ---
        # Triggered when an Admin asks about suspicious/fraud activity. Replaces the
        # previous keyword-routed synthesis call with a real tool-use loop driven by
        # assemble_context → routing → cached system prompt → slim summary.
        suspicious_keywords = ['suspicious', 'fraud', 'anomal', 'at risk', 'risk', 'velocity', 'flagged orders', 'investigate']
        if user_role == "Admin" and any(kw in prompt.lower() for kw in suspicious_keywords):
            # Extract optional entity_ref from the prompt — order/customer/IP — to focus the investigation
            entity_ref = None
            order_match = re.search(r'order\s*#?(\d+)', prompt.lower())
            if order_match:
                try:
                    oid = int(order_match.group(1))
                    cust_lookup = execute_sql(f"SELECT customer_id FROM orders WHERE order_id = {oid} AND tenant_id = '{tenant_id}' LIMIT 1")
                    parsed = json.loads(cust_lookup) if cust_lookup and cust_lookup.startswith('[') else None
                    if parsed:
                        entity_ref = str(parsed[0]['customer_id'])
                except Exception:
                    pass

            def on_event(event_type, payload):
                """Stream loop events into the chain-of-thought sidebar."""
                if event_type == "assembled":
                    sources_md = " | ".join(
                        f"{k}={v.get('tokens', 0)}t/{v.get('status', '?')}"
                        for k, v in payload.get("sources", {}).items()
                    )
                    st.session_state['chain_of_thought'].append({
                        "step": "🧠 Context assembled (zero LLM calls)",
                        "content": f"Budget {payload['budget_used']}/{payload['budget_total']} tokens. {sources_md}",
                        "tool": payload.get("system_context", "")[:800],
                    })
                elif event_type == "routed":
                    gates = payload.get("gates", {})
                    st.session_state['chain_of_thought'].append({
                        "step": f"🚦 Routed → {payload['path']} ({payload['model']})",
                        "content": payload["reason"],
                        "tool": json.dumps(gates, default=str, indent=2),
                    })
                elif event_type == "reinforced":
                    st.session_state['chain_of_thought'].append({
                        "step": "💪 Pattern reinforced",
                        "content": (f"fraud_memory pattern {payload['pattern_id']} bumped "
                                    f"(last_reinforced_at = NOW, evidence_count += 1). "
                                    f"Decay now treats this as actively-used."),
                    })
                elif event_type == "tool_call":
                    st.session_state['chain_of_thought'].append({
                        "step": f"🔧 Agent → {payload['name']}",
                        "content": json.dumps(payload.get("input", {}), default=str)[:400],
                    })
                elif event_type == "tool_result":
                    st.session_state['chain_of_thought'].append({
                        "step": f"📥 Result: {payload['name']}",
                        "content": str(payload.get("result", ""))[:600],
                    })
                elif event_type == "summary":
                    st.session_state['chain_of_thought'].append({
                        "step": "📝 Slim summary (Haiku on structured checkpoint)",
                        "content": f"reasoning_id={payload.get('reasoning_id')} confidence={payload.get('confidence')}",
                    })

            # Tier 4 in assemble_context joins agent_sessions.user_id == entity_ref.
            # The UI session was created with user_id="demo_user_ui" — that join
            # would return zero prior investigations. Mint an entity-scoped session
            # for this investigation so prior runs for the same entity are visible.
            if entity_ref:
                entity_session_id = str(uuid.uuid4())
                create_session(
                    entity_session_id,
                    user_id=str(entity_ref),
                    metadata={
                        "source": "agent_ui.investigation",
                        "parent_session_id": st.session_state['session_id'],
                        "tenant_id": tenant_id,
                        "entity_ref": str(entity_ref),
                    },
                )
                investigation_session_id = entity_session_id
                st.session_state['chain_of_thought'].append({
                    "step": "🔗 Entity-scoped session",
                    "content": f"Investigation session bound to entity_ref={entity_ref} for Tier 4 linkage.",
                    "tool": investigation_session_id,
                })
            else:
                investigation_session_id = st.session_state['session_id']

            with st.spinner("Running cognitive foundation investigation..."):
                try:
                    result = run_investigation(
                        trigger_text=prompt,
                        session_id=investigation_session_id,
                        entity_ref=entity_ref,
                        on_event=on_event,
                        tenant_id=tenant_id,
                    )
                    if result.get("error"):
                        final_response = f"⚠️ {result['error']}"
                    else:
                        report = result.get("summary", {}).get("report")
                        routing = result.get("routing", {})
                        final_response = (
                            f"**Investigation report** _(via {routing.get('path')} path, "
                            f"model={routing.get('model')}, "
                            f"rounds_budget={routing.get('max_tool_rounds')})_\n\n"
                            f"{report or '_No report generated — agent did not write a checkpoint._'}"
                        )
                except Exception as e:
                    final_response = f"❌ Investigation failed: {e}"

            message_placeholder.markdown(final_response)
            st.session_state['messages'].append({"role": "assistant", "content": final_response})
            # Conversation log stays on the UI session; the investigation's
            # episodic checkpoint is already on the entity-scoped session.
            log_interaction(st.session_state['session_id'], 'assistant', final_response, tool_used='run_investigation')
            st.rerun()

        # --- INTENT: REVIEW ANALYTICS (Admin only) ---
        review_keywords = ['review', 'sentiment', 'rating', 'satisfaction', 'churn',
                           'feedback', 'opinion', 'complaint', 'nps', 'unhappy', 'dissatisfied']
        semantic_review_keywords = ['what are customers saying', 'customer opinion',
                                    'product feedback', 'service feedback']
        is_review_query = (
            any(kw in prompt.lower() for kw in review_keywords) or
            any(kw in prompt.lower() for kw in semantic_review_keywords)
        )

        if user_role == "Admin" and is_review_query:
            review_data_raw = get_review_analytics(tenant_id=tenant_id)

            # Also run a semantic search over review text so Claude has verbatim quotes
            semantic_hits = vector_search(prompt, "reviews", tenant_id=tenant_id)

            st.session_state['chain_of_thought'].append({
                "step": "📊 Review Analytics (HTAP)",
                "content": "Running TiFlash sentiment aggregation + HNSW vector search on reviews...",
                "tool": review_data_raw
            })

            api_key = os.getenv('ANTHROPIC_API_KEY')
            if api_key:
                try:
                    client = anthropic.Anthropic(api_key=api_key)
                    synthesis_prompt = f"""You are a senior business analyst powered by TiDB HTAP.
The admin asked: "{prompt}"

Below is live data queried directly from operational tables in TiDB — no ETL, no data warehouse.
TiFlash (columnar engine) computed these aggregates in real time while new reviews were being written.

AGGREGATE ANALYTICS:
{review_data_raw}

SEMANTICALLY SIMILAR REVIEWS (vector search on review text):
{semantic_hits}

Your task:
1. Answer the admin's question directly using the data above.
2. Highlight any products or customers that show churn risk (negative sentiment trend).
3. Mention 1-2 specific verbatim quotes from the semantic results to ground your answer.
4. End with one actionable recommendation.
Keep the response under 200 words. Use markdown formatting."""

                    message = client.messages.create(
                        model="claude-haiku-4-5",
                        max_tokens=500,
                        messages=[{"role": "user", "content": synthesis_prompt}]
                    )
                    final_response = message.content[0].text
                except Exception as e:
                    final_response = f"**Review Analytics:**\n```\n{review_data_raw}\n```\n\n*⚠️ Claude synthesis failed: {e}*"
            else:
                final_response = f"**Review Analytics:**\n```\n{review_data_raw}\n```\n\n*⚠️ Add `ANTHROPIC_API_KEY` to `.env` for natural language synthesis.*"

            message_placeholder.markdown(final_response)
            st.session_state['messages'].append({"role": "assistant", "content": final_response})
            log_interaction(st.session_state['session_id'], 'assistant', final_response, tool_used='get_review_analytics')
            st.rerun()

        # --- STEP 1: IDENTIFY (SQL) ---
        st.session_state['chain_of_thought'].append({"step": "1. Identifying Entity", "content": f"Querying data as {user_role}..."})
        
        customer_context = "No specific customer context."
        
        if user_role == "Admin":
            # Admin Mode: Global stats or searching for specific users mentioned in prompt
            # For simplicity, we show global stats if no specific user is named
            
            # Simple heuristic: Check if "Bernard" is mentioned, otherwise global
            if "Bernard" in prompt:
                 customer_sql = "SELECT * FROM customers WHERE name LIKE '%Bernard%' LIMIT 1;"
                 # ... (Reuse logic logic via function ideally, but inline for now)
            else:
                 # Global Stats (tenant-scoped)
                 stats_sql = f"""
                    SELECT
                        (SELECT COUNT(*) FROM customers WHERE tenant_id = '{tenant_id}') as total_customers,
                        (SELECT COUNT(*) FROM orders WHERE tenant_id = '{tenant_id}') as total_orders,
                        (SELECT SUM(price * quantity) FROM orders
                           JOIN products ON orders.product_id = products.product_id
                                         AND products.tenant_id = orders.tenant_id
                         WHERE orders.tenant_id = '{tenant_id}') as total_revenue
                 """
                 global_stats = execute_sql(stats_sql)

                 recent_orders_sql = f"""
                    SELECT o.order_id, c.name as customer, p.name as product, o.order_date
                    FROM orders o
                    JOIN customers c ON o.customer_id = c.customer_id AND c.tenant_id = o.tenant_id
                    JOIN products p ON o.product_id = p.product_id AND p.tenant_id = o.tenant_id
                    WHERE o.tenant_id = '{tenant_id}'
                    ORDER BY o.order_date DESC
                    LIMIT 5;
                 """
                 recent_orders = execute_sql(recent_orders_sql)
                 
                 st.session_state['chain_of_thought'].append({
                    "step": "📊 Admin: Global Stats", 
                    "content": "Fetched global KPIs and recent orders.", 
                    "tool": global_stats
                 })
                 
                 # Format Admin stats as clean markdown
                 try:
                     stats = json.loads(global_stats)[0] if global_stats and global_stats != "No results found." else {}
                     orders_list = json.loads(recent_orders) if recent_orders and recent_orders != "No results found." else []
                     orders_md = "\n".join([
                         f"  - Order #{o['order_id']} | **{o['customer']}** → {o['product']} | {str(o['order_date'])[:10]}"
                         for o in orders_list
                     ])
                     customer_context = (
                         f"**Total Customers:** {stats.get('total_customers', 'N/A')} | "
                         f"**Total Orders:** {stats.get('total_orders', 'N/A')} | "
                         f"**Revenue:** ${float(stats.get('total_revenue') or 0):,.2f}\n\n"
                         f"**Recent Activity:**\n{orders_md}"
                     )
                 except Exception:
                     customer_context = f"Stats: {global_stats}"

        else: # Customer (Bernard)
            # Mocking "Bernard" user for demo purposes as requested (tenant-scoped)
            customer_sql = f"SELECT * FROM customers WHERE name LIKE '%Bernard%' AND tenant_id = '{tenant_id}' LIMIT 1;"
            customer_results = json.loads(execute_sql(customer_sql))
            
            if isinstance(customer_results, list) and len(customer_results) > 0:
                customer = customer_results[0]
                customer_id = customer['customer_id']
                
                st.session_state['chain_of_thought'].append({
                    "step": "✅ Customer Found", 
                    "content": f"{customer['name']} (VIP: {customer['vip_status']})",
                    "tool": customer
                })
                
                # Fetch Orders
                order_sql = f"""
                    SELECT o.order_id, p.name as product, p.price, o.order_date
                    FROM orders o
                    JOIN products p ON o.product_id = p.product_id AND p.tenant_id = o.tenant_id
                    WHERE o.customer_id = {customer_id} AND o.tenant_id = '{tenant_id}'
                    ORDER BY o.order_date DESC
                    LIMIT 5;
                """
                order_data = execute_sql(order_sql)
                
                st.session_state['chain_of_thought'].append({
                    "step": "📦 Identifying Orders", 
                    "content": "Fetching recent order history...", 
                    "tool": order_data
                })
                
                # Format customer profile as clean markdown
                try:
                    orders_list = json.loads(order_data) if order_data and order_data != "No results found." else []
                    orders_md = "\n".join([
                        f"  - **{o['product']}** — ${float(o['price']):,.2f} | {str(o['order_date'])[:10]}"
                        for o in orders_list
                    ])
                    customer_context = (
                        f"**{customer['name']}** | Region: `{customer['region']}` | "
                        f"VIP: {'✅ Yes' if customer['vip_status'] else 'No'}\n\n"
                        f"**Recent Orders:**\n{orders_md if orders_md else '_No orders found._'}"
                    )
                except Exception:
                    customer_context = f"Customer: {customer}\nOrders: {order_data}"
            else:
                st.session_state['chain_of_thought'].append({"step": "❌ Customer Not Found", "content": "No record for 'Bernard'."})

        # --- STEP 2: RETRIEVE (Vector, tenant-scoped) ---
        policy_data = vector_search(prompt, "sales_knowledge", tenant_id=tenant_id)

        # Deduplicate: vector search returns LIMIT 3 — top matches may be the same document
        seen_content = set()
        unique_policies = []
        for line in policy_data.split('\n'):
            content_key = line.split(' (Confidence:')[0].strip()
            if content_key and content_key not in seen_content:
                seen_content.add(content_key)
                unique_policies.append(line.strip())
        policy_display = "\n".join([f"  - {p}" for p in unique_policies]) or "_No relevant policies found._"

        st.session_state['chain_of_thought'].append({
            "step": "2. Retrieving Context",
            "content": "Vector Search on `sales_knowledge`...",
            "tool": policy_data
        })

        # --- STEP 3: SYNTHESIZE (Claude LLM) ---
        api_key = os.getenv('ANTHROPIC_API_KEY')
        if api_key:
            try:
                client = anthropic.Anthropic(api_key=api_key)
                synthesis_prompt = f"""You are a Sales Engineering assistant powered by TiDB.
The user asked: "{prompt}"

Here is the data retrieved from TiDB to answer their question:

CONTEXT:
{customer_context}

RELEVANT POLICIES:
{policy_display}

Answer the user's question directly and concisely using only the data above.
Be specific — reference actual names, dates, amounts, and product names where relevant.
If data is missing or insufficient, say so clearly. Keep the response under 150 words."""

                message = client.messages.create(
                    model="claude-haiku-4-5",
                    max_tokens=400,
                    messages=[{"role": "user", "content": synthesis_prompt}]
                )
                final_response = message.content[0].text

            except Exception as e:
                final_response = (
                    f"**Your profile:**\n{customer_context}\n\n---\n\n"
                    f"**Relevant policies:**\n{policy_display}\n\n---\n"
                    f"*⚠️ Claude synthesis failed: {e}*"
                )
        else:
            final_response = (
                f"**Your profile:**\n{customer_context}\n\n---\n\n"
                f"**Relevant policies:**\n{policy_display}\n\n---\n"
                f"*⚠️ Add `ANTHROPIC_API_KEY` to `.env` for natural language answers*"
            )
        
        message_placeholder.markdown(final_response)
        
        # Add Assistant Message to Chat
        st.session_state['messages'].append({"role": "assistant", "content": final_response})
        
        # Log to DB
        log_interaction(st.session_state['session_id'], 'assistant', final_response)
        
        # Force a rerun to update sidebar immediately
        st.rerun()
