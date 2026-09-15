"""SQLAlchemy engine for the domain read tools, from a Secrets Manager ARN.

These tools query THIS repo's demo transactional TiDB (orders / bets), which is a
DIFFERENT cluster/database from the fact layer's governed store. The connection
secret is resolved by ARN at runtime (never env-var secret material), mirroring
the fact layer's own db.py idiom.

Secret JSON keys: host, port, username, password, database, [ssl_ca].
"""

from __future__ import annotations

import json
import os
from functools import lru_cache

import boto3
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine


def _resolve_secret(secret_arn: str) -> dict:
    sm = boto3.client("secretsmanager", region_name=os.environ.get("AWS_REGION"))
    raw = sm.get_secret_value(SecretId=secret_arn)["SecretString"]
    return json.loads(raw)


@lru_cache(maxsize=8)
def get_engine(secret_arn: str) -> Engine:
    """Cached engine per secret ARN (per-tenant credential swap safe)."""
    s = _resolve_secret(secret_arn)
    host = s["host"]
    port = int(s.get("port", 4000))
    user = s["username"]
    password = s["password"]
    database = s["database"]
    url = f"mysql+pymysql://{user}:{password}@{host}:{port}/{database}"
    connect_args = {}
    if s.get("ssl_ca"):
        connect_args["ssl"] = {"ca": s["ssl_ca"]}
    # pool_pre_ping guards against stale connections between Lambda invocations.
    return create_engine(url, pool_pre_ping=True, pool_recycle=280,
                         connect_args=connect_args)
