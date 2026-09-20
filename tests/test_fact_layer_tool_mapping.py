"""
Offline tests for the logical -> AgentCore Gateway MCP tool-name mapping in
fact_layer_client.

The live Gateway exposes each tool namespaced as "<TargetName>___<tool>" (e.g.
vector_search is really VectorSearch___vector_search). This repo's public API and
internal callers must keep speaking LOGICAL names; the mapping is resolved once,
immediately before session.call_tool(). These tests prove:

  1. _resolve_gateway_tool maps all 9 logical names to the exact Gateway names.
  2. An unknown logical name fails closed (ValueError), never a silent
     pass-through that would reach the Gateway as an unknown tool.
  3. Every public wrapper actually puts the RESOLVED Gateway name on the wire
     (captured at the session.call_tool boundary) while keeping its Python
     signature unchanged.

No AWS / MCP / network: Cognito auth is stubbed and the MCP transport +
ClientSession are replaced with fakes that capture the tool name.

Run:  python -m unittest tests.test_fact_layer_tool_mapping -v
"""

import asyncio
import os
import unittest

# fact_layer_client resolves the Gateway URL from env when _call runs; give the
# process a value so the (faked) call path never blocks on config.
os.environ.setdefault("FACT_LAYER_GATEWAY_URL", "https://gateway.example/mcp")

import fact_layer_client as flc


# The single source of truth from the task: logical -> live Gateway MCP name.
EXPECTED_MAPPING = {
    "explain_fact": "ExplainFact___explain_fact",
    "get_fact_history": "GetFactHistory___get_fact_history",
    "get_fact": "GetFact___get_fact",
    "list_disputes": "ListDisputes___list_disputes",
    "query_tenant_metrics": "QueryTenantMetrics___query_tenant_metrics",
    "record_fact": "RecordFact___record_fact",
    "retract_fact": "RetractFact___retract_fact",
    "search_entities": "SearchEntities___search_entities",
    "vector_search": "VectorSearch___vector_search",
}


# ---------------------------------------------------------------------------
# Fakes for the MCP transport + session (capture the wire tool name)
# ---------------------------------------------------------------------------
class _FakeContentBlock:
    def __init__(self, text):
        self.text = text


class _FakeResult:
    isError = False

    def __init__(self, text):
        self.content = [_FakeContentBlock(text)]


class _FakeSession:
    def __init__(self, captured):
        self._captured = captured

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def initialize(self):
        return None

    async def call_tool(self, tool_name, arguments=None):
        # This is the boundary the mapping must have resolved BEFORE.
        self._captured["tool_name"] = tool_name
        self._captured["arguments"] = arguments
        return _FakeResult('{"ok": true}')


class _FakeTransport:
    """Async CM yielding (read, write, extra) like streamablehttp_client."""

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return (None, None, None)

    async def __aexit__(self, *exc):
        return False


class ResolveMappingTests(unittest.TestCase):
    def test_all_nine_logical_names_resolve_exactly(self):
        for logical, gateway in EXPECTED_MAPPING.items():
            with self.subTest(logical=logical):
                self.assertEqual(flc._resolve_gateway_tool(logical), gateway)

    def test_mapping_table_matches_expected_exactly(self):
        # No extra/missing entries — the table IS the contract with the Gateway.
        self.assertEqual(flc._GATEWAY_TOOL_NAMES, EXPECTED_MAPPING)

    def test_unknown_logical_name_fails_closed(self):
        with self.assertRaises(ValueError) as ctx:
            flc._resolve_gateway_tool("delete_everything")
        self.assertIn("delete_everything", str(ctx.exception))

    def test_acall_tool_unknown_name_raises_before_transport(self):
        # Fail closed even at the async plumbing layer.
        with self.assertRaises(ValueError):
            asyncio.run(
                flc._acall_tool("https://gw/mcp", "tok", "not_a_tool", {})
            )


class PublicWrapperWireNameTests(unittest.TestCase):
    """Each public wrapper must send the RESOLVED Gateway name to call_tool."""

    def setUp(self):
        self._captured = {}
        # Stub Cognito auth and the MCP transport/session.
        self._orig_get_token = flc._get_token
        self._orig_transport = flc.streamablehttp_client
        self._orig_session = flc.ClientSession
        flc._get_token = lambda tenant_id: "fake-token"
        flc.streamablehttp_client = lambda *a, **k: _FakeTransport()
        flc.ClientSession = lambda read, write: _FakeSession(self._captured)

    def tearDown(self):
        flc._get_token = self._orig_get_token
        flc.streamablehttp_client = self._orig_transport
        flc.ClientSession = self._orig_session

    def _call_wrapper(self, logical):
        tenant = "demo-bank-alpha"
        if logical == "record_fact":
            flc.record_fact(tenant_id=tenant, subject="fraud:customer:4",
                            predicate="status", value="confirmed",
                            source="agent_inference", confidence=0.9)
        elif logical == "get_fact":
            flc.get_fact(tenant_id=tenant, subject="fraud:customer:4",
                         predicate="status")
        elif logical == "get_fact_history":
            flc.get_fact_history(tenant_id=tenant, subject="fraud:customer:4",
                                 predicate="status")
        elif logical == "explain_fact":
            flc.explain_fact(tenant_id=tenant, subject="fraud:customer:4",
                             predicate="status")
        elif logical == "list_disputes":
            flc.list_disputes(tenant_id=tenant)
        elif logical == "search_entities":
            flc.search_entities(tenant_id=tenant, query="acme")
        elif logical == "vector_search":
            flc.vector_search(tenant_id=tenant, query_text="velocity burst")
        elif logical == "query_tenant_metrics":
            flc.query_tenant_metrics(tenant_id=tenant, metric="fact_count")
        elif logical == "retract_fact":
            flc.retract_fact(tenant_id=tenant, subject="fraud:customer:4",
                             predicate="status", source="human_investigator")
        else:
            raise AssertionError(f"no wrapper exercise for {logical}")

    def test_each_wrapper_sends_resolved_gateway_name(self):
        for logical, gateway in EXPECTED_MAPPING.items():
            with self.subTest(logical=logical):
                self._captured.clear()
                self._call_wrapper(logical)
                self.assertEqual(self._captured.get("tool_name"), gateway)
                # tenant_id stays in the arguments (semantics unchanged).
                self.assertEqual(
                    self._captured["arguments"].get("tenant_id"),
                    "demo-bank-alpha",
                )


if __name__ == "__main__":
    unittest.main()
