"""Focused tests for the corrected domain-target context contract.

Self-contained (stdlib only; no pytest framework required):

    python3 fact_layer_targets/tests/test_context.py

Verifies the real AgentCore MCP target contract for the domain read tools:
tenant_id is read from the tool argument (requested data scope), missing/empty
fails closed, a legacy `_fact_ctx` cannot override the argument, the domain DB
credential comes from DOMAIN_READS_SECRET_ARN, and both domain handlers still
scope their SQL by the tenant argument.
"""

from __future__ import annotations

import os
import re
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_HANDLERS = os.path.join(_HERE, "..", "handlers")
sys.path.insert(0, _HANDLERS)

from common import context  # noqa: E402  (handlers/common/context.py)

_ARN = "arn:aws:secretsmanager:eu-central-1:111122223333:secret:domain-reads-XyZ012"


def _with_secret(value):
    prev = os.environ.get("DOMAIN_READS_SECRET_ARN")
    if value is None:
        os.environ.pop("DOMAIN_READS_SECRET_ARN", None)
    else:
        os.environ["DOMAIN_READS_SECRET_ARN"] = value
    return prev


def test_valid_event_resolves_tenant_scope():
    prev = _with_secret(_ARN)
    try:
        ctx, args = context.extract({"tenant_id": "demo-bank-alpha", "window_hours": 24})
        assert ctx.tenant_id == "demo-bank-alpha"
        assert ctx.tenant_secret_arn == _ARN
        assert args["window_hours"] == 24
    finally:
        _with_secret(prev)


def test_missing_tenant_fails_closed():
    prev = _with_secret(_ARN)
    try:
        for ev in ({"window_hours": 1}, {}):
            try:
                context.extract(ev)
            except PermissionError:
                pass
            else:
                raise AssertionError("missing tenant_id must fail closed")
    finally:
        _with_secret(prev)


def test_empty_or_malformed_tenant_fails_closed():
    prev = _with_secret(_ARN)
    try:
        for bad in ("", "   ", None):
            try:
                context.extract({"tenant_id": bad})
            except PermissionError:
                pass
            else:
                raise AssertionError(f"tenant_id={bad!r} must fail closed")
    finally:
        _with_secret(prev)


def test_legacy_fact_ctx_cannot_override_argument():
    prev = _with_secret(_ARN)
    try:
        ev = {"tenant_id": "demo-bank-alpha",
              "_fact_ctx": {"tenant_id": "demo-bank-EVIL",
                            "tenant_secret_arn": "arn:aws:secretsmanager:x:y:secret:evil"}}
        ctx, args = context.extract(ev)
        assert ctx.tenant_id == "demo-bank-alpha", "tool argument must win over legacy _fact_ctx"
        assert ctx.tenant_secret_arn == _ARN, "credential must come from config, not _fact_ctx"
        assert "_fact_ctx" not in args
    finally:
        _with_secret(prev)


def test_no_domain_secret_fails_closed():
    prev = _with_secret(None)
    try:
        try:
            context.extract({"tenant_id": "demo-bank-alpha"})
        except ValueError as e:
            assert "DOMAIN_READS_SECRET_ARN" in str(e)
        else:
            raise AssertionError("missing DOMAIN_READS_SECRET_ARN must fail closed")
    finally:
        _with_secret(prev)


def test_both_handlers_scope_sql_by_tenant_argument():
    for name in ("transaction_velocity", "liability_concentration"):
        src = open(os.path.join(_HANDLERS, f"{name}.py")).read()
        assert "context.extract(event" in src, f"{name}: must use context.extract"
        assert re.search(r"tenant_id\s*=\s*:t", src), f"{name}: must bind a tenant_id = :t predicate"
        assert re.search(r'"t":\s*ctx\.tenant_id', src), f"{name}: must bind :t to ctx.tenant_id"
        assert "_fact_ctx" not in src, f"{name}: must not reference the retired _fact_ctx channel"


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"PASS  {t.__name__}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"FAIL  {t.__name__}: {e}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)
