import sys
import os
import random
from datetime import datetime, timedelta
from faker import Faker

# Add parent directory to path to import agent_tools
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent_tools import get_db_connection
from tenancy import resolve_tenant_id

def seed_betting_data():
    print("⚽ Seeding Sports Betting Data...")
    conn = None
    fake = Faker()

    tenant_id = resolve_tenant_id()
    print(f"   -> Seeding tenant: {tenant_id}")

    try:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)

        # Fetch existing customer IDs
        cursor.execute("SELECT customer_id FROM customers WHERE tenant_id = %s", (tenant_id,))
        valid_customers = [row['customer_id'] for row in cursor.fetchall()]

        if not valid_customers:
            print("❌ No customers found. Run generate_world.py first.")
            return

        # Clear existing betting data so re-runs are idempotent
        print("   -> Clearing existing betting data...")
        cursor.execute("DELETE FROM bets WHERE tenant_id = %s", (tenant_id,))
        cursor.execute("DELETE FROM betting_events WHERE tenant_id = %s", (tenant_id,))
        # NOTE: AUTO_INCREMENT resets are NOT tenant-scoped — they reset the
        # counter globally. Acceptable for the demo, but not isolated per tenant.
        cursor.execute("ALTER TABLE betting_events AUTO_INCREMENT = 1")
        cursor.execute("ALTER TABLE bets AUTO_INCREMENT = 1")
        print("   -> Tables cleared and IDs reset.")

        now = datetime.now()

        # ==========================================
        # STEP 1: Insert 10 Betting Events
        # ==========================================
        print("   -> Seeding 10 betting events across 3 sports...")

        events = [
            # Football — Premier League
            {
                'sport': 'Football', 'home_team': 'Arsenal', 'away_team': 'Chelsea',
                'league': 'Premier League',
                'event_time': now + timedelta(hours=2),
                'home_odds': 2.10, 'away_odds': 3.20, 'draw_odds': 3.40
            },
            {
                'sport': 'Football', 'home_team': 'Manchester City', 'away_team': 'Liverpool',
                'league': 'Premier League',
                'event_time': now + timedelta(hours=5),
                'home_odds': 1.90, 'away_odds': 3.50, 'draw_odds': 3.60
            },
            {
                'sport': 'Football', 'home_team': 'Real Madrid', 'away_team': 'Barcelona',
                'league': 'La Liga',
                'event_time': now + timedelta(hours=8),
                'home_odds': 2.20, 'away_odds': 2.80, 'draw_odds': 3.10
            },
            {
                'sport': 'Football', 'home_team': 'PSG', 'away_team': 'Marseille',
                'league': 'Ligue 1',
                'event_time': now + timedelta(hours=24),
                'home_odds': 1.85, 'away_odds': 3.30, 'draw_odds': 3.70
            },
            {
                'sport': 'Football', 'home_team': 'Bayern Munich', 'away_team': 'Borussia Dortmund',
                'league': 'Bundesliga',
                'event_time': now + timedelta(hours=36),
                'home_odds': 1.95, 'away_odds': 3.40, 'draw_odds': 3.50
            },
            # Basketball — NBA
            {
                'sport': 'Basketball', 'home_team': 'Lakers', 'away_team': 'Celtics',
                'league': 'NBA',
                'event_time': now + timedelta(hours=4),
                'home_odds': 2.05, 'away_odds': 1.80, 'draw_odds': None
            },
            {
                'sport': 'Basketball', 'home_team': 'Warriors', 'away_team': 'Bulls',
                'league': 'NBA',
                'event_time': now + timedelta(hours=10),
                'home_odds': 1.75, 'away_odds': 2.10, 'draw_odds': None
            },
            {
                'sport': 'Basketball', 'home_team': 'Heat', 'away_team': 'Nets',
                'league': 'NBA',
                'event_time': now + timedelta(hours=28),
                'home_odds': 1.90, 'away_odds': 2.00, 'draw_odds': None
            },
            # Tennis — Grand Slams / ATP
            {
                'sport': 'Tennis', 'home_team': 'Novak Djokovic', 'away_team': 'Carlos Alcaraz',
                'league': 'ATP Masters',
                'event_time': now + timedelta(hours=6),
                'home_odds': 1.80, 'away_odds': 2.05, 'draw_odds': None
            },
            {
                'sport': 'Tennis', 'home_team': 'Jannik Sinner', 'away_team': 'Daniil Medvedev',
                'league': 'ATP Masters',
                'event_time': now + timedelta(hours=32),
                'home_odds': 1.95, 'away_odds': 1.95, 'draw_odds': None
            },
        ]

        event_ids = []
        for ev in events:
            sql = """
                INSERT INTO betting_events
                (sport, home_team, away_team, league, event_time, status, home_odds, away_odds, draw_odds, tenant_id)
                VALUES (%s, %s, %s, %s, %s, 'active', %s, %s, %s, %s)
            """
            cursor.execute(sql, (
                ev['sport'], ev['home_team'], ev['away_team'], ev['league'],
                ev['event_time'], ev['home_odds'], ev['away_odds'], ev['draw_odds'], tenant_id
            ))
            event_ids.append(cursor.lastrowid)

        print(f"   -> Created {len(event_ids)} events. Event IDs: {event_ids}")

        # Map event index to event data for later use
        events_with_ids = list(zip(event_ids, events))

        # ==========================================
        # STEP 2: Normal Baseline (~60 bets)
        # ==========================================
        print("   -> Creating baseline of 60 normal bets...")

        for _ in range(60):
            c_id = random.choice(valid_customers)
            ev_id, ev = random.choice(events_with_ids)

            if ev['sport'] == 'Football':
                selection = random.choices(
                    ['home', 'away', 'draw'], weights=[45, 35, 20]
                )[0]
            else:
                selection = random.choices(['home', 'away'], weights=[50, 50])[0]

            if selection == 'home':
                odds = ev['home_odds']
            elif selection == 'away':
                odds = ev['away_odds']
            else:
                odds = ev['draw_odds']

            stake = round(random.uniform(10, 500), 2)
            potential_payout = round(stake * odds, 2)
            placed_at = fake.date_time_between(start_date='-48h', end_date='now')
            ip = fake.ipv4()

            sql = """
                INSERT INTO bets
                (customer_id, event_id, selection, stake, odds, potential_payout, status, ip_address, placed_at, tenant_id)
                VALUES (%s, %s, %s, %s, %s, %s, 'accepted', %s, %s, %s)
            """
            cursor.execute(sql, (c_id, ev_id, selection, stake, odds, potential_payout, ip, placed_at, tenant_id))

        print("   -> Baseline bets created.")

        # ==========================================
        # STEP 3: Liability Concentration Scenario
        # ==========================================
        # Pick the first football event (Arsenal vs Chelsea)
        football_events = [(eid, ev) for eid, ev in events_with_ids if ev['sport'] == 'Football']
        liability_event_id, liability_event = football_events[0]

        print(f"\n   -> 🚨 LIABILITY CONCENTRATION scenario on Event #{liability_event_id}: "
              f"{liability_event['home_team']} vs {liability_event['away_team']} ({liability_event['league']})")
        print("      12 bets all on 'home', stakes $200–$800, placed within last 2 hours")

        for _ in range(12):
            c_id = random.choice(valid_customers)
            stake = round(random.uniform(200, 800), 2)
            odds = liability_event['home_odds']
            potential_payout = round(stake * odds, 2)
            placed_at = fake.date_time_between(start_date='-2h', end_date='now')
            ip = fake.ipv4()

            sql = """
                INSERT INTO bets
                (customer_id, event_id, selection, stake, odds, potential_payout, status, ip_address, placed_at, tenant_id)
                VALUES (%s, %s, 'home', %s, %s, %s, 'accepted', %s, %s, %s)
            """
            cursor.execute(sql, (c_id, liability_event_id, stake, odds, potential_payout, ip, placed_at, tenant_id))

        print(f"      ✅ Liability concentration seeded on Event #{liability_event_id} "
              f"({liability_event['home_team']} vs {liability_event['away_team']})")

        # ==========================================
        # STEP 4: Velocity Burst Scenario
        # ==========================================
        velocity_customer_id = valid_customers[0]
        velocity_ip = '91.108.56.177'

        # Pick 8 different events (or repeat if fewer available) for variety
        velocity_events = random.choices(events_with_ids, k=8)

        print(f"\n   -> 🚨 VELOCITY BURST scenario: Customer #{velocity_customer_id} | IP: {velocity_ip}")
        print("      8 bets in the last 30 minutes across different events")

        for ev_id, ev in velocity_events:
            if ev['sport'] == 'Football':
                selection = random.choices(['home', 'away', 'draw'], weights=[45, 35, 20])[0]
            else:
                selection = random.choices(['home', 'away'], weights=[50, 50])[0]

            if selection == 'home':
                odds = ev['home_odds']
            elif selection == 'away':
                odds = ev['away_odds']
            else:
                odds = ev['draw_odds']

            stake = round(random.uniform(50, 300), 2)
            potential_payout = round(stake * odds, 2)
            placed_at = fake.date_time_between(start_date='-30m', end_date='now')

            sql = """
                INSERT INTO bets
                (customer_id, event_id, selection, stake, odds, potential_payout, status, ip_address, placed_at, tenant_id)
                VALUES (%s, %s, %s, %s, %s, %s, 'accepted', %s, %s, %s)
            """
            cursor.execute(sql, (velocity_customer_id, ev_id, selection, stake, odds, potential_payout, velocity_ip, placed_at, tenant_id))

        print(f"      ✅ Velocity burst seeded for Customer #{velocity_customer_id} from IP {velocity_ip}")

        # ==========================================
        # STEP 5: Arbitrage bot scenario (Phase A — betting adapter)
        # ==========================================
        # Single IP places opposing bets on the same event within seconds.
        # The home+away pair guarantees a small return regardless of outcome.
        print("\n   -> 🤖 ARBITRAGE BOT scenario...")
        arb_ip = '203.0.113.99'
        arb_customer = valid_customers[1] if len(valid_customers) > 1 else valid_customers[0]
        arb_event_id, arb_event = football_events[1] if len(football_events) > 1 else (event_ids[0], events[0])
        arb_time = now - timedelta(minutes=15)
        for selection, odds_key in [('home', 'home_odds'), ('away', 'away_odds')]:
            odds = arb_event[odds_key]
            stake = round(800.00 if selection == 'home' else 1100.00, 2)  # arb-shaped ratio
            cursor.execute(
                """INSERT INTO bets
                     (customer_id, event_id, selection, stake, odds, potential_payout,
                      status, ip_address, placed_at, tenant_id)
                   VALUES (%s, %s, %s, %s, %s, %s, 'accepted', %s, %s, %s)""",
                (arb_customer, arb_event_id, selection, stake, odds,
                 round(stake * odds, 2), arb_ip,
                 arb_time + timedelta(seconds=random.randint(1, 4)), tenant_id),
            )
        print(f"      ✅ Arbitrage pair seeded on Event #{arb_event_id} from IP {arb_ip}")

        # ==========================================
        # STEP 6: Multi-accounting bonus abuse
        # ==========================================
        # 3 distinct customers, one IP+device, each claiming first-deposit bonus
        # within the same session.
        print("   -> 🎁 BONUS ABUSE scenario (3 accounts, shared session)...")
        shared_session = "sess-" + "".join(random.choices("abcdef0123456789", k=12))
        bonus_customers = random.sample(valid_customers, k=min(3, len(valid_customers)))
        for c_id in bonus_customers:
            cursor.execute(
                """INSERT INTO customer_bonuses
                     (customer_id, bonus_type, claimed_at, qualifying_session, tenant_id)
                   VALUES (%s, 'first_deposit', %s, %s, %s)""",
                (c_id, now - timedelta(hours=2), shared_session, tenant_id),
            )
        print(f"      ✅ Bonus abuse seeded across {len(bonus_customers)} accounts (session={shared_session})")

        # ==========================================
        # STEP 7: Sharp-money / line-movement (odds_history)
        # ==========================================
        # Record odds at several timestamps for the first football event.
        # The pattern: a sudden line movement 10-30 minutes after a high-stake
        # bet from a previously-inactive bettor.
        print("   -> 📉 Odds history snapshots (line movement for sharp-money detection)...")
        sharp_event_id, sharp_event = football_events[0]
        baseline_home = sharp_event['home_odds']
        baseline_away = sharp_event['away_odds']
        baseline_draw = sharp_event['draw_odds']
        for minutes_ago, h_adj, a_adj in [
            (90, 0.00, 0.00),    # baseline
            (60, 0.00, 0.00),    # stable
            (30, -0.10, 0.20),   # line moves AFTER the sharp bet
            (15, -0.15, 0.30),
            (5,  -0.20, 0.40),
        ]:
            cursor.execute(
                """INSERT INTO odds_history
                     (event_id, recorded_at, home_odds, away_odds, draw_odds, suspended, tenant_id)
                   VALUES (%s, %s, %s, %s, %s, FALSE, %s)""",
                (sharp_event_id, now - timedelta(minutes=minutes_ago),
                 round(baseline_home + h_adj, 3),
                 round(baseline_away + a_adj, 3),
                 round(baseline_draw or 3.40, 3) if baseline_draw is not None else None,
                 tenant_id),
            )
        # The sharp bet: high-stake, on a fresh sport profile, 25 minutes before the move
        sharp_customer = valid_customers[4] if len(valid_customers) > 4 else valid_customers[0]
        cursor.execute(
            """INSERT INTO bets
                 (customer_id, event_id, selection, stake, odds, potential_payout,
                  status, ip_address, placed_at, tenant_id)
               VALUES (%s, %s, 'home', 5500.00, %s, %s, 'accepted', %s, %s, %s)""",
            (sharp_customer, sharp_event_id, baseline_home,
             round(5500.00 * baseline_home, 2),
             fake.ipv4(), now - timedelta(minutes=40), tenant_id),
        )
        print(f"      ✅ Sharp-money pattern seeded on Event #{sharp_event_id}")

        conn.commit()

        print("\n✅ Betting data seeded successfully!")
        print("   📊 Summary:")
        print(f"      • {len(event_ids)} betting events (Football, Basketball, Tennis)")
        print(f"      • ~60 baseline bets across all events")
        print(f"      • 12 liability concentration bets on Event #{liability_event_id} "
              f"({liability_event['home_team']} vs {liability_event['away_team']})")
        print(f"      • 8 velocity burst bets from Customer #{velocity_customer_id} (IP: {velocity_ip})")
        print("\n   🎯 Demo points to highlight:")
        print(f"      → Liability alert should fire on: Event #{liability_event_id} — "
              f"{liability_event['home_team']} vs {liability_event['away_team']}")
        print(f"      → Velocity anomaly: Customer #{velocity_customer_id} from IP {velocity_ip}")

    except Exception as e:
        print(f"❌ Error seeding betting data: {e}")
    finally:
        if conn and conn.is_connected():
            conn.close()

if __name__ == "__main__":
    seed_betting_data()
