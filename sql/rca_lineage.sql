-- ============================================================================
-- Single-query RCA — full lineage of one governed fact (ARCHITECTURE.md Thesis 10)
-- ============================================================================
--
-- Reconstructs the complete history of a fact from its first assertion through
-- every supersede/dispute to its current state, in ONE query against the fact
-- layer's append-only, hash-chained fact_event log — the "single-query root
-- cause analysis" Thesis 10 named as still-missing.
--
-- Run this against the FACT LAYER's TiDB (the governed store), not this repo's
-- transactional TiDB. This is the operator/auditor view: the caller reaches the
-- fact layer only through the Gateway tools and cannot read fact_event directly;
-- that separation is the point (typed reads only over MCP; raw audit access is
-- an operator privilege).
--
-- Parameters:
--   :tenant_id  — the tenant claim (e.g. 'demo-bank-alpha')
--   :subject    — the governed subject key (e.g. 'fraud:customer:clayton-knight-ab12cd34')
--   :predicate  — the predicate (this port maps verdicts to 'status')
--
-- Filtered by tenant_id + subject_id, ordered by observed_at: first assertion
-- first, current truth last. The prior (superseded/disputed) claims remain
-- visible — that retention IS the audit trail.
--
-- Note on identity: the fact layer keys facts by (tenant_id, subject, predicate)
-- in fact_subject, and fact_event carries subject_id. This query resolves the
-- human-readable subject string to its subject_id via fact_subject, so you can
-- ask for lineage by the same subject the caller wrote. To query by subject_id
-- directly, drop the fact_subject join and filter `e.subject_id = :subject_id`.

SELECT
    e.observed_at,                                   -- when this claim was recorded (microsecond)
    e.op,                                            -- assert | supersede | retract | correct
    e.value,                                         -- what THIS event asserted (JSON)
    e.source,                                        -- provenance (ranked by source_rank)
    e.confidence,
    e.agent_id,
    e.session_id,
    e.event_id,
    e.supersedes_event_id,                           -- which prior event this one superseded (if any)
    CASE
        WHEN e.event_id = fc.winning_event_id THEN CONCAT('CURRENT (', fc.status, ')')
        WHEN e.supersedes_event_id IS NOT NULL      THEN 'SUPERSEDED-BY-LATER'
        ELSE 'HISTORICAL'
    END                                        AS lineage_state,
    fc.status                                  AS current_subject_status,  -- active|superseded|disputed|retracted
    e.prev_hash,                                     -- per-subject hash chain (tamper-evident)
    e.event_hash
FROM fact_event   e
JOIN fact_subject s
      ON s.subject_id = e.subject_id
     AND s.tenant_id  = e.tenant_id
LEFT JOIN fact_current fc
      ON fc.subject_id = e.subject_id
WHERE e.tenant_id  = :tenant_id
  AND s.subject    = :subject
  AND s.predicate  = :predicate
ORDER BY e.observed_at ASC, e.event_id ASC;

-- Reading the result:
--   * The row(s) with op='assert' at the top are the original claim(s).
--   * A row with op='supersede' whose supersedes_event_id points at an earlier
--     event_id is the authority-driven reconciliation; the earlier event is
--     still present above it (never deleted) — that is the retained prior claim.
--   * When current_subject_status='disputed', two competing assert rows remain
--     with no supersede between them (equal authority, no recency tiebreak).
--   * prev_hash/event_hash chain each event to the previous one for this subject;
--     recomputing sha256 over the canonical fields + prev_hash verifies the chain.
