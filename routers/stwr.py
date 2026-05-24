# routers/stwr.py

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from fastapi import APIRouter, HTTPException, Query, Request
from sqlalchemy import text

from db import get_engine
from routers.signals import VALID_EXCHANGES

router = APIRouter(prefix="/stwr", tags=["stwr"])


def _norm_exchange(ex: str) -> str:
    ex = ex.strip().upper()
    if ex not in VALID_EXCHANGES:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid exchange '{ex}'. Must be one of {sorted(VALID_EXCHANGES)}",
        )
    return ex


@dataclass(frozen=True)
class ReportDef:
    code: str
    name: str
    description: str
    # SQL builder returns (sql_text, params, order_by, limit_default)
    # IMPORTANT: builders MUST tolerate extra kwargs (endpoints pass common knobs).
    build: Callable[..., tuple[str, dict[str, Any], str, int]]


# --- Helpers ---------------------------------------------------------------

def _latest_weekdate_st_data(engine, exchange: str) -> Any:
    sql = text("SELECT MAX(weekdate) AS weekdate FROM st_data WHERE exchange = :exchange")
    with engine.connect() as conn:
        row = conn.execute(sql, {"exchange": exchange}).mappings().first()
    return row["weekdate"] if row else None


def _where_date_clause(alias: str, params: dict[str, Any], start: str | None, end: str | None) -> str:
    w = ""
    if start:
        w += f" AND {alias}.weekdate >= :start"
        params["start"] = start
    if end:
        w += f" AND {alias}.weekdate <= :end"
        params["end"] = end
    return w


def _select_core(include_mast: bool) -> str:
    core = """
        d.weekdate,
        d.exchange,
        d.symbol,
        d.type,
        d.currency_code,
        d.fullname,
        d.shortname,
        d.industry_id,
        d.trend,
        d.trend_cnt,
        d.mt_cnt,
        d.prev_mtcnt,
        d.rsi,
        d.rsi_updn,
        d.vol_tag,
        d.volume,
        d.shares_os,
        d.price,
        d.adj_close,
        d.pr_change,
        d.pr_chg13,
        d.shortavg,
        d.longavg,
        d.rvol,
        d.pr_week_hi,
        d.pr_week_lo
    """
    if not include_mast:
        return core

    mast = """
        ,
        m.name AS mast_name,
        m.shortname AS mast_shortname,
        m.gm_industry_id,
        m.x_sector_name,
        m.x_industry_group_name,
        m.x_industry_name,
        m.website,
        m.location
    """
    return core + mast


def _join_mast(include_mast: bool) -> str:
    if not include_mast:
        return ""
    return """
        LEFT JOIN st_mast m
          ON m.exchange = d.exchange
         AND m.symbol = d.symbol
    """


# --- Report builders (WHERE logic translated from your PHP) ----------------
# NOTE: every builder accepts **_ to ignore extra knobs passed by endpoints.

def build_pw(
    *,
    exchange: str,
    weekdate: Any | None,
    start: str | None,
    end: str | None,
    include_mast: bool,
    min_price: float = 2.0,
    min_vol: int = 100000,
    min_rsi: int = 100,
    vol_scale: int = 100,
    **_: Any,
) -> tuple[str, dict[str, Any], str, int]:
    params: dict[str, Any] = {
        "exchange": exchange,
        "min_price": float(min_price),
        "min_vol": int(min_vol),
        "min_rsi": int(min_rsi),
        "vol_scale": int(vol_scale),
        "vol_hi": 500000,
    }

    where = """
        WHERE d.exchange = :exchange
          AND d.price >= :min_price
          AND d.volume * :vol_scale >= :min_vol
          AND d.rsi >= :min_rsi
          AND d.rsi_updn = '+'
          AND d.trend IN ('v^','v+')
          AND d.vol_tag NOT IN ('!','!!')
          AND (d.volume * :vol_scale >= :vol_hi OR d.vol_tag IN ('*','**'))
          AND d.type NOT IN ('PR','WT','RT','UN','DB','IR')
          AND d.symbol NOT IN ('TIP','HIP')
          AND (
                d.trend <> 'v+'
                OR ((d.shortavg * 13
                    - (d.price / (1 + d.pr_chg13/100)) * 3
                    + d.price * 3) / 13) > d.longavg
              )
    """

    if weekdate is not None:
        where += " AND d.weekdate = :weekdate"
        params["weekdate"] = weekdate
    else:
        where += _where_date_clause("d", params, start, end)

    sql = f"""
        SELECT {_select_core(include_mast)}
        FROM st_data d
        {_join_mast(include_mast)}
        {where}
    """
    order_by = " ORDER BY d.rsi DESC, d.shortname ASC"
    default_limit = 500
    return sql, params, order_by, default_limit


def build_bullcross(*, exchange: str, weekdate: Any | None, start: str | None, end: str | None,
                    include_mast: bool, **_: Any) -> tuple[str, dict[str, Any], str, int]:
    params: dict[str, Any] = {"exchange": exchange}
    where = """
        WHERE d.exchange = :exchange
          AND d.trend = 'v^'
          AND d.type NOT IN ('WT','RT','DB','IR')
    """
    if weekdate is not None:
        where += " AND d.weekdate = :weekdate"
        params["weekdate"] = weekdate
    else:
        where += _where_date_clause("d", params, start, end)

    sql = f"""
        SELECT {_select_core(include_mast)}
        FROM st_data d
        {_join_mast(include_mast)}
        {where}
    """
    return sql, params, " ORDER BY d.rsi DESC, d.shortname ASC", 1000


def build_bullcrosspred(*, exchange: str, weekdate: Any | None, start: str | None, end: str | None,
                        include_mast: bool, **_: Any) -> tuple[str, dict[str, Any], str, int]:
    params: dict[str, Any] = {"exchange": exchange}
    where = """
        WHERE d.exchange = :exchange
          AND d.trend = 'v+'
          AND ((d.shortavg * 13 - d.price/(1 + d.pr_chg13/100) + d.price)/13) > d.longavg
          AND d.type NOT IN ('WT','RT','DB','IR')
    """
    if weekdate is not None:
        where += " AND d.weekdate = :weekdate"
        params["weekdate"] = weekdate
    else:
        where += _where_date_clause("d", params, start, end)

    sql = f"""
        SELECT {_select_core(include_mast)}
        FROM st_data d
        {_join_mast(include_mast)}
        {where}
    """
    return sql, params, " ORDER BY d.rsi DESC, d.shortname ASC", 1000


def build_bearcross(*, exchange: str, weekdate: Any | None, start: str | None, end: str | None,
                    include_mast: bool, **_: Any) -> tuple[str, dict[str, Any], str, int]:
    params: dict[str, Any] = {"exchange": exchange}
    where = """
        WHERE d.exchange = :exchange
          AND d.trend = '^v'
          AND d.type NOT IN ('WT','RT','DB','IR')
    """
    if weekdate is not None:
        where += " AND d.weekdate = :weekdate"
        params["weekdate"] = weekdate
    else:
        where += _where_date_clause("d", params, start, end)

    sql = f"""
        SELECT {_select_core(include_mast)}
        FROM st_data d
        {_join_mast(include_mast)}
        {where}
    """
    return sql, params, " ORDER BY d.rsi ASC, d.shortname ASC", 1000


def build_bearcrosspred(*, exchange: str, weekdate: Any | None, start: str | None, end: str | None,
                        include_mast: bool, **_: Any) -> tuple[str, dict[str, Any], str, int]:
    params: dict[str, Any] = {"exchange": exchange}
    where = """
        WHERE d.exchange = :exchange
          AND d.trend = '^-'
          AND ((d.shortavg * 13 - d.price/(1 + d.pr_chg13/100) + d.price)/13) < d.longavg
          AND d.type NOT IN ('WT','RT','DB','IR')
    """
    if weekdate is not None:
        where += " AND d.weekdate = :weekdate"
        params["weekdate"] = weekdate
    else:
        where += _where_date_clause("d", params, start, end)

    sql = f"""
        SELECT {_select_core(include_mast)}
        FROM st_data d
        {_join_mast(include_mast)}
        {where}
    """
    return sql, params, " ORDER BY d.rsi ASC, d.shortname ASC", 1000


def build_nweakbull(
    *, exchange: str, weekdate: Any | None, start: str | None, end: str | None, include_mast: bool,
    min_mt_cnt: int = 5, min_cap_mil: float = 0.0, min_price: float = 0.0, **_: Any
) -> tuple[str, dict[str, Any], str, int]:
    # PHP: trend='^-' AND trend_cnt=1 AND mt_cnt>=5 AND shares_os*price>=min_cap*1,000,000 AND price>=min_pr
    # For TSX they used 250m sometimes; we keep configurable.
    params = {
        "exchange": exchange,
        "min_mt_cnt": int(min_mt_cnt),
        "min_cap": float(min_cap_mil),
        "min_price": float(min_price),
    }
    where = """
        WHERE d.exchange = :exchange
          AND d.trend = '^-'
          AND d.trend_cnt = 1
          AND d.mt_cnt >= :min_mt_cnt
          AND (d.shares_os * d.price) >= (:min_cap * 1000000)
          AND d.price >= :min_price
          AND d.type NOT IN ('WT','RT','DB','IR')
    """
    if weekdate is not None:
        where += " AND d.weekdate = :weekdate"; params["weekdate"] = weekdate
    else:
        where += _where_date_clause("d", params, start, end)

    sql = f"SELECT {_select_core(include_mast)} FROM st_data d {_join_mast(include_mast)} {where}"
    return sql, params, " ORDER BY d.rsi ASC, d.shortname ASC", 1000


def build_nweakbear(
    *, exchange: str, weekdate: Any | None, start: str | None, end: str | None, include_mast: bool,
    min_mt_cnt: int = 5, min_cap_mil: float = 0.0, min_price: float = 0.0, **_: Any
) -> tuple[str, dict[str, Any], str, int]:
    params = {
        "exchange": exchange,
        "min_mt_cnt": int(min_mt_cnt),
        "min_cap": float(min_cap_mil),
        "min_price": float(min_price),
    }
    where = """
        WHERE d.exchange = :exchange
          AND d.trend = 'v+'
          AND d.trend_cnt = 1
          AND d.mt_cnt >= :min_mt_cnt
          AND d.price >= :min_price
          AND (d.shares_os * d.price) >= (:min_cap * 1000000)
          AND d.type NOT IN ('WT','RT','DB','IR')
    """
    if weekdate is not None:
        where += " AND d.weekdate = :weekdate"; params["weekdate"] = weekdate
    else:
        where += _where_date_clause("d", params, start, end)

    sql = f"SELECT {_select_core(include_mast)} FROM st_data d {_join_mast(include_mast)} {where}"
    return sql, params, " ORDER BY d.rsi DESC, d.shortname ASC", 1000


def build_nreturnbull(
    *, exchange: str, weekdate: Any | None, start: str | None, end: str | None, include_mast: bool,
    min_mt_cnt: int = 5, min_cap_mil: float = 0.0, min_price: float = 0.0, **_: Any
) -> tuple[str, dict[str, Any], str, int]:
    params = {
        "exchange": exchange,
        "min_mt_cnt": int(min_mt_cnt),
        "min_cap": float(min_cap_mil),
        "min_price": float(min_price),
    }
    where = """
        WHERE d.exchange = :exchange
          AND d.trend = '^+'
          AND d.trend_cnt = 1
          AND d.mt_cnt >= :min_mt_cnt
          AND (d.shares_os * d.price) >= (:min_cap * 1000000)
          AND d.price >= :min_price
          AND d.type NOT IN ('WT','RT','DB','IR')
    """
    if weekdate is not None:
        where += " AND d.weekdate = :weekdate"; params["weekdate"] = weekdate
    else:
        where += _where_date_clause("d", params, start, end)

    sql = f"SELECT {_select_core(include_mast)} FROM st_data d {_join_mast(include_mast)} {where}"
    return sql, params, " ORDER BY d.rsi DESC, d.shortname ASC", 1000


def build_nreturnbear(
    *, exchange: str, weekdate: Any | None, start: str | None, end: str | None, include_mast: bool,
    min_mt_cnt: int = 5, min_cap_mil: float = 0.0, min_price: float = 0.0, **_: Any
) -> tuple[str, dict[str, Any], str, int]:
    params = {
        "exchange": exchange,
        "min_mt_cnt": int(min_mt_cnt),
        "min_cap": float(min_cap_mil),
        "min_price": float(min_price),
    }
    where = """
        WHERE d.exchange = :exchange
          AND d.trend = 'v-'
          AND d.trend_cnt = 1
          AND d.mt_cnt >= :min_mt_cnt
          AND d.price >= :min_price
          AND (d.shares_os * d.price) >= (:min_cap * 1000000)
          AND d.type NOT IN ('WT','RT','DB','IR')
    """
    if weekdate is not None:
        where += " AND d.weekdate = :weekdate"; params["weekdate"] = weekdate
    else:
        where += _where_date_clause("d", params, start, end)

    sql = f"SELECT {_select_core(include_mast)} FROM st_data d {_join_mast(include_mast)} {where}"
    return sql, params, " ORDER BY d.rsi ASC, d.shortname ASC", 1000


def build_oldbulls(
    *, exchange: str, weekdate: Any | None, start: str | None, end: str | None, include_mast: bool,
    min_mt_cnt: int = 10, min_cap_mil: float = 0.0, min_price: float = 0.0, **_: Any
) -> tuple[str, dict[str, Any], str, int]:
    params = {
        "exchange": exchange,
        "min_mt_cnt": int(min_mt_cnt),
        "min_cap": float(min_cap_mil),
        "min_price": float(min_price),
    }
    where = """
        WHERE d.exchange = :exchange
          AND d.trend IN ('^+','^-')
          AND d.trend_cnt >= 1
          AND d.mt_cnt >= :min_mt_cnt
          AND d.price >= :min_price
          AND (d.shares_os * d.price) >= (:min_cap * 1000000)
          AND d.type NOT IN ('WT','RT','DB','IR')
    """
    if weekdate is not None:
        where += " AND d.weekdate = :weekdate"; params["weekdate"] = weekdate
    else:
        where += _where_date_clause("d", params, start, end)

    sql = f"SELECT {_select_core(include_mast)} FROM st_data d {_join_mast(include_mast)} {where}"
    return sql, params, " ORDER BY d.mt_cnt DESC, d.shortname ASC", 50


def build_oldbears(
    *, exchange: str, weekdate: Any | None, start: str | None, end: str | None, include_mast: bool,
    min_mt_cnt: int = 10, min_cap_mil: float = 0.0, min_price: float = 0.0, **_: Any
) -> tuple[str, dict[str, Any], str, int]:
    params = {
        "exchange": exchange,
        "min_mt_cnt": int(min_mt_cnt),
        "min_cap": float(min_cap_mil),
        "min_price": float(min_price),
    }
    where = """
        WHERE d.exchange = :exchange
          AND d.trend IN ('v+','v-')
          AND d.trend_cnt >= 1
          AND d.mt_cnt >= :min_mt_cnt
          AND d.price >= :min_price
          AND (d.shares_os * d.price) >= (:min_cap * 1000000)
          AND d.type NOT IN ('WT','RT','DB','IR')
    """
    if weekdate is not None:
        where += " AND d.weekdate = :weekdate"; params["weekdate"] = weekdate
    else:
        where += _where_date_clause("d", params, start, end)

    sql = f"SELECT {_select_core(include_mast)} FROM st_data d {_join_mast(include_mast)} {where}"
    return sql, params, " ORDER BY d.mt_cnt DESC, d.shortname ASC", 50


def build_lvg(
    *, exchange: str, weekdate: Any | None, start: str | None, end: str | None, include_mast: bool,
    min_pr_chg: float = 1.0, min_cap_mil: float = 0.0, **_: Any
) -> tuple[str, dict[str, Any], str, int]:
    # PHP: vol_tag IN('!','!!') AND rsi_updn='+' AND pr_change>=1.0 AND type NOT IN ... ORDER BY pr_change desc, rsi desc LIMIT 50
    params = {
        "exchange": exchange,
        "min_pr_chg": float(min_pr_chg),
        "min_cap": float(min_cap_mil),
    }
    where = """
        WHERE d.exchange = :exchange
          AND d.vol_tag IN ('!','!!')
          AND d.rsi_updn = '+'
          AND (d.shares_os * d.price) >= (:min_cap * 1000000)
          AND d.pr_change >= :min_pr_chg
          AND d.type NOT IN ('WT','RT','DB','IR')
    """
    if weekdate is not None:
        where += " AND d.weekdate = :weekdate"; params["weekdate"] = weekdate
    else:
        where += _where_date_clause("d", params, start, end)

    sql = f"SELECT {_select_core(include_mast)} FROM st_data d {_join_mast(include_mast)} {where}"
    return sql, params, " ORDER BY d.pr_change DESC, d.rsi DESC", 50


def build_rvol(
    *, exchange: str, weekdate: Any | None, start: str | None, end: str | None, include_mast: bool,
    min_turnover: float = 5.0, min_vol: int = 100000, vol_scale: int = 100, min_cap_mil: float = 0.0, **_: Any
) -> tuple[str, dict[str, Any], str, int]:
    params = {
        "exchange": exchange,
        "min_turnover": float(min_turnover),
        "min_vol": int(min_vol),
        "vol_scale": int(vol_scale),
        "min_cap": float(min_cap_mil),
    }
    where = """
        WHERE d.exchange = :exchange
          AND d.volume * :vol_scale >= :min_vol
          AND (d.shares_os * d.price) >= (:min_cap * 1000000)
          AND d.rvol >= :min_turnover
          AND d.type NOT IN ('PR','WT','RT','UN','DB','IR','TF')
    """
    if weekdate is not None:
        where += " AND d.weekdate = :weekdate"; params["weekdate"] = weekdate
    else:
        where += _where_date_clause("d", params, start, end)

    sql = f"SELECT {_select_core(include_mast)} FROM st_data d {_join_mast(include_mast)} {where}"
    return sql, params, " ORDER BY d.rvol DESC, d.rsi DESC", 50


def build_uhv(
    *, exchange: str, weekdate: Any | None, start: str | None, end: str | None, include_mast: bool,
    min_vol: int = 100000, vol_scale: int = 100, **_: Any
) -> tuple[str, dict[str, Any], str, int]:
    params = {"exchange": exchange, "min_vol": int(min_vol), "vol_scale": int(vol_scale)}
    where = """
        WHERE d.exchange = :exchange
          AND d.volume * :vol_scale >= :min_vol
          AND d.vol_tag IN ('*','**')
          AND d.type NOT IN ('WT','RT','DB','IR')
    """
    if weekdate is not None:
        where += " AND d.weekdate = :weekdate"; params["weekdate"] = weekdate
    else:
        where += _where_date_clause("d", params, start, end)

    sql = f"SELECT {_select_core(include_mast)} FROM st_data d {_join_mast(include_mast)} {where}"
    return sql, params, " ORDER BY d.volume DESC, d.rsi DESC", 1000


def build_ulv(
    *, exchange: str, weekdate: Any | None, start: str | None, end: str | None, include_mast: bool,
    min_vol: int = 100000, vol_scale: int = 100, **_: Any
) -> tuple[str, dict[str, Any], str, int]:
    params = {"exchange": exchange, "min_vol": int(min_vol), "vol_scale": int(vol_scale)}
    where = """
        WHERE d.exchange = :exchange
          AND d.volume * :vol_scale >= :min_vol
          AND d.vol_tag IN ('!','!!')
          AND d.type NOT IN ('WT','RT','DB','IR')
    """
    if weekdate is not None:
        where += " AND d.weekdate = :weekdate"; params["weekdate"] = weekdate
    else:
        where += _where_date_clause("d", params, start, end)

    sql = f"SELECT {_select_core(include_mast)} FROM st_data d {_join_mast(include_mast)} {where}"
    return sql, params, " ORDER BY d.volume DESC, d.rsi DESC", 1000


def build_topgainers(
    *, exchange: str, weekdate: Any | None, start: str | None, end: str | None, include_mast: bool,
    min_price: float = 2.0, min_vol: int = 0, vol_scale: int = 100, min_cap_mil: float = 0.0, **_: Any
) -> tuple[str, dict[str, Any], str, int]:
    params = {
        "exchange": exchange,
        "min_price": float(min_price),
        "min_vol": int(min_vol),
        "vol_scale": int(vol_scale),
        "min_cap": float(min_cap_mil),
    }
    where = """
        WHERE d.exchange = :exchange
          AND d.price >= :min_price
          AND d.volume * :vol_scale >= :min_vol
          AND (d.shares_os * d.price) >= (:min_cap * 1000000)
          AND d.pr_change > 0
          AND d.type NOT IN ('WT','RT','DB','IR')
    """
    if weekdate is not None:
        where += " AND d.weekdate = :weekdate"; params["weekdate"] = weekdate
    else:
        where += _where_date_clause("d", params, start, end)

    sql = f"SELECT {_select_core(include_mast)} FROM st_data d {_join_mast(include_mast)} {where}"
    return sql, params, " ORDER BY d.pr_change DESC, d.rsi DESC", 50


def build_toplosers(
    *, exchange: str, weekdate: Any | None, start: str | None, end: str | None, include_mast: bool,
    min_price: float = 2.0, min_vol: int = 0, vol_scale: int = 100, min_cap_mil: float = 0.0, **_: Any
) -> tuple[str, dict[str, Any], str, int]:
    params = {
        "exchange": exchange,
        "min_price": float(min_price),
        "min_vol": int(min_vol),
        "vol_scale": int(vol_scale),
        "min_cap": float(min_cap_mil),
    }
    where = """
        WHERE d.exchange = :exchange
          AND d.price >= :min_price
          AND d.volume * :vol_scale >= :min_vol
          AND (d.shares_os * d.price) >= (:min_cap * 1000000)
          AND d.pr_change < 0
          AND d.type NOT IN ('WT','RT','DB','IR')
    """
    if weekdate is not None:
        where += " AND d.weekdate = :weekdate"; params["weekdate"] = weekdate
    else:
        where += _where_date_clause("d", params, start, end)

    sql = f"SELECT {_select_core(include_mast)} FROM st_data d {_join_mast(include_mast)} {where}"
    return sql, params, " ORDER BY d.pr_change ASC, d.rsi ASC", 50


def build_yrhighs(
    *, exchange: str, weekdate: Any | None, start: str | None, end: str | None, include_mast: bool,
    min_cap_mil: float = 0.0, min_price: float = 0.0, min_vol: int = 0, vol_scale: int = 100, **_: Any
) -> tuple[str, dict[str, Any], str, int]:
    params = {
        "exchange": exchange,
        "min_cap": float(min_cap_mil),
        "min_price": float(min_price),
        "min_vol": int(min_vol),
        "vol_scale": int(vol_scale),
    }
    where = """
        WHERE d.exchange = :exchange
          AND d.price >= :min_price
          AND d.volume * :vol_scale >= :min_vol
          AND (d.shares_os * d.price) >= (:min_cap * 1000000)
          AND d.yr_hi = d.pr_week_hi
          AND d.type NOT IN ('WT','RT','DB','IR')
    """
    if weekdate is not None:
        where += " AND d.weekdate = :weekdate"; params["weekdate"] = weekdate
    else:
        where += _where_date_clause("d", params, start, end)

    sql = f"SELECT {_select_core(include_mast)} FROM st_data d {_join_mast(include_mast)} {where}"
    return sql, params, " ORDER BY d.pr_change DESC, d.rsi DESC", 1000


def build_yrlows(
    *, exchange: str, weekdate: Any | None, start: str | None, end: str | None, include_mast: bool,
    min_cap_mil: float = 0.0, min_price: float = 0.0, min_vol: int = 0, vol_scale: int = 100, **_: Any
) -> tuple[str, dict[str, Any], str, int]:
    params = {
        "exchange": exchange,
        "min_cap": float(min_cap_mil),
        "min_price": float(min_price),
        "min_vol": int(min_vol),
        "vol_scale": int(vol_scale),
    }
    where = """
        WHERE d.exchange = :exchange
          AND d.price >= :min_price
          AND d.volume * :vol_scale >= :min_vol
          AND (d.shares_os * d.price) >= (:min_cap * 1000000)
          AND d.yr_lo = d.pr_week_lo
          AND d.type NOT IN ('WT','RT','DB','IR')
    """
    if weekdate is not None:
        where += " AND d.weekdate = :weekdate"; params["weekdate"] = weekdate
    else:
        where += _where_date_clause("d", params, start, end)

    sql = f"SELECT {_select_core(include_mast)} FROM st_data d {_join_mast(include_mast)} {where}"
    return sql, params, " ORDER BY d.pr_change ASC, d.rsi ASC", 1000


def build_activebyvol(
    *, exchange: str, weekdate: Any | None, start: str | None, end: str | None, include_mast: bool,
    min_cap_mil: float = 0.0, min_price: float = 0.0, min_vol: int = 0, vol_scale: int = 100, **_: Any
) -> tuple[str, dict[str, Any], str, int]:
    params = {
        "exchange": exchange,
        "min_cap": float(min_cap_mil),
        "min_price": float(min_price),
        "min_vol": int(min_vol),
        "vol_scale": int(vol_scale),
    }
    where = """
        WHERE d.exchange = :exchange
          AND d.price >= :min_price
          AND d.volume * :vol_scale >= :min_vol
          AND (d.shares_os * d.price) >= (:min_cap * 1000000)
          AND d.type NOT IN ('WT','RT','DB','IR')
    """
    if weekdate is not None:
        where += " AND d.weekdate = :weekdate"; params["weekdate"] = weekdate
    else:
        where += _where_date_clause("d", params, start, end)
    sql = f"SELECT {_select_core(include_mast)} FROM st_data d {_join_mast(include_mast)} {where}"
    return sql, params, " ORDER BY d.volume DESC", 50


def build_hmg(
    *, exchange: str, weekdate: Any | None, start: str | None, end: str | None, include_mast: bool,
    min_vol: int = 100000, vol_scale: int = 100, **_: Any
) -> tuple[str, dict[str, Any], str, int]:
    # PHP computed Mom_Index using vtsym.factor; we don't have that join here.
    # We'll approximate by sorting on pr_change * rvol (still bot-useful).
    params = {"exchange": exchange, "min_vol": int(min_vol), "vol_scale": int(vol_scale)}
    where = """
        WHERE d.exchange = :exchange
          AND d.volume * :vol_scale >= :min_vol
          AND d.type NOT IN ('WT','RT','DB','IR')
    """
    if weekdate is not None:
        where += " AND d.weekdate = :weekdate"; params["weekdate"] = weekdate
    else:
        where += _where_date_clause("d", params, start, end)
    sql = f"""
        SELECT {_select_core(include_mast)}
        , (d.pr_change * d.rvol) AS mom_index
        FROM st_data d
        {_join_mast(include_mast)}
        {where}
    """
    return sql, params, " ORDER BY mom_index DESC", 50


def build_toptrend(
    *,
    exchange: str,
    weekdate: Any | None,
    start: str | None,
    end: str | None,
    include_mast: bool,
    min_price: float = 5.0,
    min_vol: int = 100000,
    min_rsi: int = 100,
    vol_scale: int = 100,
    min_4wk_chg: float = 5.0,
    min_13wk_ma_chg: float = 10.0,
    min_range_pct: float = 1.0,
    **_: Any,
) -> tuple[str, dict[str, Any], str, int]:
    """
    FAST version (no per-row stored function calls):
      - join prior rows for 4-week price and 13-week shortavg
      - compute % change directly
    """
    params: dict[str, Any] = {
        "exchange": exchange,
        "min_price": float(min_price),
        "min_vol": int(min_vol),
        "min_rsi": int(min_rsi),
        "vol_scale": int(vol_scale),
        "min_4wk_chg": float(min_4wk_chg),
        "min_13wk_ma_chg": float(min_13wk_ma_chg),
        "min_range_pct": float(min_range_pct),
    }

    # IMPORTANT: keep equality filter on (exchange, weekdate) when weekdate is provided
    # so MySQL uses the (weekdate, exchange, symbol) PK efficiently.
    base_where = """
        WHERE d.exchange = :exchange
          AND d.type IN ('CS','TF','UN')
          AND d.price >= :min_price
          AND d.rsi >= :min_rsi
          AND d.volume * :vol_scale >= :min_vol
          AND d.trend IN ('^+','^-','v+','v^')
          AND (d.pr_week_hi / d.pr_week_lo - 1) * 100 > :min_range_pct
          AND p4.price IS NOT NULL
          AND p13.shortavg IS NOT NULL
          AND ((d.price / p4.price) - 1) * 100 >= :min_4wk_chg
          AND ((d.shortavg / p13.shortavg) - 1) * 100 >= :min_13wk_ma_chg
    """

    if weekdate is not None:
        base_where += " AND d.weekdate = :weekdate"
        params["weekdate"] = weekdate
    else:
        base_where += _where_date_clause("d", params, start, end)

    sql = f"""
        SELECT {_select_core(include_mast)}
        FROM st_data d
        LEFT JOIN st_data p4
          ON p4.exchange = d.exchange
         AND p4.symbol = d.symbol
         AND p4.weekdate = SUBDATE(d.weekdate, INTERVAL 4 WEEK)
        LEFT JOIN st_data p13
          ON p13.exchange = d.exchange
         AND p13.symbol = d.symbol
         AND p13.weekdate = SUBDATE(d.weekdate, INTERVAL 13 WEEK)
        {_join_mast(include_mast)}
        {base_where}
    """
    return sql, params, " ORDER BY d.rsi DESC", 50


# --- Report catalog --------------------------------------------------------

REPORTS: dict[str, ReportDef] = {
    "pw": ReportDef("pw", "Picks of the Week", "Bullish Xover or Weak Bearish with RSI+, liquidity + volume conditions.", build_pw),
    "bullcross": ReportDef("bullcross", "Bullish Crossovers", "trend='v^' this week; ranked by RSI desc.", build_bullcross),
    "bullcrosspred": ReportDef("bullcrosspred", "Bullish Crossover Predictions", "trend='v+' with MA-cross predicted.", build_bullcrosspred),
    "bearcross": ReportDef("bearcross", "Bearish Crossovers", "trend='^v' this week; ranked by RSI asc.", build_bearcross),
    "bearcrosspred": ReportDef("bearcrosspred", "Bearish Crossover Predictions", "trend='^-' with MA-cross predicted.", build_bearcrosspred),

    "nweakbull": ReportDef("nweakbull", "New Weakening Bullish Stocks", "trend='^-' and trend_cnt=1 (new weak bull).", build_nweakbull),
    "nweakbear": ReportDef("nweakbear", "New Weakening Bearish Stocks", "trend='v+' and trend_cnt=1 (new weak bear).", build_nweakbear),
    "nreturnbull": ReportDef("nreturnbull", "Return to (Strong) Bullish Stocks", "trend='^+' and trend_cnt=1 (return strong bull).", build_nreturnbull),
    "nreturnbear": ReportDef("nreturnbear", "Return to (Strong) Bearish Stocks", "trend='v-' and trend_cnt=1 (return strong bear).", build_nreturnbear),

    "toptrend": ReportDef("toptrend", "Top Trending stocks of the Week", "Momentum screen using 4wk price and 13wk MA change.", build_toptrend),
    "oldbulls": ReportDef("oldbulls", "Longest-Running Bullish Stocks", "Longest bull runs (mt_cnt desc).", build_oldbulls),
    "oldbears": ReportDef("oldbears", "Longest-Running Bearish Stocks", "Longest bear runs (mt_cnt desc).", build_oldbears),

    "hmg": ReportDef("hmg", "High Momentum Gains", "Approx momentum ranking (pr_change * rvol).", build_hmg),
    "lvg": ReportDef("lvg", "Low Volume Gains", "Advancing on unusually low volume.", build_lvg),
    "rvol": ReportDef("rvol", "Top Relative Volume Stocks", "Ranked by rvol desc (turnover).", build_rvol),
    "uhv": ReportDef("uhv", "Unusually High Volume Stocks", "vol_tag in (*,**).", build_uhv),
    "ulv": ReportDef("ulv", "Unusually Low Volume Stocks", "vol_tag in (!,!!).", build_ulv),

    "topgainers": ReportDef("topgainers", "Top Percentage Gainers", "pr_change > 0, ranked by pr_change desc.", build_topgainers),
    "toplosers": ReportDef("toplosers", "Top Percentage Losers", "pr_change < 0, ranked by pr_change asc.", build_toplosers),
    "yrhighs": ReportDef("yrhighs", "52 Week Highs", "yr_hi equals weekly high.", build_yrhighs),
    "yrlows": ReportDef("yrlows", "52 Week Lows", "yr_lo equals weekly low.", build_yrlows),
    "activebyvol": ReportDef("activebyvol", "Most Active by Volume", "Ranked by volume desc.", build_activebyvol),
}


# --- Endpoints -------------------------------------------------------------

@router.get("/reports/catalog")
def stwr_reports_catalog():
    return {
        "count": len(REPORTS),
        "reports": [{"code": r.code, "name": r.name, "description": r.description} for r in REPORTS.values()],
        "hint": "Use /stwr/reports/latest?rpt=... or /stwr/reports/history?rpt=...",
    }


@router.get("/reports/latest")
def stwr_reports_latest(
    request: Request,
    rpt: str = Query(..., description="Report code: e.g. pw, bullcross, toptrend, ..."),
    exchange: str = Query(..., description="Exchange code: N,Q,A,B,T,I"),
    weekdate: str | None = Query(default=None, description="Override weekdate YYYY-MM-DD; defaults to the latest available report week for the exchange"),
    include_mast: bool = Query(default=False, description="Include richer instrument metadata"),
    limit: int | None = Query(default=None, ge=1, le=50000, description="Override report default limit"),

    # Common knobs (safe to pass to all builders; unused ones are ignored)
    min_price: float = Query(default=2.0),
    min_vol: int = Query(default=100000),
    min_rsi: int = Query(default=100),
    vol_scale: int = Query(default=100),

    # Caps/age knobs (some reports use them)
    min_cap_mil: float = Query(default=0.0, description="Minimum market cap proxy in $ millions (shares_os*price)"),
    min_mt_cnt: int = Query(default=5, description="Minimum mt_cnt for certain 'new' trend-change reports"),

    # Specialized knobs
    lvg_min_pr_chg: float = Query(default=1.0),
    rvol_min_turnover: float = Query(default=5.0),

    # toptrend knobs
    tt_min_price: float = Query(default=5.0),
    tt_min_4wk_chg: float = Query(default=5.0),
    tt_min_13wk_ma_chg: float = Query(default=10.0),
    tt_min_range_pct: float = Query(default=1.0),
):
    rpt = rpt.strip().lower()
    if rpt not in REPORTS:
        raise HTTPException(
            status_code=400,
            detail={"request_id": request.state.request_id, "error": "unknown_report", "rpt": rpt, "allowed": sorted(REPORTS.keys())},
        )

    ex = _norm_exchange(exchange)
    engine = get_engine()

    wd = weekdate
    if wd is None:
        latest = _latest_weekdate_st_data(engine, ex)
        if not latest:
            raise HTTPException(
                status_code=404,
                detail={"request_id": request.state.request_id, "error": "no_data", "message": f"No Stock Trends data available for exchange {ex}"},
            )
        wd = str(latest)

    rep = REPORTS[rpt]

    sql_base, params, order_by, default_limit = rep.build(
        exchange=ex,
        weekdate=wd,
        start=None,
        end=None,
        include_mast=include_mast,
        # common
        min_price=min_price,
        min_vol=min_vol,
        min_rsi=min_rsi,
        vol_scale=vol_scale,
        # caps/age
        min_cap_mil=min_cap_mil,
        min_mt_cnt=min_mt_cnt,
        # specialized
        min_pr_chg=lvg_min_pr_chg,
        min_turnover=rvol_min_turnover,
        # toptrend
        min_4wk_chg=tt_min_4wk_chg,
        min_13wk_ma_chg=tt_min_13wk_ma_chg,
        min_range_pct=tt_min_range_pct,
        # toptrend uses min_price as tt_min_price
        # (override by passing tt_min_price into min_price when rpt is toptrend)
    )

    # Override toptrend min_price with tt_min_price cleanly
    if rpt == "toptrend":
        params["min_price"] = float(tt_min_price)

    lim = limit or default_limit
    sql = text(f"{sql_base}{order_by} LIMIT :limit")
    params["limit"] = int(lim)

    try:
        with engine.connect() as conn:
            rows = conn.execute(sql, params).mappings().all()
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail={"request_id": request.state.request_id, "error": "db_query_failed", "message": str(e)},
        )

    data = [dict(r) for r in rows]
    for d in data:
        d["symbol_exchange"] = f'{d["symbol"]}-{d["exchange"]}'

    return {
        "request_id": request.state.request_id,
        "rpt": rpt,
        "name": rep.name,
        "exchange": ex,
        "weekdate": wd,
        "count": len(data),
        "data": data,
        "note": "STWR screening rules reproduced without website rendering joins.",
    }


@router.get("/reports/history")
def stwr_reports_history(
    request: Request,
    rpt: str = Query(..., description="Report code: e.g. pw, bullcross, toptrend, ..."),
    exchange: str = Query(..., description="Exchange code: N,Q,A,B,T,I"),
    start: str | None = Query(default=None, description="Start date YYYY-MM-DD (inclusive)"),
    end: str | None = Query(default=None, description="End date YYYY-MM-DD (inclusive)"),
    group_by_week: bool = Query(default=True, description="Group results by weekdate"),
    include_mast: bool = Query(default=False),
    limit: int = Query(default=200000, ge=1, le=500000, description="Safety limit across all weeks"),

    # Common knobs
    min_price: float = Query(default=2.0),
    min_vol: int = Query(default=100000),
    min_rsi: int = Query(default=100),
    vol_scale: int = Query(default=100),

    # Caps/age knobs
    min_cap_mil: float = Query(default=0.0),
    min_mt_cnt: int = Query(default=5),

    # Specialized knobs
    lvg_min_pr_chg: float = Query(default=1.0),
    rvol_min_turnover: float = Query(default=5.0),

    # toptrend knobs
    tt_min_price: float = Query(default=5.0),
    tt_min_4wk_chg: float = Query(default=5.0),
    tt_min_13wk_ma_chg: float = Query(default=10.0),
    tt_min_range_pct: float = Query(default=1.0),
):
    rpt = rpt.strip().lower()
    if rpt not in REPORTS:
        raise HTTPException(
            status_code=400,
            detail={"request_id": request.state.request_id, "error": "unknown_report", "rpt": rpt, "allowed": sorted(REPORTS.keys())},
        )

    ex = _norm_exchange(exchange)
    engine = get_engine()
    rep = REPORTS[rpt]

    sql_base, params, order_by, _default_limit = rep.build(
        exchange=ex,
        weekdate=None,
        start=start,
        end=end,
        include_mast=include_mast,
        # common
        min_price=min_price,
        min_vol=min_vol,
        min_rsi=min_rsi,
        vol_scale=vol_scale,
        # caps/age
        min_cap_mil=min_cap_mil,
        min_mt_cnt=min_mt_cnt,
        # specialized
        min_pr_chg=lvg_min_pr_chg,
        min_turnover=rvol_min_turnover,
        # toptrend
        min_4wk_chg=tt_min_4wk_chg,
        min_13wk_ma_chg=tt_min_13wk_ma_chg,
        min_range_pct=tt_min_range_pct,
    )

    if rpt == "toptrend":
        params["min_price"] = float(tt_min_price)

    sql = text(f"{sql_base}{order_by} LIMIT :limit")
    params["limit"] = int(limit)

    try:
        with engine.connect() as conn:
            rows = conn.execute(sql, params).mappings().all()
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail={"request_id": request.state.request_id, "error": "db_query_failed", "message": str(e)},
        )

    flat = [dict(r) for r in rows]
    for d in flat:
        d["symbol_exchange"] = f'{d["symbol"]}-{d["exchange"]}'

    if not group_by_week:
        return {
            "request_id": request.state.request_id,
            "rpt": rpt,
            "name": rep.name,
            "exchange": ex,
            "start": start,
            "end": end,
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
        "rpt": rpt,
        "name": rep.name,
        "exchange": ex,
        "start": start,
        "end": end,
        "week_count": len(weeks),
        "count": len(flat),
        "weeks": weeks,
        "note": "Grouped by weekdate; ordering is report-specific.",
    }
