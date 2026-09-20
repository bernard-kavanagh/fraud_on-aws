"""
TiDB Fact Layer - Cognitive investigation loop.

This is the Stage 4 module: a real tool-use loop where the LLM chooses
which tool to call next based on what it has seen. Replaces the previous
keyword-routing-in-Python pattern for fraud investigations.

Lifecycle (mirrors AGENT_LIFECYCLE.md §1):
  1. assemble_context()       — pure SQL, zero LLM calls, 5 priority tiers
  2. route_investigation()    — code (not LLM) picks Sonnet/explore or Haiku/shortcut
  3. agent loop               — model picks tools, system prompt cached
  4. slim summary call        — Haiku reads structured checkpoint, builds report
"""

import os
import json
import anthropic

from agent_tools import (
    assemble_context,
    route_investigation,
    vector_search,
    recall_similar_fraud,
    flag_order,
    write_reasoning_checkpoint,
    compound_resolution,
    explain_fact,
    MODEL_SUMMARY,
)
from tenancy import resolve_tenant_id, resolve_agent_id


def _default_adapter():
    """Lazy-import the fraud adapter so cognitive_loop has no compile-time
    dependency on any specific adapter package. Mirrors assemble_context's
    default-adapter pattern."""
    from adapters import fraud
    return fraud


# Tool schemas exposed to the model. Each entry mirrors a function in
# agent_tools.py. The descriptions are tuned to make the model pick the right
# one — keep them honest about cost and intent.
# NOTE (task 3): execute_sql is intentionally NOT in this list. Free-form SQL as
# an agent-callable tool is retired and deliberately not replaced — a future,
# separately-sandboxed Code Interpreter path is the home for ad-hoc SQL, not this
# repo. Typed, enumerated reads (transaction velocity, liability concentration)
# live behind the fact layer's Gateway as their own Lambda targets instead.
TOOL_SCHEMAS = [
    {
        "name": "vector_search",
        "description": (
            "Semantic search against sales_knowledge (policies), products (catalog), "
            "or reviews (customer feedback). Use for soft meaning — 'find similar', "
            "'what's our policy on'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "user_query": {"type": "string"},
                "target_table": {
                    "type": "string",
                    "enum": ["sales_knowledge", "products", "reviews"],
                },
            },
            "required": ["user_query", "target_table"],
        },
    },
    {
        "name": "recall_similar_fraud",
        "description": (
            "Semantic recall against the governed fact layer — confirmed verdicts from "
            "prior investigations, tenant-scoped and embedded server-side. Use when you "
            "want broader recall than the context already surfaced. scope/entity_ref are "
            "advisory hints that sharpen the query; the authoritative filter is your "
            "tenant. The top matches are already in your Tier 5 context."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query_text": {"type": "string"},
                "scope": {"type": "string", "enum": ["global", "entity"]},
                "entity_ref": {"type": "string"},
                "k": {"type": "integer", "default": 5},
            },
            "required": ["query_text"],
        },
    },
    {
        "name": "flag_order",
        "description": (
            "Write-back tool: mark an order as flagged in TiDB. Use only after you have "
            "evidence and a clear hypothesis. This is a state-changing action visible to "
            "downstream systems."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "order_id": {"type": "integer"},
                "reason": {"type": "string"},
            },
            "required": ["order_id", "reason"],
        },
    },
    {
        "name": "write_reasoning_checkpoint",
        "description": (
            "Write a structured checkpoint to agent_reasoning. Call this AT LEAST ONCE "
            "near the end of your investigation with your distilled state: observation, "
            "hypothesis, evidence_refs, confidence, resolution. The summary report is "
            "built from this row, not from your conversation — be precise."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "observation": {"type": "string"},
                "hypothesis": {"type": "string"},
                "evidence_refs": {"type": "array", "items": {"type": "string"}},
                "confidence": {"type": "number"},
                "resolution": {"type": "string"},
            },
            "required": ["observation", "hypothesis", "confidence", "resolution"],
        },
    },
    {
        "name": "compound_resolution",
        "description": (
            "Persist a confirmed verdict to the governed fact layer (AgentCore Gateway) "
            "so future agent sessions can recall it via server-side semantic search. "
            "Call this only for high-confidence (≥0.85) confirmed outcomes — write "
            "control is enforced server-side. Set entity_ref to the focal customer_id "
            "or IP for an entity verdict; the fact layer adjudicates contradictions "
            "(higher authority supersedes, equal authority disputes) automatically. "
            "Base confidence strictly on the specific evidence you cited via tool "
            "calls in this investigation (transaction counts, velocity thresholds, "
            "matched patterns, review sentiment, etc.) — not on how persuasive your "
            "written rationale reads. If you'd assign a different number without the "
            "surrounding narrative, use that number."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "content": {"type": "string", "description": "Semantically-banded pattern description / rationale."},
                "confidence": {"type": "number"},
                "scope": {"type": "string", "enum": ["global", "entity"]},
                "entity_ref": {"type": "string", "description": "Focal customer_id or IP for an entity verdict."},
                "verdict": {"type": "string", "description": "Optional canonical verdict label (e.g. 'confirmed', 'cleared'). Same label corroborates; a different label adjudicates by authority."},
                "evidence": {
                    "type": "array",
                    "description": "References back to the evidence you cited (never copies). Each: {evidence_type, evidence_ref}, e.g. {'evidence_type':'transaction','evidence_ref':'TX-123'}.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "evidence_type": {"type": "string"},
                            "evidence_ref": {"type": "string"},
                            "relationship": {"type": "string", "enum": ["SUPPORTS", "CONTRADICTS", "DERIVED_FROM", "TRIGGERED_BY", "REVIEWED_IN"]},
                        },
                        "required": ["evidence_type", "evidence_ref"],
                    },
                },
            },
            "required": ["content", "confidence"],
        },
    },
    {
        "name": "explain_fact",
        "description": (
            "Ask the governed fact layer WHY the current verdict for an entity holds: "
            "resolution, authoritative source + policy version, supporting vs contrary "
            "evidence, assessments, and history. Use to answer 'why do we believe this?' "
            "or to check what is already durably known before re-investigating."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "entity_ref": {"type": "string", "description": "Focal customer_id or IP to explain."},
            },
            "required": ["entity_ref"],
        },
    },
]


SYSTEM_PROMPT_TEMPLATE = """You are the TiDB fraud investigation agent.

You operate on the cognitive foundation: every tool call hits a single TiDB cluster
that holds live transactions (data plane) and learned fraud patterns (context plane)
in one transaction boundary.

Your context has been pre-assembled from five priority-ordered tiers before you saw
this prompt. Trust it. The platform decided what you see; you decide what to do.

DATA PLANE SCHEMA:
{schema_hint}

ASSEMBLED CONTEXT:
{context_block}

ROUTING: this investigation was routed to the {path} path ({model}, max {max_rounds} tool rounds).
{routing_reason}

Your job:
1. Investigate the trigger using tools when needed. Don't re-fetch what the context
   already gave you.
2. Before finishing, ALWAYS call write_reasoning_checkpoint with your distilled
   observation, hypothesis, evidence_refs (list of order_ids / IPs / fact subjects you
   cited), confidence (0-1), and resolution.
3. If you are confident (≥0.85) that you have identified a confirmed, reusable
   verdict, call compound_resolution to persist it to the governed fact layer
   (adjudicated + audited server-side).
4. If write-back is warranted, call flag_order with a clear reason.

End your turn when the investigation is complete."""


def _dispatch_tool(name: str, args: dict, session_id: str,
                   tenant_id: str, agent_id: str, domain: str) -> str:
    """Route a tool call from the model to the corresponding agent_tools function.

    tenant_id / agent_id / session_id / domain are threaded in from
    run_investigation (task 9) so every tenant-scoped read/write and every
    governed-fact write carries the caller's identity. execute_sql is gone
    (task 3)."""
    if name == "vector_search":
        return vector_search(args["user_query"], args.get("target_table", "sales_knowledge"),
                             tenant_id=tenant_id)
    if name == "recall_similar_fraud":
        return recall_similar_fraud(
            args["query_text"],
            scope=args.get("scope"),
            entity_ref=args.get("entity_ref"),
            k=args.get("k", 5),
            tenant_id=tenant_id,
        )
    if name == "flag_order":
        return flag_order(args["order_id"], args["reason"], tenant_id=tenant_id)
    if name == "write_reasoning_checkpoint":
        return write_reasoning_checkpoint(
            session_id,
            observation=args["observation"],
            hypothesis=args["hypothesis"],
            evidence_refs=args.get("evidence_refs", []),
            confidence=args["confidence"],
            resolution=args["resolution"],
        )
    if name == "compound_resolution":
        return compound_resolution(
            args["content"],
            confidence=args["confidence"],
            scope=args.get("scope", "global"),
            entity_ref=args.get("entity_ref"),
            verdict=args.get("verdict"),
            evidence=args.get("evidence"),
            tenant_id=tenant_id,
            agent_id=agent_id,
            session_id=session_id,
            domain=domain,
        )
    if name == "explain_fact":
        return explain_fact(args["entity_ref"], domain=domain, tenant_id=tenant_id)
    return f"❌ Unknown tool: {name}"


def run_investigation(trigger_text: str, session_id: str,
                      entity_ref: str = None, on_event=None,
                      adapter=None, tenant_id: str = None, agent_id: str = None):
    """
    Run one full cognitive-foundation investigation.

    Args:
        trigger_text: natural-language description of what triggered the investigation.
        session_id:   active agent_sessions row.
        entity_ref:   the focal customer_id or ip_address. Optional — assemble_context
                      degrades gracefully when missing.
        on_event:     optional callback fn(event_type: str, payload: dict) for UI
                      surfacing. Events: 'assembled', 'routed', 'tool_call',
                      'tool_result', 'loop_end', 'summary'.
        adapter:      domain adapter module (e.g. `adapters.fraud` or
                      `adapters.betting`). Defaults to fraud when None — same
                      substrate, swap the plugin. Thesis 11.
        tenant_id:    tenant scope for every tenant-scoped read and every governed
                      fact write. Resolved from arg or $DEMO_TENANT_ID; never
                      hardcoded (task 9).
        agent_id:     acting agent identity for fact provenance (task 9).

    Returns dict with:
        assembled, routing, tool_trace, summary, reasoning_checkpoint_id
    """
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        return {"error": "ANTHROPIC_API_KEY not set"}

    tenant_id = resolve_tenant_id(tenant_id)
    agent_id = resolve_agent_id(agent_id)
    # Domain drives the fact subject/predicate namespace (fraud vs betting).
    resolved_adapter_name = getattr(adapter, "__name__", "") if adapter else ""
    domain = "betting" if "betting" in resolved_adapter_name else "fraud"

    client = anthropic.Anthropic(api_key=api_key)

    # ----- STAGE 1: assemble_context (with adapter plugin, tenant-scoped) -----
    assembled = assemble_context(
        entity_ref=entity_ref,
        session_id=session_id,
        trigger_text=trigger_text,
        adapter=adapter,
        tenant_id=tenant_id,
        domain=domain,
    )
    if on_event:
        on_event("assembled", assembled)

    # ----- STAGE 2: routing decision -----
    # Pass the full vector_matches list so the canonical "scan ANY" pattern
    # can shortcut on a higher-similarity match even if it's not top_match.
    #
    # Gate precedence: env var (explicit operator override) wins over adapter
    # default. If neither is set, route_investigation falls back to the module
    # default constants from agent_tools.py.
    resolved_adapter = adapter or _default_adapter()
    cg_override = float(os.environ["ROUTING_CONFIDENCE_GATE"]) if "ROUTING_CONFIDENCE_GATE" in os.environ else getattr(resolved_adapter, "CONFIDENCE_GATE", None)
    sg_override = float(os.environ["ROUTING_SIMILARITY_GATE"]) if "ROUTING_SIMILARITY_GATE" in os.environ else getattr(resolved_adapter, "SIMILARITY_GATE", None)
    routing = route_investigation(
        assembled["vector_matches"],
        confidence_gate=cg_override,
        similarity_gate=sg_override,
    )
    if on_event:
        on_event("routed", routing)

    # NOTE: pattern reinforcement is retired here. It bumped a local fraud_memory
    # row's last_reinforced_at/evidence_count to protect actively-used patterns
    # from confidence decay. Tier 5 is now the governed fact layer, whose
    # fact_current has no per-fact confidence field — a decay/reinforcement analog
    # for the fact model is a deferred design pass (explicitly out of scope for
    # this port), so there is nothing to reinforce here.

    # ----- STAGE 3: cached system prompt + tool-use loop -----
    # SCHEMA_HINT is adapter-owned (F2). Falls back to a generic "no hint
    # provided" string so adapters without a hint constant don't break.
    schema_hint = getattr(resolved_adapter, "SCHEMA_HINT",
                           "(no schema hint provided — use DESCRIBE if needed)")
    system_prompt = SYSTEM_PROMPT_TEMPLATE.format(
        schema_hint=schema_hint,
        context_block=assembled["system_context"] or "(no context available — cold start)",
        path=routing["path"],
        model=routing["model"],
        max_rounds=routing["max_tool_rounds"],
        routing_reason=routing["reason"],
    )

    messages = [{"role": "user", "content": trigger_text}]
    tool_trace = []

    for iteration in range(routing["max_tool_rounds"]):
        response = client.messages.create(
            model=routing["model"],
            max_tokens=2048,
            system=[{
                "type": "text",
                "text": system_prompt,
                "cache_control": {"type": "ephemeral"},
            }],
            tools=TOOL_SCHEMAS,
            messages=messages,
        )

        # Collect assistant content blocks (text + tool_use)
        assistant_blocks = []
        tool_uses = []
        for block in response.content:
            if block.type == "text":
                assistant_blocks.append({"type": "text", "text": block.text})
            elif block.type == "tool_use":
                assistant_blocks.append({
                    "type": "tool_use",
                    "id": block.id,
                    "name": block.name,
                    "input": block.input,
                })
                tool_uses.append(block)

        messages.append({"role": "assistant", "content": assistant_blocks})

        if response.stop_reason == "end_turn" or not tool_uses:
            break

        # Execute tool calls and feed results back
        tool_results = []
        for tu in tool_uses:
            if on_event:
                on_event("tool_call", {"name": tu.name, "input": tu.input})
            result = _dispatch_tool(tu.name, tu.input, session_id,
                                    tenant_id, agent_id, domain)
            tool_trace.append({"tool": tu.name, "input": tu.input, "result": result})
            if on_event:
                on_event("tool_result", {"name": tu.name, "result": result})
            tool_results.append({
                "type": "tool_result",
                "tool_use_id": tu.id,
                "content": str(result),
            })

        messages.append({"role": "user", "content": tool_results})

    if on_event:
        on_event("loop_end", {"iterations": iteration + 1})

    # ----- STAGE 4: slim summary call (read structured checkpoint, NOT loop replay) -----
    # Fallback: if the model never called write_reasoning_checkpoint, synthesise
    # one from tool_trace so the demo never shows an empty report mid-presentation.
    summary = _slim_summary(session_id, client, tenant_id)
    if summary.get("error") and "no reasoning checkpoint" in (summary.get("error") or "") and tool_trace:
        _synthesise_fallback_checkpoint(session_id, trigger_text, tool_trace, routing)
        summary = _slim_summary(session_id, client, tenant_id)
        summary["fallback_synthesised"] = True
    if on_event:
        on_event("summary", summary)

    return {
        "assembled": assembled,
        "routing": routing,
        "tool_trace": tool_trace,
        "summary": summary,
    }


def _synthesise_fallback_checkpoint(session_id: str, trigger_text: str,
                                    tool_trace: list, routing: dict):
    """
    Recover when the model ends the loop without calling write_reasoning_checkpoint.

    Builds a best-effort checkpoint from what the agent actually did (tool_trace)
    so the slim summary call has structured input to work from. The
    confidence is conservative — this is a recovered checkpoint, not a model
    judgment, so it stays well below the routing gate to prevent future
    investigations from shortcutting on a recovered diagnosis.
    """
    observation = f"Triggered by: {trigger_text[:200]}"
    tools_called = [step.get("tool") for step in tool_trace]
    hypothesis = (
        f"Agent loop ran via {routing.get('path')} path on {routing.get('model')} "
        f"and called {len(tool_trace)} tool(s): {', '.join(tools_called[:6])}. "
        f"No structured checkpoint was written by the agent."
    )
    evidence_refs = [
        f"{step.get('tool')}:{str(step.get('input'))[:100]}"
        for step in tool_trace[:6]
    ]
    resolution = (
        "Fallback synthesis — agent did not commit a checkpoint. "
        "Treat the report as a tool-trace narration, not a confirmed diagnosis."
    )

    write_reasoning_checkpoint(
        session_id=session_id,
        observation=observation,
        hypothesis=hypothesis,
        evidence_refs=evidence_refs,
        confidence=0.50,  # below routing gate — fallback is never load-bearing
        resolution=resolution,
    )


def _slim_summary(session_id: str, client, tenant_id: str) -> dict:
    """
    Read the latest agent_reasoning checkpoint for this session and build a
    focused 500-1500 token prompt for Haiku. The summary is generated from
    structured fields, NOT from replaying the loop conversation.

    This is the Stage 5 pattern from AGENT_LIFECYCLE.md §2 — eliminates ~37%
    of input tokens per investigation and removes the empty-report failure
    mode that came from sending the full loop messages array.

    tenant_id is the investigation's workflow scope, threaded in from
    run_investigation. session_id is a unique UUID bound to one tenant, so the
    agent_sessions join + tenant_id filter is defense-in-depth that keeps this
    reasoning read unambiguously tenant-scoped. Fails closed on a missing tenant.
    """
    from agent_tools import get_db_connection
    from mysql.connector import Error

    if not tenant_id:
        return {"error": "summary read refused: missing tenant scope", "report": None}

    conn = None
    checkpoint = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)
        cursor.execute(
            """SELECT ar.reasoning_id, ar.observation, ar.hypothesis,
                      ar.evidence_refs, ar.confidence, ar.resolution
               FROM agent_reasoning ar
               JOIN agent_sessions s ON s.session_id = ar.session_id
               WHERE ar.session_id = %s AND s.tenant_id = %s
               ORDER BY ar.created_at DESC LIMIT 1""",
            (session_id, str(tenant_id)),
        )
        checkpoint = cursor.fetchone()
    except Error as e:
        return {"error": f"summary read failed: {e}", "report": None}
    finally:
        if conn and conn.is_connected():
            conn.close()

    if not checkpoint:
        return {
            "error": "no reasoning checkpoint written — agent did not call write_reasoning_checkpoint",
            "report": None,
        }

    prompt = f"""Write a 3-paragraph fraud investigation report from this structured checkpoint.
Do not invent details. Use only what is in the checkpoint.

OBSERVATION: {checkpoint['observation']}

HYPOTHESIS: {checkpoint['hypothesis']}

EVIDENCE: {checkpoint['evidence_refs']}

CONFIDENCE: {checkpoint['confidence']}

RESOLUTION: {checkpoint['resolution']}

Format:
1. What was observed (1 paragraph)
2. Hypothesis + evidence chain (1 paragraph)
3. Resolution + confidence + recommended next step (1 paragraph)"""

    message = client.messages.create(
        model=MODEL_SUMMARY,
        max_tokens=600,
        messages=[{"role": "user", "content": prompt}],
    )
    report_text = "".join(b.text for b in message.content if b.type == "text")

    return {
        "reasoning_id": checkpoint["reasoning_id"],
        "confidence": float(checkpoint["confidence"]) if checkpoint["confidence"] else None,
        "report": report_text,
    }
