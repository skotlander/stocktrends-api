# routers/stocktrends_portfolios.py

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Path, Request
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


def _to_int(value: Any) -> int | None:
    if value is None:
        return None
    return int(value)


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


def _raise_db_query_failed(request: Request, exc: Exception) -> None:
    logger.exception(
        "Stock Trends portfolio metadata query failed; request_id=%s",
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
