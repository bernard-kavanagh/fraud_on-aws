import sys
import os
import random
import json
from datetime import datetime, timedelta
from faker import Faker

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent_tools import get_db_connection
from tenancy import resolve_tenant_id

# Realistic browser fingerprints for normal traffic
NORMAL_USER_AGENTS = [
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/121.0.0.0",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 Mobile/15E148",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/120.0.0.0 Firefox/119.0",
]

# Headless-browser signatures the demo will flag
HEADLESS_USER_AGENTS = [
    "Mozilla/5.0 (Macintosh) HeadlessChrome/121.0.0.0",
    "Mozilla/5.0 Puppeteer/22.0.0",
    "Playwright/1.41.0",
    "",  # explicit empty UA
]

# Email domain pools by trust level
TRUSTED_EMAIL_DOMAINS = ["company.com", "fastmail.com", "protonmail.com"]
FREE_EMAIL_DOMAINS = ["gmail.com", "yahoo.com", "hotmail.com", "outlook.com"]


def _rand_device_id():
    return "fp-" + "".join(random.choices("abcdef0123456789", k=16))


def seed_fraud_scenarios():
    tenant_id = resolve_tenant_id()
    print(f"🕵️  Seeding fraud-detection data (Phase A schema) for tenant '{tenant_id}'...")
    conn = None
    fake = Faker()

    try:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)

        # ----- Customer + product prerequisites -----
        cursor.execute("SELECT customer_id FROM customers WHERE tenant_id = %s", (tenant_id,))
        valid_customers = [r['customer_id'] for r in cursor.fetchall()]

        cursor.execute("SELECT product_id, price FROM products WHERE tenant_id = %s", (tenant_id,))
        products_data = cursor.fetchall()

        if not valid_customers or not products_data:
            print("❌ Run generate_world.py + seed_demo_data.py first.")
            return

        # ----- Update existing customers with email_domain + timezone -----
        # Most customers use trusted free email; a fraction get flagged as
        # "free provider thin-file" candidates for the synthetic-identity pattern.
        print("   -> Backfilling customers.email_domain + timezone...")
        for c_id in valid_customers:
            domain = random.choice(FREE_EMAIL_DOMAINS if random.random() < 0.7 else TRUSTED_EMAIL_DOMAINS)
            tz = random.choice(["America/New_York", "Europe/London", "Europe/Paris", "Asia/Tokyo", "Australia/Sydney"])
            cursor.execute(
                "UPDATE customers SET email_domain = %s, timezone = %s WHERE customer_id = %s AND email_domain IS NULL AND tenant_id = %s",
                (domain, tz, c_id, tenant_id),
            )

        # ----- Mark one product as a digital gift card (for laundering pattern) -----
        cursor.execute(
            "UPDATE products SET is_digital_giftcard = TRUE WHERE product_id = %s AND tenant_id = %s",
            (products_data[0]['product_id'], tenant_id),
        )

        # ----- SCENARIO 1: Normal baseline -----
        print("   -> Normal baseline (20 cleared orders, human checkout times)...")
        for _ in range(20):
            c_id = random.choice(valid_customers)
            prod = random.choice(products_data)
            p_id, price = prod['product_id'], prod['price']
            qty = random.randint(1, 2)
            amount = float(price * qty) if price else random.uniform(20.0, 150.0)
            date = fake.date_time_between(start_date='-7d', end_date='now')
            ip = fake.ipv4()
            country = fake.country()[:50]

            cursor.execute(
                """INSERT INTO orders
                     (tenant_id, customer_id, product_id, quantity, amount, ip_address, country,
                      billing_country, shipping_country, shipping_address,
                      device_id, user_agent, checkout_seconds, delivery_expedited,
                      status, order_date)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'cleared', %s)""",
                (tenant_id, c_id, p_id, qty, amount, ip, country,
                 country, country, fake.address(),
                 _rand_device_id(), random.choice(NORMAL_USER_AGENTS),
                 round(random.uniform(45.0, 180.0), 2),  # human checkout time
                 False, date),
            )

        # ----- SCENARIO 2: Velocity burst (same IP, multiple rapid orders) -----
        print("   -> Velocity burst: 5 orders from IP 185.15.54.22 in the last hour...")
        fraud_ip = "185.15.54.22"
        fraud_country = "Unknown"
        for _ in range(5):
            c_id = random.choice(valid_customers)
            prod = random.choice(products_data)
            amount = float(prod['price']) * 2 if prod['price'] else 400.0
            date = fake.date_time_between(start_date='-1h', end_date='now')
            cursor.execute(
                """INSERT INTO orders
                     (tenant_id, customer_id, product_id, quantity, amount, ip_address, country,
                      billing_country, shipping_country, device_id, user_agent,
                      checkout_seconds, status, order_date)
                   VALUES (%s, %s, %s, 2, %s, %s, %s, %s, %s, %s, %s, %s, 'pending', %s)""",
                (tenant_id, c_id, prod['product_id'], amount, fraud_ip, fraud_country,
                 fraud_country, fraud_country, _rand_device_id(),
                 random.choice(NORMAL_USER_AGENTS),
                 round(random.uniform(20.0, 60.0), 2), date),
            )

        # ----- SCENARIO 3: High-value first-time anomaly -----
        print("   -> High-value first-time anomaly ($8,999 to San Marino)...")
        c_id = valid_customers[0]
        prod = random.choice(products_data)
        date = fake.date_time_between(start_date='-1h', end_date='now')
        cursor.execute(
            """INSERT INTO orders
                 (tenant_id, customer_id, product_id, quantity, amount, ip_address, country,
                  billing_country, shipping_country, device_id, user_agent,
                  checkout_seconds, status, order_date)
               VALUES (%s, %s, %s, 5, 8999.00, %s, 'San Marino', 'United States', 'San Marino',
                       %s, %s, %s, 'pending', %s)""",
            (tenant_id, c_id, prod['product_id'], fake.ipv4(),
             _rand_device_id(), random.choice(NORMAL_USER_AGENTS),
             round(random.uniform(30.0, 90.0), 2), date),
        )

        # ----- SCENARIO 4: Headless-browser / automation -----
        print("   -> Headless-browser automation (sub-8s checkout, Puppeteer UA)...")
        bot_device = _rand_device_id()
        for _ in range(3):
            c_id = random.choice(valid_customers)
            prod = random.choice(products_data)
            cursor.execute(
                """INSERT INTO orders
                     (tenant_id, customer_id, product_id, quantity, amount, ip_address, country,
                      billing_country, shipping_country, device_id, user_agent,
                      checkout_seconds, status, order_date)
                   VALUES (%s, %s, %s, 1, %s, %s, 'Romania', 'Romania', 'Romania', %s, %s, %s,
                           'pending', NOW())""",
                (tenant_id, c_id, prod['product_id'],
                 float(prod['price']) if prod['price'] else 250.0,
                 fake.ipv4(), bot_device,
                 random.choice(HEADLESS_USER_AGENTS),
                 round(random.uniform(2.0, 6.5), 2)),  # sub-8s = automation signal
            )

        # ----- SCENARIO 5: Device-fingerprint reuse across accounts -----
        print("   -> Device-fingerprint reuse: one device_id across 4 accounts in 5 days...")
        shared_device = _rand_device_id()
        for c_id in random.sample(valid_customers, k=min(4, len(valid_customers))):
            cursor.execute(
                """INSERT INTO orders
                     (tenant_id, customer_id, product_id, quantity, amount, ip_address, country,
                      billing_country, shipping_country, device_id, user_agent,
                      checkout_seconds, status, order_date)
                   VALUES (%s, %s, %s, 1, 350.00, %s, 'Germany', 'Germany', 'Germany',
                           %s, %s, %s, 'pending', NOW())""",
                (tenant_id, c_id, products_data[0]['product_id'], fake.ipv4(),
                 shared_device, random.choice(NORMAL_USER_AGENTS),
                 round(random.uniform(15.0, 40.0), 2)),
            )

        # ----- SCENARIO 6: Gift-card laundering -----
        print("   -> Gift-card laundering (digital giftcard, expedited, first-time)...")
        giftcard_pid = products_data[0]['product_id']  # marked is_digital_giftcard above
        for _ in range(2):
            c_id = random.choice(valid_customers)
            cursor.execute(
                """INSERT INTO orders
                     (tenant_id, customer_id, product_id, quantity, amount, ip_address, country,
                      billing_country, shipping_country, device_id, user_agent,
                      checkout_seconds, delivery_expedited, status, order_date)
                   VALUES (%s, %s, %s, 1, 750.00, %s, 'Nigeria', 'United Kingdom', 'Nigeria',
                           %s, %s, %s, TRUE, 'pending', NOW())""",
                (tenant_id, c_id, giftcard_pid, fake.ipv4(),
                 _rand_device_id(), random.choice(NORMAL_USER_AGENTS),
                 round(random.uniform(10.0, 25.0), 2)),
            )

        # ----- SCENARIO 7: Credential-stuffing precursor (login_attempts) -----
        print("   -> Credential-stuffing burst: 6 failed logins, then success...")
        victim_id = valid_customers[1] if len(valid_customers) > 1 else valid_customers[0]
        now = datetime.now()
        for i in range(6):
            cursor.execute(
                """INSERT INTO login_attempts
                     (tenant_id, customer_id, ip_address, country, success, attempted_at)
                   VALUES (%s, %s, %s, 'Russia', FALSE, %s)""",
                (tenant_id, victim_id, fake.ipv4(), now - timedelta(minutes=30 - i * 4)),
            )
        cursor.execute(
            """INSERT INTO login_attempts
                 (tenant_id, customer_id, ip_address, country, success, attempted_at)
               VALUES (%s, %s, %s, 'Russia', TRUE, %s)""",
            (tenant_id, victim_id, fake.ipv4(), now - timedelta(minutes=2)),
        )

        # ----- SCENARIO 8: Refund — serial returner -----
        print("   -> Serial-returner profile (5 refunds, item-not-received)...")
        serial_id = valid_customers[2] if len(valid_customers) > 2 else valid_customers[0]
        cursor.execute(
            "SELECT order_id FROM orders WHERE customer_id = %s AND tenant_id = %s LIMIT 5",
            (serial_id, tenant_id),
        )
        serial_orders = cursor.fetchall()
        for row in serial_orders:
            cursor.execute(
                """INSERT INTO refunds
                     (tenant_id, order_id, customer_id, reason, refund_method, refunded_at)
                   VALUES (%s, %s, %s, 'item_not_received', 'original', %s)""",
                (tenant_id, row['order_id'], serial_id,
                 fake.date_time_between(start_date='-60d', end_date='now')),
            )

        # ----- SCENARIO 9: Chargeback precursor -----
        print("   -> Chargeback double-dip: 2 chargebacks on different cards in 6 months...")
        cb_id = valid_customers[3] if len(valid_customers) > 3 else valid_customers[0]
        for last4 in ["4242", "5512"]:
            cursor.execute(
                """INSERT INTO chargebacks
                     (tenant_id, customer_id, card_last4, filed_at, delivery_confirmed_at)
                   VALUES (%s, %s, %s, %s, %s)""",
                (tenant_id, cb_id, last4,
                 fake.date_time_between(start_date='-180d', end_date='-30d'),
                 fake.date_time_between(start_date='-180d', end_date='-32d')),
            )

        # ----- Freight-forwarder lookup seed -----
        print("   -> Seeding freight-forwarder lookup (3 known forwarders)...")
        for pattern, country in [
            ("%MyUS%", "United States"),
            ("%Shipito%", "United States"),
            ("%Forward2Me%", "United Kingdom"),
        ]:
            cursor.execute(
                "SELECT forwarder_id FROM freight_forwarders WHERE address_pattern = %s AND tenant_id = %s",
                (pattern, tenant_id),
            )
            if not cursor.fetchone():
                cursor.execute(
                    "INSERT INTO freight_forwarders (tenant_id, address_pattern, country) VALUES (%s, %s, %s)",
                    (tenant_id, pattern, country),
                )

        conn.commit()
        print("✅ Fraud dataset (Phase A) injected successfully.")

    except Exception as e:
        print(f"❌ Error seeding fraud data: {e}")
    finally:
        if conn and conn.is_connected():
            conn.close()


if __name__ == "__main__":
    seed_fraud_scenarios()
