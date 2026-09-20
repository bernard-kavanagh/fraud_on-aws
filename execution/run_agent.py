import sys
import os
import uuid
import json

# Add parent directory to path to import agent_tools
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent_tools import execute_sql, vector_search, log_interaction, create_session
from tenancy import resolve_tenant_id

def run_agent_loop():
    print("🤖 TiDB Unified Agent Initialized.")
    print("Type 'exit' to quit.")

    # Tenant scope for this session (task 9): from $DEMO_TENANT_ID, never hardcoded.
    tenant_id = resolve_tenant_id()
    print(f"Tenant: {tenant_id}")

    # Create a session ID for this conversation
    session_id = str(uuid.uuid4())
    print(f"Session ID: {session_id}")

    # Initialize Session in DB (Fixes ForeignKey Error)
    create_session(session_id, tenant_id, user_id="demo_user",
                   metadata={"source": "run_agent.cli"})
    
    # Helper to print and log
    def respond(role, content, tool_used=None):
        print(f"\n[{role.upper()}]: {content}")
        log_interaction(session_id, role, content, tool_used)

    while True:
        user_input = input("\n👤 User: ")
        if user_input.lower() in ['exit', 'quit']:
            break
            
        # 1. Log User Input
        log_interaction(session_id, 'user', user_input)
        
        # 2. Planning (Simple Heuristic for Demo)
        # In a real agent, an LLM would decide this. Here we hardcode the "Chain of Thought"
        # described in agent.md for the "Return my laptop" use case.
        
        print("\n⚙️  Thinking... (Chain of Thought)")
        
        # Step A: Identify Entities (Hard SQL)
        print("   -> 1. Identifying Customer & Orders (SQL)...")
        
        # 1. Find Customer (tenant-scoped)
        customer_sql = f"SELECT * FROM customers WHERE name LIKE '%Bernard%' AND tenant_id = '{tenant_id}' LIMIT 1;"
        customer_results = json.loads(execute_sql(customer_sql))

        if isinstance(customer_results, list) and len(customer_results) > 0:
            customer = customer_results[0]
            customer_id = customer['customer_id']
            respond('tool', f"Customer Found: {customer['name']} (VIP: {customer['vip_status']})", tool_used="execute_sql")

            # 2. Find Orders for Customer (tenant-scoped join)
            order_sql = f"""
                SELECT o.order_id, p.name as product, p.price, o.order_date
                FROM orders o
                JOIN products p ON o.product_id = p.product_id AND p.tenant_id = o.tenant_id
                WHERE o.customer_id = {customer_id} AND o.tenant_id = '{tenant_id}'
                ORDER BY o.order_date DESC
                LIMIT 5;
            """
            order_data = execute_sql(order_sql)
            respond('tool', f"Recent Orders: {order_data}", tool_used="execute_sql")

            customer_context = f"Customer: {customer}\nOrders: {order_data}"
        else:
            respond('tool', "Customer 'Bernard' not found.", tool_used="execute_sql")
            customer_context = "Customer not found."

        # Step B: Retrieve Context (Vector Search, tenant-scoped)
        print("   -> 2. Retrieving Policies (Vector)...")
        # vector_search returns a string, so we just use it
        policy_data = vector_search(user_input, "sales_knowledge", tenant_id=tenant_id)
        respond('tool', f"Policy Context: {policy_data}", tool_used="vector_search")
        
        # Step C: Synthesize (The "LLM" part)
        # Since we don't have an LLM connected in this script (it uses local tools),
        # we will output the *context* needed for the answer.
        
        final_response = f"""
        Based on the data:
        1. Entity Context: 
        {customer_context}
        
        2. Relevant Policies:
        {policy_data}
        
        (In a full LLM integration, I would now generate a natural language response combining these facts.)
        """
        
        respond('assistant', final_response)

if __name__ == "__main__":
    run_agent_loop()
