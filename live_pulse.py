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
    'database': os.getenv('TIDB_DATABASE', 'test'),
    'ssl_ca': os.getenv('TIDB_SSL_CA'),
    'ssl_verify_cert': True
}

def heartbeat():
    tenant_id = resolve_tenant_id()
    print(f"🏦 Live Pulse running as tenant: {tenant_id}")

    fake = Faker()
    conn = mysql.connector.connect(**config)
    cursor = conn.cursor()

    # 1. Fetch IDs and prices to ensure Referential Integrity
    cursor.execute("SELECT customer_id FROM customers WHERE tenant_id = %s", (tenant_id,))
    valid_customers = [row[0] for row in cursor.fetchall()]

    cursor.execute("SELECT product_id, price FROM products WHERE tenant_id = %s", (tenant_id,))
    valid_products = cursor.fetchall()  # list of (product_id, price) tuples

    print("💓 Starting Live Pulse... (Press Ctrl+C to stop)")

    try:
        while True:
            # Randomly pick a customer and product
            c_id = random.choice(valid_customers)
            product = random.choice(valid_products)
            p_id, price = product[0], float(product[1])
            qty = random.randint(1, 3)
            amount = round(price * qty, 2)
            ip = fake.ipv4()
            country = fake.country()[:50]

            # Insert as 'cleared' — only seeded fraud data should be 'pending'
            sql = """INSERT INTO orders
                     (customer_id, product_id, quantity, amount, ip_address, country, status, tenant_id)
                     VALUES (%s, %s, %s, %s, %s, %s, 'cleared', %s)"""
            cursor.execute(sql, (c_id, p_id, qty, amount, ip, country, tenant_id))
            conn.commit()

            print(f"   -> Live Order: Customer {c_id} | Product {p_id} | ${amount} | {ip}")
            time.sleep(0.5)  # The "Heartbeat" frequency
            
    except KeyboardInterrupt:
        print("\n🛑 Pulse Stopped.")
        conn.close()

if __name__ == "__main__":
    heartbeat()