# TiDB Fraud Detection — Live Data, Agentic Investigation, Governed Facts

> This project demonstrates how **TiDB can serve as the unified data substrate for a real-time AI application** — handling live transactional data, real-time analytics, vector retrieval, agent state, and the data underpinning a governed fact layer without requiring separate operational databases, warehouses, vector stores, or ETL pipelines.

The application uses **e-commerce fraud detection and sports-betting risk** as two example domains.

Live events are written to TiDB while TiFlash analyzes those same records in real time. An AI agent investigates suspicious activity using operational data, analytical signals, retrieved context, and previous investigations. Confirmed conclusions can then be promoted into a separately governed fact layer with evidence, authority, lineage, contradiction handling, and tenant isolation.

The end-to-end pattern is:

```text
Live data → real-time analytics → agent investigation → governed facts
```

The domain can change. **The underlying TiDB substrate does not.**

---

## What this project demonstrates

Most agentic applications accumulate infrastructure as their data requirements grow:

* an operational database for live application state
* a warehouse or analytical store for aggregations
* a vector database for semantic retrieval
* pipelines to synchronize those systems
* a separate store for agent workflow and investigation state
* additional infrastructure for durable organizational knowledge

This project explores a different architecture.

**TiDB provides one distributed SQL substrate across the application's live data, analytics, retrieval, and agent workflow.**

| Requirement             | TiDB capability                     | Demonstrated here                                                    |
| ----------------------- | ----------------------------------- | -------------------------------------------------------------------- |
| Live transactional data | TiKV                                | Orders, customers, bets, investigation state                         |
| Real-time analytics     | TiFlash / HTAP                      | Fraud velocity and betting liability against live data               |
| Semantic retrieval      | Native Vector / HNSW                | Product, policy, review, and contextual retrieval                    |
| Agent workflow state    | Transactional SQL                   | Sessions, reasoning checkpoints, prior investigations                |
| Governed facts          | TiDB-backed standalone fact service | Confirmed verdicts, evidence, lineage, authority, and contradictions |
| Write-back              | Transactional SQL                   | Flag orders, flag bettors, adjust odds                               |

Because TiKV and TiFlash operate over the same logical data, analytical queries can run against transactions that are still arriving.

There is no batch handoff from the operational database to a warehouse before the agent can reason over the latest state.

> **One database. Live operational and analytical context. No synchronization lag.**

---

## Architecture at a glance

```text
                 LIVE APPLICATION DATA
                         │
                         ▼
              ┌─────────────────────┐
              │        TiDB         │
              │                     │
              │  TiKV    TiFlash    │
              │  SQL     Vector     │
              └──────────┬──────────┘
                         │
                 operational data
                 analytical signals
                 semantic retrieval
                 prior investigations
                         │
                         ▼
              ┌─────────────────────┐
              │   Investigation     │
              │       Agent         │
              │                     │
              │ assemble → route →  │
              │ investigate → act   │
              └──────────┬──────────┘
                         │
                 confirmed conclusion
                         │
                         ▼
              ┌─────────────────────┐
              │   Governed Fact     │
              │       Layer         │
              │                     │
              │ evidence            │
              │ authority           │
              │ lineage             │
              │ reconciliation      │
              │ tenant isolation    │
              └──────────┬──────────┘
                         │
                         ▼
                  GOVERNED FACTS
```

The [`Governed Fact Layer`](https://github.com/bernard-kavanagh/tidb_agentcore_gateway_mcp_tools) is a separately deployed, domain-neutral service accessed by this application over MCP through an Amazon Bedrock AgentCore Gateway.

That separation is intentional.

An agent's investigation state and a durable organizational fact are not the same thing.

A model may infer that a transaction is fraudulent. That inference should not automatically become accepted truth.

The fact layer therefore models:

```text
Evidence → Assessment → Resolution
```

Conflicting assertions can be **superseded, retained as contrary evidence, or marked disputed** according to predicate-specific source authority. The complete history remains available as an auditable lineage.

This repo is a **caller** of that service through [`fact_layer_client.py`](fact_layer_client.py). The fact layer owns its own database and lifecycle independently of this application's operational schema.

For the deeper design — including investigation state, semantic facts, custodial duties, reconciliation, tenancy, and the lifecycle of knowledge — see [`ARCHITECTURE.md`](ARCHITECTURE.md).

---

## The demos

The fastest way to understand the architecture is to run the demos.

### Demo 1 — Live Fraud Detection

Live transactions are inserted into **TiKV every 500 ms** while a **TiFlash columnar query** detects transaction-velocity anomalies across those same records.

```text
 Live transactions
       │
       ▼
     TiKV ───────────────┐
       │                 │ same logical data
       ▼                 ▼
 application          TiFlash
    state            aggregation
                         │
                         ▼
                  fraud signals
```

This is the core HTAP demonstration:

> **Live writes + live analytics, without an ETL pipeline or separate warehouse.**

The Fraud Dashboard shows:

* **Active Alerts** — orders flagged as suspicious
* **Revenue at Risk** — value of pending or flagged orders
* **Velocity Anomalies** — IPs with abnormal transaction activity
* **Live Risk Queue** — the real-time transaction stream

The **Investigate with Agent →** action passes suspicious activity directly into the investigation workflow.

---

### Demo 2 — Agent Investigation

The Agent UI demonstrates how an agent can reason directly over the same data substrate.

Two contrasting flows are included.

#### Customer / RAG

The customer path combines SQL and vector retrieval for questions about purchases, products, return policies, and shipping policies.

For example:

```text
Can I return my gaming laptop?
```

The application retrieves the customer's purchase data through SQL, retrieves the relevant return policy semantically, and synthesizes the answer.

#### Admin / Investigation

The admin path demonstrates the investigation lifecycle:

```text
Trigger
   │
   ▼
Assemble context
   │
   ├── operational records
   ├── analytical signals
   ├── retrieved knowledge
   ├── prior investigations
   └── governed facts
   │
   ▼
Route investigation
   │
   ▼
Tool-use loop
   │
   ▼
Reasoning checkpoint
   │
   ▼
Assessment / action
```

The agent can query evidence, investigate anomalies, recall previous cases, write operational actions, and promote validated conclusions to the governed fact layer.

### What the agent can discover

One seeded scenario illustrates why this is more than scripted anomaly detection.

A chargeback investigation for a customer named **Clayton Knight** contains six chargebacks across two rotating cards.

The suspicious relationship itself is **not encoded in the fraud-pattern catalog**.

During the investigation, the agent independently correlates account and delivery history and discovers that **five of the six disputed orders were recorded as delivered before the customer's signup date**.

```text
Chargeback investigation
          │
          ├── customer history
          ├── six disputed orders
          ├── two payment cards
          └── delivery records
                    │
                    ▼
              agent correlates
               the timelines
                    │
                    ▼
       5 deliveries before signup
```

The important capability is not that the system can retrieve a known fraud rule.

**It can investigate relationships in live operational data and surface evidence that was not pre-labelled as the anomaly to find.**

---

### Demo 3 — Same Substrate, Different Domain

The sports-betting dashboard applies the same architecture to a different vertical.

Instead of e-commerce orders, the live stream contains bets.

TiFlash detects:

* **Liability Concentration** — excessive stake accumulating on one side of an event
* **Betting Velocity Anomalies** — unusually high betting activity from an IP

Operational write-backs can then:

* adjust odds
* flag a bettor's account

The important point is not the betting example itself.

It demonstrates the **adapter pattern**:

```text
                   TiDB substrate
                         │
              investigation lifecycle
                         │
             ┌───────────┴───────────┐
             ▼                       ▼
       Fraud adapter           Betting adapter
             │                       │
             ▼                       ▼
      transaction risk         sportsbook risk
```

The domain catalog and tools change.

**The data substrate and investigation lifecycle stay the same.**

---

### Demo 4 — Governed Facts and Contradictions

The governed adjudication demo shows what happens after an investigation produces a conclusion.

The fact layer distinguishes:

```text
Evidence → Assessment → Resolution
```

For a predicate such as `fraud_status`, different sources can have different levels of authority.

The demo exercises three outcomes.

#### SUPERSEDED

An `agent_inference` says a transaction is `cleared`.

A higher-authority `human_investigator` subsequently says it is `confirmed`, backed by investigation and chargeback evidence.

The human assessment supersedes the earlier conclusion, while the original assertion remains in the history.

#### REJECTED AS CONTRARY EVIDENCE

An established fraud review says the transaction is `confirmed`.

A `user_assertion` claims it was `legitimate`.

Because the user assertion has lower authority for the `fraud_status` predicate, it does not silently overwrite or dispute the established fact.

It is instead **retained as contrary evidence**.

#### DISPUTED

Two comparable-authority reviewers reach contradictory conclusions.

Neither conclusion wins merely because it arrived later.

The fact becomes `disputed`, and both assertions remain available for resolution.

The current state can then be explained through its lineage:

```text
 original assertion
        │
        ├── evidence
        │
        ▼
 subsequent assertion
        │
        ├── authority policy
        ├── supporting evidence
        ├── contrary evidence
        ▼
 current resolution
```

The agent can call `explain_fact` to retrieve **why** the current verdict holds: the resolution, authoritative source, applicable policy version, supporting and contrary evidence, and historical assertions.

This gives the application something fundamentally different from ordinary conversational memory:

> **A governed, explainable record of what the system currently accepts as fact — and why.**

---

## Why TiDB?

The architectural proposition demonstrated by this repo is simple:

> **An AI application should not need to copy its data through multiple specialized stores before an agent can reason over it.**

A conventional architecture can quickly become:

```text
Operational DB ─────────┐
                        │
                        ├── ETL / streaming ──► Warehouse
                        │
                        ├── sync ─────────────► Vector DB
                        │
                        └── application ──────► Agent store
                                                    │
                                                    ▼
                                                  Agent
```

Every additional copy introduces another synchronization boundary, another operational dependency, and another question about which representation is current.

This project instead uses:

```text
                    ┌──────────────┐
Live application ──►│              │
       data         │     TiDB     │
                    │              │
                    │  ┌────────┐  │
                    │  │  TiKV  │  │
                    │  └────────┘  │
                    │  ┌────────┐  │
                    │  │TiFlash │  │
                    │  └────────┘  │
                    │  ┌────────┐  │
                    │  │ Vector │  │
                    │  └────────┘  │
                    └──────┬───────┘
                           │
                           ▼
                         Agent
```

TiDB combines transactional storage, real-time analytical processing, vector retrieval, and SQL access on one distributed data platform.

The agent can therefore investigate against the same operational reality the application is using rather than waiting for that reality to propagate through several specialized stores.

The governed fact layer remains a **distinct service** because governance is a different responsibility from application state.

It owns the rules around evidence, authority, reconciliation, lineage, and tenant-scoped durable facts.

But TiDB can still provide the durable data substrate underneath both systems.

> **TiDB unifies the data substrate. The fact layer governs what becomes durable truth.**

---

## What TiDB replaces here

| TiDB capability          | What it replaces                     | Where it appears                                               |
| ------------------------ | ------------------------------------ | -------------------------------------------------------------- |
| TiKV                     | Separate transactional database      | Orders, customers, bets, agent workflow state                  |
| TiFlash / HTAP           | Separate analytical warehouse        | Fraud velocity and liability concentration against live writes |
| Native Vector / HNSW     | Separate vector database             | Product, policy, review, and contextual retrieval              |
| Unified SQL              | Multiple data-access layers          | One SQL interface across operational and analytical workloads  |
| Transactional write-back | Additional application orchestration | `flag_order`, `adjust_odds`, `flag_bettor`                     |

The fraud-velocity and betting-liability queries explicitly target TiFlash while their corresponding live-pulse processes simultaneously insert new records through TiKV.

Same logical data. Same database.

**No warehouse synchronisation step is required before the analytical signal becomes available to the application or agent.**

---

## Where to go next

If you want to **see TiDB HTAP in action**, start with the **Fraud Dashboard**.

If you want to **see an agent investigate operational data**, run the **Agent UI** and use the Clayton Knight chargeback scenario.

If you want to **see the same substrate applied to another vertical**, run the **Sports Betting Dashboard**.

If you want to **see evidence, authority, lineage, and contradiction handling**, run the **Governed Adjudication** demo.

For the deeper architectural design, see [`ARCHITECTURE.md`](ARCHITECTURE.md).

---

## Prerequisites

* Python 3.10+
* A [TiDB Cloud Starter](https://tidbcloud.com) cluster — the free tier works
* The `isrgrootx1.pem` SSL certificate — available in the TiDB Cloud **Connect** dialog under **Connection Type → General → CA certificate**

---

## Setup

### 1. Install dependencies

```bash
python3 -m venv .venv
source .venv/bin/activate

python --version
pip install --upgrade pip
```

```bash
pip install -r requirements.txt
```

Pinned set: `mysql-connector-python>=8.3,<9`, `sentence-transformers>=2.7,<3`, `python-dotenv>=1.0,<2`, `faker>=24,<26`, `streamlit>=1.30,<2`, `altair>=5,<6`, `pandas>=2.0,<3`, `anthropic>=0.30,<1`, `mcp>=1.0,<2`, `boto3>=1.34,<2` (the last two for the governed fact layer client). The `fact_layer_targets/` Lambdas additionally need `sqlalchemy` + `pymysql` when packaged.

### 2. Configure credentials

```bash
cp .env.example .env
```

Edit `.env` with your TiDB Cloud details (under **Connect → Python** in the console):

```
TIDB_HOST=gateway01.<region>.prod.aws.tidbcloud.com
TIDB_PORT=4000
TIDB_USER=<your-prefix>.root
TIDB_PASSWORD=<your-password>
TIDB_DATABASE=agentcore_fraud
TIDB_SSL_CA=/path/to/isrgrootx1.pem
ANTHROPIC_API_KEY=sk-ant-...
```

> `TIDB_DATABASE` is **this repo's own operational/workflow database**
> (`agentcore_fraud`). It is **not** the Governed Fact Layer's database — the
> fact layer is a separate service reached only over MCP (the `FACT_LAYER_*` /
> `COGNITO_*` settings below) and owns its own database independently.

> Starter and Dedicated clusters use slightly different host patterns — copy whatever the Connect dialog shows.

Add the tenant + fact-layer settings (see [`.env.example`](.env.example) for the full annotated list):

```
# Active tenant for a given run/process (NEVER hardcoded in code; required by
# every domain query and every fact-layer call). Demo tenants: demo-bank-alpha,
# demo-bank-beta.
DEMO_TENANT_ID=demo-bank-alpha

# Governed fact layer (AgentCore Gateway) — the standalone sibling service.
FACT_LAYER_GATEWAY_URL=https://<gateway-id>.gateway.bedrock-agentcore.<region>.amazonaws.com/mcp
COGNITO_CLIENT_ID=<fact-layer app client id>
COGNITO_USERNAME=<test user with a tenant_id claim>
COGNITO_PASSWORD=<password>
AWS_REGION=<region>
# For >1 tenant, prefer a per-tenant credential map:
# FACT_LAYER_TENANT_CREDENTIALS={"demo-bank-alpha":{"client_id":"...","username":"...","password":"..."},"demo-bank-beta":{...}}
```

### 3. Create the schema

In the TiDB Cloud console, open your cluster → **SQL Editor** → paste `schema.sql` → run.

`schema.sql` begins with `CREATE DATABASE IF NOT EXISTS agentcore_fraud; USE
agentcore_fraud;` and creates **only this repo's operational/workflow tables** in
one step. Every **domain** table carries a `tenant_id` column (§11 of
`schema.sql`); episodic checkpoints live in `agent_reasoning`. Workflow/episodic
state is tenant-isolated too: `agent_sessions.tenant_id` is the workflow scope
(application data scope, **not** principal identity), and prior-investigation
recall (Tier 4) filters on it so identical entity ids across tenants never
collide — `chat_history`/`agent_reasoning` inherit that scope through their unique
`session_id`. The schema file is idempotent — safe to re-run.

> **This must not initialize the Fact Layer.** Semantic memory (confirmed
> verdicts) is no longer a table here — it lives in the separately-deployed
> **Governed Fact Layer**, which owns its own database (`entity`, `fact_subject`,
> `fact_event`, `fact_evidence`, `fact_current`, `predicate_rule`, ...) and is
> created/migrated by *its own* repo. Running `schema.sql` creates the
> `agentcore_fraud` database only. Fact Layer evidence rows reference **back** to
> the operational records created here (e.g. an order/transaction id); neither
> side copies the other's tables. Both databases may co-reside on the same TiDB
> cluster with separate ownership.

### 4. Seed the demo data (per tenant)

`generate_world.py` seeds BOTH demo tenants (alpha fully, beta lightly) so a
cross-tenant Cedar deny is demonstrable out of the box. The remaining seed
scripts are **tenant-parameterized** via `DEMO_TENANT_ID` — run them once per
tenant you want scenario data for:

```bash
# Base world for BOTH demo tenants (customers, products, policies, orders)
python generate_world.py

# Scenario data for the primary tenant
export DEMO_TENANT_ID=demo-bank-alpha
python execution/seed_demo_data.py      # demo persona (VIP customer)
python execution/seed_orders.py         # order history for the persona
python execution/seed_reviews.py        # reviews w/ sentiment + embeddings (~30s)
python execution/seed_fraud_data.py     # fraud scenarios for the Fraud Dashboard
python execution/seed_betting_data.py   # optional — sports betting scenarios

# (Optional) repeat any of the above with DEMO_TENANT_ID=demo-bank-beta
```

All seed scripts are idempotent and refuse to run un-scoped (no hardcoded tenant).

---

## Running the demos

> **Two-terminal demos:** Demo 1 and Demo 3 stream live data in one terminal and serve a dashboard in another. The pulse terminal must stay running while the dashboard serves.

### Demo 1 — Fraud Dashboard

**Terminal 1** — live transactions:
```bash
python live_pulse.py
```

**Terminal 2** — dashboard:
```bash
streamlit run execution/fraud_dashboard.py
```

The dashboard auto-refreshes every 2 seconds:
- **Active Alerts** — orders flagged as suspicious
- **Revenue at Risk** — dollar value of pending/flagged orders
- **Velocity Anomalies** — IPs with 3+ transactions in 24h (TiFlash query)
- **Live Risk Queue** — the real-time transaction feed

The **"Investigate with Agent →"** button opens the Agent UI for natural-language drill-down.

### Demo 2 — Agent UI

```bash
python3 -m streamlit run execution/agent_ui.py
```

Switch roles in the sidebar to see two contrasting memory shapes.

**As "Customer (Bernard)" — the RAG path:**
- `"Can I return my gaming laptop?"` — SQL for purchase date + vector search for return policy → synthesised answer
- `"What headphones do you have?"` — semantic product search via vector index
- `"What's the shipping policy for VIP customers?"` — vector search against `sales_knowledge`

**As "Admin" — the cognitive-foundation path:**
- `"Give me a business overview"` — HTAP aggregate across customers, orders, products
- `"What do customers think about the gaming laptop?"` — vector search on the `reviews` table
- `"Give me a sentiment overview across all products"` — TiFlash sentiment aggregation, no separate ML pipeline

For admin investigations, see the **demo triggers** below — these point at IPs and customers that actually exist in the seed data.

### Demo 3 — Sports Betting Dashboard

**Terminal 1** — live bets:
```bash
python live_betting_pulse.py
```

**Terminal 2** — dashboard:
```bash
streamlit run execution/sports_betting_dashboard.py --server.port 8003
```

Two signals refresh every 2 seconds:
- **Liability Concentration** — events with 65%+ stake on one side. Action: **📉 Adjust Odds** reduces the overloaded side by 12% and increases the opposing side by 8%
- **Betting Velocity Anomalies** — IPs with 5+ bets in 24 hours. Action: **🚩 Flag Account** moves all accepted bets from that IP to flagged status

### Demo 4 (optional) — CLI Investigation

```bash
python execution/betting_investigation.py "<trigger text>" [entity_ref]
```

Terminal version of the cognitive-foundation investigation loop. Same lifecycle as the Admin path in Demo 2 (assemble → route → tool-use → slim summary), no UI. Useful for showing raw tool-trace output or scripting investigations against the betting adapter. Pass an entity_ref (customer_id or IP) for the full Tier 4 prior-investigations lookup.

### Demo 5 — Governed adjudication (Evidence → Assessment → Resolution)

The fact layer now models **predicate-specific, versioned authority** and
distinguishes evidence, assessment, and resolution. Verdicts are written under
predicate `fraud_status` (which carries per-source authority), with evidence
references and assessor type. This demo shows all three adjudication outcomes:

```bash
DEMO_TENANT_ID=demo-bank-alpha python scenarios/contradiction_demo.py
```

- **A — SUPERSEDED:** `agent_inference` says `cleared`; `human_investigator`
  (higher authority for `fraud_status`) says `confirmed` with evidence
  (INV-847, CB-991) → supersedes; the prior claim stays in the hash-chained log.
- **B — REJECTED (the key one):** `fraud_review` says `confirmed`; a
  `user_assertion` says `legitimate`. Because a customer is *low* authority for
  `fraud_status`, the contradiction is **retained as contrary evidence** and does
  **not** overturn the confirmation (no auto-dispute).
- **C — DISPUTED:** two comparable-authority reviewers contradict → `disputed`,
  both retained, no recency tiebreak.

It then calls **`explain_fact`** to show *why* the current verdict holds
(resolution, authoritative source + policy version, supporting vs contrary
evidence, history). The agent loop also exposes `explain_fact` as a tool, and
`compound_resolution` now forwards evidence refs + assessor type + an idempotency
key. To see the full retained lineage, run the **single-query RCA** in
[`sql/rca_lineage.sql`](sql/rca_lineage.sql) against the fact layer's TiDB with
the tenant + subject the script prints — it reconstructs the fact's entire
lineage (first assertion → every supersede/dispute → current truth) in one query.

> **Cross-tenant deny (reference profile, ENFORCE):** point `DEMO_TENANT_ID` at one
> tenant while authenticating with a token whose `tenant_id` claim is the *other*
> tenant; the reference Cedar tenant-equality policy denies the call at the Gateway
> (in ENFORCE), before TiDB is touched. Requires two distinct tenant identities
> (see `FACT_LAYER_TENANT_CREDENTIALS`).

### Domain read tools on the fact-layer Gateway

Two typed, tenant-scoped read tools (`transaction_velocity`, `liability_concentration`)
are registered as **additional targets on the same Gateway** as the fact layer —
not as new metrics inside the domain-agnostic `query_tenant_metrics`. Code and a
dry-run deploy script live in [`fact_layer_targets/`](fact_layer_targets/README.md).

---

## Known-good demo triggers

> ⚠️ **Important:** Querying an IP or customer that isn't in the seed data leaves the agent with nothing to verify on the SHORTCUT path and produces a fallback summary at 0.50 confidence. Use the triggers below for reliable demos.

| Vertical | Trigger (paste into Admin chat or CLI) | Expected path |
|---|---|---|
| Fraud — velocity burst | `investigate suspicious orders from IP 185.15.54.22` | SHORTCUT once warm (5 pending orders seeded) |
| Fraud — headless bot | `investigate orders with Puppeteer or Playwright user agents` | EXPLORE first time, SHORTCUT after compound |
| Fraud — chargeback fraud | `investigate customer 4 for chargeback fraud` | EXPLORE — agent finds Clayton Knight's 6 chargebacks across 2 rotating cards, deliveries dated *before* signup |
| Betting — arbitrage | `Customer placing opposing home + away bets from IP 203.0.113.99 within seconds` | SHORTCUT once arbitrage pattern is in `fraud_memory` |
| Betting — velocity | `customer 1 placed 8 bets from IP 91.108.56.177 in 30 minutes` | EXPLORE first time |

The **Clayton Knight investigation** is the strongest single demo — the agent independently discovers the *delivery-confirmed-before-signup* anomaly (5 of 6 chargebacks), which is not in any seed catalog. That's the capability multiplier in one slide.

---

## File structure

```
Agent_AG/
├── ARCHITECTURE.md          # Architecture deep-dive: theses, custodial duties, lifecycle
├── MEMORY_MAINTENANCE_POC.md  # Reconciliation live (single-mode); HITL-queue + Compaction remain POC decisions
│
├── agent_tools.py           # Substrate: assemble_context (Tier 5 = fact layer),
│                            #   route_investigation, recall_similar_fraud (fact layer),
│                            #   compound_resolution (→ record_fact), write_reasoning_checkpoint
├── fact_layer_client.py     # MCP caller for the governed fact layer + subject-key convention
├── tenancy.py               # tenant_id / agent_id resolution (never hardcoded)
├── cognitive_loop.py        # Investigation loop: assemble → route → tool-use → slim summary
├── generate_world.py        # Seeds the full database for BOTH demo tenants (run once)
├── schema.sql               # Full TiDB schema — every domain table tenant-scoped (§11)
├── live_pulse.py            # Streams live orders every 500ms (Demo 1), tenant-scoped
├── live_betting_pulse.py    # Streams live bets every 500ms (Demo 3), tenant-scoped
├── requirements.txt         # Pinned dependencies
├── .env.example             # Credential template (+ tenant + fact-layer config)
│
├── adapters/                       # Domain plugins on a generic substrate
│   ├── fraud/__init__.py           # 16 e-commerce fraud patterns + tenant-scoped tier callables
│   └── betting/__init__.py         # 3 sports-betting patterns + tenant-scoped tier callables
│
├── fact_layer_targets/             # Domain read tools registered on the SAME Gateway (task 10)
│   ├── handlers/                   #   transaction_velocity + liability_concentration Lambdas
│   ├── schemas/                    #   typed tool inputSchemas
│   ├── cedar/domain_reads.cedar    #   additive tenant-isolation permits (2 actions)
│   └── deploy_domain_targets.sh    #   registers targets + permits on the existing gateway
│
├── scenarios/
│   └── contradiction_demo.py       # Live supersede + dispute through record_fact (task 11)
├── sql/
│   └── rca_lineage.sql             # Single-query RCA over fact_event (task 12)
│
├── execution/
│   ├── agent_ui.py                  # Demo 2 — Streamlit chat UI (tenant selector in sidebar)
│   ├── fraud_dashboard.py           # Demo 1 — Real-time fraud monitor (tenant selector)
│   ├── sports_betting_dashboard.py  # Demo 3 — Betting risk + fraud monitor (tenant selector)
│   ├── betting_investigation.py     # Demo 4 — CLI cognitive-foundation investigation
│   ├── run_agent.py                 # Legacy CLI helper (pre-cognitive-foundation; kept for compatibility)
│   ├── seed_demo_data.py            # Creates the demo persona (Bernard) — tenant-scoped
│   ├── seed_orders.py               # Adds order history for the demo persona — tenant-scoped
│   ├── seed_reviews.py              # Reviews with sentiment scores and embeddings — tenant-scoped
│   ├── seed_fraud_data.py           # Fraud scenarios for Demo 1 — tenant-scoped
│   ├── seed_betting_data.py         # Betting events and scenarios for Demo 3 — tenant-scoped
│   └── apply_fraud_schema.py        # Schema migration helper (run if needed)
│
└── directives/
    └── tidb_agent_demo.md           # Demo directive: lifecycle, business value, demo flow
```

For the architecture, theses, custodial-duty implementation details, and POC-phase design questions, see [ARCHITECTURE.md](ARCHITECTURE.md) and [MEMORY_MAINTENANCE_POC.md](MEMORY_MAINTENANCE_POC.md).

---

## Troubleshooting

| Error | Fix |
|---|---|
| `SSL connection error` | Check `TIDB_SSL_CA` in `.env` points to the downloaded `isrgrootx1.pem` |
| `No results found` for customer queries | Run `seed_demo_data.py` and `seed_orders.py` |
| `No velocity anomalies` on Fraud Dashboard | Run `seed_fraud_data.py` |
| `No liability concentration` on Betting Dashboard | Run `seed_betting_data.py` |
| `No velocity anomalies` on Betting Dashboard | Run `seed_betting_data.py` — seeds the IP burst scenario |
| `No results` for sentiment/review queries | Run `seed_reviews.py` |
| `TOKENIZERS_PARALLELISM` warning | Already handled in `agent_ui.py` — safe to ignore |
| TiFlash query falls back to TiKV | Replica sync takes ~1 min after schema creation — wait and retry |
| Agent returns 0.50 confidence fallback | The triggered IP/customer isn't in seed data — use the known-good triggers above |

---

## Composability with TiDB Python SDK

PingCAP's official [pytidb](https://github.com/pingcap/pytidb) SDK ships an MCP server, a Pydantic-style schema layer, and built-in embedding functions (cloud-hosted Titan, AWS Bedrock-hosted Titan via Bedrock IAM, or local). The Cognitive Foundation **composes with pytidb**, not against it: pytidb is the data-access layer, the Cognitive Foundation provides the memory semantics — typed three-tier memory, custodial duties, substrate-driven routing — one layer above it. Adopting pytidb's `EmbeddingFunction` or its MCP server requires no schema changes here. Both projects converge on TiDB as the substrate for AI-era memory, which we treat as independent corroboration of the architectural bet rather than a competing approach.

See [ARCHITECTURE.md](ARCHITECTURE.md#differentiation-pytidb-is-the-sdk-this-is-the-pattern) for the differentiation table and production-deployment shape.

---

## Cognitive Foundation Portfolio

This repo is one of three implementations demonstrating the cognitive foundation across different domains:

| Repo | Domain | Memory tier spotlight | Custodial duty spotlight | Business outcome |
|---|---|---|---|---|
| [`tidb-self-healing-db-agent`](https://github.com/bernard-kavanagh/tidb-self-healing-db-agent) | Database operations | **Procedural** | Write control + branching safety | Reduced MTTR, safe autonomous remediation |
| [`ev_charger_anomaly_detection`](https://github.com/bernard-kavanagh/ev_charger_anomaly_detection) | Industrial IoT | **Semantic** | All five duties — the production reference | 10× token reduction, 24/7 monitoring at capped cost |
| [`tidb_fraud_detection`](https://github.com/bernard-kavanagh/tidb_fraud_detection) | Fintech / Gaming | **Three tiers, two adapters, governed fact layer** | Write Control + Reconciliation live server-side (single-mode); Dedup retired (canonical subject keys); Decay + Compaction + HITL-queue as POC decisions | Adaptive fraud detection, regulatory-grade audit trail, multi-tenant isolation, multi-vertical adapter proof |

All three repos run on the same principle: a **unified data substrate** where the agent's memory lives alongside operational data. The domain adapter changes. The substrate stays the same.

*The model forgets everything. The platform remembers. The human decides.*
— Bernard Kavanagh, *Cognitive Foundation series*
