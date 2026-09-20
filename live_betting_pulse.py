import mysql.connector
import os
import time
import random
from faker import Faker
from dotenv import load_dotenv
from tenancy import resolve_tenant_id

load_dotenv()

config = {
    'host': os.getenv('TIDB_HOST'),
    'port': int(os.getenv('TIDB_PORT', 4000)),
    'user': os.getenv('TIDB_USER'),
    'password': os.getenv('TIDB_PASSWORD'),
    'database': os.getenv('TIDB_DATABASE', 'agentcore_fraud'),
    'ssl_ca': os.getenv('TIDB_SSL_CA'),
    'ssl_verify_cert': True
}

def heartbeat():
    tenant_id = resolve_tenant_id()
    print(f"🏦 Live Betting Pulse running as tenant: {tenant_id}")

    fake = Faker()
    conn = mysql.connector.connect(**config)
    cursor = conn.cursor()

    # 1. Fetch valid customer IDs and active betting events
    cursor.execute("SELECT customer_id FROM customers WHERE tenant_id = %s", (tenant_id,))
    valid_customers = [row[0] for row in cursor.fetchall()]

    cursor.execute("""
        SELECT event_id, home_team, away_team, sport, home_odds, away_odds, draw_odds
        FROM betting_events
        WHERE status = 'active' AND tenant_id = %s
    """, (tenant_id,))
    active_events = cursor.fetchall()  # (event_id, home_team, away_team, sport, home_odds, away_odds, draw_odds)

    if not valid_customers or not active_events:
        print("❌ No customers or active events found. Run seed_betting_data.py first.")
        conn.close()
        return

    print("💓 Starting Live Betting Pulse... (Press Ctrl+C to stop)")

    try:
        while True:
            # Re-fetch active events each tick so suspended events are excluded immediately
            cursor.execute("""
                SELECT event_id, home_team, away_team, sport, home_odds, away_odds, draw_odds
                FROM betting_events
                WHERE status = 'active' AND tenant_id = %s
            """, (tenant_id,))
            active_events = cursor.fetchall()

            if not active_events:
                print("⏸️  No active events — waiting...")
                time.sleep(2)
                continue

            c_id = random.choice(valid_customers)
            event = random.choice(active_events)
            event_id, home_team, away_team, sport, home_odds, away_odds, draw_odds = event

            home_odds = float(home_odds)
            away_odds = float(away_odds)
            draw_odds = float(draw_odds) if draw_odds is not None else None

            # Selection weighted: 50% home, 35% away, 15% draw (draw only if available)
            if draw_odds is not None:
                selection = random.choices(['home', 'away', 'draw'], weights=[50, 35, 15])[0]
            else:
                selection = random.choices(['home', 'away'], weights=[50, 35])[0]

            if selection == 'home':
                odds = home_odds
            elif selection == 'away':
                odds = away_odds
            else:
                odds = draw_odds

            stake = round(random.uniform(10, 300), 2)
            potential_payout = round(stake * odds, 2)
            ip = fake.ipv4()

            sql = """
                INSERT INTO bets
                (customer_id, event_id, selection, stake, odds, potential_payout, status, ip_address, tenant_id)
                VALUES (%s, %s, %s, %s, %s, %s, 'accepted', %s, %s)
            """
            cursor.execute(sql, (c_id, event_id, selection, stake, odds, potential_payout, ip, tenant_id))
            conn.commit()

            event_name = f"{home_team} vs {away_team}"
            print(f"   -> Live Bet: {event_name} | {selection.upper()} @ {odds} | ${stake} stake | payout ${potential_payout}")
            time.sleep(0.5)  # The "Heartbeat" frequency

    except KeyboardInterrupt:
        print("\n🛑 Betting Pulse Stopped.")
        conn.close()

if __name__ == "__main__":
    heartbeat()
