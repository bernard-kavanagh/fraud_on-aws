import streamlit as st
import pandas as pd
import mysql.connector
import time
import altair as alt
import sys
import os

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from agent_tools import DB_CONFIG, adjust_odds, flag_bettor
from tenancy import resolve_tenant_id, DEMO_TENANTS

# --- 1. CONFIGURATION ---
st.set_page_config(page_title="TiDB Sports Betting Risk Monitor", layout="wide", page_icon="⚽")

st.markdown("""
<style>
    .stApp { background-color: #0E1117; color: #FAFAFA; }
    .kpi-card {
        background: linear-gradient(135deg, #1f2937 0%, #111827 100%);
        border: 1px solid #374151; border-radius: 12px; padding: 24px;
        box-shadow: 0 4px 6px -1px rgba(0,0,0,0.1); text-align: center;
        transition: transform 0.2s ease, box-shadow 0.2s ease;
    }
    .kpi-card:hover { transform: translateY(-2px); border-color: #4B5563; }
    .kpi-title {
        color: #9CA3AF; font-size: 14px; font-weight: 600;
        text-transform: uppercase; letter-spacing: 0.05em; margin-bottom: 8px;
    }
    .kpi-value {
        font-size: 36px; font-weight: 700; margin: 0;
        background: linear-gradient(to right, #60A5FA, #3B82F6);
        -webkit-background-clip: text; -webkit-text-fill-color: transparent;
    }
    .kpi-value.alert {
        background: linear-gradient(to right, #F87171, #EF4444);
        -webkit-background-clip: text; -webkit-text-fill-color: transparent;
    }
    .kpi-value.success {
        background: linear-gradient(to right, #34D399, #10B981);
        -webkit-background-clip: text; -webkit-text-fill-color: transparent;
    }
    .kpi-desc { color: #6B7280; font-size: 12px; margin-top: 10px; line-height: 1.4; }
    .section-label {
        color: #6B7280; font-size: 11px; font-weight: 600;
        text-transform: uppercase; letter-spacing: 0.08em;
        border-bottom: 1px solid #1f2937; padding-bottom: 4px; margin-bottom: 8px;
    }
    h1, h2, h3 { font-family: 'Inter', sans-serif; }
    .stDataFrame { border-radius: 8px; overflow: hidden; }
    .css-1d391kg { background-color: #111827; }
</style>
""", unsafe_allow_html=True)

# --- 2. DATA LOADING ---
def query_to_df(cursor, sql, params=None):
    cursor.execute(sql, params or ())
    rows = cursor.fetchall()
    columns = [col[0] for col in cursor.description]
    return pd.DataFrame(rows, columns=columns)

def get_betting_data(tenant_id):
    conn = mysql.connector.connect(**DB_CONFIG)
    cursor = conn.cursor()

    df_kpi = query_to_df(cursor, """
        SELECT status, COUNT(*) as count,
               SUM(stake) as total_staked,
               SUM(potential_payout) as total_liability
        FROM bets WHERE tenant_id = %s GROUP BY status
    """, (tenant_id,))

    liability_sql = """
    SELECT /*+ read_from_storage(tiflash[bets]) */
        e.event_id,
        CONCAT(e.home_team, ' vs ', e.away_team) as event_name,
        e.sport, e.league, e.home_odds, e.away_odds,
        SUM(b.stake) as total_staked,
        SUM(b.potential_payout) as total_liability,
        ROUND(SUM(CASE WHEN b.selection = 'home' THEN b.stake ELSE 0 END)
              / NULLIF(SUM(b.stake), 0) * 100, 1) as home_pct
    FROM bets b
    JOIN betting_events e ON b.event_id = e.event_id
    WHERE b.placed_at >= NOW() - INTERVAL 24 HOUR
    AND b.status = 'accepted'
    AND e.status = 'active'
    AND b.tenant_id = %s
    AND e.tenant_id = %s
    GROUP BY e.event_id, e.home_team, e.away_team, e.sport, e.league, e.home_odds, e.away_odds
    HAVING home_pct >= 65 OR home_pct <= 35
    ORDER BY total_liability DESC
    LIMIT 10
    """
    try:
        df_liability = query_to_df(cursor, liability_sql, (tenant_id, tenant_id))
    except Exception:
        df_liability = query_to_df(cursor, liability_sql.replace(
            "/*+ read_from_storage(tiflash[bets]) */", ""), (tenant_id, tenant_id))

    velocity_sql = """
    SELECT /*+ read_from_storage(tiflash[bets]) */
        b.ip_address,
        COUNT(b.bet_id) as bet_count,
        COUNT(DISTINCT b.customer_id) as unique_accounts,
        ROUND(SUM(b.stake), 2) as total_staked,
        MAX(b.placed_at) as last_bet_at
    FROM bets b
    WHERE b.placed_at >= NOW() - INTERVAL 24 HOUR
    AND b.status = 'accepted'
    AND b.tenant_id = %s
    GROUP BY b.ip_address
    HAVING bet_count >= 5
    ORDER BY bet_count DESC
    LIMIT 10
    """
    try:
        df_velocity = query_to_df(cursor, velocity_sql, (tenant_id,))
    except Exception:
        df_velocity = query_to_df(cursor, velocity_sql.replace(
            "/*+ read_from_storage(tiflash[bets]) */", ""), (tenant_id,))

    df_feed = query_to_df(cursor, """
        SELECT b.bet_id,
               CONCAT(e.home_team, ' vs ', e.away_team) as event_name,
               e.sport, b.selection, b.stake, b.odds, b.potential_payout,
               b.status, b.placed_at
        FROM bets b
        JOIN betting_events e ON b.event_id = e.event_id
        WHERE b.tenant_id = %s
        AND e.tenant_id = %s
        ORDER BY b.placed_at DESC
        LIMIT 15
    """, (tenant_id, tenant_id))

    conn.close()
    return df_kpi, df_liability, df_velocity, df_feed

# --- 3. SIDEBAR ---
tenant_id = st.sidebar.selectbox("Tenant", DEMO_TENANTS)

with st.sidebar:
    st.header("ℹ️ Demo Guide")
    st.markdown("""
**Two signals, one database:**

**📉 Risk** — Liability concentration.
When 65%+ of stakes land on one side, adjust odds to rebalance the book. Market stays open.

**🚩 Fraud** — Betting velocity anomalies.
Multiple bets from the same IP signals multi-accounting or bot activity. Flag the account.

**Both queries hit TiFlash** (columnar) while `live_betting_pulse.py` writes to TiKV every 500ms. No ETL, no enrichment pipeline.

**Run the live pulse:**
```
python live_betting_pulse.py
```

**Talking point:**
> Ververica needs Flink + a separate enrichment store to combine streaming bets with historical context. TiDB does it in one query — the same DB holds the stream and the history.

**Vertical:** Gaming, gambling operators
    """)

# --- 4. SESSION STATE ---
for key, default in [
    ('adjust_event_id', None), ('adjust_selection', None),
    ('flag_ip', None), ('last_action_msg', None)
]:
    if key not in st.session_state:
        st.session_state[key] = default

# --- 5. PROCESS PENDING WRITE-BACKS ---
if st.session_state.adjust_event_id is not None:
    result = adjust_odds(st.session_state.adjust_event_id, st.session_state.adjust_selection, tenant_id=tenant_id)
    st.session_state.last_action_msg = result
    st.session_state.adjust_event_id = None
    st.session_state.adjust_selection = None

if st.session_state.flag_ip is not None:
    result = flag_bettor(st.session_state.flag_ip, tenant_id=tenant_id)
    st.session_state.last_action_msg = result
    st.session_state.flag_ip = None

# --- 6. LOAD DATA ---
df_kpi, df_liability, df_velocity, df_feed = get_betting_data(tenant_id)

df_liability['total_liability'] = pd.to_numeric(df_liability['total_liability'], errors='coerce')
df_liability = df_liability.dropna(subset=['total_liability', 'home_pct'])

total_exposure = float(df_kpi.loc[df_kpi['status'] == 'accepted', 'total_liability'].sum()) \
    if 'accepted' in df_kpi['status'].values else 0.0
total_staked_kpi = float(df_kpi.loc[df_kpi['status'] == 'accepted', 'total_staked'].sum()) \
    if 'accepted' in df_kpi['status'].values else 0.0
flagged_count = int(df_kpi.loc[df_kpi['status'] == 'flagged', 'count'].sum()) \
    if 'flagged' in df_kpi['status'].values else 0
flagged_class = "alert" if flagged_count > 0 else "success"

# --- 7. RENDER UI ---
st.title("⚽ TiDB Real-Time Betting Risk & Fraud Monitor")
st.markdown(
    "Powered by **TiDB HTAP**. Bets write to TiKV in real time. "
    "Risk and fraud signals aggregate live via TiFlash — no ETL, no enrichment pipeline."
)

if st.session_state.last_action_msg:
    if "✅" in st.session_state.last_action_msg:
        st.success(st.session_state.last_action_msg)
    else:
        st.error(st.session_state.last_action_msg)

# KPIs
st.markdown(f"""
<div style="display: flex; gap: 20px; margin-bottom: 24px;">
    <div class="kpi-card" style="flex: 1;">
        <div class="kpi-title">Total Exposure</div>
        <div class="kpi-value alert">${total_exposure:,.2f}</div>
        <div class="kpi-desc">Max liability if all accepted selections win.</div>
    </div>
    <div class="kpi-card" style="flex: 1;">
        <div class="kpi-title">Total Staked (24h)</div>
        <div class="kpi-value">${total_staked_kpi:,.2f}</div>
        <div class="kpi-desc">Gross stake volume. Revenue to the book if all selections lose.</div>
    </div>
    <div class="kpi-card" style="flex: 1;">
        <div class="kpi-title">Flagged Bets</div>
        <div class="kpi-value {flagged_class}">{flagged_count}</div>
        <div class="kpi-desc">Bets held for review. Excluded from exposure calculations.</div>
    </div>
</div>
""", unsafe_allow_html=True)

st.markdown("<div style='margin-bottom: 8px;'></div>", unsafe_allow_html=True)

# Two signal panels
col1, col2 = st.columns(2)

with col1:
    st.subheader("📉 Liability Concentration")
    st.markdown(
        "Events where **65%+ of stakes are on one side**. "
        "Adjust odds to rebalance — keeps the market open and attracts opposing money."
    )
    if not df_liability.empty:
        chart = alt.Chart(df_liability).mark_bar(
            cornerRadiusTopLeft=3, cornerRadiusTopRight=3, color='#F59E0B'
        ).encode(
            y=alt.Y('event_name:N', sort='-x', title='Event'),
            x=alt.X('total_liability:Q', title='Liability ($)'),
            tooltip=['event_name', 'sport', 'league', 'total_staked', 'total_liability', 'home_pct']
        ).properties(height=220).configure_view(strokeWidth=0).configure_axis(grid=False)
        st.altair_chart(chart, use_container_width=True)

        st.markdown('<div class="section-label">Risk Actions — Adjust Odds</div>', unsafe_allow_html=True)
        hdr = st.columns([2.5, 1, 1, 1.5])
        hdr[0].markdown("**Event**")
        hdr[1].markdown("**Home %**")
        hdr[2].markdown("**Liability**")
        hdr[3].markdown("**Action**")

        for _, row in df_liability.iterrows():
            overloaded = 'home' if float(row['home_pct']) >= 65 else 'away'
            c1, c2, c3, c4 = st.columns([2.5, 1, 1, 1.5])
            c1.write(row['event_name'])
            c2.write(f"{row['home_pct']}%")
            c3.write(f"${float(row['total_liability']):,.0f}")
            if c4.button("📉 Adjust Odds", key=f"adj_{row['event_id']}"):
                st.session_state.adjust_event_id = int(row['event_id'])
                st.session_state.adjust_selection = overloaded
                st.session_state.last_action_msg = None
                st.rerun()
    else:
        st.info("No liability concentration detected.")

with col2:
    st.subheader("🚩 Betting Velocity Anomalies")
    st.markdown(
        "IPs with **5+ bets in 24 hours**. "
        "Signals multi-accounting, arbitrage bots, or coordinated activity."
    )
    if not df_velocity.empty:
        st.dataframe(
            df_velocity[['ip_address', 'bet_count', 'unique_accounts', 'total_staked']].style.format({
                'total_staked': lambda x: f'${x:,.2f}' if x is not None else 'N/A'
            }),
            height=220,
            use_container_width=True,
            hide_index=True
        )

        st.markdown('<div class="section-label">Fraud Actions — Flag Account</div>', unsafe_allow_html=True)
        hdr2 = st.columns([2.5, 1, 1, 1.5])
        hdr2[0].markdown("**IP Address**")
        hdr2[1].markdown("**Bets**")
        hdr2[2].markdown("**Staked**")
        hdr2[3].markdown("**Action**")

        for _, row in df_velocity.iterrows():
            c1, c2, c3, c4 = st.columns([2.5, 1, 1, 1.5])
            c1.write(row['ip_address'])
            c2.write(int(row['bet_count']))
            c3.write(f"${float(row['total_staked']):,.0f}")
            if c4.button("🚩 Flag", key=f"flag_{row['ip_address']}"):
                st.session_state.flag_ip = str(row['ip_address'])
                st.session_state.last_action_msg = None
                st.rerun()
    else:
        st.info("No velocity anomalies detected.")

# Live Bet Feed
st.markdown("---")
st.subheader("📋 Live Bet Feed")
st.markdown("The 15 most recent bets. Flagged bets are highlighted — excluded from exposure until reviewed.")

def highlight_status(val):
    if val == 'flagged':
        return 'background-color: rgba(239, 68, 68, 0.2); color: #FCA5A5;'
    elif val == 'accepted':
        return 'background-color: rgba(52, 211, 153, 0.1); color: #6EE7B7;'
    elif val == 'suspended':
        return 'background-color: rgba(245, 158, 11, 0.15); color: #FCD34D;'
    return ''

if not df_feed.empty:
    st.dataframe(
        df_feed.style.map(highlight_status, subset=['status']).format({
            'stake': lambda x: f'${x:,.2f}' if x is not None else 'N/A',
            'potential_payout': lambda x: f'${x:,.2f}' if x is not None else 'N/A',
            'placed_at': lambda x: str(x)
        }),
        height=300,
        use_container_width=True,
        hide_index=True
    )
else:
    st.info("No bets in the feed yet.")

# --- 8. AUTO-REFRESH (2s) ---
time.sleep(2)
st.rerun()
