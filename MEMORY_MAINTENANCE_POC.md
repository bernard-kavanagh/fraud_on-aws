# Memory Maintenance — POC Planning Conversations

> A working document for the POC planning phase between **your team** (Fraud
> Operations / Risk / Data & Analytics) and the TiDB cognitive-foundation
> implementation team.
>
> This isn't a feature spec. It's the set of decisions only your team can make,
> framed so we can have one conversation that gets each one right.

---

## Why this document exists

The cognitive foundation maintains agent memory through custodial duties. Since
the **port onto the AgentCore Governed Fact Layer**, semantic memory (confirmed
verdicts + their recall) lives in a separately-deployed governed service reached
over its Gateway, and the duty statuses have changed:

| Duty | Status | Note |
|---|---|---|
| Write Control | ✅ Live (server-side) | Enforced inside the fact layer's `record_fact` (a confidence floor rejected before adjudication). The caller no longer runs a local floor. |
| Reconciliation | ✅ **Live (single-mode)** | Now automatic in `record_fact` adjudication: higher-authority contradiction auto-supersedes (prior event retained + hash-chained); equal/lower-authority contradiction → `disputed`. See Duty 3 below. |
| Deduplication | ⛔ Retired | Deleted in favour of **canonical subject keying**: paraphrased assertions about the same fact collide on one `(tenant_id, subject, predicate)` identity and are adjudicated by `record_fact` — no cosine-distance merge. |
| Confidence Decay | 🟡 Deferred | `fact_current` has no per-fact confidence field; a decay analog for the fact model is its own design pass, out of scope for the port. |
| Compaction | 🟡 POC-phase decision | `fact_event` stays append-only and unbounded for now — see Duty 5. |

**Reconciliation is now live in single-mode** (auto supersede-by-authority or
dispute). What is NOT built, and remains a decision only your team can make, is
the **human-in-loop queue mode** and the **evidence-weighted mode** — and
**Compaction** (when and how memory is archived). Those require domain-specific
judgment about your data, regulatory environment, risk appetite, and operating
model.

We could ship defaults for those too. At tier-1 scale, defaults are how you cause
incidents. This document frames each as the conversation we need to have together.

---

## Duty 3 — Reconciliation

> **Status since the fact-layer port: single-mode reconciliation is LIVE.** Every
> `record_fact` write runs the fact layer's deterministic adjudication: a
> contradiction from a strictly higher-authority source **auto-supersedes** (the
> prior claim is retained, hash-chained, and visible in the `fact_event` audit
> trail — see [`sql/rca_lineage.sql`](sql/rca_lineage.sql)); an equal/lower-authority
> contradiction resolves to **`disputed`** (both claims retained, no recency
> tiebreak). This is exactly the single-mode auto path described in Q2 below.
>
> What is intentionally **still a decision, not built**: the **human-in-loop
> queue** (a `reconciliation_queue` table + approval UI) and the
> **evidence-weighted mode**. The rest of this section is the framing for that
> decision — bring it to POC kickoff. The Q1/Q2 material below now reads as
> "which mode do you want on top of the live single-mode default," not "should we
> build reconciliation at all."

### The gap (in your operational language)

A fraud investigation today produces a stored pattern in `fraud_memory` — call
it Pattern A: *"Customer X exhibits velocity burst from IP Y, flagged as fraud,
confidence 0.93."* Two weeks later, the manual-review team clears Customer X
after a chargeback investigation finds the velocity was a legitimate B2B bulk
purchase. The reviewer's resolution writes Pattern B: *"Customer X velocity
burst from IP Y was a false positive, cleared after Tier 2 review, confidence
0.95."*

Both patterns now exist in `fraud_memory`. Both match the same entity. They
contradict.

The next time Customer X exhibits a similar signature, the routing layer can
shortcut on **either** pattern. Without reconciliation, the system has no way
to know which one represents current ground truth.

### Why it matters at your scale

The arithmetic at your transaction volume:

- 15–17M transactions per day
- Even a 0.1% rate of routing onto a stale-contradicted pattern = **15,000 to 17,000 wrongly-handled decisions per day**
- Each wrong decision is either a false positive (customer friction, complaint volume) or a false negative (fraud loss, regulatory exposure)
- At a typical UK card fraud loss rate, the dollar impact of un-reconciled memory at this scale runs into seven figures annually

Reconciliation isn't optional at this scale. **How** it's done is the question.

### The five decisions only your team can make

These are the questions we need to settle together before we build the
reconciliation layer.

**Q1. What signal counts as a "contradiction"?**

Two patterns at low cosine distance might be:

- **Reinforcing** (same pattern, paraphrased) — our deduplication duty handles this, no human needed
- **Refining** (same target, more specific signal) — both true, newer doesn't supersede older
- **Contradicting** (same target, opposing verdict) — this is the reconciliation case

The system can't distinguish reinforcing from contradicting based on text similarity alone. **Our recommended approach** is to add a structured `verdict` field (FRAUD / CLEARED / AMBIGUOUS) to every pattern. Contradiction then becomes a deterministic SQL query: same entity, opposing verdict.

*Your decision:* Are you comfortable adding the `verdict` field? Alternative is a content-based heuristic (regex on FRAUD vs CLEARED tokens), which is fragile.

**Q2. Auto-resolve, or queue for human review?**

Two design paths:

- **Auto-resolve:** the newer/higher-evidence/higher-confidence pattern wins. Loser gets `superseded_by` automatically. Fast, fully automated.
- **Queue for review:** detected contradictions write to a `reconciliation_queue` table. A human (or higher-tier model) makes the call. Slower, fully auditable.

**For a regulated bank, our strong recommendation is the queue.** The cost of a wrong auto-reconciliation (retiring a still-valid fraud pattern that catches real losses) is greater than the cost of a duplicate review.

*Your decision:* Queue, auto-resolve, or hybrid (auto for some patterns, queue for others based on entity or confidence threshold)?

**Q3. Who resolves the queue?**

Three plausible owners. None are wrong. Each has downstream implications:

| Owner | Strengths | Trade-offs |
|---|---|---|
| **Fraud Ops analyst** | Lives the patterns, knows the false-positive cost | Adds queue work to an already-busy queue |
| **Risk / Compliance team** | Natural owner of "is this an SLA-breaching pattern retirement?" | Slower decision cycle |
| **Higher-tier LLM (e.g. Opus) on a scheduled review job** | Fast, consistent | Requires buyer comfort with AI-resolving-AI |

*Your decision:* Who owns the queue? This determines what UI we build, what SLA the queue runs to, and what audit trail we need.

**Q4. What happens to the patterns while the queue item is unresolved?**

While a contradiction is pending review:

- **(A) Both stay active** — both keep firing in the routing layer until resolved. Risk: the wrong one drives shortcuts during the review window.
- **(B) Older goes inactive, newer stays** — risk: if reviewer decides older was right, you've had a coverage gap during the review window.
- **(C) Both go inactive** — neither fires until resolved. Safest, but downgrades routing temporarily (explore path, not shortcut).

**Our recommendation for tier-1 environments: (C).** Better to lose a shortcut for 24 hours than to make a wrong call during the review window. But this is your operating-model decision.

*Your decision:* Which state do queued patterns sit in?

**Q5. What metadata do we keep on every reconciliation event?**

For Article 14 (EU AI Act, human-oversight) and your internal audit:

- `reconciliation_id` — primary key
- `pattern_a_id`, `pattern_b_id` — the contradicting pair
- `detected_at` — when the detector first surfaced them
- `resolved_at`, `resolved_by` (analyst id / model id)
- `resolution_action` — `KEEP_A` / `KEEP_B` / `KEEP_BOTH` / `MERGE`
- `rationale` — short text from the resolver

This becomes a regulatory artefact. We recommend the full set; you tell us what's surplus to your audit needs.

### Implementation paths after we've settled the five questions

Once we have your answers, three implementation patterns are available:

- **Minimal queue (recommended for POC):** detection job + queue table + sidebar resolve button. ~1 day of work. Demonstrates the model.
- **Full queue with workflow:** detection + queue + assignment + SLA tracking + dashboards + integration into your existing case management system. ~2 weeks.
- **Auto-resolve hybrid:** lightweight queue for low-stakes contradictions, auto-resolve for high-confidence newer evidence. Risk-tuned.

Pick the path that fits your POC scope; we'll build the version that does.

---

## Duty 5 — Compaction

### The gap (in your operational language)

Over time, `fraud_memory` accumulates three categories of rows that don't earn
their keep:

1. **Superseded patterns** — rows where `superseded_by IS NOT NULL` (the dedup duty wrote these). They're not queried by the active routing path, but they're still in the table consuming space and slightly slowing vector-index lookups.
2. **Decayed-below-floor patterns** — rows where confidence has dropped below 0.30 (the decay duty's floor). They can't drive shortcuts. They're effectively dead but still present.
3. **Near-duplicates the dedup pass missed** — clusters of 3–5 patterns where pairwise distance is just above 0.15 (dedup's threshold), but the centroid is dense. Each is "different" from its neighbour, but together they're noise.

At ~20 active patterns (your POC seed), this is invisible. At 50,000 patterns (your production scale after a year), vector-index latency starts degrading, audit trail size becomes a retention question, and the dead patterns hide useful ones in Tier 5 recall.

### Why it matters at your scale

Three operational metrics move with cluster size:

- **Vector-index lookup latency** — grows roughly sub-linearly with active rows in an HNSW index. At 50k rows it's still in single-digit milliseconds; at 500k it starts to matter for the 50ms context-assembly budget.
- **Audit-trail storage** — superseded patterns are regulatory artefacts. EU GDPR Article 17 (right to erasure) and PCI DSS 3.1 (data minimisation) both apply. You have minimums (audit retention) and maximums (data minimisation).
- **Recall noise** — dead patterns in Tier 5 dilute the top-K returned to the routing layer. Even if they fail the gate, they consume slots that could have surfaced live patterns.

Compaction is the operational discipline that keeps these in check.

### The decisions only your team can make

**Q1. What's the cadence?**

- **Weekly** — aggressive, smaller batches per run, less to review on each pass
- **Monthly** — typical for regulated environments, gives audit teams time to review what's about to be archived
- **Quarterly** — minimum for environments with strict data-retention reviews

*Your decision:* Cadence is driven by your audit team's review capacity and your retention policy.

**Q2. What's the retention horizon for each category?**

| Row category | Common ranges | Our default |
|---|---|---|
| Superseded patterns (still useful for audit) | 90 days – 7 years | 90 days |
| Decayed-below-floor patterns | 30 days – 1 year | 60 days |
| Near-duplicate cluster pruning | event-driven, no time component | only when cluster centroid > 3 patterns |

*Your decision:* What does your retention policy actually require? Your audit team and your compliance team likely have firm answers. We adjust to match.

**Q3. Archive, or hard-delete?**

- **Archive** — move to a `fraud_memory_archive` table (or partition). Still queryable by audit teams; not in the active routing path. Regulatory audit-ready.
- **Hard-delete** — DROP the row. Smaller table, but lost forever. Defensible only for the near-duplicate category, never for superseded or decayed.

*Your decision:* Default archive for everything; hard-delete only if you have a specific data-minimisation requirement.

**Q4. Who signs off on each compaction run?**

- **Automated, no sign-off** — the compaction job runs on cadence, audit trail is written, no human in the loop. Defensible only for archive (not delete).
- **Compaction proposal + approval** — the job writes a proposal to a `compaction_queue` table; an approver clears it before execution. Audit-friendly, slower.
- **Dry-run preview + manual run** — the current `consolidate_fraud_memory()` and `decay_fraud_memory()` shape: preview button shows what would happen, apply button does it.

*Your decision:* Sign-off model. Drives whether compaction is a scheduled job or an operator-triggered process.

### Implementation paths after we've settled the four questions

Three patterns, again from minimal to comprehensive:

- **Manual compaction with dry-run UI** (POC-ready): same shape as decay — preview button, apply button. Operator-triggered. ~30 min of build.
- **Scheduled compaction with approval queue**: cron-driven detection, queue for approval, execution on clear. ~1 day.
- **Full lifecycle with archive table + retention enforcement**: archive partitioned by month, retention policy enforced as SQL constraint, audit-export pipeline. ~3-5 days.

---

## How we'll use this in POC planning

This document drives **two conversations**:

1. **Discovery conversation** (Week 1): we walk through the questions in this document together. Your team's answers determine the spec.
2. **Implementation conversation** (Week 2+): we build to the spec, you review at each milestone.

At the end of POC, the reconciliation and compaction duties don't just exist —
they **fit your operating model**, your audit team's retention horizon, your
analyst's queue capacity, and your risk appetite. That's not something we
could deliver by shipping defaults.

---

## What we're delivering at POC kickoff

Before this conversation begins, we'll have:

- ✅ Write Control + **Reconciliation live and demonstrable** — both governed server-side in the fact layer's `record_fact` (single-mode: auto-supersede-by-authority or dispute). Live demo: [`scenarios/contradiction_demo.py`](scenarios/contradiction_demo.py)
- ✅ Deduplication **retired** in favour of canonical subject keying (contradictions/paraphrases collide on one governed fact identity)
- ✅ Single-query RCA over the append-only, hash-chained `fact_event` audit log ([`sql/rca_lineage.sql`](sql/rca_lineage.sql)) — full lineage from first assertion through every supersede/dispute to current truth
- ✅ The cognitive foundation lifecycle running end-to-end (assemble → route → loop → summary) at the scale of your seed data, with Tier 5 recall served by the governed fact layer

The **customer-shaped portion of the work** that remains: the **human-in-loop
reconciliation queue** and **evidence-weighted mode** (on top of the live
single-mode default), **Compaction**, and a **confidence-decay analog** for the
governed fact model. We build those with you, not for you.

---

## References

- [REDUNDANT_CODE_AUDIT.md](./REDUNDANT_CODE_AUDIT.md) — current state of the codebase
- [directives/tidb_agent_demo.md](./directives/tidb_agent_demo.md) — demo flow + business value mapping
- [VOCABULARY.md](./VOCABULARY.md) — canonical terminology
- [README.md](./README.md) — repo overview and architecture
