"""liability_concentration — typed, tenant-scoped betting read tool. No caller SQL.

Returns the events carrying the most concentrated book liability for the caller's
tenant: per (event, selection), the total staked, the total potential payout
(the book's exposure if that selection wins), and the bet count — ordered by
exposure. A sportsbook uses this to spot one-sided books before an event settles.

DOMAIN read tool owned by this (caller) repo, registered as its OWN target on the
fact layer's Gateway — NOT a metric inside query_tenant_metrics (which stays
domain-agnostic). Typed/enumerated: bounded params, never caller SQL.
"""

from __future__ import annotations

from sqlalchemy import text

from .common import context
from .common.db import get_engine

_MAX_TOP_K = 50
_DEFAULT_TOP_K = 10


def handler(event, _lambda_context=None):
    try:
        ctx, args = context.extract(event)
    except (KeyError, PermissionError, ValueError) as e:
        return {"error": str(e), "error_type": type(e).__name__}

    top_k = min(int(args.get("top_k", _DEFAULT_TOP_K)), _MAX_TOP_K)
    min_stake = float(args.get("min_stake", 0))

    engine = get_engine(ctx.tenant_secret_arn)
    with engine.connect() as cx:
        rows = cx.execute(
            text(
                "SELECT b.event_id, b.selection, "
                "       e.home_team, e.away_team, e.sport, "
                "       COUNT(*) AS bet_count, "
                "       ROUND(SUM(b.stake), 2) AS total_stake, "
                "       ROUND(SUM(b.potential_payout), 2) AS total_exposure "
                "FROM bets b "
                "LEFT JOIN betting_events e "
                "       ON e.event_id = b.event_id AND e.tenant_id = b.tenant_id "
                "WHERE b.tenant_id = :t AND b.status = 'accepted' "
                "GROUP BY b.event_id, b.selection, e.home_team, e.away_team, e.sport "
                "HAVING SUM(b.stake) >= :min_stake "
                "ORDER BY total_exposure DESC "
                "LIMIT :k"
            ),
            {"t": ctx.tenant_id, "min_stake": min_stake, "k": top_k},
        ).mappings().all()

    results = [
        {
            "event_id": int(r["event_id"]) if r["event_id"] is not None else None,
            "selection": r["selection"],
            "fixture": (f"{r['home_team']} vs {r['away_team']}"
                        if r["home_team"] else None),
            "sport": r["sport"],
            "bet_count": int(r["bet_count"]),
            "total_stake": float(r["total_stake"] or 0),
            "total_exposure": float(r["total_exposure"] or 0),
        }
        for r in rows
    ]
    return {
        "tenant_id": ctx.tenant_id,
        "top_k": top_k,
        "count": len(results),
        "results": results,
    }


def lambda_handler(event, context_):  # noqa: N802 (AWS naming)
    return handler(event, context_)
