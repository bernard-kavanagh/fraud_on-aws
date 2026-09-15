"""
CLI entry-point for running a cognitive-foundation investigation on the
betting adapter. Demonstrates Thesis 11 — same substrate, different plugin.

Usage:
  python execution/betting_investigation.py "<trigger text>" [entity_ref]
"""

import os
import sys
import uuid
import json

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cognitive_loop import run_investigation
from agent_tools import create_session
from adapters import betting
from tenancy import resolve_tenant_id, resolve_agent_id


def main():
    if len(sys.argv) < 2:
        print("Usage: DEMO_TENANT_ID=demo-bank-alpha python execution/betting_investigation.py "
              "\"<trigger text>\" [entity_ref]")
        sys.exit(1)

    trigger = sys.argv[1]
    entity_ref = sys.argv[2] if len(sys.argv) > 2 else None

    # Identity for this invocation (task 9): tenant from $DEMO_TENANT_ID (never
    # hardcoded); agent id for fact provenance.
    tenant_id = resolve_tenant_id()
    agent_id = resolve_agent_id()

    session_id = str(uuid.uuid4())
    create_session(
        session_id,
        user_id=str(entity_ref) if entity_ref else "betting_cli",
        metadata={
            "source": "betting_investigation.cli",
            "tenant_id": tenant_id,
            "entity_ref": str(entity_ref) if entity_ref else None,
        },
    )

    def log(event_type, payload):
        print(f"[{event_type}] " + json.dumps(payload, default=str)[:400])

    result = run_investigation(
        trigger_text=trigger,
        session_id=session_id,
        entity_ref=entity_ref,
        on_event=log,
        adapter=betting,
        tenant_id=tenant_id,
        agent_id=agent_id,
    )

    print("\n=== ROUTING ===")
    print(json.dumps(result.get("routing", {}), default=str, indent=2))

    print("\n=== SUMMARY ===")
    summary = result.get("summary", {})
    print(summary.get("report") or summary.get("error") or "(no summary)")


if __name__ == "__main__":
    main()
