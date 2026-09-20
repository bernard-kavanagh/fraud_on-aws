# Architecture — The Cognitive Foundation

This document is the architectural deep-dive for the fraud detection repo. For setup, demo instructions, and troubleshooting, see [README.md](README.md). For the POC-phase design questions on Reconciliation and Compaction, see [MEMORY_MAINTENANCE_POC.md](MEMORY_MAINTENANCE_POC.md).

---

## What's wired today

This repo implements 8 of the 12 theses of the cognitive foundation framework. The status below is grep-able in the codebase, not aspirational.

| # | Thesis | Status | Where it lives |
|---|---|---|---|
| 03 | Custodial duties | ✅ Ported to fact layer | Write control + reconciliation now LIVE server-side in the governed fact layer's `record_fact`; dedup retired (canonical subject keys); decay analog deferred; compaction a POC decision — see Custodial duties table below |
| 04 | Substrate consolidation | ✅ Live | One TiDB cluster holds transactions, semantic memory, episodic checkpoints, policy knowledge — one transaction boundary, no vector store, no cache, no warehouse |
| 05 | Context assembly | ✅ Live | [`assemble_context()`](agent_tools.py) builds a 5-tier prompt under a 3,600-token budget. Pure SQL, zero LLM calls, ~50 ms target |
| 06 | Substrate-driven routing | ✅ Live | [`route_investigation()`](agent_tools.py) scans all Tier 5 matches; any row passing both gates → Haiku/3-round shortcut; none passing → Sonnet/15-round explore |
| 07 | Three tiers, no conflation | ✅ Live | [`agent_reasoning`](schema.sql) is structured episodic checkpoints; semantic memory is now the governed fact layer (recall via [`fact_layer_client.vector_search`](fact_layer_client.py)); procedural logic lives in the adapter |
| 08 | Supersede | ✅ **Live** | Auto-supersede on contradiction is now LIVE inside the fact layer's `record_fact`: a higher-authority contradiction supersedes (prior event retained in `fact_event`); equal authority disputes. Demonstrated by [`scenarios/contradiction_demo.py`](scenarios/contradiction_demo.py) |
| 10 | Compliance is architectural | ✅ **Delivered** | ACID-bounded writes, append-only hash-chained `fact_event`, vectors-as-datatype. The single-query RCA that Thesis 10 named as still-missing now ships: [`sql/rca_lineage.sql`](sql/rca_lineage.sql) reconstructs a fact's full lineage (first assertion → every supersede/dispute → current state) from one `fact_event` query, filtered by tenant_id + subject, ordered by observed_at |
| 11 | Pattern generic, domain is plugin | ✅ Live | Two adapters share one substrate: [`adapters/fraud/`](adapters/fraud/__init__.py) (16 e-commerce patterns) and [`adapters/betting/`](adapters/betting/__init__.py) (3 sportsbook patterns). `assemble_context(adapter=...)` and `run_investigation(adapter=...)` pick the plugin |

**Theses not yet delivered:**
- **01** — *Memory is infrastructure* — narrative claim, not testable code
- **02** — *Model forgets / human decides* — reconciliation is now live single-mode (auto supersede-by-authority or dispute) in the fact layer; a human-in-loop approval queue remains a POC-phase decision, not built here
- **09** — *Branching for agent isolation* — not in this repo; covered by the [DBA agent repo](https://github.com/bernard-kavanagh/tidb-self-healing-db-agent)
- **12** — *System of thought, not record* — narrative claim, not testable code

---

## The problem this architecture solves

Fraud detection hits the **Memory Wall** when transaction patterns evolve faster than static rules can adapt, and when every investigation starts cold — context, prior patterns, and entity history rebuilt from scratch on every alert.

Most "unified database" pitches show a dashboard. This repo shows an **agent that reasons, queries, and acts** — combining SQL joins, vector similarity search, and real-time columnar analytics — all through a single TiDB connection string. The cognitive foundation solves the cold-start problem with persistent three-tier memory, substrate-driven model routing, and lifecycle management served through budget-constrained context assembly.

---

## The cognitive foundation lifecycle

```
                            Trigger (Admin chat / CLI / dashboard "Investigate")
                                                  │
                                                  ▼
   ┌──────────────────────────────────────────────────────────────────────────┐
   │  STEP 1 — assemble_context()    pure SQL · zero LLM calls · ~50 ms       │
   │                                                                          │
   │   T1 entity profile        T2 recent activity         T3 active checkpt   │
   │   T4 prior investigations  T5 semantic (fraud_memory) ← vector retrieval  │
   │   ──────────────────────────────────────────────────────────────────     │
   │   Returns: system_context (≤3,600 tokens), top_match, vector_matches      │
   └──────────────────────────────────────────────────────────────────────────┘
                                                  │
                                                  ▼
   ┌──────────────────────────────────────────────────────────────────────────┐
   │  STEP 2 — route_investigation()  code-driven, not LLM                    │
   │                                                                          │
   │   Scan vector_matches:                                                   │
   │     any row with confidence ≥ 0.85 AND similarity ≥ gate                 │
   │       yes → SHORTCUT (Haiku, 3 rounds)                                   │
   │       no  → EXPLORE  (Sonnet, 15 rounds)                                 │
   │                                                                          │
   │   On SHORTCUT match → reinforce_pattern() bumps evidence_count           │
   └──────────────────────────────────────────────────────────────────────────┘
                                                  │
                                                  ▼
   ┌──────────────────────────────────────────────────────────────────────────┐
   │  STEP 3 — agent loop (cognitive_loop.run_investigation)                  │
   │                                                                          │
   │   System prompt cached (cache_control: ephemeral) + adapter SCHEMA_HINT  │
   │   Tools: execute_sql, vector_search, recall_similar_fraud,               │
   │          flag_order, write_reasoning_checkpoint, compound_resolution     │
   │   Model picks the next tool. Loop ends on end_turn or round budget.      │
   └──────────────────────────────────────────────────────────────────────────┘
                                                  │
                                                  ▼
   ┌──────────────────────────────────────────────────────────────────────────┐
   │  STEP 4 — slim summary call (Haiku on structured checkpoint, NOT loop)   │
   │                                                                          │
   │   SELECT latest agent_reasoning row → 3-paragraph investigation report   │
   │   Fallback: synthesise checkpoint from tool_trace if agent didn't write  │
   └──────────────────────────────────────────────────────────────────────────┘
                                                  │
                                                  ▼
   ┌──────────────────────────────────────────────────────────────────────────┐
   │  ONE TiDB CLUSTER                                                        │
   │                                                                          │
   │   TiKV (row) ────► live transactions, bets, orders, sessions             │
   │   TiFlash (col) ─► HTAP velocity + liability queries on same writes      │
   │   Vector / HNSW ─► fraud_memory, sales_knowledge, reviews                │
   │                                                                          │
   │   Custodial duties (governed fact layer, single-mode):                   │
   │     1 Write Control ✅ server-side   2 Reconciliation ✅ LIVE (auto)      │
   │     3 Deduplication ⛔ retired (canonical keys)   4 Decay 🟡 deferred     │
   │     5 Compaction 🟡 POC                                                   │
   └──────────────────────────────────────────────────────────────────────────┘
```

**Five priority-ordered context sources, one substrate-driven routing decision, one cached system prompt, one structured summary.** The agent is stateless and ephemeral; the substrate remembers on its behalf. Pure SQL handles everything that isn't reasoning — assembly, routing, custodial duties — and the model only ever sees a curated prompt under a hard token budget.

See [directives/tidb_agent_demo.md](directives/tidb_agent_demo.md) for the lifecycle described in operator language.

---

## Three-tier memory

This repo implements all three tiers of the cognitive foundation's memory architecture.

### Episodic memory

**Tables:** `agent_reasoning` (structured checkpoints), `chat_history` (conversation transcript), `agent_sessions`

`agent_reasoning` stores **outcomes-only checkpoints** — `observation`, `hypothesis`, `evidence_refs` (JSON), `confidence`, `resolution`. Written by the agent loop via [`write_reasoning_checkpoint()`](agent_tools.py). The Step 4 slim summary call reads ONE row of this table to produce the investigation report — it does not replay the loop conversation. Memory grows at O(investigations), not O(reasoning steps) — Thesis 03 (write control).

`chat_history` remains as a UI-facing transcript for the conversational paths. It is not the episodic memory the routing layer reads.

### Semantic memory

**Tables:** `fraud_memory` (learned patterns), `sales_knowledge`, `reviews`

`fraud_memory` is the compounding tier. Each confirmed investigation can write a pattern via [`compound_resolution()`](agent_tools.py) — vector-embedded, scoped `global` or `entity`, with `confidence`, `evidence_count`, `superseded_by`, and `last_reinforced_at`. Future agent sessions recall these patterns via [`recall_similar_fraud()`](agent_tools.py) or surface them automatically through Tier 5 of `assemble_context()`. The routing gate reads `confidence ≥ 0.85 AND similarity ≥ 0.55` from this table to decide Sonnet/explore vs Haiku/shortcut.

`sales_knowledge` and `reviews` remain hand-seeded reference knowledge for the customer/RAG flow.

**Cold-start solution.** [`adapters/fraud/`](adapters/fraud/__init__.py) ships with a `SEED_CATALOG` of 16 e-commerce fraud patterns (velocity, ATO, refund abuse, synthetic identity, device-fingerprint reuse, headless-browser, chargeback, gift-card laundering). [`adapters/betting/`](adapters/betting/__init__.py) adds 3 sportsbook patterns. Click the *"🌱 Seed fraud_memory"* sidebar button to load them — the cluster skips the warm-up curve and routes shortcut from invocation 1, the same way production EV charger clusters do.

### Procedural memory

**Implementation:** Agent directives (`directives/tidb_agent_demo.md`), escalation logic, write-back actions

The 'how-to' layer: when to flag an order for review vs auto-resolve, when to adjust odds vs freeze a market, when to escalate to a human analyst. Currently encoded in the agent directive and the write-back tools (`flag_order`, `adjust_odds`, `flag_bettor`). These write-back actions demonstrate **human-in-the-loop** decision gates — the agent surfaces the anomaly, the human (or automated policy) decides the action.

> **Explicit procedural memory** — storing learned escalation strategies as a distinct memory type with its own retrieval path — is planned for a future iteration of the cognitive foundation portfolio.

---

## Custodial duties

Memory that isn't maintained becomes a liability. Custodial duties keep the substrate honest.

> **Port note (governed fact layer).** Semantic memory — confirmed fraud/betting
> verdicts and their recall — no longer lives in a local `fraud_memory` table. It
> is now the **AgentCore Governed Fact Layer**, a separately-deployed standalone
> service reached over its Gateway ([`fact_layer_client.py`](fact_layer_client.py)).
> That move changes where three of these duties run and retires a fourth. The
> statuses below reflect the port.

| Duty | Status | Implementation |
|---|---|---|
| **Write Control** | ✅ Live (server-side) | Enforced inside the fact layer's `record_fact` (`handlers/common/write_control.py`) — a confidence floor rejected before adjudication. This repo no longer runs a local floor check; [`compound_resolution()`](agent_tools.py) simply calls `record_fact`. `write_reasoning_checkpoint()` still stores distilled checkpoints, not transcripts — memory grows at O(investigations). |
| **Reconciliation** | ✅ **Live (single-mode)** | Now automatic in the fact layer's `record_fact` adjudication on every write: a contradiction from a strictly **higher-authority source auto-supersedes** (prior event retained, hash-chained, visible in `fact_event`); an **equal/lower-authority contradiction resolves to `disputed`** (both events retained, no recency tiebreak). Demonstrated live by [`scenarios/contradiction_demo.py`](scenarios/contradiction_demo.py). **Human-in-loop queue mode remains a POC-phase decision** for a real customer conversation — a `reconciliation_queue` table, an approval UI, and an evidence-weighted mode are explicitly *not* built here (see [MEMORY_MAINTENANCE_POC.md](MEMORY_MAINTENANCE_POC.md)). |
| **Deduplication** | ⛔ Retired | Cosine-distance dedup (`consolidate_fraud_memory()`) is deleted. It was the wrong mechanism once fact identity is canonical rather than fuzzy-matched: with canonical **subject keys** (see [`fact_layer_client.py`](fact_layer_client.py)), two paraphrased assertions about the same fact collide on one `(tenant_id, subject, predicate)` identity and are handled by `record_fact`'s corroboration/adjudication path — no embedding-distance merge needed. |
| **Confidence Decay** | 🟡 Deferred (no fact-model analog) | The local `fraud_memory` decay is disconnected by the port. `fact_current` has no per-fact confidence column to decay against, so a decay analog for the fact model needs its own design pass — explicitly out of scope here. |
| **Compaction** | 🟡 POC-phase decision | `fact_event` stays append-only and unbounded for this port. Retention/archival policy, cadence, and horizon are customer-led decisions for POC planning. |

> Reconciliation and write control are grep-able in the fact layer service; the caller-side threading is in [`fact_layer_client.py`](fact_layer_client.py) / [`agent_tools.py`](agent_tools.py). Tunable thresholds (routing gates) remain env-overridable via `.env`.
>
> The remaining POC-phase decisions have a dedicated planning artifact: **[MEMORY_MAINTENANCE_POC.md](MEMORY_MAINTENANCE_POC.md)**. The human-in-loop reconciliation queue and the compaction policy are intentionally *not* shipped as code — they require domain-specific judgment about regulatory environment, risk appetite, and operating model that only the buying team can make.

---

## Differentiation: pytidb is the SDK, this is the pattern

PingCAP's official [pytidb](https://github.com/pingcap/pytidb) SDK ships hybrid search, auto-embedding, a Pydantic-style schema layer, and an MCP server. Its memory-feature documentation points at a flat `memories` table with vector retrieval — useful, but a different layer of the stack. The Cognitive Foundation sits one layer above:

| pytidb's memory shape (data-access layer) | Cognitive Foundation (memory-semantics layer) |
|---|---|
| Single `memories` table | Three typed tiers: `agent_reasoning` (episodic), the governed fact layer (semantic, over the Gateway), procedural in the adapter |
| Vector retrieval | 5-tier priority-ordered context assembly under a 3,600-token budget; Tier 5 is tenant-scoped, server-side-embedded fact-layer recall |
| No lifecycle | Governed custodial duties: write control + reconciliation live server-side in `record_fact` (single-mode); dedup retired for canonical subject keying; decay deferred; compaction a POC decision |
| No temporal model | Append-only hash-chained `fact_event` audit log; `supersedes_event_id` lineage; single-query RCA ([`sql/rca_lineage.sql`](sql/rca_lineage.sql)) |
| No routing | Substrate-driven model selection on `confidence × similarity` |
| No domain pattern | Adapter-as-plugin (Thesis 11) — same substrate, swap the catalog |

The two compose. You can adopt pytidb's `TiDBClient` connection pooling, `EmbeddingFunction` (cloud, Bedrock, or local), or its built-in MCP server without changing this repo's schema. PingCAP investing in pytidb is independent corroboration that TiDB is the right substrate for AI-era memory; this repo is what you build *on top* of that substrate when you need typed memory and lifecycle semantics, not just embed-and-retrieve.

### Production deployment shape

The agent layer is fundamentally serverless-shaped: each `run_investigation()` call is stateless from the agent's perspective; all state lives in TiDB. That maps cleanly onto:

- **AWS Lambda** triggered by EventBridge / SQS / Kinesis (event-driven, scale-to-zero)
- **AWS ECS Fargate** for sustained worker throughput (no cold start, always-on)
- **AWS Step Functions + Lambda** for long-running investigations with retry semantics or human-in-loop pauses
- **AWS Bedrock Agents** as a managed-agent runtime drop-in for the Anthropic-SDK loop in `cognitive_loop.py`

This repo is a demo, not a production deployment. It does not ship Terraform / CDK / Lambda / IAM templates — those are customer-environment-specific. The honest answer to *"how do I run this in production?"* is: clone, adapt, deploy how your platform team usually does. The four serverless shapes above are the recommended deployment patterns because the architecture is designed for them — the substrate carries the state, the agent layer is ephemeral.

---

## Why this matters for fraud and gaming

Three claims this architecture makes that the standard "Flink + Kafka + vector DB + warehouse" stack cannot:

**1. No sync lag between live transactions and the agent's view of them.** The fraud-velocity query uses `/*+ read_from_storage(tiflash[orders]) */` to hit the columnar engine for real-time analytics on data that is *simultaneously* being written transactionally to TiKV. No ETL, no separate data warehouse, no consistency gap between what the dashboard sees and what the agent reasons about.

**2. Enrichment, memory, and live data share one transaction boundary.** Ververica solves the bet-stream-plus-enrichment problem with Flink + a separate enrichment store. TiDB does it in one query — same database holds the live bet stream (TiKV) and the historical context for enrichment (TiFlash). Two HTAP queries, two write-back actions, one connection string.

**3. The audit trail is reconstructable in a single SQL query.** Regulatory-grade RCA — from trigger to assembled context to routing decision to tool trace to checkpoint to resolution — lives in one log, in one database, with one transaction boundary. This is Thesis 10: compliance as an architectural property, not a reporting layer bolted on top. For the *governed fact* half of the story, [`sql/rca_lineage.sql`](sql/rca_lineage.sql) reconstructs a fact's entire lineage — first assertion through every authority supersede and every dispute to current truth — from one query against the append-only, hash-chained `fact_event` log.

The cognitive foundation eliminates these separations architecturally — enrichment context (semantic memory), investigation history (episodic memory), and live transactions (data plane) share one transaction boundary. No ETL, no sync, no consistency gaps.

---

## Cross-references

- **[README.md](README.md)** — setup, demo instructions, troubleshooting, known-good triggers
- **[MEMORY_MAINTENANCE_POC.md](MEMORY_MAINTENANCE_POC.md)** — design questions for Reconciliation and Compaction (customer-facing)
- **[directives/tidb_agent_demo.md](directives/tidb_agent_demo.md)** — the lifecycle in operator language
