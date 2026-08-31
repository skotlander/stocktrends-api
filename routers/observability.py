from __future__ import annotations

import os

from fastapi import APIRouter, HTTPException, Request
from sqlalchemy import text

from db import get_metering_engine

router = APIRouter(prefix="/observability", tags=["observability"])

_RECENT_REQUESTS_LIMIT = 10

# Environment variable that must be set to enable this endpoint.
# When unset the endpoint responds 403 to every request regardless of headers.
INTERNAL_OBSERVABILITY_SECRET_ENV_VAR = "INTERNAL_OBSERVABILITY_SECRET"

# Header the caller must present with the matching value.
INTERNAL_OBSERVABILITY_SECRET_HEADER = "X-Internal-Secret"


def _require_internal_secret(request: Request) -> None:
    """
    Gate access to internal-only endpoints with a shared secret.

    The secret is read from INTERNAL_OBSERVABILITY_SECRET at request time
    (not module load) so it can be set/cleared by tests without restarting.

    Access model:
    - /v1/observability/ is an internal API-key-bypass prefix in api_key.py →
      customer API-key auth is bypassed entirely for observability paths.
    - This function is the only auth layer.  A valid API key alone is not
      sufficient; the internal secret must be present and correct.
    - When the env var is unset the endpoint is disabled (403).

    Why not customer-scoped:
    - MPP rows in api_request_economics carry customer_id=NULL because
      _apply_agent_pay_context() sets request.state.customer_id=None.
    - There is no safe customer-ownership claim we can verify against the row.
    - Until persistent tenant identity is added to MPP economics rows,
      exposing the endpoint at all requires it to be internal-only.
    """
    secret = os.getenv(INTERNAL_OBSERVABILITY_SECRET_ENV_VAR)
    if not secret:
        raise HTTPException(status_code=403, detail="Observability endpoint is disabled")

    presented = request.headers.get(INTERNAL_OBSERVABILITY_SECRET_HEADER, "")
    if not presented or presented != secret:
        raise HTTPException(status_code=403, detail="Internal access only")


@router.get(
    "/mpp/sessions/{payment_channel_id}",
    summary="MPP session summary (internal/admin only)",
    description=(
        "Internal endpoint — requires X-Internal-Secret header matching "
        "INTERNAL_OBSERVABILITY_SECRET env var. "
        "Returns an aggregated summary of all MPP-rail requests for a payment channel. "
        "Scoped to payment_rail='mpp' only. "
        "MPP rows carry customer_id=NULL; scoping is by payment_channel_id alone. "
        "Returns 404 if no MPP records exist for the given channel. "
        "total_stc_captured and total_billed_usd_captured reflect settled charges only "
        "(payment_status='captured'); total_stc_requested covers all rows."
    ),
)
def get_mpp_session(payment_channel_id: str, request: Request):
    _require_internal_secret(request)

    # MPP rows have customer_id=NULL — do not filter by customer_id.
    params = {"payment_channel_id": payment_channel_id}

    engine = get_metering_engine()
    with engine.begin() as conn:
        summary_row = conn.execute(
            text("""
                SELECT
                    COUNT(*) AS request_count,
                    MAX(session_id) AS session_id,
                    MIN(created_at) AS first_seen_at,
                    MAX(created_at) AS last_seen_at,
                    SUM(COALESCE(stc_cost, 0)) AS total_stc_requested,
                    SUM(
                        CASE WHEN payment_status = 'captured'
                             THEN COALESCE(stc_cost, 0) ELSE 0 END
                    ) AS total_stc_captured,
                    SUM(
                        CASE WHEN payment_status = 'captured'
                             THEN COALESCE(billed_amount_usd, 0) ELSE 0 END
                    ) AS total_billed_usd_captured
                FROM api_request_economics
                WHERE payment_channel_id = :payment_channel_id
                  AND payment_rail = 'mpp'
            """),
            params,
        ).mappings().first()

        if not summary_row or int(summary_row.get("request_count") or 0) == 0:
            raise HTTPException(status_code=404, detail="MPP session not found")

        status_rows = conn.execute(
            text("""
                SELECT
                    payment_status,
                    COUNT(*) AS request_count
                FROM api_request_economics
                WHERE payment_channel_id = :payment_channel_id
                  AND payment_rail = 'mpp'
                GROUP BY payment_status
                ORDER BY request_count DESC, payment_status
            """),
            params,
        ).mappings().all()

        recent_rows = conn.execute(
            text("""
                SELECT
                    request_id,
                    payment_status,
                    payment_reference,
                    stc_cost,
                    billed_amount_usd,
                    pricing_rule_id,
                    created_at
                FROM api_request_economics
                WHERE payment_channel_id = :payment_channel_id
                  AND payment_rail = 'mpp'
                ORDER BY created_at DESC
                LIMIT :limit
            """),
            {**params, "limit": _RECENT_REQUESTS_LIMIT},
        ).mappings().all()

    summary = dict(summary_row)
    return {
        "request_id": getattr(request.state, "request_id", None),
        "payment_channel_id": payment_channel_id,
        "session_id": summary.get("session_id"),
        "request_count": int(summary.get("request_count") or 0),
        "first_seen_at": summary["first_seen_at"].isoformat() if summary.get("first_seen_at") else None,
        "last_seen_at": summary["last_seen_at"].isoformat() if summary.get("last_seen_at") else None,
        "total_stc_requested": float(summary.get("total_stc_requested") or 0),
        "total_stc_captured": float(summary.get("total_stc_captured") or 0),
        "total_billed_usd_captured": float(summary.get("total_billed_usd_captured") or 0),
        "payment_status_breakdown": [
            {
                "payment_status": row.get("payment_status"),
                "request_count": int(row.get("request_count") or 0),
            }
            for row in status_rows
        ],
        "recent_requests": [
            {
                "request_id": row.get("request_id"),
                "payment_status": row.get("payment_status"),
                "payment_reference": row.get("payment_reference"),
                "stc_cost": float(row["stc_cost"]) if row.get("stc_cost") is not None else None,
                "billed_amount_usd": float(row["billed_amount_usd"]) if row.get("billed_amount_usd") is not None else None,
                "pricing_rule_id": row.get("pricing_rule_id"),
                "created_at": row.get("created_at").isoformat() if row.get("created_at") else None,
            }
            for row in recent_rows
        ],
    }
