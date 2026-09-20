import json

from fact_layer_client import record_fact

TENANT = "demo-bank-alpha"


def seed(**kwargs):
    result = record_fact(tenant_id=TENANT, **kwargs)
    print(f"\n{kwargs['subject']} / {kwargs['predicate']}")
    print(json.dumps(result, indent=2, default=str))


# 1. Velocity-burst assessment
seed(
    subject="ip:185.15.54.22",
    predicate="fraud_status",
    value={
        "status": "SUSPICIOUS",
        "reason": "velocity_burst",
    },
    source="fraud_model",
    confidence=0.91,
    entity_type="ip_address",
    canonical_key="185.15.54.22",
    assessor_type="model",
    evidence=[
        {
            "evidence_type": "transaction",
            "evidence_ref": "30031",
        },
        {
            "evidence_type": "transaction",
            "evidence_ref": "30032",
        },
        {
            "evidence_type": "transaction",
            "evidence_ref": "30033",
        },
        {
            "evidence_type": "transaction",
            "evidence_ref": "30034",
        },
        {
            "evidence_type": "transaction",
            "evidence_ref": "30035",
        },
    ],
    context_summary=(
        "Five recent transactions from IP 185.15.54.22 across multiple "
        "customer accounts triggered a transaction-velocity anomaly."
    ),
    idempotency_key="demo-alpha-velocity-185.15.54.22-v1",
)


# 2. High-value / geographic anomaly
seed(
    subject="transaction:30036",
    predicate="fraud_status",
    value={
        "status": "SUSPICIOUS",
        "reason": "high_value_first_time_geographic_anomaly",
    },
    source="fraud_model",
    confidence=0.88,
    entity_type="transaction",
    canonical_key="30036",
    assessor_type="model",
    evidence=[
        {
            "evidence_type": "transaction",
            "evidence_ref": "30036",
        }
    ],
    context_summary=(
        "Order 30036 is an $8,999 transaction associated with San Marino "
        "and was generated as a high-value first-time anomaly."
    ),
    idempotency_key="demo-alpha-order-30036-model-v1",
)


# 3. Human review of the same transaction.
# This deliberately creates a second assessment so the Fact Layer,
# rather than the application, determines the governed resolution.
seed(
    subject="transaction:30036",
    predicate="fraud_status",
    value={
        "status": "FRAUD",
        "reason": "human_review_confirmed",
    },
    source="human_investigator",
    confidence=0.98,
    entity_type="transaction",
    canonical_key="30036",
    assessor_type="human",
    evidence=[
        {
            "evidence_type": "transaction",
            "evidence_ref": "30036",
        }
    ],
    context_summary=(
        "Human investigator reviewed the high-value first-time transaction "
        "and confirmed it as fraud."
    ),
    idempotency_key="demo-alpha-order-30036-human-v1",
)


print("\n✅ Fact Layer demo seed complete.")