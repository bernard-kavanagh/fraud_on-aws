import sys
import os
import mysql.connector
import random
from faker import Faker

# Add parent directory to path to import agent_tools
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent_tools import get_db_connection
from tenancy import resolve_tenant_id

def seed_bernard_orders():
    print("🌱 Seeding Orders for Bernard...")
    tenant_id = resolve_tenant_id()
    conn = None
    fake = Faker()

    try:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)

        # 1. Get Bernard's ID
        cursor.execute("SELECT customer_id FROM customers WHERE name LIKE '%Bernard%' AND tenant_id = %s LIMIT 1", (tenant_id,))
        result = cursor.fetchone()
        
        if not result:
            print("❌ Bernard not found! Run seed_demo_data.py first.")
            return
            
        bernard_id = result['customer_id']
        print(f"👤 Found Bernard (ID: {bernard_id})")

        # 2. Get Product IDs
        cursor.execute("SELECT product_id FROM products WHERE tenant_id = %s", (tenant_id,))
        products = cursor.fetchall()
        
        if not products:
            print("❌ No products found! Run generate_world.py first.")
            return
            
        product_ids = [p['product_id'] for p in products]

        # 3. Create Orders
        num_orders = 5
        print(f"📦 Creating {num_orders} orders...")
        
        for _ in range(num_orders):
            p_id = random.choice(product_ids)
            qty = random.randint(1, 3)
            # Create simulated past dates
            date = fake.date_time_between(start_date='-1M', end_date='now')
            
            sql = "INSERT INTO orders (customer_id, product_id, quantity, order_date, tenant_id) VALUES (%s, %s, %s, %s, %s)"
            cursor.execute(sql, (bernard_id, p_id, qty, date, tenant_id))
            
        conn.commit()
        print("✅ Orders created successfully!")

    except Exception as e:
        print(f"❌ Error seeding orders: {e}")
    finally:
        if conn and conn.is_connected():
            conn.close()

if __name__ == "__main__":
    seed_bernard_orders()
