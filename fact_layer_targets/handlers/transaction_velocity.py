"""transaction_velocity — typed, tenant-scoped fraud read tool. No caller SQL.

Returns IP addresses exhibiting order-velocity bursts within a bounded time
window for the caller's tenant: IPs with >= min_orders orders in the last
window_hours, with per-IP order count, distinct customers, and value totals.

This is a DOMAIN read tool that lives in this (caller) repo and is registered as
its OWN target on the fact layer's Gateway — it is deliberately NOT a metric
inside query_tenant_metrics (which stays domain-agnostic). Reads are typed and
enumerated: the caller supplies bounded parameters, never SQL.

Tenant filtering scopes the data to the requested tenant_id (taken from the tool
argument). Authorization that the caller may select that scope is the Gateway
governance layer's job (reference profile: Cedar tenant-equality); the WHERE
clause here is data scoping, not authorization.
"""

from __future__ import annotations

from sqlalchemy import text

from .common import context
from .common.db import get_engine

_MAX_WINDOW_HOURS = 168        # cap look-back at 7 days
_DEFAULT_WINDOW_HOURS = 24
_MIN_ORDERS_FLOOR = 2
_DEFAULT_MIN_ORDERS = 3
_MAX_ROWS = 50


def handler(event, _lambda_context=None):
    try:
        ctx, args = context.extract(event)
    except (KeyError, PermissionError, ValueError) as e:
        return {"error": str(e), "error_type": type(e).__name__}

    window_hours = min(int(args.get("window_hours", _DEFAULT_WINDOW_HOURS)), _MAX_WINDOW_HOURS)
    min_orders = max(int(args.get("min_orders", _DEFAULT_MIN_ORDERS)), _MIN_ORDERS_FLOOR)

    engine = get_engine(ctx.tenant_secret_arn)
    with engine.connect() as cx:
        rows = cx.execute(
            text(
                "SELECT ip_address, "
                "       COUNT(*) AS order_count, "
                "       COUNT(DISTINCT customer_id) AS distinct_customers, "
                "       ROUND(SUM(amount), 2) AS total_amount, "
                "       ROUND(MAX(amount), 2) AS max_amount "
                "FROM orders "
                "WHERE tenant_id = :t "
                "  AND order_date >= NOW() - INTERVAL :w HOUR "
                "GROUP BY ip_address "
                "HAVING COUNT(*) >= :m "
                "ORDER BY order_count DESC, total_amount DESC "
                "LIMIT :k"
            ),
            {"t": ctx.tenant_id, "w": window_hours, "m": min_orders, "k": _MAX_ROWS},
        ).mappings().all()

    results = [
        {
            "ip_address": r["ip_address"],
            "order_count": int(r["order_count"]),
            "distinct_customers": int(r["distinct_customers"]),
            "total_amount": float(r["total_amount"] or 0),
            "max_amount": float(r["max_amount"] or 0),
        }
        for r in rows
    ]
    return {
        "tenant_id": ctx.tenant_id,
        "window_hours": window_hours,
        "min_orders": min_orders,
        "count": len(results),
        "results": results,
    }


def lambda_handler(event, context_):  # noqa: N802 (AWS naming)
    return handler(event, context_)
