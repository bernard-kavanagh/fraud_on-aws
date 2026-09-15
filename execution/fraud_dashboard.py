import streamlit as st
import pandas as pd
import mysql.connector
import time
import altair as alt
import sys
import os

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from agent_tools import DB_CONFIG
from tenancy import resolve_tenant_id, DEMO_TENANTS

# --- 1. CONFIGURATION ---
st.set_page_config(page_title="TiDB Real-Time Fraud Monitor", layout="wide", page_icon="🚨")

# Custom CSS for "Wow" factor
st.markdown("""
<style>
    /* Premium dark mode feels */
    .stApp {
        background-color: #0E1117;
        color: #FAFAFA;
    }
    
    .kpi-card {
        background: linear-gradient(135deg, #1f2937 0%, #111827 100%);
        border: 1px solid #374151;
        border-radius: 12px;
        padding: 24px;
        box-shadow: 0 4px 6px -1px rgba(0, 0, 0, 0.1), 0 2px 4px -1px rgba(0, 0, 0, 0.06);
        text-align: center;
        transition: transform 0.2s ease, box-shadow 0.2s ease;
    }
    .kpi-card:hover {
        transform: translateY(-2px);
        box-shadow: 0 10px 15px -3px rgba(0, 0, 0, 0.1), 0 4px 6px -2px rgba(0, 0, 0, 0.05);
        border-color: #4B5563;
    }
    .kpi-title {
        color: #9CA3AF;
        font-size: 14px;
        font-weight: 600;
        text-transform: uppercase;
        letter-spacing: 0.05em;
        margin-bottom: 8px;
    }
    .kpi-value {
        font-size: 36px;
        font-weight: 700;
        margin: 0;
        background: linear-gradient(to right, #60A5FA, #3B82F6);
        -webkit-background-clip: text;
        -webkit-text-fill-color: transparent;
    }
    .kpi-value.alert {
        background: linear-gradient(to right, #F87171, #EF4444);
        -webkit-background-clip: text;
        -webkit-text-fill-color: transparent;
    }
    .kpi-value.success {
        background: linear-gradient(to right, #34D399, #10B981);
        -webkit-background-clip: text;
        -webkit-text-fill-color: transparent;
    }
    .kpi-desc {
        color: #6B7280;
        font-size: 12px;
        margin-top: 10px;
        line-height: 1.4;
    }
    h1, h2, h3 {
        font-family: 'Inter', sans-serif;
    }
    
    /* Sleek dataframes */
    .stDataFrame {
        border-radius: 8px;
        overflow: hidden;
    }
    
    /* Sidebar styling */
    .css-1d391kg {
        background-color: #111827;
    }
</style>
""", unsafe_allow_html=True)

# --- 2. DATA LOADING (HTAP Queries) ---
def query_to_df(cursor, sql, params=None):
    """Execute a query and return results as a DataFrame without pd.read_sql."""
    cursor.execute(sql, params or ())
    rows = cursor.fetchall()
    columns = [col[0] for col in cursor.description]
    return pd.DataFrame(rows, columns=columns)

def get_fraud_data(tenant_id):
    conn = mysql.connector.connect(**DB_CONFIG)
    cursor = conn.cursor()

    # KPI 1: Total Pending/Flagged vs Cleared
    kpi_query = """
    SELECT status, COUNT(*) as count, SUM(amount) as value
    FROM orders
    WHERE tenant_id = %s
    GROUP BY status
    """
    df_kpi = query_to_df(cursor, kpi_query, (tenant_id,))

    # Query 2: Fraud Velocity by IP (The HTAP Engine Query)
    velocity_query = """
    SELECT /*+ read_from_storage(tiflash[orders]) */
        ip_address,
        COUNT(order_id) as volume,
        SUM(amount) as total_at_risk
    FROM orders
    WHERE order_date >= NOW() - INTERVAL 24 HOUR
    AND ip_address IS NOT NULL
    AND tenant_id = %s
    GROUP BY ip_address
    HAVING volume >= 3
    ORDER BY volume DESC
    LIMIT 10
    """
    try:
        df_velocity = query_to_df(cursor, velocity_query, (tenant_id,))
    except Exception:
        # Fallback if TiFlash isn't perfectly synced yet
        velocity_query_fallback = velocity_query.replace("/*+ read_from_storage(tiflash[orders]) */", "")
        df_velocity = query_to_df(cursor, velocity_query_fallback, (tenant_id,))

    # Query 3: Recent High-Risk Transactions
    risk_query = """
    SELECT o.order_id, c.name as customer, o.amount, o.ip_address, o.country, o.status, o.order_date
    FROM orders o
    JOIN customers c ON o.customer_id = c.customer_id
    WHERE o.status IN ('flagged', 'pending')
    AND o.tenant_id = %s
    AND c.tenant_id = %s
    ORDER BY o.order_date DESC
    LIMIT 15
    """
    df_risk = query_to_df(cursor, risk_query, (tenant_id, tenant_id))

    conn.close()
    return df_kpi, df_velocity, df_risk

# --- 3. UI LAYOUT ---
tenant_id = st.sidebar.selectbox("Tenant", DEMO_TENANTS)

st.title("🚨 TiDB Real-Time Risk & Fraud Monitor")
st.markdown("Powered by **TiDB HTAP**. Transactional events trigger real-time aggregations via TiFlash without moving data.")

placeholder = st.empty()

while True:
    try:
        df_kpi, df_velocity, df_risk = get_fraud_data(tenant_id)
        
        # Calculate KPIs safely
        flagged_count = int(df_kpi.loc[df_kpi['status'] == 'flagged', 'count'].sum()) if 'flagged' in df_kpi['status'].values else 0
        pending_count = int(df_kpi.loc[df_kpi['status'] == 'pending', 'count'].sum()) if 'pending' in df_kpi['status'].values else 0
        total_risk_val = float(df_kpi.loc[df_kpi['status'].isin(['flagged', 'pending']), 'value'].sum())
        cleared_count = int(df_kpi.loc[df_kpi['status'] == 'cleared', 'count'].sum()) if 'cleared' in df_kpi['status'].values else 0

        with placeholder.container():
            # ROW 1: Premium KPIs
            st.markdown(f"""
            <div style="display: flex; gap: 20px; margin-bottom: 24px;">
                <div class="kpi-card" style="flex: 1;">
                    <div class="kpi-title">Active Alerts (Pending+Flagged)</div>
                    <div class="kpi-value alert">{flagged_count + pending_count} Orders</div>
                    <div class="kpi-desc">Orders matching velocity burst or high-value anomaly rules. Requires immediate review before fulfillment.</div>
                </div>
                <div class="kpi-card" style="flex: 1;">
                    <div class="kpi-title">Revenue At Risk</div>
                    <div class="kpi-value alert">${total_risk_val:,.2f}</div>
                    <div class="kpi-desc">Total value of uncleared transactions. Held in <em>pending</em> or <em>flagged</em> state until a fraud decision is made.</div>
                </div>
                <div class="kpi-card" style="flex: 1;">
                    <div class="kpi-title">Cleared Transactions</div>
                    <div class="kpi-value success">{cleared_count}</div>
                    <div class="kpi-desc">Orders that passed fraud review and are approved for fulfillment. Revenue is safe to recognise.</div>
                </div>
            </div>
            """, unsafe_allow_html=True)

            # ROW 1b: CTA
            cta_col, spacer = st.columns([1, 3])
            with cta_col:
                st.link_button(
                    "🔍 Investigate with Agent →",
                    url="http://localhost:8501",
                    help="Open the TiDB Agent UI to query suspicious orders, flag transactions, and get natural language explanations.",
                    type="primary"
                )
            st.markdown("<div style='margin-bottom: 16px;'></div>", unsafe_allow_html=True)

            # ROW 2: Charts and Tables
            col1, col2 = st.columns([1, 1.5])
            
            with col1:
                st.subheader("⚠️ Velocity Anomalies (HTAP Query)")
                st.markdown(
                    "IPs with **3+ transactions in the last 24 hours**. "
                    "This query runs live against TiFlash (columnar engine) while new orders are simultaneously "
                    "being written to TiKV — no ETL, no data warehouse."
                )
                if not df_velocity.empty:
                    # Altair chart for velocity
                    chart = alt.Chart(df_velocity).mark_bar(
                        cornerRadiusTopLeft=3,
                        cornerRadiusTopRight=3,
                        color='#EF4444'
                    ).encode(
                        x=alt.X('ip_address:N', sort='-y', title='IP Address'),
                        y=alt.Y('volume:Q', title='Transaction Volume'),
                        tooltip=['ip_address', 'volume', 'total_at_risk']
                    ).properties(height=350).configure_view(strokeWidth=0).configure_axis(grid=False)
                    
                    st.altair_chart(chart, width='stretch')
                else:
                    st.info("No velocity anomalies detected.")
                    
            with col2:
                st.subheader("🕵️ Live Risk Queue")
                st.markdown(
                    "The 15 most recent **flagged** and **pending** orders. "
                    "Use the Agent UI to ask _\"why is order #X suspicious?\"_ or say _\"flag order #X\"_ to escalate."
                )

                # Style the dataframe based on status
                def highlight_status(val):
                    if val == 'flagged':
                        return 'background-color: rgba(239, 68, 68, 0.2); color: #FCA5A5;'
                    elif val == 'pending':
                        return 'background-color: rgba(245, 158, 11, 0.2); color: #FCD34D;'
                    return ''

                if not df_risk.empty:
                    st.dataframe(
                        df_risk.style.map(highlight_status, subset=['status'])\
                                     .format({'amount': lambda x: f'${x:,.2f}' if x is not None else 'N/A', 'order_date': lambda x: str(x)}),
                        height=350,
                        width='stretch',
                        hide_index=True
                    )
                else:
                    st.success("No risky transactions currently in queue.")
                    
    except Exception as e:
        st.error(f"Database error: {e}")
        
    # Refresh every 2 seconds for that sweet "live" feel
    time.sleep(2)
