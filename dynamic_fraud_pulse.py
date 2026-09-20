"""
Dynamic Fraud Pulse — one unified real-time stream.

Merges the normal heartbeat (live_pulse.py) and the fraud scenarios
(execution/seed_fraud_data.py) into a SINGLE continuous stream: most ticks are
ordinary 'cleared' orders, and every so often — probabilistically, or on a fixed
cadence — one live fraud scenario fires (a velocity burst from one IP, a headless
bot checkout, a high-value anomaly, device-fingerprint reuse, gift-card
laundering, or a credential-stuffing burst). Fraud emerges organically alongside
legitimate traffic over time, instead of being dropped in as one historical batch.

Tenant-scoped like everything else: the active tenant comes from $DEMO_TENANT_ID
(never hardcoded). Fraud orders are inserted 'pending' (so the dashboard's alert /
velocity panels light up); normal orders are 'cleared'.

Usage:
  DEMO_TENANT_ID=demo-bank-alpha python dynamic_fraud_pulse.py
  DEMO_TENANT_ID=demo-bank-alpha python dynamic_fraud_pulse.py --interval 0.5 --fraud-prob 0.06
  DEMO_TENANT_ID=demo-bank-alpha python dynamic_fraud_pulse.py --fraud-every-seconds 20
  # (--fraud-every-seconds forces a fixed cadence and overrides --fraud-prob)

Prerequisites: the tenant's customers + products must already exist
(run generate_world.py first). Ctrl+C to stop.
"""

import argparse
import os
import random
import time
from collections import Counter
from datetime import datetime, timedelta

from faker import Faker
from dotenv import load_dotenv

import mysql.connector

from tenancy import resolve_tenant_id

load_dotenv()

config = {
    'host': os.getenv('TIDB_HOST'),
    'port': int(os.getenv('TIDB_PORT', 4000)),
    'user': os.getenv('TIDB_USER'),
    'password': os.getenv('TIDB_PASSWORD'),
    'database': os.getenv('TIDB_DATABASE', 'agentcore_fraud'),
    'ssl_ca': os.getenv('TIDB_SSL_CA'),
    'ssl_verify_cert': True,
}

# Browser fingerprints (mirrors execution/seed_fraud_data.py so the stream and the
# batch seeder produce identically-shaped signals).
NORMAL_USER_AGENTS = [
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/121.0.0.0",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 Mobile/15E148",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/120.0.0.0 Firefox/119.0",
]
HEADLESS_USER_AGENTS = [
    "Mozilla/5.0 (Macintosh) HeadlessChrome/121.0.0.0",
    "Mozilla/5.0 Puppeteer/22.0.0",
    "Playwright/1.41.0",
    "",  # explicit empty UA
]

# The known velocity IP the README demo triggers reference. Repeated bursts
# accumulate here, which only strengthens the velocity signal.
VELOCITY_IP = "185.15.54.22"


def _rand_device_id():
    return "fp-" + "".join(random.choices("abcdef0123456789", k=16))


# ---------------------------------------------------------------------------
# Tick handlers — each returns a short human-readable description of what fired.
# `ctx` bundles the shared per-run state (cursor, tenant, faker, id pools).
# ---------------------------------------------------------------------------
def normal_order(ctx) -> str:
    """The heartbeat: one ordinary 'cleared' order (mirrors live_pulse.py)."""
    c_id = random.choice(ctx.customers)
    p_id, price = random.choice(ctx.products)
    qty = random.randint(1, 3)
    amount = round(float(price) * qty, 2) if price else round(random.uniform(20, 150), 2)
    ip = ctx.fake.ipv4()
    country = ctx.fake.country()[:50]
    ctx.cursor.execute(
        """INSERT INTO orders
             (tenant_id, customer_id, product_id, quantity, amount, ip_address, country,
              status, order_date)
           VALUES (%s, %s, %s, %s, %s, %s, %s, 'cleared', NOW())""",
        (ctx.tenant_id, c_id, p_id, qty, amount, ip, country),
    )
    return f"Order: cust {c_id} | prod {p_id} | ${amount} | {ip}"


def velocity_burst(ctx) -> str:
    """5 rapid 'pending' orders from one IP (card-testing → high-value pull)."""
    n = 5
    for _ in range(n):
        c_id = random.choice(ctx.customers)
        p_id, price = random.choice(ctx.products)
        amount = round(float(price) * 2, 2) if price else 400.0
        ctx.cursor.execute(
            """INSERT INTO orders
                 (tenant_id, customer_id, product_id, quantity, amount, ip_address, country,
                  billing_country, shipping_country, device_id, user_agent,
                  checkout_seconds, status, order_date)
               VALUES (%s, %s, %s, 2, %s, %s, 'Unknown', 'Unknown', 'Unknown', %s, %s, %s,
                       'pending', NOW())""",
            (ctx.tenant_id, c_id, p_id, amount, VELOCITY_IP, _rand_device_id(),
             random.choice(NORMAL_USER_AGENTS), round(random.uniform(20, 60), 2)),
        )
    return f"🚨 VELOCITY BURST: {n} rapid pending orders from IP {VELOCITY_IP}"


def high_value_anomaly(ctx) -> str:
    """One high-value 'pending' order with billing/shipping-country mismatch."""
    c_id = random.choice(ctx.customers)
    p_id, _ = random.choice(ctx.products)
    ctx.cursor.execute(
        """INSERT INTO orders
             (tenant_id, customer_id, product_id, quantity, amount, ip_address, country,
              billing_country, shipping_country, device_id, user_agent,
              checkout_seconds, status, order_date)
           VALUES (%s, %s, %s, 5, 8999.00, %s, 'San Marino', 'United States', 'San Marino',
                   %s, %s, %s, 'pending', NOW())""",
        (ctx.tenant_id, c_id, p_id, ctx.fake.ipv4(), _rand_device_id(),
         random.choice(NORMAL_USER_AGENTS), round(random.uniform(30, 90), 2)),
    )
    return f"🚨 HIGH-VALUE ANOMALY: $8,999 order, cust {c_id}, billing≠shipping"


def headless_bot(ctx) -> str:
    """3 'pending' orders with a headless UA and sub-8s checkout, one shared device."""
    n = 3
    bot_device = _rand_device_id()
    ua = random.choice(HEADLESS_USER_AGENTS)
    for _ in range(n):
        c_id = random.choice(ctx.customers)
        p_id, price = random.choice(ctx.products)
        ctx.cursor.execute(
            """INSERT INTO orders
                 (tenant_id, customer_id, product_id, quantity, amount, ip_address, country,
                  billing_country, shipping_country, device_id, user_agent,
                  checkout_seconds, status, order_date)
               VALUES (%s, %s, %s, 1, %s, %s, 'Romania', 'Romania', 'Romania', %s, %s, %s,
                       'pending', NOW())""",
            (ctx.tenant_id, c_id, p_id, float(price) if price else 250.0,
             ctx.fake.ipv4(), bot_device, ua, round(random.uniform(2.0, 6.5), 2)),
        )
    return f"🚨 HEADLESS BOT: {n} sub-8s checkouts, UA='{ua or '(empty)'}', device {bot_device}"


def device_reuse(ctx) -> str:
    """One 'pending' order across several accounts sharing a device fingerprint."""
    shared_device = _rand_device_id()
    sample = random.sample(ctx.customers, k=min(4, len(ctx.customers)))
    for c_id in sample:
        p_id, _ = random.choice(ctx.products)
        ctx.cursor.execute(
            """INSERT INTO orders
                 (tenant_id, customer_id, product_id, quantity, amount, ip_address, country,
                  billing_country, shipping_country, device_id, user_agent,
                  checkout_seconds, status, order_date)
               VALUES (%s, %s, %s, 1, 350.00, %s, 'Germany', 'Germany', 'Germany',
                       %s, %s, %s, 'pending', NOW())""",
            (ctx.tenant_id, c_id, p_id, ctx.fake.ipv4(),
             shared_device, random.choice(NORMAL_USER_AGENTS), round(random.uniform(15, 40), 2)),
        )
    return f"🚨 DEVICE REUSE: {len(sample)} accounts sharing device {shared_device}"


def giftcard_laundering(ctx) -> str:
    """2 'pending' expedited digital-gift-card orders (first-time monetisation)."""
    pid = ctx.giftcard_product_id or ctx.products[0][0]
    for _ in range(2):
        c_id = random.choice(ctx.customers)
        ctx.cursor.execute(
            """INSERT INTO orders
                 (tenant_id, customer_id, product_id, quantity, amount, ip_address, country,
                  billing_country, shipping_country, device_id, user_agent,
                  checkout_seconds, delivery_expedited, status, order_date)
               VALUES (%s, %s, %s, 1, 750.00, %s, 'Nigeria', 'United Kingdom', 'Nigeria',
                       %s, %s, %s, TRUE, 'pending', NOW())""",
            (ctx.tenant_id, c_id, pid, ctx.fake.ipv4(),
             _rand_device_id(), random.choice(NORMAL_USER_AGENTS), round(random.uniform(10, 25), 2)),
        )
    return f"🚨 GIFT-CARD LAUNDERING: 2 expedited digital gift-card orders (prod {pid})"


def credential_stuffing(ctx) -> str:
    """6 failed logins then a success for one victim (ATO precursor)."""
    victim = random.choice(ctx.customers)
    now = datetime.now()
    for i in range(6):
        ctx.cursor.execute(
            """INSERT INTO login_attempts
                 (tenant_id, customer_id, ip_address, country, success, attempted_at)
               VALUES (%s, %s, %s, 'Russia', FALSE, %s)""",
            (ctx.tenant_id, victim, ctx.fake.ipv4(), now - timedelta(minutes=30 - i * 4)),
        )
    ctx.cursor.execute(
        """INSERT INTO login_attempts
             (tenant_id, customer_id, ip_address, country, success, attempted_at)
           VALUES (%s, %s, %s, 'Russia', TRUE, %s)""",
        (ctx.tenant_id, victim, ctx.fake.ipv4(), now),
    )
    return f"🚨 CREDENTIAL STUFFING: 6 failed + 1 success login for cust {victim}"


# (name, weight) — weights bias which fraud scenario fires when a fraud tick hits.
FRAUD_SCENARIOS = [
    ("velocity_burst", 3, velocity_burst),
    ("high_value_anomaly", 2, high_value_anomaly),
    ("headless_bot", 2, headless_bot),
    ("device_reuse", 1, device_reuse),
    ("giftcard_laundering", 1, giftcard_laundering),
    ("credential_stuffing", 1, credential_stuffing),
]


class _Ctx:
    """Shared per-run state passed to every tick handler."""
    __slots__ = ("cursor", "tenant_id", "fake", "customers", "products", "giftcard_product_id")


def _pick_fraud():
    names = [s[0] for s in FRAUD_SCENARIOS]
    weights = [s[1] for s in FRAUD_SCENARIOS]
    fns = {s[0]: s[2] for s in FRAUD_SCENARIOS}
    name = random.choices(names, weights=weights)[0]
    return name, fns[name]


def run(interval: float, fraud_prob: float, fraud_every_seconds: float | None):
    tenant_id = resolve_tenant_id()
    fake = Faker()
    conn = mysql.connector.connect(**config)
    cursor = conn.cursor()

    # Prerequisites — same tenant scope the batch scripts use.
    cursor.execute("SELECT customer_id FROM customers WHERE tenant_id = %s", (tenant_id,))
    customers = [r[0] for r in cursor.fetchall()]
    cursor.execute("SELECT product_id, price FROM products WHERE tenant_id = %s", (tenant_id,))
    products = cursor.fetchall()
    if not customers or not products:
        print(f"❌ Tenant '{tenant_id}' has no customers/products. Run generate_world.py first.")
        conn.close()
        return

    # Ensure one product is a digital gift card so the laundering scenario is meaningful.
    giftcard_product_id = products[0][0]
    cursor.execute(
        "UPDATE products SET is_digital_giftcard = TRUE WHERE product_id = %s AND tenant_id = %s",
        (giftcard_product_id, tenant_id),
    )
    conn.commit()

    ctx = _Ctx()
    ctx.cursor = cursor
    ctx.tenant_id = tenant_id
    ctx.fake = fake
    ctx.customers = customers
    ctx.products = products
    ctx.giftcard_product_id = giftcard_product_id

    mode = (f"cadence: one fraud scenario every {fraud_every_seconds}s"
            if fraud_every_seconds else f"probabilistic: {fraud_prob:.0%} of ticks are fraud")
    print(f"🏦 Dynamic Fraud Pulse — tenant '{tenant_id}' | {len(customers)} customers, "
          f"{len(products)} products")
    print(f"💓 {mode} | interval {interval}s | Ctrl+C to stop\n")

    stats = Counter()
    last_fraud = time.monotonic()
    try:
        while True:
            now = time.monotonic()
            if fraud_every_seconds is not None:
                fire_fraud = (now - last_fraud) >= fraud_every_seconds
            else:
                fire_fraud = random.random() < fraud_prob

            if fire_fraud:
                name, fn = _pick_fraud()
                desc = fn(ctx)
                stats[name] += 1
                last_fraud = now
                print(f"   {desc}")
            else:
                desc = normal_order(ctx)
                stats["normal_order"] += 1
                print(f"   -> Live {desc}")

            conn.commit()
            time.sleep(interval)
    except KeyboardInterrupt:
        total = sum(stats.values())
        frauds = total - stats["normal_order"]
        print("\n🛑 Pulse stopped.")
        print(f"   {total} ticks | {stats['normal_order']} normal | {frauds} fraud events")
        for name, _, _ in FRAUD_SCENARIOS:
            if stats[name]:
                print(f"     - {name}: {stats[name]}")
    finally:
        if conn.is_connected():
            conn.close()


def main():
    p = argparse.ArgumentParser(description="Unified normal+fraud real-time stream.")
    p.add_argument("--interval", type=float, default=0.5, help="seconds between ticks (default 0.5)")
    p.add_argument("--fraud-prob", type=float, default=0.06,
                   help="probability a tick is a fraud scenario (default 0.06 → ~94%% normal)")
    p.add_argument("--fraud-every-seconds", type=float, default=None,
                   help="force one fraud scenario every N seconds (overrides --fraud-prob)")
    args = p.parse_args()
    run(args.interval, args.fraud_prob, args.fraud_every_seconds)


if __name__ == "__main__":
    main()
