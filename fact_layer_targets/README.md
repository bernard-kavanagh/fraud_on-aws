# Domain read tools (Gateway targets owned by this repo)

Two typed, tenant-scoped **read** tools that this repo contributes to the fact
layer's **existing** AgentCore Gateway as additional Lambda targets:

| Target name        | Tool                     | Cedar action                                   | Reads   |
|--------------------|--------------------------|------------------------------------------------|---------|
| `FraudVelocity`    | `transaction_velocity`   | `FraudVelocity___transaction_velocity`         | `orders`|
| `BettingLiability` | `liability_concentration`| `BettingLiability___liability_concentration`   | `bets`  |

## Why these live here (not in the fact layer)

- `query_tenant_metrics` in the fact layer stays **domain-agnostic** — it exposes
  only generic fact metrics (fact_count, dispute_rate, …). These two tools are
  **domain-specific** reads over *this repo's* transactional TiDB (orders/bets),
  so they are their own targets with their own typed schemas, not new named
  metrics inside `query_tenant_metrics`.
- They are still **governed the same way**: registered on the same Gateway, behind
  the same Cognito JWT inbound auth and the same Cedar engine, and each has a
  matching tenant-isolation permit (`cedar/domain_reads.cedar`). Reads are typed
  and enumerated — bounded params, never caller-supplied SQL.

## Layout

```
handlers/
  transaction_velocity.py      # FraudVelocity target
  liability_concentration.py   # BettingLiability target
  common/context.py            # interceptor-context extraction (mirrors fact layer)
  common/db.py                 # SQLAlchemy engine from a Secrets Manager ARN
schemas/                       # tool inputSchemas (type:object at top level, no json wrapper)
cedar/domain_reads.cedar       # additive tenant-isolation permits (2 actions)
deploy_domain_targets.sh       # registers the targets + permits on the EXISTING gateway
env.example.sh                 # operator inputs (copy to env.sh)
```

## Deploy

The fact layer must already be deployed (its gateway + Cedar engine exist). Then,
from this directory:

```
cp env.example.sh env.sh    # fill in GATEWAY_ARN, DOMAIN_READS_SECRET_ARN, Lambda ARNs
bash deploy_domain_targets.sh          # dry-run (prints commands)
DEPLOY_FOR_REAL=1 bash deploy_domain_targets.sh   # after verifying CLI flags
```

The script only **adds** targets and permits to the existing gateway; it never
touches the fact layer repo's `deploy.sh`, schema, Cedar, or `record_fact`.

## Packaging

Lambda runtime deps: `sqlalchemy`, `pymysql`, `boto3` (boto3 is on the Lambda
runtime). Package `handlers/` as each function's code; entrypoints are
`transaction_velocity.lambda_handler` and `liability_concentration.lambda_handler`.
The TiDB connection secret is resolved by ARN at runtime (`DOMAIN_READS_SECRET_ARN`)
— no secret material in env or code.
