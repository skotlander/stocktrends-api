# routers/breadth.py
#
# Sector / Industry breadth endpoints
# - Uses st_data.industry_id joined to st_listsectorsandindustries.industry_code
# - Computes bullish/bearish breadth + maturity (trend_cnt, mt_cnt) + RSI strength
#
# Endpoints:
#   GET /v1/breadth/sector/latest
#   GET /v1/breadth/sector/history
#
# Notes:
# - Defaults to canonical equities (CS+UN); `cs_only` is retained as a legacy alias.
# - Volume in st_data is legacy-scaled in your rules (volume * 100); keep vol_scale knob.
# - Caching for /v1/breadth/sector/latest is handled at nginx, not in app memory.

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query, Request
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from api.routing import pre_payment_semantic_validator
from db import get_engine
from routers.signals import VALID_EXCHANGES
from services.market_semantics import CANONICAL_REPORTING_EXCHANGES
from utils.history_bounds import (
    DEFAULT_HISTORY_WINDOW_WEEKS,
    LIMIT_SOURCE_CALLER,
    LIMIT_SOURCE_DEFAULT,
    build_applied_bounds,
    history_default_limit,
    history_max_limit,
    probe_limit,
    resolve_history_window,
    split_probe_rows,
)

router = APIRouter(prefix="/breadth", tags=["breadth"])

GroupLevel = Literal["sector", "industry_group", "industry"]
Population = Literal["equities", "cs", "all"]

# Bounds for /breadth/sector/history.  A bare request previously ran the full
# multi-decade series through a 200000-row ceiling and returned ~48 MB; these
# are the values that make the default slice a deliberate research window.
# Read from the shared bounds table so the runtime Query below, the OpenAPI
# schema derived from it, and the discovery registry all state the same numbers.
# The pre-existing explicit ceiling is retained so that any caller who already
# raises `limit` deliberately keeps working unchanged.
HISTORY_PATH = "/v1/breadth/sector/history"
HISTORY_DEFAULT_LIMIT = history_default_limit(HISTORY_PATH)
HISTORY_MAX_LIMIT = history_max_limit(HISTORY_PATH)
HISTORY_WIDEN_HINT = (
    "Supply start and/or end to select a different range, and raise limit up to "
    f"{HISTORY_MAX_LIMIT} for more rows. When start and end are both omitted, a "
    f"trailing {DEFAULT_HISTORY_WINDOW_WEEKS}-week window ending at the latest "
    "available weekdate is applied."
)


# --- Normalizers ------------------------------------------------------------

def _norm_exchange(ex: str) -> str:
    ex = ex.strip().upper()
    if ex != "*" and ex not in VALID_EXCHANGES:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid exchange '{ex}'. Must be one of {sorted(VALID_EXCHANGES)}",
        )
    return ex


def _resolve_population(population: str | None, cs_only: bool | None) -> Population:
    """Resolve the additive population contract without silently overriding legacy input."""
    if population is not None and population not in {"equities", "cs", "all"}:
        raise HTTPException(status_code=400, detail="population must be one of: equities, cs, all")
    legacy_population = None if cs_only is None else ("cs" if cs_only else "all")
    if population is not None and legacy_population is not None and population != legacy_population:
        raise HTTPException(
            status_code=400,
            detail="population conflicts with cs_only; use population=cs with cs_only=true or population=all with cs_only=false",
        )
    return population or legacy_population or "equities"


def _validate_exchange_values(request: Request, values: dict) -> None:
    """
    Pre-payment adapter over `_norm_exchange`.

    The optional exchange filter is a fixed vocabulary decided by the query
    string alone, so an exchange code that does not exist is refused before any
    payment rail is touched.  Whether sector breadth aggregates for the requested week exist is a data question and stays
    behind the payment gate.

    Calls the same `_norm_exchange` the endpoint calls, so the 400 is unchanged.
    """
    exchange = values.get("exchange")
    if exchange:
        _norm_exchange(exchange)
    _resolve_population(values.get("population"), values.get("cs_only"))



def _latest_weekdate(engine, exchange: str | None, *, canonical: bool = True) -> Any:
    if exchange and exchange != "*":
        sql = text("SELECT MAX(weekdate) AS weekdate FROM st_data WHERE exchange = :exchange")
        params = {"exchange": exchange}
    elif canonical or exchange == "*":
        # The canonical aggregate is only A/N/Q/T.  `*` is persisted in the
        # shadow summary, but raw fallback needs the same source constraint.
        sql = text("SELECT MAX(weekdate) AS weekdate FROM st_data WHERE exchange IN ('A','N','Q','T')")
        params = {}
    else:
        sql = text("SELECT MAX(weekdate) AS weekdate FROM st_data")
        params = {}
    with engine.connect() as conn:
        row = conn.execute(sql, params).mappings().first()
    return row["weekdate"] if row else None


def _latest_summary_weekdate(engine, exchange: str | None, population: Population) -> Any:
    """Anchor canonical fast-path requests to a materialized shadow week."""
    sql = text(
        "SELECT MAX(weekdate) AS weekdate FROM st_sector_summary_shadow "
        "WHERE type = :type AND exchange = :exchange"
    )
    params = {"type": "EQ" if population == "equities" else "CS", "exchange": exchange or "*"}
    with engine.connect() as conn:
        row = conn.execute(sql, params).mappings().first()
    return row["weekdate"] if row else None


def _explicit_shadow_range_available(
    engine, exchange: str | None, population: Population, start: str | None, end: str | None,
) -> bool:
    """Use shadow only when every explicitly requested boundary is materialized."""
    sql = text(
        "SELECT MIN(weekdate) AS min_weekdate, MAX(weekdate) AS max_weekdate "
        "FROM st_sector_summary_shadow WHERE type = :type AND exchange = :exchange"
    )
    params = {"type": "EQ" if population == "equities" else "CS", "exchange": exchange or "*"}
    with engine.connect() as conn:
        row = conn.execute(sql, params).mappings().first()
    if not row or not row.get("min_weekdate") or not row.get("max_weekdate"):
        return False
    floor, high_water = str(row["min_weekdate"]), str(row["max_weekdate"])
    return (start is None or floor <= start <= high_water) and (
        end is None or floor <= end <= high_water
    )


def _current_summary_shadow_anchor(
    engine,
    exchange: str | None,
    population: Population,
    *,
    start: str | None = None,
    end: str | None = None,
) -> Any | None:
    """Return a usable shadow anchor, or select the existing raw path.

    The shadow is a serving-db publication optimization, not an authoritative
    source. Its availability checks are isolated here: a failed shadow lookup
    is recoverable, while the raw anchor lookup below remains outside that
    handler so a raw-data failure is never hidden.
    """
    try:
        shadow_weekdate = _latest_summary_weekdate(engine, exchange, population)
        if not shadow_weekdate:
            return None
        if (start is not None or end is not None) and not _explicit_shadow_range_available(
            engine, exchange, population, start, end
        ):
            return None
    except DBAPIError:
        # Only the optional published-shadow availability probe is recoverable.
        return None

    raw_weekdate = _latest_weekdate(
        engine, exchange, canonical=population != "all"
    )
    if raw_weekdate and str(shadow_weekdate) < str(raw_weekdate):
        return None
    return shadow_weekdate


def _group_cols(level: GroupLevel) -> tuple[str, str]:
    """
    Returns:
      (select_group_cols, group_by_cols)
    """
    if level == "sector":
        sel = "s.sector_code, s.sector_name"
        grp = "s.sector_code, s.sector_name"
        return sel, grp
    if level == "industry_group":
        sel = "s.industry_group_code, s.industry_group_name"
        grp = "s.industry_group_code, s.industry_group_name"
        return sel, grp
    if level == "industry":
        sel = "s.industry_code, s.industry_name"
        grp = "s.industry_code, s.industry_name"
        return sel, grp
    raise ValueError("Invalid group_level")


def _use_sector_summary(
    *,
    level: GroupLevel,
    population: Population | None = None,
    cs_only: bool | None = None,
    include_unknown: bool,
    min_price: float | None,
    min_volume: int | None,
    exchange: str | None,
) -> bool:
    """
    True when st_sector_summary can satisfy the request without raw st_data aggregation.

    `st_sector_summary_shadow` is aggregated per (weekdate, sector, exchange, type).  It
    can therefore answer a *single-exchange* request directly: the stored row is
    already the aggregate over exactly the population the caller asked for.

    The shadow table has a stored `*` row that is the direct A/N/Q/T aggregate,
    so an omitted exchange can use it without re-averaging exchange summaries.
    """
    return (
        level == "sector"
        and (population or ("cs" if cs_only is True else "all" if cs_only is False else "equities")) in {"cs", "equities"}
        and include_unknown is False
        and min_price is None
        and min_volume is None
        and (exchange is None or exchange == "*" or exchange in CANONICAL_REPORTING_EXCHANGES)
    )


def _breadth_summary_sql(
    *,
    start: str | None,
    end: str | None,
    exchange: str | None,
    population: Population = "equities",
) -> tuple[str, dict[str, Any]]:
    """Build exact canonical sector SQL against validated shadow aggregates."""
    params: dict[str, Any] = {"type": "EQ" if population == "equities" else "CS"}
    where = "WHERE ss.type = :type"

    summary_exchange = exchange or "*"
    if summary_exchange:
        where += " AND ss.exchange = :exchange"
        params["exchange"] = summary_exchange

    if start:
        where += " AND ss.weekdate >= :start"
        params["start"] = start

    if end:
        where += " AND ss.weekdate <= :end"
        params["end"] = end

    sql = f"""
        SELECT
            ss.weekdate,
            ss.sector_code,
            ss.sector_name,
            ss.total AS observed_count,
            (ss.bullish_count + ss.bearish_count) AS classified_count,
            ss.bullish_count,
            ss.bearish_count,
            ss.neutral_count,
            ss.avg_trend_cnt,
            ss.avg_trend_cnt_bullish,
            ss.avg_trend_cnt_bearish,
            ss.max_trend_cnt,
            ss.avg_mt_cnt,
            ss.avg_mt_cnt_bullish,
            ss.avg_mt_cnt_bearish,
            ss.max_mt_cnt,
            ss.avg_rsi,
            ss.rsi_ge_110_count,
            ss.rsi_ge_120_count,
            ss.young_bullish_count,
            ss.mature_bullish_count,
            cv.classified_population_count,
            cv.mapped_classified_count,
            cv.unmapped_classified_count
        FROM st_sector_summary_shadow ss
        LEFT JOIN st_sector_summary_coverage_shadow cv
          ON cv.weekdate = ss.weekdate
         AND cv.exchange = ss.exchange
         AND cv.type = ss.type
        {where}
    """
    return sql, params


def _breadth_coverage_sql(
    *, start: str | None, end: str | None, exchange: str | None, population: Population
) -> tuple[str, dict[str, Any]]:
    """Request-level canonical coverage, including all-unmapped populations."""
    params: dict[str, Any] = {
        "type": "EQ" if population == "equities" else "CS",
        "exchange": exchange or "*",
    }
    where = "WHERE cv.type = :type AND cv.exchange = :exchange"
    if start:
        where += " AND cv.weekdate >= :start"
        params["start"] = start
    if end:
        where += " AND cv.weekdate <= :end"
        params["end"] = end
    return f"""
        SELECT cv.weekdate, cv.classified_population_count,
               cv.mapped_classified_count, cv.unmapped_classified_count
        FROM st_sector_summary_coverage_shadow cv
        {where}
        ORDER BY cv.weekdate ASC
    """, params


def _postprocess_coverage(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Preserve missing coverage as undefined; never manufacture a denominator."""
    output: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        denominator = int(item.get("classified_population_count") or 0)
        mapped = int(item.get("mapped_classified_count") or 0)
        item["mapped_coverage_ratio"] = (mapped / denominator) if denominator else None
        output.append(item)
    return output


def _where_clause(
    *,
    params: dict[str, Any],
    weekdate: str | None,
    start: str | None,
    end: str | None,
    exchange: str | None,
    population: Population,
    min_price: float | None,
    min_volume: int | None,
    vol_scale: int,
    include_unknown: bool,
) -> str:
    where = "WHERE 1=1"

    if exchange and exchange != "*":
        where += " AND d.exchange = :exchange"
        params["exchange"] = exchange

    if weekdate:
        where += " AND d.weekdate = :weekdate"
        params["weekdate"] = weekdate
    else:
        if start:
            where += " AND d.weekdate >= :start"
            params["start"] = start
        if end:
            where += " AND d.weekdate <= :end"
            params["end"] = end

    if population == "cs":
        where += " AND d.type = 'CS'"
    elif population == "equities":
        where += " AND d.type IN ('CS','UN')"

    if exchange == "*" or (exchange is None and population != "all"):
        where += " AND d.exchange IN ('A','N','Q','T')"

    if min_price is not None:
        where += " AND d.price >= :min_price"
        params["min_price"] = float(min_price)

    if min_volume is not None:
        # legacy scaling (volume * 100) in your rules
        where += " AND d.volume * :vol_scale >= :min_volume"
        params["vol_scale"] = int(vol_scale)
        params["min_volume"] = int(min_volume)

    if not include_unknown:
        where += " AND s.sector_code IS NOT NULL"

    return where


# --- SQL builders -----------------------------------------------------------

def _breadth_sql(
    *,
    level: GroupLevel,
    weekdate: str | None,
    start: str | None,
    end: str | None,
    exchange: str | None,
    population: Population | None = None,
    cs_only: bool | None = None,
    min_price: float | None,
    min_volume: int | None,
    vol_scale: int,
    include_unknown: bool,
) -> tuple[str, dict[str, Any]]:
    sel_group, grp_group = _group_cols(level)

    params: dict[str, Any] = {}
    where = _where_clause(
        params=params,
        weekdate=weekdate,
        start=start,
        end=end,
        exchange=exchange,
        population=(population if population is not None else (
            "cs" if cs_only is True else "all" if cs_only is False else "equities"
        )),
        min_price=min_price,
        min_volume=min_volume,
        vol_scale=vol_scale,
        include_unknown=include_unknown,
    )

    bullish_set = "('^+','^-','v^')"
    bearish_set = "('v-','v+','^v')"
    neutral_set = "('--','=')"

    sql = f"""
        SELECT
            d.weekdate,
            {sel_group},

            COUNT(*) AS observed_count,

            SUM(d.trend IN {bullish_set}) AS bullish_count,
            SUM(d.trend IN {bearish_set}) AS bearish_count,
            SUM(d.trend IN {neutral_set}) AS neutral_count,

            AVG(CASE WHEN d.trend IN ('^+','^-','v^','v-','v+','^v') THEN d.trend_cnt END) AS avg_trend_cnt,
            AVG(CASE WHEN d.trend IN {bullish_set} THEN d.trend_cnt END) AS avg_trend_cnt_bullish,
            AVG(CASE WHEN d.trend IN {bearish_set} THEN d.trend_cnt END) AS avg_trend_cnt_bearish,
            MAX(d.trend_cnt) AS max_trend_cnt,

            AVG(CASE WHEN d.trend IN ('^+','^-','v^','v-','v+','^v') THEN d.mt_cnt END) AS avg_mt_cnt,
            AVG(CASE WHEN d.trend IN {bullish_set} THEN d.mt_cnt END) AS avg_mt_cnt_bullish,
            AVG(CASE WHEN d.trend IN {bearish_set} THEN d.mt_cnt END) AS avg_mt_cnt_bearish,
            MAX(d.mt_cnt) AS max_mt_cnt,

            AVG(CASE WHEN d.trend IN ('^+','^-','v^','v-','v+','^v')
                      AND d.rsi IS NOT NULL AND d.rsi <= 10000 THEN d.rsi END) AS avg_rsi,
            SUM(CASE WHEN d.rsi IS NOT NULL AND d.rsi <= 10000 AND d.rsi >= 110 THEN 1 ELSE 0 END) AS rsi_ge_110_count,
            SUM(CASE WHEN d.rsi IS NOT NULL AND d.rsi <= 10000 AND d.rsi >= 120 THEN 1 ELSE 0 END) AS rsi_ge_120_count,

            SUM(d.trend IN {bullish_set} AND d.trend_cnt <= 4) AS young_bullish_count,
            SUM(d.trend IN {bullish_set} AND d.trend_cnt >= 20) AS mature_bullish_count

        FROM st_data d
        LEFT JOIN st_listsectorsandindustries s
          ON s.industry_code = d.industry_id

        {where}

        GROUP BY d.weekdate, {grp_group}
    """
    return sql, params


def _postprocess(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for r in rows:
        observed_count = int(r.get("observed_count", r.get("total", 0)) or 0)
        bullish = int(r.get("bullish_count") or 0)
        bearish = int(r.get("bearish_count") or 0)
        neutral = int(r.get("neutral_count") or 0)

        rsi110 = int(r.get("rsi_ge_110_count") or 0)
        rsi120 = int(r.get("rsi_ge_120_count") or 0)

        young_bull = int(r.get("young_bullish_count") or 0)
        mature_bull = int(r.get("mature_bullish_count") or 0)

        classified_count = int(r.get("classified_count") or (bullish + bearish))
        unclassified_count = observed_count - bullish - bearish - neutral

        def directional_pct(x: int) -> float:
            return (x / classified_count) if classified_count else 0.0

        def observed_pct(x: int) -> float:
            return (x / observed_count) if observed_count else 0.0

        # `total` historically named the observed row count.  The canonical
        # public contract makes it the directional classified denominator.
        r["total"] = classified_count
        r["classified_count"] = classified_count
        r["observed_count"] = observed_count
        r["unclassified_count"] = max(0, unclassified_count)
        r["bullish_pct"] = directional_pct(bullish)
        r["bearish_pct"] = directional_pct(bearish)
        r["neutral_pct"] = observed_pct(neutral)
        r["net_breadth"] = bullish - bearish

        r["rsi_ge_110_pct"] = observed_pct(rsi110)
        r["rsi_ge_120_pct"] = observed_pct(rsi120)

        r["young_bullish_pct"] = directional_pct(young_bull)
        r["mature_bullish_pct"] = directional_pct(mature_bull)
        denominator = int(r.get("classified_population_count") or 0)
        mapped = int(r.get("mapped_classified_count") or 0)
        r["mapped_coverage_ratio"] = (mapped / denominator) if denominator else None

        out.append(r)
    return out


def _sort_key_for_level(level: GroupLevel) -> str:
    return " ORDER BY bullish_count DESC, avg_rsi DESC"


# --- Endpoints --------------------------------------------------------------

@router.get("/sector/latest")
@pre_payment_semantic_validator(_validate_exchange_values)
def breadth_sector_latest(
    request: Request,
    group_level: GroupLevel = Query(default="sector", description="Group by: sector | industry_group | industry"),
    exchange: str | None = Query(default=None, description="Optional exchange filter (A,N,Q,T; legacy B/I). Omit or use * for canonical A/N/Q/T aggregate."),
    weekdate: str | None = Query(default=None, description="Override weekdate YYYY-MM-DD; default latest."),
    population: Population | None = Query(default=None, description="Population: equities (CS+UN, default), cs, or all (legacy broad)."),
    cs_only: bool | None = Query(default=None, description="Legacy alias: true=cs; false=all. Omit for canonical equities."),
    include_unknown: bool = Query(default=False, description="Include rows where industry_id mapping is missing."),
    min_price: float | None = Query(default=None, description="Optional min price filter."),
    min_volume: int | None = Query(default=None, description="Optional min weekly volume filter in actual shares traded (e.g., 100000 = 100,000 shares)."),
    vol_scale: int = Query(default=100, description="Legacy volume scaling multiplier used in historical rules."),
    limit: int = Query(default=5000, ge=1, le=50000, description="Safety limit on number of groups returned."),
):
    engine = get_engine()

    ex = _norm_exchange(exchange) if exchange else None
    resolved_population = _resolve_population(population, cs_only)
    use_summary = _use_sector_summary(
        level=group_level, population=resolved_population, include_unknown=include_unknown,
        min_price=min_price, min_volume=min_volume, exchange=ex,
    )
    summary_anchor = (
        _current_summary_shadow_anchor(
            engine, ex, resolved_population, start=weekdate, end=weekdate
        )
        if use_summary
        else None
    )
    use_summary = summary_anchor is not None

    wd = weekdate
    if wd is None:
        latest = (
            summary_anchor
            if use_summary
            else _latest_weekdate(engine, ex, canonical=resolved_population != "all")
        )
        if not latest:
            raise HTTPException(
                status_code=404,
                detail={"request_id": request.state.request_id, "error": "no_data", "message": "No Stock Trends data available."},
            )
        wd = str(latest)

    if use_summary:
        sql_base, params = _breadth_summary_sql(
            start=wd, end=wd, exchange=ex, population=resolved_population,
        )
        order = " ORDER BY bullish_count DESC, avg_rsi DESC"
    else:
        sql_base, params = _breadth_sql(
            level=group_level, weekdate=wd, start=None, end=None, exchange=ex,
            population=resolved_population, min_price=min_price, min_volume=min_volume,
            vol_scale=vol_scale, include_unknown=include_unknown,
        )
        order = _sort_key_for_level(group_level)

    sql = text(f"{sql_base}{order} LIMIT :limit")
    params["limit"] = int(limit)

    try:
        with engine.connect() as conn:
            rows = conn.execute(sql, params).mappings().all()
            coverage_rows = []
            if use_summary:
                coverage_sql, coverage_params = _breadth_coverage_sql(
                    start=wd, end=wd, exchange=ex, population=resolved_population,
                )
                coverage_rows = conn.execute(text(coverage_sql), coverage_params).mappings().all()
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail={"request_id": request.state.request_id, "error": "db_query_failed", "message": str(e)},
        )

    data = _postprocess([dict(r) for r in rows])
    coverage = _postprocess_coverage([dict(r) for r in coverage_rows])

    return {
        "request_id": request.state.request_id,
        "group_level": group_level,
        "exchange": ex or ("*" if resolved_population != "all" else None),
        "weekdate": wd,
        "population": resolved_population,
        "cs_only": (resolved_population == "cs") if cs_only is not None else None,
        "include_unknown": include_unknown,
        "coverage": coverage[0] if coverage else None,
        "count": len(data),
        "data": data,
        "hint": "Use /breadth/sector/history for time series. Defaults are tuned for bot efficiency.",
    }


@router.get("/sector/history")
@pre_payment_semantic_validator(_validate_exchange_values)
def breadth_sector_history(
    request: Request,
    group_level: GroupLevel = Query(default="sector", description="Group by: sector | industry_group | industry"),
    exchange: str | None = Query(default=None, description="Optional exchange filter (A,N,Q,T; legacy B/I). Omit or use * for canonical A/N/Q/T aggregate."),
    start: str | None = Query(default=None, description="Start date YYYY-MM-DD (inclusive)"),
    end: str | None = Query(default=None, description="End date YYYY-MM-DD (inclusive)"),
    group_by_week: bool = Query(default=True, description="Group results by weekdate"),
    population: Population | None = Query(default=None, description="Population: equities (CS+UN, default), cs, or all (legacy broad)."),
    cs_only: bool | None = Query(default=None, description="Legacy alias: true=cs; false=all. Omit for canonical equities."),
    include_unknown: bool = Query(default=False),
    min_price: float | None = Query(default=None),
    min_volume: int | None = Query(default=None),
    vol_scale: int = Query(default=100),
    limit: int = Query(
        default=HISTORY_DEFAULT_LIMIT,
        ge=1,
        le=HISTORY_MAX_LIMIT,
        description=(
            "Safety limit across all rows returned. When start and end are both "
            f"omitted, a trailing {DEFAULT_HISTORY_WINDOW_WEEKS}-week window is also applied."
        ),
    ),
):
    engine = get_engine()
    ex = _norm_exchange(exchange) if exchange else None
    resolved_population = _resolve_population(population, cs_only)
    use_summary = _use_sector_summary(
        level=group_level, population=resolved_population, include_unknown=include_unknown,
        min_price=min_price, min_volume=min_volume, exchange=ex,
    )
    summary_anchor = (
        _current_summary_shadow_anchor(
            engine, ex, resolved_population, start=start, end=end
        )
        if use_summary
        else None
    )
    use_summary = summary_anchor is not None

    # Bounding runs here, inside paid execution, rather than in the registered
    # pre-payment validator: it shapes the work performed, it does not decide
    # whether the request was answerable.
    effective_start, effective_end, window_source = resolve_history_window(
        start=start,
        end=end,
        anchor_weekdate=lambda: (
            summary_anchor
            if use_summary
            else _latest_weekdate(engine, ex, canonical=resolved_population != "all")
        ),
    )
    limit_source = (
        LIMIT_SOURCE_CALLER if "limit" in request.query_params else LIMIT_SOURCE_DEFAULT
    )

    if use_summary:
        sql_base, params = _breadth_summary_sql(
            start=effective_start,
            end=effective_end,
            exchange=ex,
            population=resolved_population,
        )
        order = " ORDER BY weekdate ASC, bullish_count DESC, avg_rsi DESC"
    else:
        sql_base, params = _breadth_sql(
            level=group_level,
            weekdate=None,
            start=effective_start,
            end=effective_end,
            exchange=ex,
            population=resolved_population,
            min_price=min_price,
            min_volume=min_volume,
            vol_scale=vol_scale,
            include_unknown=include_unknown,
        )
        order = " ORDER BY d.weekdate ASC, bullish_count DESC, avg_rsi DESC"

    sql = text(f"{sql_base}{order} LIMIT :limit")
    params["limit"] = probe_limit(limit)

    try:
        with engine.connect() as conn:
            rows = conn.execute(sql, params).mappings().all()
            coverage_rows = []
            if use_summary:
                coverage_sql, coverage_params = _breadth_coverage_sql(
                    start=effective_start, end=effective_end, exchange=ex,
                    population=resolved_population,
                )
                coverage_rows = conn.execute(text(coverage_sql), coverage_params).mappings().all()
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail={"request_id": request.state.request_id, "error": "db_query_failed", "message": str(e)},
        )

    bounded_rows, truncated_by_limit = split_probe_rows(list(rows), limit)
    flat = _postprocess([dict(r) for r in bounded_rows])
    coverage = _postprocess_coverage([dict(r) for r in coverage_rows])

    applied_bounds = build_applied_bounds(
        start=effective_start,
        end=effective_end,
        window_source=window_source,
        limit=limit,
        limit_source=limit_source,
        max_limit=HISTORY_MAX_LIMIT,
        rows_returned=len(flat),
        truncated_by_limit=truncated_by_limit,
        widen_with=HISTORY_WIDEN_HINT,
    )

    if not group_by_week:
        return {
            "request_id": request.state.request_id,
            "group_level": group_level,
            "exchange": ex or ("*" if resolved_population != "all" else None),
            "start": start,
            "end": end,
            "population": resolved_population,
            "cs_only": (resolved_population == "cs") if cs_only is not None else None,
            "include_unknown": include_unknown,
            "coverage": coverage,
            "applied_bounds": applied_bounds,
            "count": len(flat),
            "data": flat,
        }

    weeks: list[dict[str, Any]] = []
    current = None
    bucket: list[dict[str, Any]] = []

    for row in flat:
        wk = str(row["weekdate"])
        if current is None:
            current = wk
        if wk != current:
            weeks.append({"weekdate": current, "count": len(bucket), "data": bucket})
            current = wk
            bucket = []
        bucket.append(row)

    if current is not None:
        weeks.append({"weekdate": current, "count": len(bucket), "data": bucket})

    return {
        "request_id": request.state.request_id,
        "group_level": group_level,
        "exchange": ex or ("*" if resolved_population != "all" else None),
        "start": start,
        "end": end,
        "population": resolved_population,
        "cs_only": (resolved_population == "cs") if cs_only is not None else None,
        "include_unknown": include_unknown,
        "coverage": coverage,
        "applied_bounds": applied_bounds,
        "week_count": len(weeks),
        "count": len(flat),
        "weeks": weeks,
        "note": "Grouped by weekdate; each week sorted by bullish_count then avg_rsi.",
    }
