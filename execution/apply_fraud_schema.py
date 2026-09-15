import sys
import os
import mysql.connector

# Add parent directory to path to import agent_tools
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent_tools import get_db_connection

def update_schema():
    print("🔄 Updating Schema for Fraud Detection...")
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        
        # Add columns to orders table
        alter_statements = [
            "ALTER TABLE orders ADD COLUMN amount DECIMAL(10,2);",
            "ALTER TABLE orders ADD COLUMN ip_address VARCHAR(45);",
            "ALTER TABLE orders ADD COLUMN country VARCHAR(50);",
            "ALTER TABLE orders ADD COLUMN status ENUM('pending','flagged','cleared','fraudulent') DEFAULT 'pending';",
            "ALTER TABLE orders ADD COLUMN flagged_reason TEXT;"
        ]
        
        for statement in alter_statements:
            try:
                print(f"Executing: {statement}")
                cursor.execute(statement)
                print("✅ Success")
            except mysql.connector.Error as err:
                # Code 1060 means Duplicate column name, which is fine if it already exists
                if err.errno == 1060:
                    print("⚠️ Column already exists, skipping.")
                else:
                    print(f"❌ Error: {err}")
                    
        conn.commit()
        print("🎉 Schema update complete!")

    except Exception as e:
        print(f"❌ Connection Error: {e}")
    finally:
        if conn and conn.is_connected():
            conn.close()

if __name__ == "__main__":
    update_schema()
