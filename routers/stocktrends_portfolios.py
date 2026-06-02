# routers/stocktrends_portfolios.py

from __future__ import annotations

import logging
from datetime import date
from typing import Any

from fastapi import APIRouter, HTTPException, Path, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import text

from db import get_engine


logger = logging.getLogger("stocktrends_api.stocktrends_portfolios")
router = APIRouter(prefix="/stocktrends/portfolios", tags=["stocktrends-portfolios"])


class StockTrendsPortfolioMetadata(BaseModel):
    port_id: int = Field(..., description="Official Stock Trends portfolio identifier.")
    name: str | None = Field(default=None, description="Official Stock Trends portfolio name.")
    strategy_id: int | None = Field(default=None, description="Legacy Stock Trends strategy identifier.")
    exchanges: str | None = Field(default=None, description="Exchange universe configured for the portfolio.")
    selection_universe: str | None = Field(
        default=None,
        description=(
            "Stock Trends strategy selection/filter universe from stp_ports.index_symbols. "
            "This is not benchmark metadata."
        ),
    )
    description: str | None = Field(default=None, description="Portfolio description.")
    status: int = Field(..., description="Legacy live/test status. Public endpoints expose only status=1.")


class StockTrendsPortfolioListResponse(BaseModel):
    request_id: str
    count: int
    data: list[StockTrendsPortfolioMetadata]


class StockTrendsPortfolioDetailResponse(BaseModel):
    request_id: str
    data: StockTrendsPortfolioMetadata


class StockTrendsPortfolioReturnsPortfolio(BaseModel):
    port_id: int = Field(..., description="Official Stock Trends portfolio identifier.")
    name: str | None = Field(default=None, description="Official Stock Trends portfolio name.")
    selection_universe: str | None = Field(
        default=None,
        description="Stock Trends strategy selection/filter universe configured for the portfolio.",
    )


class StockTrendsPortfolioReturnPoint(BaseModel):
    weekdate: str = Field(..., description="Week-ending date for the portfolio return observation.")
    buys: int | None = Field(
        default=None,
        description="Official portfolio buy activity recorded for the week.",
    )
    sells: int | None = Field(
        default=None,
        description="Official portfolio sell activity recorded for the week.",
    )
    held: int | None = Field(
        default=None,
        description="Official portfolio held count recorded for the week.",
    )
    net_proceeds: float | None = Field(
        default=None,
        description="Official net proceeds recorded for the portfolio observation.",
    )
    realized_gain: float | None = Field(
        default=None,
        description="Official realized gain for the portfolio observation.",
    )
    cumulative_realized_gain: float | None = Field(
        default=None,
        description="Official cumulative realized gain through the observation week.",
    )
    total_valuation: float | None = Field(
        default=None,
        description="Official total portfolio valuation for the observation week.",
    )
    unrealized_gain: float | None = Field(
        default=None,
        description="Official unrealized gain for the portfolio observation.",
    )
    cumulative_total_gain: float | None = Field(
        default=None,
        description="Official cumulative total gain through the observation week.",
    )
    tsx_index: float | None = Field(
        default=None,
        description="TSX index reference value recorded with the portfolio observation.",
    )
    sp_index: float | None = Field(
        default=None,
        description="S&P index reference value recorded with the portfolio observation.",
    )


class StockTrendsPortfolioReturnsResponse(BaseModel):
    request_id: str
    port_id: int
    portfolio: StockTrendsPortfolioReturnsPortfolio
    count: int
    returns: list[StockTrendsPortfolioReturnPoint]


class StockTrendsPortfolioClosedPosition(BaseModel):
    position_id: int = Field(..., description="Official Stock Trends closed-position identifier.")
    symbol: str | None = Field(default=None, description="Security ticker symbol.")
    exchange: str | None = Field(default=None, description="Stock Trends exchange code.")
    name: str | None = Field(default=None, description="Security name.")
    date_in: str | None = Field(default=None, description="Position entry date.")
    price_in: float | None = Field(default=None, description="Position entry price.")
    qty: int | None = Field(default=None, description="Position share quantity.")
    transaction_cost_in: float | None = Field(default=None, description="Entry transaction cost.")
    cost_adjustments: float | None = Field(default=None, description="Entry-side cost adjustments.")
    total_cost: float | None = Field(default=None, description="Total entry cost.")
    stop_loss: float | None = Field(default=None, description="Recorded stop-loss level.")
    date_out: str | None = Field(default=None, description="Position close date.")
    weeks_held: int | None = Field(default=None, description="Number of weeks the position was held.")
    sell_trigger: str = Field(..., description="Historical close trigger. Empty live-position triggers are excluded.")
    price_out: float | None = Field(default=None, description="Position close price.")
    transaction_cost_out: float | None = Field(default=None, description="Exit transaction cost.")
    sell_adjustments: float | None = Field(default=None, description="Exit-side sale adjustments.")
    total_proceeds: float | None = Field(default=None, description="Total close proceeds.")
    gain_loss: float | None = Field(default=None, description="Realized gain or loss.")
    gain_loss_percent: float | None = Field(default=None, description="Realized gain or loss percentage.")
    weekdate: str | None = Field(default=None, description="Week-ending date associated with the closed position.")


class StockTrendsPortfolioClosedPositionsResponse(BaseModel):
    request_id: str
    port_id: int
    portfolio: StockTrendsPortfolioReturnsPortfolio
    count: int
    positions: list[StockTrendsPortfolioClosedPosition]


class StockTrendsPortfolioSummaryReturns(BaseModel):
    count: int = Field(..., description="Number of public return observations summarized.")
    first_weekdate: str | None = Field(default=None, description="Earliest public return observation weekdate.")
    latest_weekdate: str | None = Field(default=None, description="Latest public return observation weekdate.")
    latest_total_valuation: float | None = Field(
        default=None,
        description="Latest official total portfolio valuation in the summarized public return history.",
    )
    latest_cumulative_total_gain: float | None = Field(
        default=None,
        description="Latest official cumulative total gain in the summarized public return history.",
    )
    latest_cumulative_realized_gain: float | None = Field(
        default=None,
        description="Latest official cumulative realized gain in the summarized public return history.",
    )


class StockTrendsPortfolioSummaryClosedPositions(BaseModel):
    count: int = Field(..., description="Number of closed historical positions summarized.")
    first_date_in: str | None = Field(default=None, description="Earliest entry date among closed historical positions.")
    first_date_out: str | None = Field(default=None, description="Earliest close date among closed historical positions.")
    latest_date_out: str | None = Field(default=None, description="Latest close date among closed historical positions.")
    total_realized_gain_loss: float = Field(
        ...,
        description="Total realized gain or loss across closed historical positions.",
    )
    average_gain_loss_percent: float | None = Field(
        default=None,
        description="Average realized gain/loss percentage across closed historical positions.",
    )
    winning_positions: int = Field(..., description="Closed positions with positive realized gain/loss.")
    losing_positions: int = Field(..., description="Closed positions with negative realized gain/loss.")


class StockTrendsPortfolioSummaryVerification(BaseModel):
    returns_endpoint: str = Field(..., description="Public endpoint for portfolio return-history evidence.")
    historical_positions_endpoint: str = Field(
        ...,
        description="Public endpoint for closed historical position evidence.",
    )
    current_live_holdings_excluded: bool = Field(
        ...,
        description="True when current live holdings are intentionally excluded from the public summary.",
    )


class StockTrendsPortfolioHistorySummary(BaseModel):
    returns: StockTrendsPortfolioSummaryReturns
    closed_positions: StockTrendsPortfolioSummaryClosedPositions
    verification: StockTrendsPortfolioSummaryVerification


class StockTrendsPortfolioSummaryResponse(BaseModel):
    request_id: str
    port_id: int
    portfolio: StockTrendsPortfolioReturnsPortfolio
    summary: StockTrendsPortfolioHistorySummary


def _to_int(value: Any) -> int | None:
    if value is None:
        return None
    return int(value)


def _to_float(value: Any) -> float | None:
    if value is None:
        return None
    return float(value)


def _to_int_or_zero(value: Any) -> int:
    if value is None:
        return 0
    return int(value)


def _to_float_or_zero(value: Any) -> float:
    if value is None:
        return 0.0
    return float(value)


def _to_date_string(value: Any) -> str | None:
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def _row_to_portfolio(row: Any) -> dict[str, Any]:
    data = dict(row)
    return {
        "port_id": int(data["port_id"]),
        "name": data.get("name"),
        "strategy_id": _to_int(data.get("strategy_id")),
        "exchanges": data.get("exchanges"),
        "selection_universe": data.get("index_symbols"),
        "description": data.get("description"),
        "status": int(data["status"]),
    }


def _row_to_portfolio_summary(row: Any) -> dict[str, Any]:
    portfolio = _row_to_portfolio(row)
    return {
        "port_id": portfolio["port_id"],
        "name": portfolio["name"],
        "selection_universe": portfolio["selection_universe"],
    }


def _row_to_return_point(row: Any) -> dict[str, Any]:
    data = dict(row)
    weekdate = data.get("weekdate")
    return {
        "weekdate": weekdate.isoformat() if hasattr(weekdate, "isoformat") else str(weekdate),
        "buys": _to_int(data.get("buys")),
        "sells": _to_int(data.get("sells")),
        "held": _to_int(data.get("held")),
        "net_proceeds": _to_float(data.get("net_proceeds")),
        "realized_gain": _to_float(data.get("realizedgain")),
        "cumulative_realized_gain": _to_float(data.get("cum_realizedgain")),
        "total_valuation": _to_float(data.get("totalvaluation")),
        "unrealized_gain": _to_float(data.get("unrealizedgain")),
        "cumulative_total_gain": _to_float(data.get("cum_totalgain")),
        "tsx_index": _to_float(data.get("tsxindex")),
        "sp_index": _to_float(data.get("spindex")),
    }


def _row_to_closed_position(row: Any) -> dict[str, Any]:
    data = dict(row)
    return {
        "position_id": int(data["position_id"]),
        "symbol": data.get("symbol"),
        "exchange": data.get("exchange"),
        "name": data.get("name"),
        "date_in": _to_date_string(data.get("date_in")),
        "price_in": _to_float(data.get("price_in")),
        "qty": _to_int(data.get("qty")),
        "transaction_cost_in": _to_float(data.get("trcost_in")),
        "cost_adjustments": _to_float(data.get("cost_adjs")),
        "total_cost": _to_float(data.get("total_cost")),
        "stop_loss": _to_float(data.get("stop_loss")),
        "date_out": _to_date_string(data.get("date_out")),
        "weeks_held": _to_int(data.get("weeks_held")),
        "sell_trigger": str(data["sell_trigger"]),
        "price_out": _to_float(data.get("price_out")),
        "transaction_cost_out": _to_float(data.get("trcost_out")),
        "sell_adjustments": _to_float(data.get("sell_adjs")),
        "total_proceeds": _to_float(data.get("total_proceeds")),
        "gain_loss": _to_float(data.get("gain_loss")),
        "gain_loss_percent": _to_float(data.get("gl_percent")),
        "weekdate": _to_date_string(data.get("weekdate")),
    }


def _rows_to_history_summary(
    returns_summary_row: Any,
    returns_latest_row: Any,
    closed_positions_summary_row: Any,
    *,
    port_id: int,
) -> dict[str, Any]:
    returns_summary = dict(returns_summary_row or {})
    returns_latest = dict(returns_latest_row or {})
    closed_summary = dict(closed_positions_summary_row or {})

    return {
        "returns": {
            "count": _to_int_or_zero(returns_summary.get("return_count")),
            "first_weekdate": _to_date_string(returns_summary.get("first_weekdate")),
            "latest_weekdate": _to_date_string(returns_summary.get("latest_weekdate")),
            "latest_total_valuation": _to_float(returns_latest.get("totalvaluation")),
            "latest_cumulative_total_gain": _to_float(returns_latest.get("cum_totalgain")),
            "latest_cumulative_realized_gain": _to_float(returns_latest.get("cum_realizedgain")),
        },
        "closed_positions": {
            "count": _to_int_or_zero(closed_summary.get("closed_position_count")),
            "first_date_in": _to_date_string(closed_summary.get("first_date_in")),
            "first_date_out": _to_date_string(closed_summary.get("first_date_out")),
            "latest_date_out": _to_date_string(closed_summary.get("latest_date_out")),
            "total_realized_gain_loss": _to_float_or_zero(closed_summary.get("total_realized_gain_loss")),
            "average_gain_loss_percent": _to_float(closed_summary.get("average_gain_loss_percent")),
            "winning_positions": _to_int_or_zero(closed_summary.get("winning_positions")),
            "losing_positions": _to_int_or_zero(closed_summary.get("losing_positions")),
        },
        "verification": {
            "returns_endpoint": f"/v1/stocktrends/portfolios/{port_id}/returns",
            "historical_positions_endpoint": f"/v1/stocktrends/portfolios/{port_id}/positions/history",
            "current_live_holdings_excluded": True,
        },
    }


def _raise_db_query_failed(request: Request, exc: Exception) -> None:
    logger.exception(
        "Stock Trends portfolio query failed; request_id=%s",
        request.state.request_id,
        exc_info=True,
    )
    raise HTTPException(
        status_code=500,
        detail={
            "request_id": request.state.request_id,
            "error": "db_query_failed",
            "message": "Database query failed.",
        },
    )


@router.get(
    "",
    response_model=StockTrendsPortfolioListResponse,
    summary="List official Stock Trends model portfolios",
    description=(
        "Official Stock Trends model portfolios. "
        "Returns live official Stock Trends model portfolio metadata from stp_ports. "
        "Only status=1 portfolios are exposed. These are not user-created or "
        "decision-engine portfolios."
    ),
)
def list_stocktrends_portfolios(request: Request):
    sql = text(
        """
        SELECT
            port_id,
            name,
            strategy_id,
            exchanges,
            index_symbols,
            description,
            status
        FROM stp_ports
        WHERE status = 1
        ORDER BY port_id ASC
        """
    )

    engine = get_engine()
    try:
        with engine.connect() as conn:
            rows = conn.execute(sql).mappings().all()
    except Exception as exc:
        _raise_db_query_failed(request, exc)

    data = [_row_to_portfolio(row) for row in rows]
    return {
        "request_id": request.state.request_id,
        "count": len(data),
        "data": data,
    }


@router.get(
    "/{port_id}",
    response_model=StockTrendsPortfolioDetailResponse,
    summary="Get official Stock Trends model portfolio metadata",
    description=(
        "Official Stock Trends model portfolio. "
        "Returns metadata for one live official Stock Trends model portfolio from "
        "stp_ports. Only status=1 portfolios are exposed; nonexistent or inactive "
        "portfolio IDs return 404."
    ),
)
def get_stocktrends_portfolio(
    request: Request,
    port_id: int = Path(..., ge=1, description="Official Stock Trends portfolio identifier."),
):
    sql = text(
        """
        SELECT
            port_id,
            name,
            strategy_id,
            exchanges,
            index_symbols,
            description,
            status
        FROM stp_ports
        WHERE port_id = :port_id
          AND status = 1
        LIMIT 1
        """
    )

    engine = get_engine()
    try:
        with engine.connect() as conn:
            row = conn.execute(sql, {"port_id": port_id}).mappings().first()
    except Exception as exc:
        _raise_db_query_failed(request, exc)

    if not row:
        raise HTTPException(
            status_code=404,
            detail={
                "request_id": request.state.request_id,
                "error": "portfolio_not_found",
                "port_id": port_id,
            },
        )

    return {
        "request_id": request.state.request_id,
        "data": _row_to_portfolio(row),
    }


@router.get(
    "/{port_id}/returns",
    response_model=StockTrendsPortfolioReturnsResponse,
    summary="Get official Stock Trends portfolio returns history",
    description=(
        "Official Stock Trends portfolio returns history. "
        "Returns chronological performance observations for one live official "
        "Stock Trends model portfolio. Only status=1 portfolios are exposed; "
        "nonexistent or inactive portfolio IDs return 404."
    ),
)
def get_stocktrends_portfolio_returns(
    request: Request,
    port_id: int = Path(..., ge=1, description="Official Stock Trends portfolio identifier."),
    start_date: date | None = Query(
        default=None,
        description="Inclusive start weekdate filter in YYYY-MM-DD format.",
    ),
    end_date: date | None = Query(
        default=None,
        description="Inclusive end weekdate filter in YYYY-MM-DD format.",
    ),
):
    portfolio_sql = text(
        """
        SELECT
            port_id,
            name,
            strategy_id,
            exchanges,
            index_symbols,
            description,
            status
        FROM stp_ports
        WHERE port_id = :port_id
          AND status = 1
        LIMIT 1
        """
    )

    returns_where = ["port_id = :port_id"]
    returns_params: dict[str, Any] = {"port_id": port_id}
    if start_date is not None:
        returns_where.append("weekdate >= :start_date")
        returns_params["start_date"] = start_date
    if end_date is not None:
        returns_where.append("weekdate <= :end_date")
        returns_params["end_date"] = end_date

    returns_sql = text(
        f"""
        SELECT
            weekdate,
            buys,
            sells,
            held,
            net_proceeds,
            realizedgain,
            cum_realizedgain,
            totalvaluation,
            unrealizedgain,
            cum_totalgain,
            tsxindex,
            spindex
        FROM stp_returnslog
        WHERE {" AND ".join(returns_where)}
        ORDER BY weekdate ASC
        """
    )

    engine = get_engine()
    try:
        with engine.connect() as conn:
            portfolio_row = conn.execute(portfolio_sql, {"port_id": port_id}).mappings().first()
            if not portfolio_row:
                raise HTTPException(
                    status_code=404,
                    detail={
                        "request_id": request.state.request_id,
                        "error": "portfolio_not_found",
                        "port_id": port_id,
                    },
                )
            return_rows = conn.execute(returns_sql, returns_params).mappings().all()
    except HTTPException:
        raise
    except Exception as exc:
        _raise_db_query_failed(request, exc)

    returns = [_row_to_return_point(row) for row in return_rows]
    return {
        "request_id": request.state.request_id,
        "port_id": int(port_id),
        "portfolio": _row_to_portfolio_summary(portfolio_row),
        "count": len(returns),
        "returns": returns,
    }


@router.get(
    "/{port_id}/summary",
    response_model=StockTrendsPortfolioSummaryResponse,
    summary="Get official Stock Trends portfolio public history summary",
    description=(
        "Official Stock Trends portfolio public history summary. "
        "Summarizes public metadata, returns history, and closed historical "
        "positions for one live official Stock Trends model portfolio. Current "
        "live holdings are intentionally excluded. Optional start_date and "
        "end_date filters apply to return weekdates and closed-position close dates."
    ),
)
def get_stocktrends_portfolio_summary(
    request: Request,
    port_id: int = Path(..., ge=1, description="Official Stock Trends portfolio identifier."),
    start_date: date | None = Query(
        default=None,
        description=(
            "Inclusive start date in YYYY-MM-DD format. Applies to return "
            "weekdates and closed-position close dates."
        ),
    ),
    end_date: date | None = Query(
        default=None,
        description=(
            "Inclusive end date in YYYY-MM-DD format. Applies to return "
            "weekdates and closed-position close dates."
        ),
    ),
):
    portfolio_sql = text(
        """
        SELECT
            port_id,
            name,
            strategy_id,
            exchanges,
            index_symbols,
            description,
            status
        FROM stp_ports
        WHERE port_id = :port_id
          AND status = 1
        LIMIT 1
        """
    )

    returns_where = ["port_id = :port_id"]
    returns_params: dict[str, Any] = {"port_id": port_id}
    if start_date is not None:
        returns_where.append("weekdate >= :start_date")
        returns_params["start_date"] = start_date
    if end_date is not None:
        returns_where.append("weekdate <= :end_date")
        returns_params["end_date"] = end_date

    returns_summary_sql = text(
        f"""
        SELECT
            COUNT(*) AS return_count,
            MIN(weekdate) AS first_weekdate,
            MAX(weekdate) AS latest_weekdate
        FROM stp_returnslog
        WHERE {" AND ".join(returns_where)}
        """
    )

    returns_latest_sql = text(
        f"""
        SELECT
            weekdate,
            totalvaluation,
            cum_totalgain,
            cum_realizedgain
        FROM stp_returnslog
        WHERE {" AND ".join(returns_where)}
        ORDER BY weekdate DESC
        LIMIT 1
        """
    )

    positions_where = [
        "port_id = :port_id",
        "sell_trigger IS NOT NULL",
        "sell_trigger <> ''",
    ]
    positions_params: dict[str, Any] = {"port_id": port_id}
    if start_date is not None:
        positions_where.append("date_out >= :start_date")
        positions_params["start_date"] = start_date
    if end_date is not None:
        positions_where.append("date_out <= :end_date")
        positions_params["end_date"] = end_date

    closed_positions_summary_sql = text(
        f"""
        SELECT
            COUNT(*) AS closed_position_count,
            MIN(date_in) AS first_date_in,
            MIN(date_out) AS first_date_out,
            MAX(date_out) AS latest_date_out,
            SUM(gain_loss) AS total_realized_gain_loss,
            AVG(gl_percent) AS average_gain_loss_percent,
            SUM(CASE WHEN gain_loss > 0 THEN 1 ELSE 0 END) AS winning_positions,
            SUM(CASE WHEN gain_loss < 0 THEN 1 ELSE 0 END) AS losing_positions
        FROM stp_positions
        WHERE {" AND ".join(positions_where)}
        """
    )

    engine = get_engine()
    try:
        with engine.connect() as conn:
            portfolio_row = conn.execute(portfolio_sql, {"port_id": port_id}).mappings().first()
            if not portfolio_row:
                raise HTTPException(
                    status_code=404,
                    detail={
                        "request_id": request.state.request_id,
                        "error": "portfolio_not_found",
                        "port_id": port_id,
                    },
                )

            returns_summary_row = conn.execute(returns_summary_sql, returns_params).mappings().first()
            returns_latest_row = conn.execute(returns_latest_sql, returns_params).mappings().first()
            closed_positions_summary_row = conn.execute(
                closed_positions_summary_sql,
                positions_params,
            ).mappings().first()
    except HTTPException:
        raise
    except Exception as exc:
        _raise_db_query_failed(request, exc)

    return {
        "request_id": request.state.request_id,
        "port_id": int(port_id),
        "portfolio": _row_to_portfolio_summary(portfolio_row),
        "summary": _rows_to_history_summary(
            returns_summary_row,
            returns_latest_row,
            closed_positions_summary_row,
            port_id=int(port_id),
        ),
    }


@router.get(
    "/{port_id}/positions/history",
    response_model=StockTrendsPortfolioClosedPositionsResponse,
    summary="Get official Stock Trends historical closed positions",
    description=(
        "Official Stock Trends historical closed-position records for one live "
        "model portfolio. Current live holdings are intentionally excluded. "
        "Only status=1 portfolios are exposed; nonexistent or inactive portfolio "
        "IDs return 404."
    ),
)
def get_stocktrends_portfolio_positions_history(
    request: Request,
    port_id: int = Path(..., ge=1, description="Official Stock Trends portfolio identifier."),
    start_date: date | None = Query(
        default=None,
        description="Inclusive start close-date filter in YYYY-MM-DD format.",
    ),
    end_date: date | None = Query(
        default=None,
        description="Inclusive end close-date filter in YYYY-MM-DD format.",
    ),
):
    portfolio_sql = text(
        """
        SELECT
            port_id,
            name,
            strategy_id,
            exchanges,
            index_symbols,
            description,
            status
        FROM stp_ports
        WHERE port_id = :port_id
          AND status = 1
        LIMIT 1
        """
    )

    positions_where = [
        "port_id = :port_id",
        "sell_trigger IS NOT NULL",
        "sell_trigger <> ''",
    ]
    positions_params: dict[str, Any] = {"port_id": port_id}
    if start_date is not None:
        positions_where.append("date_out >= :start_date")
        positions_params["start_date"] = start_date
    if end_date is not None:
        positions_where.append("date_out <= :end_date")
        positions_params["end_date"] = end_date

    positions_sql = text(
        f"""
        SELECT
            position_id,
            symbol,
            exchange,
            name,
            date_in,
            price_in,
            qty,
            trcost_in,
            cost_adjs,
            total_cost,
            stop_loss,
            date_out,
            weeks_held,
            sell_trigger,
            price_out,
            trcost_out,
            sell_adjs,
            total_proceeds,
            gain_loss,
            gl_percent,
            weekdate
        FROM stp_positions
        WHERE {" AND ".join(positions_where)}
        ORDER BY date_out ASC, position_id ASC
        """
    )

    engine = get_engine()
    try:
        with engine.connect() as conn:
            portfolio_row = conn.execute(portfolio_sql, {"port_id": port_id}).mappings().first()
            if not portfolio_row:
                raise HTTPException(
                    status_code=404,
                    detail={
                        "request_id": request.state.request_id,
                        "error": "portfolio_not_found",
                        "port_id": port_id,
                    },
                )
            position_rows = conn.execute(positions_sql, positions_params).mappings().all()
    except HTTPException:
        raise
    except Exception as exc:
        _raise_db_query_failed(request, exc)

    positions = [_row_to_closed_position(row) for row in position_rows]
    return {
        "request_id": request.state.request_id,
        "port_id": int(port_id),
        "portfolio": _row_to_portfolio_summary(portfolio_row),
        "count": len(positions),
        "positions": positions,
    }
