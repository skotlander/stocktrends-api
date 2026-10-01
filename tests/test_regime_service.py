from datetime import date
from decimal import Decimal
from math import inf, nan

from services import market_semantics, regime_service


_WEEKDATE = date(2026, 9, 25)


def _classified_rows() -> list[dict[str, object]]:
    return [
        {"trend": "^+", "cnt": 3},
        {"trend": "^v", "cnt": 1},
    ]


def test_compute_regime_score_uses_classified_denominator():
    assert regime_service.compute_regime_score(_classified_rows()) == 0.5


def test_exact_neutral_trends_do_not_dilute_regime_score():
    rows = _classified_rows() + [
        {"trend": "--", "cnt": 10_000},
        {"trend": "=", "cnt": 20_000},
    ]

    assert regime_service.compute_regime_score(rows) == 0.5


def test_unknown_or_unclassified_trends_do_not_dilute_regime_score():
    rows = _classified_rows() + [
        {"trend": "", "cnt": 10_000},
        {"trend": None, "cnt": 20_000},
        {"trend": "unknown", "cnt": 30_000},
    ]

    assert regime_service.compute_regime_score(rows) == 0.5


def test_only_neutral_or_unclassified_rows_have_no_regime_score():
    rows = [
        {"trend": "--", "cnt": 10},
        {"trend": "=", "cnt": 10},
        {"trend": "", "cnt": 10},
        {"trend": None, "cnt": 10},
        {"trend": "unknown", "cnt": 10},
    ]

    assert regime_service.compute_regime_score(rows) is None


def test_compute_scores_by_week_inherits_classified_denominator():
    prior_week = date(2026, 9, 18)
    rows = [
        {"weekdate": _WEEKDATE, "trend": "^+", "cnt": 3},
        {"weekdate": _WEEKDATE, "trend": "^v", "cnt": 1},
        {"weekdate": _WEEKDATE, "trend": "--", "cnt": 100},
        {"weekdate": prior_week, "trend": "=", "cnt": 100},
    ]

    assert regime_service.compute_scores_by_week([_WEEKDATE, prior_week], rows) == [
        (_WEEKDATE, 0.5)
    ]


def test_regime_thresholds_are_preserved():
    assert regime_service.classify_regime(0.10) == "bullish"
    assert regime_service.classify_regime(0.0999) == "mixed"
    assert regime_service.classify_regime(-0.10) == "bearish"
    assert regime_service.classify_regime(-0.0999) == "mixed"


def test_regime_service_reexports_canonical_directional_trends():
    assert regime_service.BULLISH_TRENDS is market_semantics.BULLISH_TRENDS
    assert regime_service.BEARISH_TRENDS is market_semantics.BEARISH_TRENDS
    assert regime_service.BULLISH_TRENDS == frozenset({"^+", "^-", "v^"})
    assert regime_service.BEARISH_TRENDS == frozenset({"^v", "v+", "v-"})


def test_market_semantics_constants_match_the_canonical_contract():
    assert market_semantics.CANONICAL_EQUITY_TYPES == ("CS", "UN")
    assert market_semantics.CANONICAL_REPORTING_EXCHANGES == ("A", "N", "Q", "T")
    assert market_semantics.NEUTRAL_TRENDS == frozenset({"--", "="})
    assert market_semantics.CLASSIFIED_TRENDS == frozenset(
        {"^+", "^-", "v^", "^v", "v+", "v-"}
    )


def test_aggregate_rsi_validity_has_no_lower_bound():
    assert market_semantics.AGGREGATE_RSI_MAX_VALID == 10_000
    assert market_semantics.is_valid_aggregate_rsi(None) is False
    assert market_semantics.is_valid_aggregate_rsi(10_000) is True
    assert market_semantics.is_valid_aggregate_rsi(10_000.0001) is False
    assert market_semantics.is_valid_aggregate_rsi(Decimal("10000")) is True
    assert market_semantics.is_valid_aggregate_rsi(600) is True
    assert market_semantics.is_valid_aggregate_rsi(nan) is False
    assert market_semantics.is_valid_aggregate_rsi(inf) is False
    assert market_semantics.is_valid_aggregate_rsi(-inf) is False
    assert market_semantics.is_valid_aggregate_rsi(-10_000) is True
