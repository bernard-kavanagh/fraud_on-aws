# Cognitive Foundation — Demo Directive

> Operating directive for the TiDB cognitive-foundation fraud demo.
> Read by: AI assistants working on this repo, sales engineers prepping the
> demo, and the agent itself (Thesis 07 — procedural memory: how to act on
> what it knows).
>
> Canonical runtime instructions live in `cognitive_loop.SYSTEM_PROMPT_TEMPLATE`
> and the per-adapter `SCHEMA_HINT` constants. This directive supersedes the
> pre-cognitive-foundation "Analytical RAG" framing that the same file used to
> carry.

---

## Goal

Demonstrate that the cognitive foundation **compounds fraud intelligence on a single TiDB cluster** — every confirmed investigation becomes a routable, recallable pattern that makes the next investigation cheaper and more accurate, with no retraining and no pipeline.

The demo's job is to show the buyer that their stated pain — scale, retrieval, data movement, branching — is what this architecture eliminates.

---

## Who's in the room

Two buying centres are usually present in one meeting:

1. **Fraud Operations** (Economic Crime Hub, Fraud Prevention CoE). Live the pain daily. Care about analyst Mean-Time-to-Decision and false-positive rate.
2. **Data & Analytics**. Own the infrastructure decision. Care about scale, data-movement cost, and consolidation of the stack.

The demo must speak to both simultaneously — the cognitive-foundation story is that **one cluster eliminates both classes of pain** (the data-movement substrate for one buyer, the assembled-context investigation surface for the other).

---

## What the buyer told us (the pain, in their words)

| Their language | What it means | What the demo answers |
|---|---|---|
| 15–17M transactions, 10M wps, ~100M events | Tier-1 scale; current stack is buckling | HTAP dashboard — TiKV writes + TiFlash queries on same cluster, no ETL |
| Retrieval is hard — dataset is huge, need device/IP/history together | The **Memory Wall**, in their language | `assemble_context()` — five tiers, one SQL pass, <50ms, zero LLM calls |
| "How do you handle data movement?" | Movement is their latency; latency is their fraud | One connection string. No pipeline. Same data, two engines. |
| Need branching — payment journey, filter 10% for analysis | Want event-chain context, not just event-point | `agent_reasoning` checkpoints + Tier 4 prior investigations |
| Mentioned Flink | They've evaluated streaming layers | TiDB is the memory substrate underneath Flink — complementary, not competitive |

---

## The three business-value metrics

These map 1:1 to what the buyer said.

### 1. Detection latency: hours → milliseconds
- **Their current flow:** OLTP → batch ETL → historical store → fraud rules → alert.
- **The bottleneck:** the ETL.
- **The cognitive-foundation answer:** TiFlash is a columnar replica of TiKV kept in sync in real-time. Rules run on live operational data through the columnar engine. Detection happens **on the same write path, not after it.**
- **The line:** *"We catch it on swipe, not the next day."*

### 2. Mean Time to Decision: stale queues → instant context
- **Their current flow:** analyst opens a queue item, retrieves IP history, device history, account history, prior patterns, payment journey — each from a different system. Five-system tax.
- **The cognitive-foundation answer:** `assemble_context()` builds the full picture in ~50ms with **zero LLM calls**. Analyst arrives at the decision with a pre-assembled brief.
- **The line:** *"Your analysts open a queue item and spend the first five minutes pulling context from four different systems. We've eliminated those five minutes."*

### 3. False positive rate: noise → precision
- **At buyer scale,** even 1% FPR = 150,000–170,000 legitimate transactions wrongly blocked per day. Customer friction, complaint volume, frozen revenue.
- **The cognitive-foundation answer:** the agent cross-references device fingerprint + account history + semantic memory before flagging. Multi-variable context = precise signal.
- **The line:** *"The agent doesn't just fire on velocity — it fires on velocity AND a matching pattern in fraud_memory AND a confidence-graded prior investigation."*

---

## The cognitive-foundation lifecycle (powers everything below)

Four stages. Canonical. Implemented end-to-end in this repo.

1. **ASSEMBLE** — `assemble_context(entity_ref, session_id, trigger_text, adapter)` builds a 5-tier prompt under a 3,600-token budget. Pure SQL. Zero LLM calls. ~50ms.
   - T1 entity profile (customer + risk band)
   - T2 recent activity (orders/bets)
   - T3 active investigation (latest `agent_reasoning` row)
   - T4 prior investigations (entity-scoped session history)
   - T5 semantic memory (fraud_memory via vector similarity, capped at 500 tokens)

2. **ROUTE** — `route_investigation(vector_matches, confidence_gate, similarity_gate)` scans Tier 5 matches. If **any** row passes both gates → Haiku/3-round shortcut. Otherwise → Sonnet/15-round explore. Per-adapter gates (fraud 0.45 / betting 0.55) reflect calibrated trigger language per domain.

3. **LOOP** — cached system prompt (`cache_control: ephemeral`) + tool-use. The model picks tools from a small surface: `execute_sql`, `vector_search`, `recall_similar_fraud`, `flag_order`, `write_reasoning_checkpoint`, `compound_resolution`. Must write a structured checkpoint before ending.

4. **SUMMARY** — `_slim_summary()` reads the latest `agent_reasoning` row and produces a 3-paragraph report from structured fields (observation / hypothesis / evidence / confidence / resolution). **Does NOT replay the loop.** Fallback synthesises a 0.50-confidence checkpoint from `tool_trace` if the agent didn't write one.

---

## The five custodial duties

What turns a vector table into actual memory (Thesis 03).

| Duty | Live? | Where |
|---|---|---|
| 1. Write Control | ✅ | `compound_resolution()` rejects writes below `WRITE_CONTROL_MIN_CONFIDENCE` (0.85). |
| 2. Deduplication | ✅ | `consolidate_fraud_memory()`. Merges cosine-distance < 0.15. Sets `superseded_by`. |
| 3. Reconciliation | 🟡 stub | `reconcile_fraud_memory()`. Spec ready, implementation next. |
| 4. Confidence Decay | 🟡 stub | `decay_fraud_memory(half_life_days)`. |
| 5. Compaction | 🟡 stub | `compact_fraud_memory()`. |

On the demo: only one duty needs to be visible (dedup). The other four are named in the directive and the README so the architecture is grep-able even where it's not yet running.

---

## Demo flow (15 minutes)

### 1. Fraud Dashboard — 2 min
Run `live_pulse.py` (writes) and `fraud_dashboard.py` (TiFlash query) side by side. Show writes every 500ms, the velocity-anomaly query running concurrently on the same cluster.

> **What to say:** *"Same cluster, same data, no ETL. Scale this to your write volume — the architecture doesn't change. This is the data-movement answer."*

### 2. Admin Investigation — "any fraudulent orders?" — 8 min
Open `agent_ui.py`, role = Admin. Use a **known-good trigger** (see README "Known-good demo triggers" section):

- `investigate suspicious orders from IP 185.15.54.22` (velocity burst — 5 pending orders seeded)
- `investigate customer 4 for chargeback fraud` (Clayton Knight — the agent finds *delivery-confirmed-before-signup*, an anomaly check it invents itself)

Walk through the sidebar in real-time:
- **Context assembled** — *"Budget 107/3600 tokens. Zero LLM calls. Pure SQL."*
- **Routing decision** — *"Code, not the model, decided which model runs. Sonnet on cold, Haiku on warm. The substrate picks."*
- **Tool calls** — SQL queries, vector recall, write-back. *"Notice the model never asks for schema — the adapter ships it."*
- **Structured checkpoint** — *"Episodic memory. Not a transcript. Observation, hypothesis, evidence, confidence, resolution."*
- **Slim summary** — *"The summary reads the checkpoint, not the loop. ~37% fewer tokens, zero empty reports."*

### 3. Run it again — 2 min
Same trigger, second run. This time it shortcuts.

> **What to say:** *"First investigation: Sonnet, 15 rounds. Second: Haiku, 3 rounds. Same quality report. The system learned. The substrate is why both happen in the same place."*

This is the warm-up curve from AGENT_LIFECYCLE.md §4 made visible in 30 seconds.

### 4. Dedup duty — 1 min
Click "🧹 Run dedup (custodial duty)" in the sidebar.

> **What to say:** *"Memory maintenance is deterministic and auditable. Every merge writes a `superseded_by` link. Nothing decays silently. This is one of five duties — reconciliation and decay run on the same shape."*

### 5. Close — 2 min
Open `.env`. Show the single connection string.

> **What to say:** *"No Pinecone. No Redis. No separate warehouse. One TiDB cluster. One bill. The fraud intelligence compounds here."*

---

## Database UI — write-back proof moments

Open your TiDB SQL editor (or any MySQL-compatible UI) in a split-screen next
to the agent UI. Have these four queries pre-loaded in tabs. They are the
proof artefacts behind every claim the demo makes.

### Tab 1 — `orders` (the write-back surface)

```sql
SELECT order_id, customer_id, amount, ip_address, country,
       status, flagged_reason, order_date
FROM orders
WHERE status IN ('pending','flagged')
   OR order_date >= NOW() - INTERVAL 1 HOUR
ORDER BY order_date DESC
LIMIT 25;
```

**Proof moment:** Before launching the investigation, run this query — show
the `pending` rows. After the agent fires `flag_order`, refresh. A row that
was `pending` is now `flagged` with the agent's natural-language justification
in `flagged_reason`.

> *"One refresh, one row, the whole 'agent writes back to operational data'
> story. The flagged_reason field is the agent's own reasoning persisted
> back to the same row that holds the transaction. No separate alert pipeline,
> no enrichment lag — same row, same cluster, ACID."*

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

**Proof moment:** Switch here after the investigation completes. The newest
row is the structured checkpoint the slim-summary call reads from.

> *"The summary report you just read wasn't built by replaying the agent's
> conversation — it was built from this one row. Five structured fields:
> observation, hypothesis, evidence_refs, confidence, resolution. EU AI Act
> Article 14 asks for human-oversight evidence. This is it — every decision
> the agent made, in one row, in one query, defensible to an auditor."*

### Tab 3 — `fraud_memory` (semantic memory, the compounding signal)

```sql
SELECT pattern_id, scope, entity_ref,
       confidence,
       evidence_count,
       superseded_by,
       last_reinforced_at,
       LEFT(content, 80) AS pattern
FROM fraud_memory
WHERE superseded_by IS NULL
ORDER BY last_reinforced_at DESC
LIMIT 20;
```

**Three things to point at:**

1. **`evidence_count`** — *"Every time a pattern wins the routing gate, this
   number ticks up. It's the substrate's measure of which patterns are pulling
   weight."*
2. **`last_reinforced_at`** — *"This drives the confidence-decay duty. Patterns
   that haven't been reinforced in the configured half-life window fade —
   stale knowledge doesn't poison future reasoning."*
3. **`superseded_by`** — show a row where it's non-null. *"This pattern was
   retired by the deduplication custodial duty. The chain is preserved — full
   audit history of what was learned and what replaced it. Reconciliation
   uses the same column when contradicting evidence is resolved."*

**The killer two-run demo:** Run the same investigation **twice**.

- **First run:** agent fires `compound_resolution()` → new row in `fraud_memory`
  with `evidence_count = 1`.
- **Second run** (same trigger): routing SHORTCUTs against that new pattern →
  `evidence_count` ticks to 2, `last_reinforced_at` becomes "just now."

That's the **Gap-2 reinforcement signal + Duty-4 decay coupling** on screen,
in one refresh. The system measurably learned in 60 seconds.

### Tab 4 — `agent_sessions` (lineage, the audit trail)

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

**Proof moment:** When you want to show the compliance / audit story.

> *"Every session knows where it came from. The UI bootstrap session created
> the investigation session. The investigation session knows the entity it
> was scoped to. Three minutes from now I can rebuild exactly what this
> analyst saw, what the agent did, what got flagged, and which fraud pattern
> got reinforced — all in this one cluster, no external sync, no audit-export
> pipeline."*

For full lineage walk: `parent_session` from this query feeds back into
`WHERE session_id = '<parent>'` — recursive trail of the entire investigation
chain.

---

## The money-shot sequence

If you want one *"watch this"* moment during the demo, run this sequence with
the database UI in split-screen next to the agent UI:

1. **DB Tab 1 visible.** Filter `WHERE status='pending'`. Note the order_id of a row that matches the trigger you're about to use.
2. **Agent UI.** Type `investigate suspicious orders from IP 185.15.54.22` (or your chosen trigger from the README's known-good list).
3. Let the investigation run. Agent flags the order live.
4. **DB Tab 1 — refresh.** Row is now `flagged`, with the agent's reasoning in `flagged_reason`.
5. **Switch to DB Tab 2.** New `agent_reasoning` row at the top. *"That's the episodic memory write."*
6. **Switch to DB Tab 3.** Either `evidence_count` bumped on an existing pattern (warm path) or a brand-new pattern row appeared (cold path that compounded). *"That's the semantic memory write."*
7. **Switch to DB Tab 4.** New session row with `source = 'agent_ui.investigation'` and `parent_session` pointing at the bootstrap. *"That's the audit trail."*

Four tabs, four writes, **one cluster, one transaction boundary, one connection
string** — the full Thesis 04 + Thesis 10 story in 90 seconds of clicking.

## What NOT to show in the database UI

- **`chat_history` table.** Verbose, mostly noise, distracts from the
  structured `agent_reasoning` story. The point of `agent_reasoning` is that
  it's the cleaner audit artefact. Don't undercut it by showing the
  transcript table.
- **Legacy session metadata.** Sessions from before the audit-trail commit
  have `JSON_EXTRACT(metadata, '$.source') = 'run_agent.py'` — relabel them
  to `'legacy'` before the demo, or filter them out at query time, or be
  prepared to tell the honest story.

---

## Audience-specific opening lines

**For Data & Analytics:**
> *"You told us retrieval is hard because the dataset is huge. We've built something that shows you why — and what the alternative looks like."*

**For the Economic Crime Hub:**
> *"Your analysts open a queue item and spend the first five minutes pulling context from four different systems. We've eliminated those five minutes."*

---

## The Flink conversation

They will ask. Don't dodge. Their scale warrants a streaming layer.

> *"Flink is excellent at stream processing — ingesting events, applying windowed rules, detecting velocity bursts in real-time. We don't replace that. What we replace is everything that comes after the detection event: the investigation context, the pattern memory, the agent reasoning, the historical lookups. Flink fires the alert. TiDB is where the intelligence lives that makes the alert meaningful."*

This is the Ververica wedge: **they solve detection. TiDB solves memory.** At tier-1 scale both layers are needed. Positioning TiDB as Flink's memory substrate is honest and stronger than positioning it as Flink's replacement.

---

## The closing line

For the Fraud Operations lead:

> *"Every fraud pattern your team has ever investigated is currently trapped in a ticket system or an analyst's memory. This platform turns every confirmed investigation into a compounding signal — the next agent session that encounters a similar pattern arrives already briefed, routes to a cheaper model, and produces a structured report in seconds. The system gets more accurate and cheaper at the same time, automatically, with no retraining."*

---

## Operating principles for the agent (procedural memory)

When invoked on this repo as a working assistant:

1. **Trust the assembled context.** It was built before you saw the prompt. Don't re-fetch what Tiers 1–5 already gave you.
2. **Don't call `DESCRIBE` unless the schema isn't in your prompt.** The adapter's `SCHEMA_HINT` is in every system prompt; using it saves a tool round.
3. **Write a structured checkpoint before ending.** The summary is built from your checkpoint, not your conversation. Make the checkpoint precise.
4. **Persist only what passes write control.** `compound_resolution()` will reject below 0.85; don't try to bypass.
5. **Honour the routing decision.** If you're on the shortcut path with 3 rounds, work fast. If you're on explore with 15, work thoroughly.

---

## References

- [../ARCHITECTURE.md](../ARCHITECTURE.md) — architecture deep-dive: theses status, lifecycle diagram, three-tier memory, custodial duties
- [../MEMORY_MAINTENANCE_POC.md](../MEMORY_MAINTENANCE_POC.md) — POC planning conversations for Duty 3 (Reconciliation) and Duty 5 (Compaction). Bring this to the POC kickoff.
- [cognitive_loop.py](../cognitive_loop.py) — the loop and the system prompt
- [agent_tools.py](../agent_tools.py) — substrate functions (assemble, route, duties)
- [adapters/fraud/__init__.py](../adapters/fraud/__init__.py) — fraud adapter tier callables + SEED_CATALOG + SCHEMA_HINT
- [adapters/betting/__init__.py](../adapters/betting/__init__.py) — second adapter (Thesis 11 proof)
