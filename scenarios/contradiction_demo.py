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


def _record(tenant_id, subject, value, source, session_id, agent_id, label):
    print(f"\n--- {label} ---")
    print(f"    subject={subject} value={value!r} source={source}")
    res = fact_layer.record_fact(
        tenant_id=tenant_id, subject=subject, predicate="status",
        value=value, source=source, confidence=CONF,
        agent_id=agent_id, session_id=session_id,
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
    subject_b = fact_layer.entity_subject("fraud", f"same-authority-{run}")

    print("=" * 72)
    print(f"Governed-fact contradiction demo — tenant={tenant_id} run={run}")
    print("=" * 72)

    before_sup = _metric(tenant_id, "superseded_count")
    before_dis = _metric(tenant_id, "disputed_count")
    print(f"\nBaseline metrics: superseded_count={before_sup} disputed_count={before_dis}")

    # ---- SCENARIO A: supersede by authority ----
    print("\n### SCENARIO A — supersede by authority (higher authority wins) ###")
    _record(tenant_id, subject_a, "cleared", fact_layer.SOURCE_AGENT,
            session_id, agent_id,
            "A1 lower-authority assert (agent_inference, rank 40): 'cleared'")
    a2 = _record(tenant_id, subject_a, "fraudulent", fact_layer.SOURCE_SYSTEM,
                 session_id, agent_id,
                 "A2 higher-authority contradiction (system_of_record, rank 100): 'fraudulent'")
    a_ok = a2.get("decision") == "supersede" or a2.get("op") == "supersede"
    print(f"    EXPECT supersede -> {'PASS' if a_ok else 'CHECK'} "
          f"(decision={a2.get('decision')}, status={a2.get('status')})")

    # ---- SCENARIO B: equal-authority dispute ----
    print("\n### SCENARIO B — equal-authority contradiction -> disputed ###")
    _record(tenant_id, subject_b, "cleared", fact_layer.SOURCE_HUMAN,
            session_id, agent_id,
            "B1 assert (user_assertion, rank 60): 'cleared'")
    b2 = _record(tenant_id, subject_b, "fraudulent", fact_layer.SOURCE_HUMAN,
                 session_id, agent_id,
                 "B2 equal-authority contradiction (user_assertion, rank 60): 'fraudulent'")
    b_ok = b2.get("status") == "disputed" or b2.get("decision") == "dispute"
    print(f"    EXPECT disputed -> {'PASS' if b_ok else 'CHECK'} "
          f"(decision={b2.get('decision')}, status={b2.get('status')}, "
          f"competing={b2.get('competing_event_ids')})")

    # ---- caller-visible metric deltas ----
    after_sup = _metric(tenant_id, "superseded_count")
    after_dis = _metric(tenant_id, "disputed_count")
    print(f"\nMetrics after: superseded_count={after_sup} (was {before_sup}), "
          f"disputed_count={after_dis} (was {before_dis})")

    print("\n" + "=" * 72)
    print("To see the PRIOR claim still in the audit trail (append-only, hash-chained),")
    print("run sql/rca_lineage.sql against the fact layer's TiDB with:")
    print(f"    tenant_id = {tenant_id!r}")
    print(f"    subject   = {subject_a!r}   (Scenario A — should show assert then supersede)")
    print(f"    subject   = {subject_b!r}   (Scenario B — should show two competing asserts, disputed)")
    print("=" * 72)


if __name__ == "__main__":
    main()
