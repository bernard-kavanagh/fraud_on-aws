// Tenant isolation for THIS repo's domain read tools, added to the fact layer's
// existing Gateway (the same policy engine, in ENFORCE mode: deny-by-default,
// forbid-overrides-permit). These permits are ADDITIVE to the fact layer's own
// tenant_isolation.cedar — they do not modify or replace it.
//
// Same rule that earns its keep everywhere else: the tool argument tenant_id
// must equal the caller's tenant_id identity claim (compared literally; Cedar
// has no string concatenation, so tenant_id must arrive as a first-class JWT
// claim mapped to a principal tag by the Gateway authorizer).
//
// Action naming is <targetName>___<toolName>. Keep the target names in
// deploy_domain_targets.sh in sync:
//   FraudVelocity      -> transaction_velocity     -> FraudVelocity___transaction_velocity
//   BettingLiability   -> liability_concentration  -> BettingLiability___liability_concentration
// Replace <GATEWAY_ARN> at deploy time (the SAME gateway ARN as the fact layer).

permit(
  principal is AgentCore::OAuthUser,
  action == AgentCore::Action::"FraudVelocity___transaction_velocity",
  resource == AgentCore::Gateway::"<GATEWAY_ARN>"
) when {
  principal.hasTag("tenant_id")
  && context has input
  && context.input has tenant_id
  && context.input.tenant_id == principal.getTag("tenant_id")
};

permit(
  principal is AgentCore::OAuthUser,
  action == AgentCore::Action::"BettingLiability___liability_concentration",
  resource == AgentCore::Gateway::"<GATEWAY_ARN>"
) when {
  principal.hasTag("tenant_id")
  && context has input
  && context.input has tenant_id
  && context.input.tenant_id == principal.getTag("tenant_id")
};
