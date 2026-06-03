from __future__ import annotations

from datetime import date
from decimal import Decimal, InvalidOperation
from statistics import median
from typing import Any

from sqlalchemy import text

try:
    from sqlalchemy.exc import DBAPIError as _DBAPIError
    from sqlalchemy.exc import OperationalError as _OperationalError
except Exception:  # pragma: no cover - import fallback for bare tooling
    _DBAPIError = Exception
    _OperationalError = Exception

DBAPIError = _DBAPIError if isinstance(_DBAPIError, type) else Exception
OperationalError = _OperationalError if isinstance(_OperationalError, type) else DBAPIError

STIM_SELECT_OUTCOME_SUMMARY_TABLE = "stweekly.stim_select_outcome_summary"
STIM_SELECT_OUTCOME_SIGNAL_ID = "stim_select"
STIM_SELECT_OUTCOME_DEFAULT_WINDOW = "trailing_10y"

STIM_SELECT_BASE_4WK = Decimal("0.00")
STIM_SELECT_BASE_13WK = Decimal("2.19")
STIM_SELECT_BASE_40WK = Decimal("6.45")
STIM_SELECT_MIN_PRICE = Decimal("2.0")
STIM_SELECT_MIN_VOLUME = 1000
STIM_SELECT_OUTCOME_EXCHANGES = {"N", "Q", "A", "B", "T"}
STIM_SELECT_OUTCOME_DEFAULT_WINDOW_YEARS = 10
MYSQL_ER_NO_SUCH_TABLE = 1146
STIM_SELECT_SUPPORTED_DEFAULT_SUMMARY_COMBINATIONS = (
    {"exchange": None, "limit_rank": None},
    {"exchange": None, "limit_rank": 10},
)

HORIZON_DEFINITIONS = {
    "4w": {
        "field": "fpr_chg4",
        "suffix": "4wk",
        "base": STIM_SELECT_BASE_4WK,
        "base_column": "base_period_mean_4wk",
    },
    "13w": {
        "field": "fpr_chg13",
        "suffix": "13wk",
        "base": STIM_SELECT_BASE_13WK,
        "base_column": "base_period_mean_13wk",
    },
    "40w": {
        "field": "fpr_chg40",
        "suffix": "40wk",
        "base": STIM_SELECT_BASE_40WK,
        "base_column": "base_period_mean_40wk",
    },
}


class StimSelectOutcomeSummaryTableMissing(Exception):
    """Raised when the durable historical outcome summary table is unavailable."""


def _candidate_error_codes(exc: BaseException):
    for candidate in (getattr(exc, "orig", None), exc):
        if candidate is None:
            continue
        for attr in ("errno", "code"):
            value = getattr(candidate, attr, None)
            if value is not None:
                yield value
        args = getattr(candidate, "args", ())
        if isinstance(args, (tuple, list)):
            yield from args
        elif args:
            yield args


def is_mysql_no_such_table_error(exc: BaseException) -> bool:
    for value in _candidate_error_codes(exc):
        try:
            if int(value) == MYSQL_ER_NO_SUCH_TABLE:
                return True
        except (TypeError, ValueError):
            continue
    return False


def build_default_summary_key(*, exchange: str | None, limit_rank: int | None) -> str:
    exchange_part = exchange or "all"
    rank_part = "all" if limit_rank is None else str(int(limit_rank))
    return (
        f"{STIM_SELECT_OUTCOME_SIGNAL_ID}:"
        f"default_{STIM_SELECT_OUTCOME_DEFAULT_WINDOW}:"
        f"exchange={exchange_part}:"
        f"limit_rank={rank_part}"
    )


def subtract_years(value: date, years: int) -> date:
    try:
        return value.replace(year=value.year - years)
    except ValueError:
        return value.replace(year=value.year - years, month=2, day=28)


def to_decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None


def to_int_or_zero(value: Any) -> int:
    if value is None:
        return 0
    return int(value)


def rate(numerator: int, denominator: int) -> float:
    if denominator <= 0:
        return 0.0
    return numerator / denominator


def create_stim_select_outcome_summary_table(conn) -> None:
    conn.execute(
        text(f"""
            CREATE TABLE IF NOT EXISTS {STIM_SELECT_OUTCOME_SUMMARY_TABLE} (
                summary_key VARCHAR(191) NOT NULL,
                signal_id VARCHAR(64) NOT NULL,
                exchange VARCHAR(8) NULL,
                limit_rank INT NULL,
                start_date DATE NOT NULL,
                end_date DATE NOT NULL,
                generated_at DATETIME NOT NULL,
                source_latest_mature_weekdate DATE NULL,
                `count` BIGINT NOT NULL DEFAULT 0,
                count_4wk BIGINT NOT NULL DEFAULT 0,
                count_13wk BIGINT NOT NULL DEFAULT 0,
                count_40wk BIGINT NOT NULL DEFAULT 0,
                first_weekdate DATE NULL,
                latest_weekdate DATE NULL,
                average_fpr_chg4 DECIMAL(18, 6) NULL,
                median_fpr_chg4 DECIMAL(18, 6) NULL,
                positive_return_count_4wk BIGINT NOT NULL DEFAULT 0,
                positive_return_rate_4wk DECIMAL(18, 10) NOT NULL DEFAULT 0,
                outperform_base_count_4wk BIGINT NOT NULL DEFAULT 0,
                outperform_base_rate_4wk DECIMAL(18, 10) NOT NULL DEFAULT 0,
                average_fpr_chg13 DECIMAL(18, 6) NULL,
                median_fpr_chg13 DECIMAL(18, 6) NULL,
                positive_return_count_13wk BIGINT NOT NULL DEFAULT 0,
                positive_return_rate_13wk DECIMAL(18, 10) NOT NULL DEFAULT 0,
                outperform_base_count_13wk BIGINT NOT NULL DEFAULT 0,
                outperform_base_rate_13wk DECIMAL(18, 10) NOT NULL DEFAULT 0,
                average_fpr_chg40 DECIMAL(18, 6) NULL,
                median_fpr_chg40 DECIMAL(18, 6) NULL,
                positive_return_count_40wk BIGINT NOT NULL DEFAULT 0,
                positive_return_rate_40wk DECIMAL(18, 10) NOT NULL DEFAULT 0,
                outperform_base_count_40wk BIGINT NOT NULL DEFAULT 0,
                outperform_base_rate_40wk DECIMAL(18, 10) NOT NULL DEFAULT 0,
                base_period_mean_4wk DECIMAL(10, 4) NOT NULL,
                base_period_mean_13wk DECIMAL(10, 4) NOT NULL,
                base_period_mean_40wk DECIMAL(10, 4) NOT NULL,
                PRIMARY KEY (summary_key),
                KEY idx_stim_select_outcome_summary_lookup (
                    signal_id,
                    exchange,
                    limit_rank
                ),
                KEY idx_stim_select_outcome_summary_generated_at (generated_at)
            )
        """)
    )


def fetch_default_stim_select_outcome_summary(
    conn,
    *,
    exchange: str | None,
    limit_rank: int | None,
) -> dict[str, Any] | None:
    summary_key = build_default_summary_key(exchange=exchange, limit_rank=limit_rank)
    try:
        row = conn.execute(
            text(f"""
                SELECT
                    summary_key,
                    signal_id,
                    exchange,
                    limit_rank,
                    start_date,
                    end_date,
                    generated_at,
                    source_latest_mature_weekdate,
                    `count` AS outcome_count,
                    count_4wk,
                    count_13wk,
                    count_40wk,
                    first_weekdate,
                    latest_weekdate,
                    average_fpr_chg4,
                    median_fpr_chg4,
                    positive_return_count_4wk,
                    positive_return_rate_4wk,
                    outperform_base_count_4wk,
                    outperform_base_rate_4wk,
                    average_fpr_chg13,
                    median_fpr_chg13,
                    positive_return_count_13wk,
                    positive_return_rate_13wk,
                    outperform_base_count_13wk,
                    outperform_base_rate_13wk,
                    average_fpr_chg40,
                    median_fpr_chg40,
                    positive_return_count_40wk,
                    positive_return_rate_40wk,
                    outperform_base_count_40wk,
                    outperform_base_rate_40wk,
                    base_period_mean_4wk,
                    base_period_mean_13wk,
                    base_period_mean_40wk
                FROM {STIM_SELECT_OUTCOME_SUMMARY_TABLE}
                WHERE summary_key = :summary_key
                  AND signal_id = :signal_id
                LIMIT 1
            """),
            {
                "summary_key": summary_key,
                "signal_id": STIM_SELECT_OUTCOME_SIGNAL_ID,
            },
        ).mappings().first()
    except DBAPIError as exc:
        if is_mysql_no_such_table_error(exc):
            raise StimSelectOutcomeSummaryTableMissing() from exc
        raise
    return dict(row) if row else None


def replace_stim_select_outcome_summary(conn, record: dict[str, Any]) -> str:
    summary_key = record.get("summary_key") or build_default_summary_key(
        exchange=record.get("exchange"),
        limit_rank=record.get("limit_rank"),
    )
    params = {
        **record,
        "summary_key": summary_key,
        "signal_id": record.get("signal_id", STIM_SELECT_OUTCOME_SIGNAL_ID),
    }

    conn.execute(
        text(f"""
            DELETE FROM {STIM_SELECT_OUTCOME_SUMMARY_TABLE}
            WHERE summary_key = :summary_key
        """),
        {"summary_key": summary_key},
    )
    conn.execute(
        text(f"""
            INSERT INTO {STIM_SELECT_OUTCOME_SUMMARY_TABLE} (
                summary_key,
                signal_id,
                exchange,
                limit_rank,
                start_date,
                end_date,
                generated_at,
                source_latest_mature_weekdate,
                `count`,
                count_4wk,
                count_13wk,
                count_40wk,
                first_weekdate,
                latest_weekdate,
                average_fpr_chg4,
                median_fpr_chg4,
                positive_return_count_4wk,
                positive_return_rate_4wk,
                outperform_base_count_4wk,
                outperform_base_rate_4wk,
                average_fpr_chg13,
                median_fpr_chg13,
                positive_return_count_13wk,
                positive_return_rate_13wk,
                outperform_base_count_13wk,
                outperform_base_rate_13wk,
                average_fpr_chg40,
                median_fpr_chg40,
                positive_return_count_40wk,
                positive_return_rate_40wk,
                outperform_base_count_40wk,
                outperform_base_rate_40wk,
                base_period_mean_4wk,
                base_period_mean_13wk,
                base_period_mean_40wk
            )
            VALUES (
                :summary_key,
                :signal_id,
                :exchange,
                :limit_rank,
                :start_date,
                :end_date,
                :generated_at,
                :source_latest_mature_weekdate,
                :count,
                :count_4wk,
                :count_13wk,
                :count_40wk,
                :first_weekdate,
                :latest_weekdate,
                :average_fpr_chg4,
                :median_fpr_chg4,
                :positive_return_count_4wk,
                :positive_return_rate_4wk,
                :outperform_base_count_4wk,
                :outperform_base_rate_4wk,
                :average_fpr_chg13,
                :median_fpr_chg13,
                :positive_return_count_13wk,
                :positive_return_rate_13wk,
                :outperform_base_count_13wk,
                :outperform_base_rate_13wk,
                :average_fpr_chg40,
                :median_fpr_chg40,
                :positive_return_count_40wk,
                :positive_return_rate_40wk,
                :outperform_base_count_40wk,
                :outperform_base_rate_40wk,
                :base_period_mean_4wk,
                :base_period_mean_13wk,
                :base_period_mean_40wk
            )
        """),
        params,
    )
    return summary_key


def outcome_base_where(
    *,
    ex: str | None,
    start_date: date | None,
    end_date: date | None,
    params: dict[str, Any],
) -> str:
    params.update(
        {
            "base_4wk": STIM_SELECT_BASE_4WK,
            "base_13wk": STIM_SELECT_BASE_13WK,
            "base_40wk": STIM_SELECT_BASE_40WK,
            "min_price": STIM_SELECT_MIN_PRICE,
            "min_volume": STIM_SELECT_MIN_VOLUME,
        }
    )
    where = """
        WHERE b.x4wk1 > :base_4wk
          AND b.x13wk1 > :base_13wk
          AND b.x40wk1 > :base_40wk
          AND a.price >= :min_price
          AND a.volume > :min_volume
          AND a.fpr_chg13 IS NOT NULL
    """

    if start_date is not None:
        where += " AND a.weekdate >= :start_date"
        params["start_date"] = start_date
    if end_date is not None:
        where += " AND a.weekdate <= :end_date"
        params["end_date"] = end_date
    if ex is not None:
        where += " AND a.exchange = :exchange"
        params["exchange"] = ex

    return where


def outcome_source_sql(where: str) -> str:
    return f"""
        FROM stweekly.st_data a
        JOIN stweekly.st_returnmeans b
          ON a.weekdate = b.weekdate
         AND a.exchange = b.exchange
         AND a.symbol = b.symbol
        {where}
    """


def latest_mature_outcome_date_sql(where: str):
    return text(f"""
        SELECT MAX(a.weekdate) AS latest_mature_outcome_date
        {outcome_source_sql(where)}
    """)


def ranked_outcomes_cte(where: str) -> str:
    return f"""
        WITH qualifying AS (
            SELECT
                a.weekdate,
                a.exchange,
                a.symbol,
                a.fpr_chg4,
                a.fpr_chg13,
                a.fpr_chg40,
                ROW_NUMBER() OVER (
                    PARTITION BY a.weekdate
                    ORDER BY
                        CASE
                            WHEN b.x13wksd IS NULL OR b.x13wksd = 0 THEN -999999999999
                            ELSE ((b.x13wk - :base_13wk) / b.x13wksd)
                        END DESC,
                        b.x13wk DESC,
                        a.exchange ASC,
                        a.symbol ASC
                ) AS rank_13wk_probability
            {outcome_source_sql(where)}
        )
    """


def outcome_aggregate_sql(*, where: str, limit_rank: int | None):
    if limit_rank is None:
        return text(f"""
            SELECT
                COUNT(*) AS outcome_count,
                COUNT(a.fpr_chg4) AS count_4wk,
                COUNT(a.fpr_chg13) AS count_13wk,
                COUNT(a.fpr_chg40) AS count_40wk,
                MIN(a.weekdate) AS first_weekdate,
                MAX(a.weekdate) AS latest_weekdate,
                AVG(a.fpr_chg4) AS average_fpr_chg4,
                AVG(a.fpr_chg13) AS average_fpr_chg13,
                AVG(a.fpr_chg40) AS average_fpr_chg40,
                SUM(CASE WHEN a.fpr_chg4 > 0 THEN 1 ELSE 0 END) AS positive_return_count_4wk,
                SUM(CASE WHEN a.fpr_chg13 > 0 THEN 1 ELSE 0 END) AS positive_return_count_13wk,
                SUM(CASE WHEN a.fpr_chg40 > 0 THEN 1 ELSE 0 END) AS positive_return_count_40wk,
                SUM(CASE WHEN a.fpr_chg4 > :base_4wk THEN 1 ELSE 0 END) AS outperform_base_count_4wk,
                SUM(CASE WHEN a.fpr_chg13 > :base_13wk THEN 1 ELSE 0 END) AS outperform_base_count_13wk,
                SUM(CASE WHEN a.fpr_chg40 > :base_40wk THEN 1 ELSE 0 END) AS outperform_base_count_40wk
            {outcome_source_sql(where)}
        """)

    return text(f"""
        {ranked_outcomes_cte(where)}
        SELECT
            COUNT(*) AS outcome_count,
            COUNT(fpr_chg4) AS count_4wk,
            COUNT(fpr_chg13) AS count_13wk,
            COUNT(fpr_chg40) AS count_40wk,
            MIN(weekdate) AS first_weekdate,
            MAX(weekdate) AS latest_weekdate,
            AVG(fpr_chg4) AS average_fpr_chg4,
            AVG(fpr_chg13) AS average_fpr_chg13,
            AVG(fpr_chg40) AS average_fpr_chg40,
            SUM(CASE WHEN fpr_chg4 > 0 THEN 1 ELSE 0 END) AS positive_return_count_4wk,
            SUM(CASE WHEN fpr_chg13 > 0 THEN 1 ELSE 0 END) AS positive_return_count_13wk,
            SUM(CASE WHEN fpr_chg40 > 0 THEN 1 ELSE 0 END) AS positive_return_count_40wk,
            SUM(CASE WHEN fpr_chg4 > :base_4wk THEN 1 ELSE 0 END) AS outperform_base_count_4wk,
            SUM(CASE WHEN fpr_chg13 > :base_13wk THEN 1 ELSE 0 END) AS outperform_base_count_13wk,
            SUM(CASE WHEN fpr_chg40 > :base_40wk THEN 1 ELSE 0 END) AS outperform_base_count_40wk
        FROM qualifying
        WHERE rank_13wk_probability <= :limit_rank
    """)


def outcome_values_sql(*, where: str, limit_rank: int | None):
    if limit_rank is None:
        return text(f"""
            SELECT
                a.fpr_chg4,
                a.fpr_chg13,
                a.fpr_chg40
            {outcome_source_sql(where)}
        """)

    return text(f"""
        {ranked_outcomes_cte(where)}
        SELECT
            fpr_chg4,
            fpr_chg13,
            fpr_chg40
        FROM qualifying
        WHERE rank_13wk_probability <= :limit_rank
    """)


def latest_mature_outcome_date(conn) -> date | None:
    params: dict[str, Any] = {}
    where = outcome_base_where(
        ex=None,
        start_date=None,
        end_date=None,
        params=params,
    )
    row = conn.execute(
        latest_mature_outcome_date_sql(where),
        params,
    ).mappings().first()
    value = (row or {}).get("latest_mature_outcome_date")
    if value is None or isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value))
    except ValueError:
        return None


def compute_summary_record(
    conn,
    *,
    exchange: str | None,
    limit_rank: int | None,
    generated_at: Any,
) -> dict[str, Any]:
    latest_weekdate = latest_mature_outcome_date(conn)
    if latest_weekdate is None:
        raise RuntimeError("No mature ST-IM Select outcome date was found.")

    start_date = subtract_years(latest_weekdate, STIM_SELECT_OUTCOME_DEFAULT_WINDOW_YEARS)
    params: dict[str, Any] = {}
    where = outcome_base_where(
        ex=exchange,
        start_date=start_date,
        end_date=latest_weekdate,
        params=params,
    )
    if limit_rank is not None:
        params["limit_rank"] = int(limit_rank)

    aggregate_row = conn.execute(
        outcome_aggregate_sql(where=where, limit_rank=limit_rank),
        params,
    ).mappings().first()
    value_rows = conn.execute(
        outcome_values_sql(where=where, limit_rank=limit_rank),
        params,
    ).mappings().all()

    aggregate = dict(aggregate_row or {})
    count = to_int_or_zero(aggregate.get("outcome_count"))
    record: dict[str, Any] = {
        "summary_key": build_default_summary_key(exchange=exchange, limit_rank=limit_rank),
        "signal_id": STIM_SELECT_OUTCOME_SIGNAL_ID,
        "exchange": exchange,
        "limit_rank": limit_rank,
        "start_date": start_date,
        "end_date": latest_weekdate,
        "generated_at": generated_at,
        "source_latest_mature_weekdate": latest_weekdate,
        "count": count,
        "first_weekdate": aggregate.get("first_weekdate"),
        "latest_weekdate": aggregate.get("latest_weekdate"),
        "base_period_mean_4wk": STIM_SELECT_BASE_4WK,
        "base_period_mean_13wk": STIM_SELECT_BASE_13WK,
        "base_period_mean_40wk": STIM_SELECT_BASE_40WK,
    }

    for horizon, definition in HORIZON_DEFINITIONS.items():
        field = definition["field"]
        suffix = definition["suffix"]
        values = [
            value
            for row in value_rows
            if (value := to_decimal(dict(row).get(field))) is not None
        ]
        positive_count = to_int_or_zero(aggregate.get(f"positive_return_count_{suffix}"))
        outperform_count = to_int_or_zero(aggregate.get(f"outperform_base_count_{suffix}"))
        horizon_count = to_int_or_zero(aggregate.get(f"count_{suffix}"))
        record[f"count_{suffix}"] = horizon_count
        record[f"average_{field}"] = aggregate.get(f"average_{field}")
        record[f"median_{field}"] = median(values) if values else None
        record[f"positive_return_count_{suffix}"] = positive_count
        record[f"positive_return_rate_{suffix}"] = rate(positive_count, horizon_count)
        record[f"outperform_base_count_{suffix}"] = outperform_count
        record[f"outperform_base_rate_{suffix}"] = rate(outperform_count, horizon_count)

    return record
