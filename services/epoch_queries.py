"""Read-only queries for the frozen Market Epoch v1 serving contract."""

from __future__ import annotations

from datetime import date
from typing import Any

from sqlalchemy import text


EPOCH_V1_MODEL_VERSION = "epoch_v1_2026-09-25"

_EPOCH_COLUMNS = """
    weekdate,
    model_version,
    model_payload_sha256,
    classifier_code_sha,
    raw_cluster_id,
    epoch_id,
    epoch_name,
    assigned_distance,
    second_nearest_distance,
    separation_margin,
    bullish_ratio,
    avg_mt_cnt_bull,
    avg_mt_cnt_bear,
    avg_trend_cnt,
    pct_trend_cnt_ge_4,
    rsi_median,
    classified_count,
    rsi_valid_count,
    previous_raw_cluster_id,
    previous_epoch_id,
    weeks_in_epoch,
    changed_this_week,
    classified_at
"""


def fetch_latest_epoch_row(conn: Any) -> Any | None:
    """Return the newest persisted snapshot for the frozen Epoch v1 model."""
    return conn.execute(
        text(
            f"""
            SELECT {_EPOCH_COLUMNS}
            FROM st_market_epoch
            WHERE model_version = :model_version
            ORDER BY weekdate DESC
            LIMIT 1
            """
        ),
        {"model_version": EPOCH_V1_MODEL_VERSION},
    ).mappings().first()


def fetch_epoch_history_rows(
    conn: Any,
    *,
    limit: int,
    start_date: date | None = None,
    end_date: date | None = None,
) -> list[Any]:
    """Return newest-first persisted Epoch v1 snapshots with inclusive bounds."""
    params: dict[str, Any] = {
        "model_version": EPOCH_V1_MODEL_VERSION,
        "limit": int(limit),
    }
    clauses = ["model_version = :model_version"]
    if start_date is not None:
        clauses.append("weekdate >= :start_date")
        params["start_date"] = start_date
    if end_date is not None:
        clauses.append("weekdate <= :end_date")
        params["end_date"] = end_date

    rows = conn.execute(
        text(
            f"""
            SELECT {_EPOCH_COLUMNS}
            FROM st_market_epoch
            WHERE {' AND '.join(clauses)}
            ORDER BY weekdate DESC
            LIMIT :limit
            """
        ),
        params,
    ).mappings().all()
    return list(rows)
