"""
Offline tests for workflow/episodic tenant isolation.

These prove the fix for the pre-init tenant-scoping gap: the inherited
agent_sessions / agent_reasoning workflow state is now unambiguously scoped by
tenant_id (application DATA SCOPE — not principal identity), so identical entity
ids across tenants can no longer surface one tenant's investigation to another.

No TiDB / AWS connection: the SQL read paths are exercised against an in-memory
SQLite mirror of the two tables (a thin cursor shim translates the MySQL `%s`
placeholders and returns dict rows, matching mysql.connector's
`cursor(dictionary=True)`), and the write/validation paths are exercised with a
capturing fake connection. The SQL under test is the REAL query text from
adapters.fraud / adapters.betting.tier_4_prior and agent_tools.create_session.

Run:  python -m unittest tests.test_workflow_tenant_scope -v
"""

import os
import sqlite3
import unittest

# tenancy.resolve_tenant_id refuses to run un-scoped; give the process a default
# so importing/using modules that resolve tenants never blocks on env.
os.environ.setdefault("DEMO_TENANT_ID", "demo-bank-alpha")

import agent_tools
from adapters import fraud as fraud_adapter
from adapters import betting as betting_adapter

TENANT_A = "demo-bank-alpha"
TENANT_B = "demo-bank-beta"
SHARED_ENTITY = "4"  # the same customer_id value exists under BOTH tenants


# ---------------------------------------------------------------------------
# In-memory SQLite mirror + a cursor shim that speaks the app's dialect
# ---------------------------------------------------------------------------
class _DictCursor:
    """Wrap a sqlite3 cursor so it accepts MySQL `%s` params and yields dict rows.

    Only the surface the code under test uses (execute / fetchall / fetchone) is
    implemented. `%s` -> `?` is a literal swap; the test SQL contains no literal
    percent signs, so this is safe here.
    """

    def __init__(self, sqlite_cursor):
        self._cur = sqlite_cursor
        self.execute_calls = []

    def execute(self, sql, params=()):
        self.execute_calls.append((sql, params))
        self._cur.execute(sql.replace("%s", "?"), params)
        return self

    def fetchall(self):
        return [dict(r) for r in self._cur.fetchall()]

    def fetchone(self):
        row = self._cur.fetchone()
        return dict(row) if row is not None else None


def _seed_two_tenants():
    """Build the SQLite mirror with the SAME entity id under both tenants.

    Returns a _DictCursor over a connection holding:
      agent_sessions:  one session per tenant, both with user_id = SHARED_ENTITY
      agent_reasoning: one prior investigation per session, distinguishable text
    """
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute(
        """CREATE TABLE agent_sessions (
               session_id TEXT PRIMARY KEY,
               tenant_id  TEXT NOT NULL,
               user_id    TEXT,
               created_at TEXT DEFAULT CURRENT_TIMESTAMP
           )"""
    )
    cur.execute(
        """CREATE TABLE agent_reasoning (
               reasoning_id INTEGER PRIMARY KEY AUTOINCREMENT,
               session_id   TEXT,
               observation  TEXT,
               hypothesis   TEXT,
               evidence_refs TEXT,
               confidence   REAL,
               resolution   TEXT,
               created_at   TEXT DEFAULT CURRENT_TIMESTAMP
           )"""
    )
    # Tenant A: prior investigation on customer 4
    cur.execute(
        "INSERT INTO agent_sessions (session_id, tenant_id, user_id) VALUES (?,?,?)",
        ("sess-A", TENANT_A, SHARED_ENTITY),
    )
    cur.execute(
        """INSERT INTO agent_reasoning
             (session_id, observation, hypothesis, confidence, resolution)
           VALUES (?,?,?,?,?)""",
        ("sess-A", "A observed velocity burst", "A_TENANT_FRAUD", 0.91, "flagged"),
    )
    # Tenant B: DIFFERENT prior investigation on the SAME entity id (customer 4)
    cur.execute(
        "INSERT INTO agent_sessions (session_id, tenant_id, user_id) VALUES (?,?,?)",
        ("sess-B", TENANT_B, SHARED_ENTITY),
    )
    cur.execute(
        """INSERT INTO agent_reasoning
             (session_id, observation, hypothesis, confidence, resolution)
           VALUES (?,?,?,?,?)""",
        ("sess-B", "B observed nothing", "B_TENANT_CLEAN", 0.20, "cleared"),
    )
    conn.commit()
    return _DictCursor(conn.cursor())


# ---------------------------------------------------------------------------
# Capturing fake connection for write-path tests (no DB)
# ---------------------------------------------------------------------------
class _CapturingCursor:
    def __init__(self):
        self.calls = []
        self.lastrowid = 1

    def execute(self, sql, params=()):
        self.calls.append((sql, params))

    def fetchone(self):
        return None

    def fetchall(self):
        return []


class _CapturingConn:
    def __init__(self):
        self.cur = _CapturingCursor()
        self.committed = False

    def cursor(self, *a, **k):
        return self.cur

    def commit(self):
        self.committed = True

    def is_connected(self):
        return True

    def close(self):
        pass


class TierFourTenantIsolation(unittest.TestCase):
    """Requirement 1 & 2: same entity id, different tenants, no crossover."""

    def _rows_text(self, lines):
        return " || ".join(lines)

    def test_tenant_A_retrieves_its_own_prior_investigation(self):
        cur = _seed_two_tenants()
        lines, status = fraud_adapter.tier_4_prior(cur, SHARED_ENTITY, tenant_id=TENANT_A)
        self.assertEqual(status, "ok")
        blob = self._rows_text(lines)
        self.assertIn("A_TENANT_FRAUD", blob)
        self.assertNotIn("B_TENANT_CLEAN", blob)

    def test_tenant_B_cannot_see_tenant_A_investigation_for_same_entity(self):
        cur = _seed_two_tenants()
        lines, status = fraud_adapter.tier_4_prior(cur, SHARED_ENTITY, tenant_id=TENANT_B)
        self.assertEqual(status, "ok")
        blob = self._rows_text(lines)
        # B sees ONLY its own row; A's fraud verdict must never leak across.
        self.assertIn("B_TENANT_CLEAN", blob)
        self.assertNotIn("A_TENANT_FRAUD", blob)

    def test_third_tenant_with_same_entity_gets_nothing(self):
        cur = _seed_two_tenants()
        lines, status = fraud_adapter.tier_4_prior(cur, SHARED_ENTITY,
                                                   tenant_id="demo-bank-gamma")
        self.assertEqual(status, "no_prior_investigations")
        self.assertEqual(lines, [])

    def test_betting_adapter_has_identical_isolation(self):
        cur = _seed_two_tenants()
        a_lines, a_status = betting_adapter.tier_4_prior(cur, SHARED_ENTITY, tenant_id=TENANT_A)
        self.assertEqual(a_status, "ok")
        self.assertIn("A_TENANT_FRAUD", self._rows_text(a_lines))
        self.assertNotIn("B_TENANT_CLEAN", self._rows_text(a_lines))


class SessionCreationPersistsTenant(unittest.TestCase):
    """Requirement 3: create_session persists the supplied tenant scope."""

    def setUp(self):
        self._orig = agent_tools.get_db_connection
        self.fake = _CapturingConn()
        agent_tools.get_db_connection = lambda: self.fake

    def tearDown(self):
        agent_tools.get_db_connection = self._orig

    def test_tenant_written_into_agent_sessions_insert(self):
        agent_tools.create_session("sess-new", TENANT_B, user_id="4",
                                   metadata={"source": "unit-test"})
        self.assertTrue(self.fake.committed)
        self.assertEqual(len(self.fake.cur.calls), 1)
        sql, params = self.fake.cur.calls[0]
        self.assertIn("tenant_id", sql)
        self.assertIn("agent_sessions", sql)
        # params order: (session_id, tenant_id, user_id, metadata_json)
        self.assertEqual(params[0], "sess-new")
        self.assertEqual(params[1], TENANT_B)
        self.assertEqual(params[2], "4")


class FailClosedOnMissingTenant(unittest.TestCase):
    """Requirement 4: missing/empty tenant fails closed on create AND on read."""

    def test_create_session_rejects_empty_tenant_before_touching_db(self):
        # get_db_connection must never be reached — prove it by making it explode.
        orig = agent_tools.get_db_connection

        def _boom():
            raise AssertionError("get_db_connection reached with an un-scoped session")

        agent_tools.get_db_connection = _boom
        try:
            for bad in ("", None, "   "):
                with self.assertRaises(ValueError):
                    agent_tools.create_session("sess-x", bad, user_id="4")
        finally:
            agent_tools.get_db_connection = orig

    def test_tier_4_read_fails_closed_without_running_sql(self):
        cur = _seed_two_tenants()
        for bad in (None, ""):
            lines, status = fraud_adapter.tier_4_prior(cur, SHARED_ENTITY, tenant_id=bad)
            self.assertEqual(status, "degraded_no_tenant")
            self.assertEqual(lines, [])
        # No SQL was executed for the un-scoped reads.
        self.assertEqual(cur.execute_calls, [])

    def test_slim_summary_refuses_without_tenant(self):
        from cognitive_loop import _slim_summary
        result = _slim_summary("sess-A", client=None, tenant_id=None)
        self.assertIsNone(result["report"])
        self.assertIn("tenant", result["error"].lower())


if __name__ == "__main__":
    unittest.main(verbosity=2)
