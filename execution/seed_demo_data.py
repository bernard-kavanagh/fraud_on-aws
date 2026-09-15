import sys
import os
import mysql.connector

# Add parent directory to path to import agent_tools
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent_tools import get_db_connection
from tenancy import resolve_tenant_id

def seed_bernard():
    print("🌱 Seeding Demo Data...")
    tenant_id = resolve_tenant_id()
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)

        # Check if Bernard exists
        cursor.execute("SELECT * FROM customers WHERE name LIKE '%Bernard%' AND tenant_id = %s", (tenant_id,))
        if cursor.fetchone():
            print("✅ User 'Bernard' already exists.")
            return

        # Insert Bernard
        print("👤 Creating user 'Bernard' (VIP)...")
        sql = """
            INSERT INTO customers (name, email, region, vip_status, tenant_id)
            VALUES (%s, %s, %s, %s, %s)
        """
        cursor.execute(sql, ("Bernard", "bernard@example.com", "EMEA", True, tenant_id))
        conn.commit()
        print("✅ User 'Bernard' created successfully!")

    except Exception as e:
        print(f"❌ Error seeding data: {e}")
    finally:
        if conn and conn.is_connected():
            conn.close()

if __name__ == "__main__":
    seed_bernard()
