"""
Domain adapters for the cognitive foundation substrate.

Thesis 11 — the pattern is generic, the domain is a plugin. Each adapter
defines its tier callables, banding rules, anomaly weights, and seed
catalog. Everything else — context assembly, routing, custodial duties,
the agent loop — is shared substrate.

To add a new vertical, drop a new adapter package here and point
assemble_context() at it. No substrate changes required.
"""
