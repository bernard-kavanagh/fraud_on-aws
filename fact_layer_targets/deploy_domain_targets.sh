#!/usr/bin/env bash
# Register THIS repo's domain read tools as ADDITIONAL Lambda targets on the fact
# layer's EXISTING Gateway, and add matching tenant-isolation Cedar permits.
#
# This script is owned by THIS repo. It does NOT modify the fact layer repo's
# deploy.sh or its schema/Cedar/adjudication — it only *adds* two targets and two
# permits to the already-deployed gateway. Run the fact layer's own deploy first
# (its gateway + policy engine must already exist).
#
# PREREQUISITE — verify CLI syntax before running. AgentCore CLI flags evolve;
# check the installed CLI first:
#     agentcore --help
#     agentcore add gateway-target --help
#     agentcore add policy --help   (or the current "append/update policy" verb)
# Then set DEPLOY_FOR_REAL=1 to execute (the run() guard prints commands until then).

set -euo pipefail
cd "$(dirname "$0")"
source env.sh

run() {
  if [ "${DEPLOY_FOR_REAL:-0}" = "1" ]; then "$@"; else echo "  [dry-run] $*"; fi
}
require() { [ -n "${!1:-}" ] || { echo "missing env: $1"; exit 1; }; }
require AWS_REGION
require GATEWAY_NAME
require GATEWAY_ARN
require DOMAIN_READS_SECRET_ARN
require FRAUD_VELOCITY_ARN
require BETTING_LIABILITY_ARN

echo "== 1. Register the two domain read handlers as targets on the EXISTING gateway =="
# Target names drive Cedar action names (<targetName>___<toolName>) — keep them in
# sync with cedar/domain_reads.cedar.
run agentcore add gateway-target --type lambda-function-arn --name FraudVelocity \
  --lambda-arn "$FRAUD_VELOCITY_ARN" \
  --tool-schema-file schemas/transaction_velocity.schema.json \
  --gateway "$GATEWAY_NAME"

run agentcore add gateway-target --type lambda-function-arn --name BettingLiability \
  --lambda-arn "$BETTING_LIABILITY_ARN" \
  --tool-schema-file schemas/liability_concentration.schema.json \
  --gateway "$GATEWAY_NAME"

echo "== 2. Append tenant-isolation Cedar permits for the two new actions =="
# Render the gateway ARN into a temp copy; never sed -i the tracked template.
RENDERED_POLICY="$(mktemp)"
sed "s|<GATEWAY_ARN>|$GATEWAY_ARN|g" cedar/domain_reads.cedar > "$RENDERED_POLICY"
echo "   rendered policy -> $RENDERED_POLICY"
# NOTE: this must ADD to the engine's existing policy set (the fact layer's
# tenant_isolation.cedar), not replace it. Confirm the current CLI verb for
# appending a policy document to an attached engine before enabling DEPLOY_FOR_REAL.
run agentcore add policy --gateway "$GATEWAY_NAME" \
  --policy-file "$RENDERED_POLICY" --mode ENFORCE

echo "== 3. Deploy =="
run agentcore deploy

echo
echo "Post-deploy checks:"
echo "  - list-tools on the gateway now advertises transaction_velocity + liability_concentration"
echo "  - a cross-tenant call (tenant_id arg != token claim) is DENIED for BOTH new actions"
echo "    (confirm in CloudWatch: agentcore logs --since 15m --query \"policy\")"
