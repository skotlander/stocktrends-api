# routers/meta.py

from __future__ import annotations

from fastapi import APIRouter, Request

from discovery.inference_semantics import inference_contract, stim_provider_profile
from discovery.provenance import (
    INDICATORS_PROVENANCE_TEXT,
    STIM_PROVENANCE_TEXT,
    data_provenance,
    provenance_reference,
)
from routers.signals import VALID_EXCHANGES

router = APIRouter(prefix="/meta", tags=["meta"])


@router.get("/indicators")
def meta_indicators(request: Request):
    """
    Canonical definitions for Stock Trends fields used across endpoints.
    Intended for bots/agents so they don't need to scrape the website.
    """
    return {
        "request_id": getattr(request.state, "request_id", None),
        "provenance_summary": INDICATORS_PROVENANCE_TEXT,
        "data_provenance": data_provenance(),
        "exchanges": {
            "allowed": sorted(list(VALID_EXCHANGES)),
            "meaning": {
                "T": "Toronto Stock Exchange (TSX)",
                "N": "New York Stock Exchange (NYSE)",
                "Q": "NASDAQ",
                "A": "AMEX",
                "B": "Other/alternate US listing bucket (if used in your DB)",
                "I": "Index/indicator bucket (if used in your DB)",
            },
            "note": "Exact exchange meanings can be customized to your production mapping.",
        },
        "fields": {
            "trend": {
                "type": "enum",
                "allowed": ["^+", "^-", "v^", "v+", "v-", "^v", "--", "="],
                "meaning": {
                    "^+": "Bullish (strong bullish trend)",
                    "^-": "Weak Bullish (bullish but weakening; often precedes bearish crossover)",
                    "v^": "Bullish Crossover (13wk MA crosses above 40wk MA)",
                    "v+": "Weak Bearish (bearish but recovering; often precedes bullish crossover)",
                    "v-": "Bearish (strong bearish trend)",
                    "^v": "Bearish Crossover (13wk MA crosses below 40wk MA)",
                    "--": "No trend / not classified",
                    "=": "Neutral / unchanged (if used)",
                },
                "note": "Trend is derived from price vs 13wk and 40wk moving averages per Stock Trends methodology.",
            },
            "trend_cnt": {
                "type": "int",
                "meaning": "Number of consecutive weeks the instrument has had the current trend code.",
            },
            "mt_cnt": {
                "type": "int",
                "meaning": "Number of consecutive weeks in the current major trend category (Bullish bucket vs Bearish bucket).",
                "bullish_bucket": ["^+", "^-", "v^"],
                "bearish_bucket": ["v-", "v+", "^v"],
            },
            "prev_mtcnt": {
                "type": "int",
                "meaning": "Previous week's mt_cnt (primarily meaningful on crossover weeks when mt_cnt resets).",
            },
            "rsi": {
                "type": "int",
                "meaning": "13-week relative strength index vs benchmark (S&P 500) where >100 implies outperformance.",
            },
            "rsi_updn": {
                "type": "enum",
                "allowed": ["+", "-", "0", ""],
                "meaning": {
                    "+": "Outperformed benchmark this week",
                    "-": "Underperformed benchmark this week",
                    "0": "Roughly flat vs benchmark this week",
                    "": "Not available / missing",
                },
            },
            "vol_tag": {
                "type": "enum",
                "allowed": ["", "!", "!!", "*", "**", "~"],
                "meaning": {
                    "": "No volume tag / normal",
                    "!": "Unusually low volume (low)",
                    "!!": "Unusually low volume (very low) — treated as low",
                    "*": "Unusually high volume (high)",
                    "**": "Unusually high volume (very high) — treated as high",
                    "~": "Special/other tag (if used in your dataset)",
                },
                "equivalences": {
                    "low": ["!", "!!"],
                    "high": ["*", "**"],
                },
            },
            "fpr_chg4/13/40": {
                "type": "float",
                "meaning": "Forward price return (%) after subsequent 4/13/40-week periods (historical realized).",
                "note": "Used for evaluating ST-IM distributions; may be NULL where forward window not available.",
            },
        },
        "links": {
            "human_guides": [
                "https://stocktrends.com/learn/stock-trends-guides",
                "https://stocktrends.com/component/strategy/stdata_layout",
                "https://stocktrends.com/learn/stock-trends-handbook",
            ],
            "machine_guides": [
                "/v1/openapi.json",
                "/v1/meta/inference",
                "/v1/meta/indicators",
            ],
        },
        "cognition_context": {
            "signal_source_role": (
                "Stock Trends classifications convert raw weekly market behavior into "
                "structured, repeatable signal states that can be consumed by multiple "
                "inference providers."
            ),
            "provenance_reference": provenance_reference(),
            "inference_contract": "/v1/meta/inference",
            "current_baseline_provider": "/v1/meta/stim",
        },
    }


@router.get("/inference")
def meta_inference(request: Request):
    """
    Provider-agnostic Stock Trends inference and cognition contract.

    This is the reusable contract for API discovery, OpenAPI extensions,
    x402/MPP metadata, and future MCP reasoning tools. Provider-specific
    profiles, including ST-IM, hang off this contract.
    """
    return {
        "request_id": getattr(request.state, "request_id", None),
        **inference_contract(),
    }


@router.get("/stim")
def meta_stim(request: Request):
    """
    ST-IM provider profile and distribution metadata.
    """
    profile = stim_provider_profile()
    return {
        "request_id": getattr(request.state, "request_id", None),
        "inference_contract": "/v1/meta/inference",
        "description": STIM_PROVENANCE_TEXT,
        "data_provenance": data_provenance(),
        "inference_provider": {
            "provider_id": profile["provider_id"],
            "provider_name": profile["provider_name"],
            "provider_role": profile["provider_role"],
            "not_final_intelligence_layer": profile["not_final_intelligence_layer"],
            "architecture_source": profile["architecture_source"],
        },
        "base_period_mean_returns_pct": profile["base_period_mean_returns_pct"],
        "forecast_horizons": profile["forecast_horizons"],
        "columns": {
            "x4wk1/x13wk1/x40wk1": "Lower confidence interval bound for mean return (%)",
            "x4wk2/x13wk2/x40wk2": "Upper confidence interval bound for mean return (%)",
            "x4wk/x13wk/x40wk": "Midpoint / mean estimate (%)",
            "x4wksd/x13wksd/x40wksd": "Std dev of assumed normal distribution (%)",
        },
        "classification_role": profile["classification_role"],
        "randomness_assumptions": profile["randomness_assumptions"],
        "distribution_framing": profile["distribution_framing"],
        "probability_interpretation": profile["probability_interpretation"],
        "stim_select": profile["stim_select"],
        "strengths": profile["strengths"],
        "portfolio_applications": profile["portfolio_applications"],
        "limitations": profile["limitations"],
        "future_provider_compatibility": (
            "ST-IM is the current baseline inference provider. Future Causal AI "
            "providers should integrate through /v1/meta/inference rather than "
            "forcing all cognition surfaces into ST-IM-specific fields."
        ),
        "missing_data_note": "If a symbol has no ST-IM estimate for a weekdate, the provider could not estimate reliably due to insufficient samples.",
    }


@router.get("/stwr")
def meta_stwr(request: Request):
    """
    Report codes and what each report generally represents.
    """
    return {
        "request_id": getattr(request.state, "request_id", None),
        "endpoint_family": "/v1/stwr/reports/latest and /v1/stwr/reports/history",
        "reports": [
            {"code": "pw", "name": "Picks of the Week", "hint": "Bullish xover or weak bearish + RSI+/volume rules"},
            {"code": "select", "name": "ST-IM Select stocks of the Week", "hint": "Published select = ST-IM probability and confidence-interval criteria"},
            {"code": "toptrend", "name": "Top Trending", "hint": "Bullish bucket + momentum filters"},
            {"code": "bullcross", "name": "Bullish Crossovers", "hint": "trend=v^"},
            {"code": "bearcross", "name": "Bearish Crossovers", "hint": "trend=^v"},
            # You can expand this list as you add report builders
        ],
        "note": "This is summary metadata; the authoritative logic lives in the report SQL builders.",
    }
