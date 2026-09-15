# Operator-supplied inputs for registering THIS repo's domain read tools onto the
# fact layer's EXISTING Gateway. Copy to env.sh and fill in. ARNs/IDs/URLs only —
# NO secret material (the TiDB connection secret is resolved by ARN at runtime).

export AWS_REGION="eu-central-1"

# The fact layer's Gateway — the SAME gateway the fact-layer tools are on. We add
# targets to it; we do NOT create a new gateway.
export GATEWAY_NAME="fact-layer-gw"
export GATEWAY_ARN=""                    # required to render the Cedar permits

# Secrets Manager ARN of THIS repo's demo TiDB connection secret (the
# transactional store orders/bets live in). Secret JSON keys:
# host, port, username, password, database, [ssl_ca]. Passed to the Lambdas as
# DOMAIN_READS_SECRET_ARN (read by ARN at runtime; never a raw secret in env).
export DOMAIN_READS_SECRET_ARN="arn:aws:secretsmanager:eu-central-1:<ACCT>:secret:<NAME>"

# Lambda ARNs (filled in after packaging/deploying the two handler functions).
export FRAUD_VELOCITY_ARN=""             # transaction_velocity.lambda_handler
export BETTING_LIABILITY_ARN=""          # liability_concentration.lambda_handler
