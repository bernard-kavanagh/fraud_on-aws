"""
Live two-source contradiction scenario, driven through the governed fact layer's
record_fact over the Gateway (task 11).

This is the reconciliation story made concrete and LIVE (single-mode: automatic
authority-rank supersede, or dispute — exactly what the fact layer's
adjudication.py already does). No reconciliation queue, no human-review UI, no
evidence-weighted mode — those remain a POC-phase decision for a real customer
conversation, not built here.

It reuses the Clayton Knight chargeback narrative from the repo docs, mapped onto
seeded fraud entities:

  SCENARIO A — supersede by authority (higher authority wins; prior retained)
    subject = fraud:customer:<A>   predicate = status
    1) source=agent_inference (rank 40)   value="cleared"      -> assert
    2) source=system_of_record (rank 100) value="fraudulent"   -> SUPERSEDE
       (fact_current re-points to 'fraudulent'; the prior 'cleared' event stays
        in the append-only, hash-chained fact_event log — the audit trail.)

  SCENARIO B — equal-authority contradiction -> disputed (both retained)
    subject = fraud:customer:<B>   predicate = status
    1) source=user_assertion (rank 60)    value="cleared"      -> assert
    2) source=user_assertion (rank 60)    value="fraudulent"   -> DISPUTE
       (fact_current.status='disputed'; no recency tiebreak; both events kept.)

What this script can show directly (as a caller): record_fact's returned
decision/op/status/competing_event_ids for each write, and the tenant metric
deltas (superseded_count, disputed_count) via query_tenant_metrics. To SEE the
prior claim still in the audit trail, run the single-query RCA in
sql/rca_lineage.sql against the fact layer's TiDB with the printed subject +
tenant (the caller has no direct fact_event read — that is the operator's view).

Usage:
  DEMO_TENANT_ID=demo-bank-alpha \
  FACT_LAYER_GATEWAY_URL=... COGNITO_CLIENT_ID=... COGNITO_USERNAME=... \
  COGNITO_PASSWORD=... AWS_REGION=... \
  python scenarios/contradiction_demo.py
"""

import os
import sys
import json
import uuid

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import fact_layer_client as fact_layer
from tenancy import resolve_tenant_id, resolve_agent_id, new_session_id

CONF = 0.90  # above the fact layer's server-side write-control floor (0.85)


PRED = fact_layer.FRAUD_PREDICATE  # 'fraud_status' — predicate-specific authority


def _record(tenant_id, subject, value, source, session_id, agent_id, label,
            assessor_type=None, evidence=None):
    print(f"\n--- {label} ---")
    print(f"    subject={subject} value={value!r} source={source}")
    res = fact_layer.record_fact(
        tenant_id=tenant_id, subject=subject, predicate=PRED,
        value=value, source=source, confidence=CONF,
        agent_id=agent_id, session_id=session_id,
        assessor_type=assessor_type, evidence=evidence,
    )
    print("    ->", json.dumps(res, default=str))
    return res


def _metric(tenant_id, name):
    r = fact_layer.query_tenant_metrics(tenant_id=tenant_id, metric=name)
    return r.get("value") if isinstance(r, dict) else r


def main():
    tenant_id = resolve_tenant_id()
    agent_id = resolve_agent_id()
    session_id = new_session_id()
    run = uuid.uuid4().hex[:8]  # fresh subjects per run so prior state can't interfere

    subject_a = fact_layer.entity_subject("fraud", f"clayton-knight-{run}")
    subject_b = fact_layer.entity_subject("fraud", f"rejected-{run}")
    subject_c = fact_layer.entity_subject("fraud", f"disputed-{run}")

    print("=" * 72)
    print(f"Governed-fact adjudication demo (predicate={PRED}) — tenant={tenant_id} run={run}")
    print("=" * 72)

    # ---- SCENARIO A: higher authority SUPERSEDES (prior retained) ----
    print("\n### SCENARIO A — higher authority supersedes ###")
    _record(tenant_id, subject_a, "cleared", fact_layer.SOURCE_AGENT,
            session_id, agent_id, "A1 agent_inference (55): 'cleared'",
            assessor_type="agent")
    a2 = _record(tenant_id, subject_a, "confirmed", fact_layer.SOURCE_HUMAN,
                 session_id, agent_id, "A2 human_investigator (100): 'confirmed'",
                 assessor_type="human",
                 evidence=[{"evidence_type": "investigation", "evidence_ref": "INV-847"},
                           {"evidence_type": "chargeback", "evidence_ref": "CB-991"}])
    print(f"    EXPECT SUPERSEDED -> {'PASS' if a2.get('outcome') == 'SUPERSEDED' else 'CHECK'} "
          f"(outcome={a2.get('outcome')}, status={a2.get('status')})")

    # ---- SCENARIO B: LOWER authority contradiction is REJECTED (not disputed) ----
    print("\n### SCENARIO B — lower-authority contradiction is REJECTED (winner stands) ###")
    _record(tenant_id, subject_b, "confirmed", fact_layer.SOURCE_FRAUD_REVIEW,
            session_id, agent_id, "B1 fraud_review (95): 'confirmed'", assessor_type="system")
    b2 = _record(tenant_id, subject_b, "legitimate", fact_layer.SOURCE_USER,
                 session_id, agent_id, "B2 user_assertion (20): 'legitimate'",
                 assessor_type="user")
    b_ok = b2.get("outcome") == "REJECTED" and b2.get("status") != "disputed"
    print(f"    EXPECT REJECTED, current stays 'confirmed' -> {'PASS' if b_ok else 'CHECK'} "
          f"(outcome={b2.get('outcome')}, status={b2.get('status')})")

    # ---- SCENARIO C: comparable authority -> DISPUTED ----
    print("\n### SCENARIO C — comparable authority contradiction -> DISPUTED ###")
    _record(tenant_id, subject_c, "confirmed", fact_layer.SOURCE_HUMAN,
            session_id, agent_id, "C1 human_investigator (100): 'confirmed'", assessor_type="human")
    c2 = _record(tenant_id, subject_c, "legitimate", fact_layer.SOURCE_HUMAN,
                 session_id, agent_id, "C2 human_investigator (100): 'legitimate'", assessor_type="human")
    print(f"    EXPECT DISPUTED -> {'PASS' if c2.get('status') == 'disputed' else 'CHECK'} "
          f"(outcome={c2.get('outcome')}, status={c2.get('status')}, "
          f"competing={c2.get('competing_event_ids')})")

    # ---- explain_fact: WHY does subject_a hold? ----
    print("\n### explain_fact — why is subject_a what it is? ###")
    exp = fact_layer.explain_fact(tenant_id=tenant_id, subject=subject_a, predicate=PRED)
    print(json.dumps(exp, default=str, indent=2)[:1200])

    print("\n" + "=" * 72)
    print("Audit trail (append-only, hash-chained): run sql/rca_lineage.sql on the")
    print("fact layer's TiDB with tenant_id + subject to see every prior claim, e.g.:")
    print(f"    tenant={tenant_id!r} subject={subject_a!r}  (A: cleared -> SUPERSEDED by confirmed)")
    print(f"    tenant={tenant_id!r} subject={subject_b!r}  (B: confirmed; legitimate REJECTED, retained)")
    print(f"    tenant={tenant_id!r} subject={subject_c!r}  (C: two comparable claims -> disputed)")
    print("=" * 72)


if __name__ == "__main__":
    main()
