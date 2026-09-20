# Fraud Investigation — Demo Directive

> Operating directive for the TiDB + AgentCore fraud-investigation demo.
> Read by: AI assistants working on this repo, sales engineers prepping the
> demo, and the agent itself (procedural memory: how to act on what it knows).
>
> Canonical runtime instructions live in `cognitive_loop.SYSTEM_PROMPT_TEMPLATE`
> and the per-adapter `SCHEMA_HINT` constants. The governed Fact Layer's
> authoritative contract lives in **Repo 1** (`../aws/ARCHITECTURE.md`) — this
> directive cross-references it rather than restating it.
>
> This revision re-orders the story. The center of the demo is now **a real
> fraud investigation that reaches an explainable conclusion** — not four
> database writes and not the A/B/C governance proof. The compounding-memory
> and governance material is preserved but repositioned as supporting acts.

---

## Goal

Show the buyer three things, in this order of emphasis:

1. **Primary — an explainable investigation.** A suspicious transaction triggers
   an investigation that combines *four conceptually-distinct* sources —
   operational evidence, governed institutional context, confirmed historical
   precedent, and authoritative documentation — into a conclusion the agent can
   **explain**: why this verdict holds, on whose authority, against what contrary
   evidence.

2. **Secondary — compounding precedent.** A confirmed investigation can create
   reusable institutional/semantic precedent that *can* make a subsequent
   similar investigation more efficient. This is a benefit we demonstrate the
   mechanism for; the magnitude is a benchmark opportunity, not an established
   fact (see **Claims audit**).

3. **Governance proof (short).** The Fact Layer independently authorizes the
   *requested data scope* of every call. This is a ~2-minute architectural aside
   — an important property, not the business demo.

The demo's job is still to show the buyer that their stated pain — scale,
retrieval, data movement, branching — is what this architecture addresses. But
the "watch this" moment is now **`explain_fact`**, not the write-back tab.

---

## Who's in the room

Two buying centres are usually present in one meeting:

1. **Fraud Operations** (Economic Crime Hub, Fraud Prevention CoE). Live the pain
   daily. Care about analyst Mean-Time-to-Decision, false-positive rate, and
   whether a decision is *defensible*.
2. **Data & Analytics**. Own the infrastructure decision. Care about scale,
   data-movement cost, and consolidation of the stack.

Speak to both: the investigation story lands for Fraud Ops (an explainable,
briefed decision); the substrate/consolidation story lands for Data & Analytics
(operational + analytical + memory on one cluster, governed context alongside).

---

## What the buyer told us (the pain, in their words)

| Their language | What it means | What the demo answers |
|---|---|---|
| 15–17M transactions, 10M wps, ~100M events | Tier-1 scale; current stack is buckling | HTAP dashboard — TiKV writes + TiFlash queries on same cluster, no ETL |
| Retrieval is hard — dataset is huge, need device/IP/history together | The **Memory Wall**, in their language | `assemble_context()` — operational tiers in one SQL pass, plus governed Tier-5 recall |
| "How do you handle data movement?" | Movement is their latency; latency is their fraud | One connection string for operational + analytical + episodic memory. No ETL pipeline. |
| Need branching — payment journey, filter 10% for analysis | Want event-chain context, not just event-point | `agent_reasoning` checkpoints + Tier-4 prior investigations |
| Mentioned Flink | They've evaluated streaming layers | TiDB is the memory substrate underneath Flink — complementary, not competitive |
| "Can you defend a decision to an auditor / regulator?" | Explainability is a procurement gate | `explain_fact` — current resolution, winning authority, supporting vs contrary evidence, full history |

---

## The responsibility model (read this before the flow)

The single most important correction in this revision: **the four sources the
agent draws on are conceptually distinct systems.** Do not imply they all live
in the Fact Layer, and do not imply they share one transaction boundary. They do
not.

| Concern | What it holds | Where it lives (this repo) |
|---|---|---|
| **Fraud application / operational domain** | transaction / customer / device / IP data; alerts and anomaly signals; investigation business context; operational write-back (e.g. flagging an order) | Local TiDB: `orders`, `customers`, etc.; `flag_order` write-back |
| **Workflow / episodic memory** | `agent_reasoning`; sessions/checkpoints; investigation process state | Local TiDB: `agent_reasoning`, `agent_sessions` |
| **Semantic / pattern memory** | similarity recall; reusable candidate precedent; useful *candidate* signal — **not by itself the authority mechanism for institutional truth** | Governed Fact Layer via `vector_search` (Tier 5) — server-side embedded, tenant-scoped |
| **Governed Fact Layer** | durable governed assertions/resolutions; history / disagreement; provenance / authority; `get_fact`, `get_fact_history`, `list_disputes`, `explain_fact`, `record_fact`. **Domain-neutral** even though this deployment seeds fraud predicates (`fraud_status`, `liability_status`) | Separately-deployed AgentCore service (**Repo 1**), reached over its Gateway via `fact_layer_client.py` |
| **Authoritative documentation** | official policy/docs source | Local TiDB: `sales_knowledge` (policies), reached on-demand via `vector_search(target_table='sales_knowledge')` — separate from operational records and governed facts |

Key distinctions to keep straight (from `../aws/ARCHITECTURE.md`):

- **business context ≠ authenticated principal.** Acme/John/TX123 is business
  context; the authenticated principal is the Cognito identity making the call.
- **subject/customer ≠ tenant.** John is a subject; Acme is the tenant scope.
- **`tenant_id` = requested data scope**, sent as a tool argument — *not* identity.
- **scope filter ≠ authorization.** Filtering rows by `tenant_id` is not the same
  as authorizing the request; the Gateway authorizes the requested scope
  independently.
- **Fact Layer = generic / domain-neutral.** Fraud predicates are
  deployment-seeded *vocabulary*, not a change to the mechanism.
- **AgentCore governance and Fact Layer adjudication are separate concerns.**
  One decides *may this caller request this scope*; the other decides *what is
  currently true and why*.
- **Operational DB remains the system of record for transactions.** The Fact
  Layer is the governed record of *what the agentic organization currently
  resolves to be true, and why*.

---

## Canonical demo narrative

**Business context:** Tenant **Acme** · Customer **John** · Transaction **TX123**.

> Naming note: "Acme / John / TX123" is the narrative framing. The repo today
> ships concrete entities (`demo-bank-alpha` tenant; customer 4 "Clayton Knight";
> IP `185.15.54.22`). Mapping the Acme/John/TX123 names onto deterministic seed
> data is a **SEED** gap — see *Demo implementation gaps*.

The story: a suspicious transaction is detected for John at Acme. The
orchestration layer conceptually obtains, from **four distinct sources**:

1. **John's operational transaction evidence/history** — from the operational DB.
2. **John's relevant governed facts/history** — from the Fact Layer, scoped to Acme.
3. **Acme's confirmed historical fraud precedent/patterns** — semantic recall from
   the Fact Layer (tenant-wide), surfacing reusable *candidate* precedent.
4. **Authoritative official policy/documentation** — from the docs source
   (`sales_knowledge`).

The agent combines these into one investigation and reaches an explainable
conclusion. **Keep the sources conceptually distinct** when you narrate — three
of them are three different systems.

---

## Core demo sequence (~15 min; a 3–5 min executive slice is called out below)

### Beat 1 — Suspicious transaction / investigation context (2 min)

Establish Acme + John + TX123. Show the operational trigger and the relevant
transaction evidence.

- Optionally run `execution/fraud_dashboard.py` (TiFlash) next to a writer to
  show the HTAP substrate detecting velocity on the live write path.
- Open `execution/agent_ui.py`, select tenant **Acme** (`demo-bank-alpha`), and
  use a known-good trigger from the README (e.g. `investigate suspicious orders
  from IP 185.15.54.22`, or `investigate customer 4 for chargeback fraud`).

> **What to say:** *"A transaction just tripped an anomaly signal. Here's the
> operational evidence an analyst would start from — same cluster the write
> landed on, no ETL, no lag."*

### Beat 2 — Assemble investigation context (2 min)

`assemble_context()` builds the agent's prompt before the model runs.

- **What it supplies today (grounded):**
  - **Tiers 1, 2, 4 — operational evidence:** entity profile, recent activity,
    prior investigations. Adapter-delegated, tenant-scoped, **pure SQL, zero LLM
    calls** — this is the fast local path.
  - **Tier 3 — active checkpoint:** substrate-generic session state from
    `agent_reasoning`.
  - **Tier 5 — governed semantic recall:** `fact_layer_client.vector_search`,
    tenant-scoped, **embedded server-side on the Fact Layer** — reusable
    candidate precedent for Acme.

- **What it does *not* supply today (do not claim otherwise):**
  - `assemble_context()` does **not** call `get_fact` / `get_fact_history` /
    `explain_fact` — Tier 5 is *recall*, not the governed-fact read path.
  - `assemble_context()` does **not** pull official documentation — the docs
    source is a *separate* on-demand `vector_search('sales_knowledge')` tool the
    agent can call during the loop, not part of assembly. Wiring John-specific
    governed facts and official docs into the assembled brief is a **WIRE** gap.

> **Latency wording (corrected):** the "~50 ms, pure SQL, zero LLM calls" claim
> holds for the **local operational tiers (1–4)** only. **Tier 5 is a remote,
> governed, server-side-embedded call** over MCP — it carries network + embedding
> latency and is *not* local SQL. Do not state a single sub-50 ms figure for the
> whole assembly. Whole-assembly latency is a **benchmark opportunity**, not a
> measured claim.

> **What to say:** *"Before the model sees anything, the platform assembles the
> brief: John's operational history in one local SQL pass, plus governed
> precedent for Acme recalled from the Fact Layer. The model doesn't decide what
> to remember — the platform decides for it."*

### Beat 3 — Investigate (3 min)

The model reasons over the assembled evidence and picks tools from a small
surface: `vector_search`, `recall_similar_fraud`, `flag_order`,
`write_reasoning_checkpoint`, `compound_resolution`, `explain_fact`.

Preserve the existing tool-call / checkpoint / write-back demonstration:

- **Routing** decided which model runs (code, not the model): a Tier-5 match
  passing both gates → Haiku/3-round shortcut; otherwise → Sonnet/15-round
  explore.
- **Tool calls** — SQL-free governed reads/recall and the write-back.
  *"The model never asks for schema — the adapter ships it in the prompt."*
- **Structured checkpoint** — `write_reasoning_checkpoint` writes episodic memory
  (observation / hypothesis / evidence / confidence / resolution), not a
  transcript.
- **Operational write-back** — `flag_order` persists the agent's justification
  back onto the operational row.

> **What to say:** *"The agent investigates, cites its evidence, writes a
> structured checkpoint, and flags the order — all on the same cluster that holds
> the transaction."*

### Beat 4 — Controlled disagreement (2 min)

Demonstrate two conflicting assessments (e.g. FRAUD vs NOT_FRAUD) and show that
**authority adjudicates** — establishing a *current governed resolution* while
**preserving the contrary assertion in history** rather than silently
overwriting it.

- This is LIVE today via `scenarios/contradiction_demo.py` (a CLI scenario, not
  the UI investigation flow). It shows the Fact Layer's `record_fact`
  adjudication:
  - **higher authority supersedes** (prior event retained, hash-chained);
  - **lower-authority contradiction is rejected** (winner stands, contrary
    retained as evidence);
  - **comparable authority → `disputed`** (both retained, no recency tiebreak).

> **Target beat / gap:** wiring this *exact* FRAUD-vs-NOT_FRAUD disagreement onto
> the canonical John/TX123 entity inside the investigation UI is a **WIRE+SEED**
> gap. Today it runs as a deterministic-per-run CLI scenario. Describe it as the
> target beat and demonstrate it via the CLI script if the UI wiring isn't ready.

> **What to say:** *"The model can be wrong. A human investigator can disagree.
> The Fact Layer doesn't pick recency — it picks authority, and it keeps the
> loser on the record."*

### Beat 5 — Explain the resolution — **the money shot** (3 min)

Call `explain_fact` and show **why the current resolution exists** — resolution,
winning authority + policy version, supporting vs contrary evidence, assessments,
superseded/disputed prior assertions, and outcome history — to the extent the
Fact Layer actually returns them.

- The agent has `explain_fact` as a wired tool; `scenarios/contradiction_demo.py`
  also calls it directly and prints the provenance payload.
- **This is the principal "watch this" moment.** Not four database writes — *one
  question answered with governed provenance.*

> **UI gap:** the UI surfaces `explain_fact` output only as a raw tool-result
> line in the chain-of-thought. A formatted provenance/authority/history panel is
> a **WIRE** gap (tool exists; presentation missing). For the strongest version
> today, run `explain_fact` via the CLI scenario and read the structured payload.

> **What to say:** *"This is the difference between an alert and a decision. The
> platform can tell you what it believes about John, why, on whose authority, and
> what evidence argued the other way — in one call."*

### Governance aside (~2 min — keep it short)

> *"Every Fact Layer call declares the data scope it is requesting. In this
> investigation, that scope is Acme."*

Then show the reference authorization profile (A/B/C):

| # | Caller identity | Requested scope | Cedar decision |
|---|---|---|---|
| A | Acme identity | Acme | **ALLOW** |
| B | Acme identity | Globex | **DENY** |
| C | identity without a tenant claim | Acme | **DENY** |

Critical wording (do not paraphrase loosely):

- `tenant_id` is **requested data scope, not identity**.
- **tenant filtering is not authorization** — the governance layer authorizes the
  requested scope independently of any row filter.
- **Cognito + Cedar tenant-equality is the current *reference profile*,** not a
  universal Fact Layer requirement. The Fact Layer is domain- and
  auth-mechanism-neutral (`../aws/ARCHITECTURE.md`).
- The current deployment is **LOG_ONLY**. Therefore describe A/B/C as **observed
  Cedar policy *decisions*** — **not** as proof that a DENY currently *blocks*
  target/DB execution. ENFORCE is the production posture; LOG_ONLY records the
  decision without blocking.
- **L0 and L4 are proven; L2/L3 are supported by controlled inference**, not by
  directly-exposed per-condition telemetry (see Repo 1).

> **Gap:** there is no A/B/C ALLOW/DENY *panel* in the demo UI today. The
> cross-tenant DENY is demonstrable by configuration (point `DEMO_TENANT_ID` at
> one tenant while authenticating with a token whose claim is another), and the
> UI tenant selector shows isolation. A presentable A/B/C view is a **BUILD** gap.

> **What to say:** *"tenant_id is a requested scope, not a login. The governance
> layer decides whether this caller may ask for Acme's data at all — separately
> from what the Fact Layer then decides is true. Today that's running in
> log-only, so what you're seeing is the policy decision being recorded."*

### Compounding intelligence / second-run benefit (2 min — secondary)

Preserve the existing semantic-recall + routing demonstration, clearly
distinguished from governed institutional facts.

- **Semantic/pattern memory ≠ governed institutional facts.** Tier-5 recall
  surfaces reusable *candidate* precedent; it does not by itself establish
  institutional truth — adjudication does.
- If the implementation demonstrably routes a *warm* investigation differently
  (shortcut vs explore), retain that demonstration: first run explores; a second
  run against seeded/confirmed precedent can route to the shortcut path.

> **Do not state as fact:** "same quality", "more accurate", "cheaper", exact
> latency, exact token reduction (e.g. "~37% fewer tokens"), or other quantitative
> benefits — **unless the repo contains measured evidence** (it does not, today).
> Mark these as **hypotheses / benchmark opportunities**. What *is* grounded: the
> routing mechanism (code-driven model selection) and the recall mechanism exist
> and run.

> **What to say:** *"A confirmed verdict becomes recallable precedent for the next
> similar case at Acme. Whether that makes the next run cheaper or faster is
> exactly the kind of thing we'd measure with you in a POC — the mechanism is
> here; the number is yours to establish."*

### Close (2 min)

Connect the consolidation story to the broader architecture — **without**
claiming every concern is one transaction boundary.

- **One TiDB cluster** holds the operational/analytical fraud data (TiKV +
  TiFlash), the workflow/history (`agent_reasoning`, `agent_sessions`), and the
  authoritative docs (`sales_knowledge`).
- **The governed Fact Layer is a separate, governed service** (Repo 1) reached
  over its Gateway — it is *not* in the operational transaction boundary. That
  separation is deliberate: governed institutional truth is adjudicated and
  audited independently of the operational system of record.
- **AgentCore governs tool access** to both.

> **What to say (corrected):** *"Operational data, analytics, workflow memory,
> and policy docs consolidate onto one cluster — one connection string, no ETL.
> Governed institutional truth lives one step out, in a service whose whole job is
> to adjudicate and audit what's true. Two boundaries, on purpose — not five
> bolted-together stores."*

---

### The 3–5 minute executive vertical slice

If you only have five minutes, run: **Beat 1 (context) → Beat 2 (assemble) →
Beat 3 (investigate + flag) → Beat 5 (`explain_fact` money shot)**, with the
governance aside as a single sentence. Beat 4 (disagreement) and the
compounding/second-run act are the extended cut. See *Demo implementation gaps*
for what must be deterministic to make this slice reliable.

---

## Database UI — supporting proof (not the money shot)

Keep the database tabs as **technical proof / supporting material**. They are the
receipts behind the investigation, not the headline sequence. Open a
MySQL-compatible UI split-screen next to the agent UI.

### Tab 1 — `orders` (the operational write-back surface)

```sql
SELECT order_id, customer_id, amount, ip_address, country,
       status, flagged_reason, order_date
FROM orders
WHERE status IN ('pending','flagged')
   OR order_date >= NOW() - INTERVAL 1 HOUR
ORDER BY order_date DESC
LIMIT 25;
```

**Proof moment:** run before the investigation to show `pending` rows; refresh
after `flag_order` fires to show the row now `flagged` with the agent's
justification in `flagged_reason`.

> *"The agent's reasoning persisted back onto the same operational row that holds
> the transaction — same cluster, ACID, no separate alert pipeline."*

### Tab 2 — `agent_reasoning` (episodic memory, the audit trail)

```sql
SELECT reasoning_id, session_id,
       LEFT(observation, 100)  AS observation,
       LEFT(hypothesis, 100)   AS hypothesis,
       evidence_refs,
       confidence,
       LEFT(resolution, 120)   AS resolution,
       created_at
FROM agent_reasoning
ORDER BY created_at DESC
LIMIT 10;
```

**Proof moment:** the newest row is the structured checkpoint the slim-summary
call reads from.

> *"The report you read was built from this one row — five structured fields, not
> a replayed transcript. This is the workflow/episodic record; it is human-oversight
> evidence an auditor can read. (Framing only — do not assert a specific regulatory
> article such as EU AI Act Article 14 as a compliance guarantee.)"*

### Tab 3 — governed facts: **operator view is on the Fact Layer, not here**

The local `fraud_memory` table **no longer holds semantic memory** — it was
retired when semantic memory moved to the governed Fact Layer. Do **not** show a
local `fraud_memory` tab as the "compounding signal."

Instead, the governed-fact lineage lives in the Fact Layer's own TiDB. The
operator view is the single-query RCA:

```sql
-- Run against the FACT LAYER's TiDB (Repo 1), not this repo's operational cluster.
-- See sql/rca_lineage.sql — reconstructs a fact's full lineage
-- (first assertion → every supersede/dispute → current truth) from the
-- append-only, hash-chained fact_event log, filtered by tenant_id + subject.
```

> **Gap:** presenting governed-fact history/provenance *in the demo UI* (rather
> than via a SQL editor on the Fact Layer's cluster) is a **WIRE/BUILD** gap. The
> agent-facing equivalent is `explain_fact` (Beat 5).

### Tab 4 — `agent_sessions` (lineage / audit trail)

```sql
SELECT session_id, user_id,
       JSON_UNQUOTE(JSON_EXTRACT(metadata, '$.source'))             AS source,
       JSON_UNQUOTE(JSON_EXTRACT(metadata, '$.parent_session_id'))  AS parent_session,
       JSON_UNQUOTE(JSON_EXTRACT(metadata, '$.entity_ref'))         AS entity,
       created_at
FROM agent_sessions
ORDER BY created_at DESC
LIMIT 15;
```

**Proof moment (compliance/audit story):** every session knows where it came
from; `parent_session` feeds a recursive walk of the investigation chain.

### What NOT to show

- **`chat_history` table.** Verbose transcript; undercuts the cleaner
  `agent_reasoning` story.
- **Legacy session metadata.** Pre-audit-trail sessions may carry
  `source = 'run_agent.py'`; relabel to `'legacy'`, filter them, or tell the
  honest story.
- **A local `fraud_memory` tab as the semantic-memory money shot** — retired; see
  Tab 3.

---

## Audience-specific opening lines

**For Data & Analytics:**
> *"You told us retrieval is hard because the dataset is huge. We'll show you why
> — and what one governed substrate for operational data, memory, and policy
> looks like."*

**For the Economic Crime Hub:**
> *"Your analysts open a queue item and spend the first minutes pulling context
> from four different systems, then still can't fully defend the call. We assemble
> the context and — the part that matters — we can explain the verdict."*

---

## The Flink conversation

They will ask. Don't dodge. Their scale warrants a streaming layer.

> *"Flink is excellent at stream processing — ingesting events, applying windowed
> rules, detecting velocity bursts in real-time. We don't replace that. We replace
> everything after the detection event: the investigation context, the pattern
> memory, the agent reasoning, the governed record of what's true. Flink fires the
> alert. TiDB and the Fact Layer are where the intelligence lives that makes the
> alert meaningful and defensible."*

**They solve detection. We solve memory and governed truth.**

---

## The closing line

For the Fraud Operations lead (qualified — no unmeasured economics):

> *"Every fraud pattern your team has ever investigated is currently trapped in a
> ticket system or an analyst's memory. This architecture turns a confirmed
> investigation into recallable precedent, and — the part procurement cares about
> — into a governed fact you can explain: what we believe, why, on whose
> authority, and what argued against it. Whether the next investigation also comes
> out cheaper or faster is exactly what we'd measure together in a POC."*

---

## Operating principles for the agent (procedural memory)

When invoked on this repo as a working assistant:

1. **Trust the assembled context.** Tiers 1–4 (operational) and Tier 5 (governed
   recall) were built before you saw the prompt. Don't re-fetch what they gave you.
2. **Don't call `DESCRIBE` unless the schema isn't in your prompt.** The adapter's
   `SCHEMA_HINT` ships in every system prompt.
3. **Write a structured checkpoint before ending.** The summary is built from your
   checkpoint, not your conversation. Make it precise.
4. **Persist only confirmed verdicts.** `compound_resolution()` calls the governed
   `record_fact`; **write control is enforced server-side** on the Fact Layer.
   Don't try to bypass it.
5. **Use `explain_fact` before re-investigating** — check what is already durably
   known and why.
6. **Honour the routing decision.** Shortcut path (3 rounds) → work fast; explore
   path (15 rounds) → work thoroughly.

---

## Claims audit

Every quantitative or absolute claim in the prior directive, audited against
current implementation. Retained only where grounded; otherwise qualified or removed.

| Prior claim | Status | Disposition |
|---|---|---|
| "<50ms / ~50ms" whole-assembly | ⚠️ Partly false | Holds for **local Tiers 1–4** (pure SQL, zero LLM). **Tier 5 is a remote server-side-embedded MCP call** — not local, not sub-50ms. Reworded; whole-assembly latency = benchmark opportunity. |
| "~37% fewer tokens" | ❌ Unmeasured | Removed as fact. Marked hypothesis / benchmark target. |
| "same quality" (warm run) | ❌ Unmeasured | Removed. Routing mechanism retained; quality parity is a hypothesis. |
| "more accurate" / "cheaper" | ❌ Unmeasured | Qualified as POC-measurable, not established. |
| "automatically" (system gets better) | ⚠️ Overreach | Softened; write control gates what persists. |
| "one transaction boundary" (all concerns) | ❌ Incorrect | Fact Layer is a **separate service** (Repo 1). Corrected to *two* boundaries by design; operational/analytical/episodic/docs on one cluster, governed truth on the Fact Layer. |
| "every confirmed investigation becomes a routable pattern" | ⚠️ Overreach | Softened — server-side write control (confidence floor) gates persistence. |
| EU AI Act Article 14 | ❌ Removed as guarantee | Kept only as *framing* ("human-oversight evidence"), not a compliance claim. |
| FPR math (150k–170k/day at 1%) | ✅ Illustrative | Retained as arithmetic on the buyer's stated scale, labelled illustrative — not a measured product metric. |
| Reconciliation / decay / compaction "stubs" | ⚠️ Stale | Updated: reconciliation is **LIVE single-mode server-side**; dedup **retired** (canonical subject keys); decay **deferred** (no fact-model analog); compaction **POC-phase**. Per `ARCHITECTURE.md` + `../aws/ARCHITECTURE.md`. |
| `fraud_memory` table as semantic-memory money shot | ❌ Retired | Local `fraud_memory` no longer holds semantic memory; operator view is RCA on the Fact Layer's TiDB. |
| A/B/C DENY "blocks execution" | ⚠️ Deployment-dependent | Current deployment is **LOG_ONLY** — A/B/C are **observed Cedar decisions**, not proof of blocking. ENFORCE is production posture. |

**Do not manufacture benchmark evidence.** Where a number would help, run it in a
POC with the customer and cite the measurement.

---

## Demo implementation gaps

Derived from inspecting current Repo 2 code. Classification:
**READY** (implemented, locally demonstrable now) · **WIRE** (capability exists,
orchestration/UI wiring missing) · **SEED** (implementation exists, deterministic
demo data needed) · **BUILD** (capability does not currently exist).

| Demo beat / capability | Status | Evidence / gap |
|---|---|---|
| **Fact Layer MCP client** (`record_fact`, `get_fact`, `get_fact_history`, `list_disputes`, `explain_fact`, `vector_search`, `search_entities`, `retract_fact`, `query_tenant_metrics`) | **READY** | Full thin wrappers in `fact_layer_client.py`. Requires the Fact Layer Gateway env (`FACT_LAYER_GATEWAY_URL`, Cognito creds) reachable. |
| **`assemble_context()` operational tiers (1–4)** | **READY** | Pure SQL, adapter-delegated, tenant-scoped (`agent_tools.py:497`). |
| **`assemble_context()` Tier-5 governed recall** | **READY** | `fact_layer_client.vector_search`, server-side embedded, tenant-scoped (`agent_tools.py:602`). Depends on Gateway reachability + seeded catalog. |
| **John-specific governed facts in the assembled brief** (`get_fact`/`get_fact_history`) | **WIRE** | `assemble_context()` calls only Tier-5 *recall*, not `get_fact`/`get_fact_history`. Adding a per-subject governed-fact read to assembly is unbuilt. |
| **Official-doc / policy retrieval in the brief** | **WIRE** | Exists only as an on-demand agent tool `vector_search('sales_knowledge')` (`cognitive_loop.py`), not part of `assemble_context()`. |
| **`get_fact`** | **READY** (agent-callable requires WIRE) | Client wrapper ready; **not** exposed as an agent loop tool. |
| **`get_fact_history`** | **WIRE** | Client wrapper ready; not an agent tool and not in the UI. |
| **`list_disputes`** | **WIRE** | Client wrapper ready; not an agent tool and not in the UI. |
| **`explain_fact` (agent tool)** | **READY** | Wired as a loop tool (`cognitive_loop.py:166`, dispatch `:259`) and callable in `scenarios/contradiction_demo.py`. |
| **`explain_fact` presented as a provenance/authority panel** | **WIRE** | Surfaced only as a raw tool-result line in the UI chain-of-thought; no formatted provenance panel. |
| **`vector_search` (semantic recall)** | **READY** | Both Tier-5 (`assemble_context`) and on-demand (`recall_similar_fraud`, `agent_tools.py:988`). |
| **Investigate loop** (routing, tools, checkpoint, write-back) | **READY** | `cognitive_loop.run_investigation` + `agent_ui.py`. |
| **Operational write-back** (`flag_order`) | **READY** | Loop tool; DB Tab 1 proof. |
| **Structured episodic checkpoint + slim summary** | **READY** | `write_reasoning_checkpoint` + Stage-4 summary. |
| **Controlled FRAUD vs NOT_FRAUD disagreement** | **READY (CLI) / WIRE+SEED (in UI on John)** | LIVE via `scenarios/contradiction_demo.py` (supersede / reject / dispute). Uses random per-run subjects; wiring onto canonical John/TX123 inside the investigation UI is unbuilt. |
| **`explain_fact` money-shot on the disputed fact** | **READY (CLI) / WIRE (UI panel)** | CLI prints the provenance payload; UI panel missing. |
| **Governance A/B/C ALLOW/DENY as a presentable view** | **BUILD** | Cross-tenant DENY is config-demonstrable and the UI has a tenant selector, but there is no A/B/C panel. Deployment is LOG_ONLY (decisions observed, not blocking). |
| **Second-run model routing (warm shortcut)** | **WIRE + SEED** | `route_investigation` exists (`agent_tools.py`); reliable warm shortcut needs deterministic seeded precedent matching a known trigger. |
| **Deterministic demo seed** (Acme/John/TX123 across operational DB + Fact Layer) | **SEED** | Catalog seeding exists (`seed_fraud_memory_from_adapter`, UI button); concrete entities today are `demo-bank-alpha` / customer 4 / IP `185.15.54.22`. Acme/John/TX123 naming + aligned operational+fact seed is unbuilt. |
| **Deterministic demo reset** | **BUILD (partial)** | UI "Clear Memory" resets session only; no operational/fact reset script. Fact Layer is append-only (reset via fresh subjects, as `contradiction_demo.py` does with per-run ids). |
| **UI presentation of provenance/history** | **BUILD** | No provenance/history/dispute rendering in `agent_ui.py`; operator view is RCA SQL on the Fact Layer's TiDB. |

### Minimum implementation path for a deterministic 3–5 min executive slice

To make **Beat 1 → 2 → 3 → 5** reliable and deterministic (no live-demo surprises):

1. **SEED** one canonical entity end-to-end: an Acme (`demo-bank-alpha`) customer
   "John" with a specific order "TX123" in `orders`, plus a matching governed fact
   for `fraud:customer:<john>` seeded on the Fact Layer, and a relevant policy row
   in `sales_knowledge`. Reuse existing seed scripts + `seed_fraud_memory_from_adapter`.
2. **WIRE** `get_fact` (or `explain_fact`) into the assembled brief *display* so
   Beat 2 shows John's governed facts as a distinct source (keep it conceptually
   separate from Tier-5 recall and from docs).
3. **WIRE** an `explain_fact` provenance panel into `agent_ui.py` (resolution,
   winning authority, supporting vs contrary evidence, history) so Beat 5 is a
   formatted money shot, not a raw tool-result line.
4. **(Extended cut) WIRE + SEED** the FRAUD-vs-NOT_FRAUD disagreement onto the John
   subject so Beat 4 → Beat 5 runs on the same entity the audience just watched.
5. **(Optional) BUILD** a minimal A/B/C panel for the governance aside; until then
   demonstrate DENY by configuration and state LOG_ONLY explicitly.

No gaps are implemented in this pass — this is documentation/planning only.

---

## References

- **[../aws/ARCHITECTURE.md](../../aws/ARCHITECTURE.md)** — **Repo 1**: the governed
  Fact Layer's authoritative contract (adjudication, Cedar governance, LOG_ONLY vs
  ENFORCE, L0–L4). Cross-reference rather than duplicate.
- [../ARCHITECTURE.md](../ARCHITECTURE.md) — this repo's architecture: theses
  status, lifecycle, three-tier memory, custodial duties (fact-layer port)
- [../MEMORY_MAINTENANCE_POC.md](../MEMORY_MAINTENANCE_POC.md) — POC planning for
  reconciliation-queue mode and compaction policy
- [../fact_layer_client.py](../fact_layer_client.py) — MCP caller + subject-key convention
- [../cognitive_loop.py](../cognitive_loop.py) — the loop, tools, and system prompt
- [../agent_tools.py](../agent_tools.py) — `assemble_context`, routing, `compound_resolution`, `explain_fact`
- [../scenarios/contradiction_demo.py](../scenarios/contradiction_demo.py) — LIVE controlled-disagreement + `explain_fact` scenario
- [../sql/rca_lineage.sql](../sql/rca_lineage.sql) — single-query fact lineage (operator view, on the Fact Layer's TiDB)
- [../fact_layer_targets/README.md](../fact_layer_targets/README.md) — this repo's domain read targets on the Fact Layer Gateway
- [adapters/fraud/__init__.py](../adapters/fraud/__init__.py) — fraud adapter tiers + SEED_CATALOG + SCHEMA_HINT
