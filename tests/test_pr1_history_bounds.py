"""
PR1 — agent-safe history defaults, and the workflow that consumes them.

The production case this exists to prevent
------------------------------------------
An external agent bought `GET /v1/breadth/sector/history` over x402 with an
empty query string and received HTTP 200 with 47,651,791 bytes in 8.47 seconds.
Nothing in the response told it the payload had been shaped by a 200000-row
ceiling, and nothing bounded the date range at all.

The invariant these tests hold:

    A bare request to a paid historical endpoint is bounded on purpose, and the
    bounds it received are reported back as machine-readable values.

Bounding is service shaping, so it must stay behind the payment execution
boundary. The settlement-ordering and pre-payment suites own that separation;
nothing here moves it.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from support.payment_harness import (
    payment_governed_routes,
    v1_path,
    x402_headers,
)

# Module stubs for sqlalchemy/db/etc. are provided by tests/conftest.py.
import main
import routers.breadth as breadth_router
import routers.leadership as leadership_router
import routers.selections as selections_router
import routers.selections_published as selections_published_router
import routers.stwr as stwr_router
from discovery.endpoint_metadata import get_endpoint_metadata
from routers.ai import ai_tools
from routers.workflows import WORKFLOW_ID_EXAMPLES, WORKFLOW_REGISTRY
from utils.history_bounds import (
    DEFAULT_HISTORY_WINDOW_WEEKS,
    HISTORY_ENDPOINT_BOUNDS,
    build_applied_bounds,
    probe_limit,
    resolve_history_window,
    split_probe_rows,
)

ANCHOR_WEEKDATE = "2026-08-28"


# ===========================================================================
# Recording engine
# ===========================================================================

class RecordingEngine:
    """
    An engine that records every statement and its bound parameters.

    Status codes cannot show that a window was applied — only the SQL and the
    parameters actually sent can. These tests therefore assert against the
    recorded statements rather than inferring bounding from a row count.
    """

    def __init__(self, responder=None):
        self.executed: list[tuple[str, dict[str, Any]]] = []
        self._responder = responder or self._default_responder

    # -- engine surface -----------------------------------------------------
    def connect(self):
        return self

    def begin(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def execute(self, statement, params=None):
        sql = str(statement)
        bound = dict(params or {})
        self.executed.append((sql, bound))
        return _Result(self._responder(sql, bound))

    # -- helpers ------------------------------------------------------------
    @staticmethod
    def _default_responder(sql: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        if "MAX(weekdate)" in sql:
            return [{"weekdate": ANCHOR_WEEKDATE, "wd": ANCHOR_WEEKDATE}]
        return []

    @property
    def data_statements(self) -> list[tuple[str, dict[str, Any]]]:
        """Every statement except the latest-weekdate anchor probe."""
        return [(sql, p) for sql, p in self.executed if "MAX(weekdate)" not in sql]

    def only_data_statement(self) -> tuple[str, dict[str, Any]]:
        statements = self.data_statements
        assert len(statements) == 1, f"expected one data query, got {len(statements)}"
        return statements[0]


class _Result:
    def __init__(self, rows: list[dict[str, Any]]):
        self._rows = rows

    def mappings(self):
        return self

    def all(self):
        return list(self._rows)

    def first(self):
        return self._rows[0] if self._rows else None


def breadth_rows(count: int) -> list[dict[str, Any]]:
    return [
        {
            "weekdate": f"2026-0{1 + (i % 8)}-0{1 + (i % 8)}",
            "sector_code": f"S{i % 11}",
            "sector_name": f"Sector {i % 11}",
            "total": 100,
            "bullish_count": 60,
            "bearish_count": 30,
            "neutral_count": 10,
            "avg_trend_cnt": 5.0,
            "avg_mt_cnt": 9.0,
            "max_trend_cnt": 40,
            "max_mt_cnt": 80,
            "avg_rsi": 104.0,
            "rsi_ge_110_count": 20,
            "rsi_ge_120_count": 5,
            "young_bullish_count": 12,
            "mature_bullish_count": 18,
        }
        for i in range(count)
    ]


@pytest.fixture
def anchored_engine(monkeypatch):
    """
    Install a recording engine on every router these tests drive.

    `sqlalchemy` is a MagicMock in this test environment, so `text()` would
    return an opaque mock and the recorded statement would be unreadable. It is
    replaced with the identity function: the routers build their SQL as strings
    either way, and this keeps the statement inspectable.
    """

    def _install(module, responder=None) -> RecordingEngine:
        engine = RecordingEngine(responder)
        monkeypatch.setattr(module, "get_engine", lambda: engine)
        monkeypatch.setattr(module, "text", lambda sql: sql)
        return engine

    return _install


@pytest.fixture
def client(payment_harness):
    """A TestClient over the real app with every economic side effect spied."""
    return payment_harness.client


# ===========================================================================
# 1. Cross-surface parity: one definition of every bound
# ===========================================================================

def test_bounds_table_openapi_and_discovery_registry_all_agree(client):
    """
    Runtime, OpenAPI and discovery metadata are three publications of the same
    number. They previously transcribed their own copies and drifted — the
    static manifest advertised 500 for a route enforcing 200000, and 52 for two
    routes enforcing 260 — so all three are now derived and checked together.
    """
    main.v1.openapi_schema = None
    schema = client.get("/v1/openapi.json").json()

    for path, bounds in HISTORY_ENDPOINT_BOUNDS.items():
        parameters = schema["paths"][path[len("/v1"):]]["get"].get("parameters", [])
        limit_param = next((p for p in parameters if p.get("name") == "limit"), None)
        assert limit_param is not None, f"{path} publishes no limit parameter"

        assert limit_param["schema"]["default"] == bounds["default_limit"], path
        assert limit_param["schema"]["maximum"] == bounds["max_limit"], path

        registry = get_endpoint_metadata(path, "GET") or {}
        published = (registry.get("optional_inputs", {}).get("limit") or {})
        assert published.get("safe_default") == bounds["default_limit"], path
        assert published.get("maximum") == bounds["max_limit"], path


def test_static_manifest_never_publishes_a_stale_history_default():
    """Where the hand-maintained manifest states a default, it must be the real one."""
    with open("static/tools.json", encoding="utf-8") as handle:
        manifest = json.load(handle)
    tools = {tool["path"]: tool for tool in manifest["tools"]}

    for path, bounds in HISTORY_ENDPOINT_BOUNDS.items():
        tool = tools.get(path[len("/v1"):])
        if tool is None:
            continue
        for param in tool.get("parameters", []):
            if param.get("name") != "limit":
                continue
            assert param.get("default") == bounds["default_limit"], (
                f"static/tools.json advertises limit default "
                f"{param.get('default')} for {path}, runtime enforces "
                f"{bounds['default_limit']}"
            )
            assert param.get("maximum") == bounds["max_limit"], path


# ===========================================================================
# 2. The window helper itself
# ===========================================================================

def test_default_window_applies_only_when_both_bounds_are_omitted():
    start, end, source = resolve_history_window(
        start=None, end=None, anchor_weekdate=ANCHOR_WEEKDATE
    )
    assert source == "default_trailing_window"
    assert end == ANCHOR_WEEKDATE
    assert start == "2024-09-06"  # 103 weeks back, inclusive of the anchor week


@pytest.mark.parametrize(
    "start,end",
    [("2020-01-03", None), (None, "2020-01-03"), ("2020-01-03", "2021-01-01")],
)
def test_explicitly_bounded_requests_keep_exactly_the_range_they_asked_for(start, end):
    got_start, got_end, source = resolve_history_window(
        start=start, end=end, anchor_weekdate=ANCHOR_WEEKDATE
    )
    assert (got_start, got_end) == (start, end)
    assert source == "caller_supplied"


def test_window_degrades_safely_when_there_is_no_data_to_anchor_on():
    start, end, source = resolve_history_window(
        start=None, end=None, anchor_weekdate=None
    )
    assert (start, end) == (None, None)
    assert source == "unbounded_no_anchor_weekdate"


def test_truncation_is_observed_rather_than_inferred():
    """
    A row count equal to the limit is ambiguous: the result may have been that
    size, or it may have been cut off. Fetching limit+1 removes the ambiguity.
    """
    assert probe_limit(10) == 11

    rows, truncated = split_probe_rows(list(range(11)), 10)
    assert truncated is True and len(rows) == 10

    rows, truncated = split_probe_rows(list(range(10)), 10)
    assert truncated is False and len(rows) == 10

    rows, truncated = split_probe_rows(list(range(3)), 10)
    assert truncated is False and len(rows) == 3


def test_applied_bounds_publishes_every_value_needed_to_widen_the_request():
    bounds = build_applied_bounds(
        start="2024-09-06",
        end=ANCHOR_WEEKDATE,
        window_source="default_trailing_window",
        limit=5000,
        limit_source="default",
        max_limit=500000,
        rows_returned=1144,
        truncated_by_limit=False,
        widen_with="prose",
    )
    assert set(bounds) == {
        "start",
        "end",
        "window_source",
        "default_window_weeks",
        "limit",
        "limit_source",
        "max_limit",
        "rows_returned",
        "truncated_by_limit",
        "widen_with",
    }
    # The prose hint must never be the only place a bound appears.
    machine_readable = {k: v for k, v in bounds.items() if k != "widen_with"}
    assert machine_readable["default_window_weeks"] == DEFAULT_HISTORY_WINDOW_WEEKS
    assert machine_readable["max_limit"] == 500000
    assert machine_readable["truncated_by_limit"] is False


# ===========================================================================
# 3. /v1/breadth/sector/history — the endpoint the production case hit
# ===========================================================================

def test_bare_breadth_history_request_is_bounded_on_both_axes(client, anchored_engine):
    engine = anchored_engine(breadth_router)

    response = client.get("/v1/breadth/sector/history", headers=x402_headers())

    assert response.status_code == 200
    sql, params = engine.only_data_statement()

    # A date window was applied even though the caller supplied none.
    assert params["start"] == "2024-09-06"
    assert params["end"] == ANCHOR_WEEKDATE
    assert "weekdate >= :start" in sql and "weekdate <= :end" in sql

    # And the row ceiling is the new default, not the old 200000.
    assert params["limit"] == probe_limit(5000)
    assert params["limit"] < 200000


def test_bare_breadth_history_can_no_longer_produce_the_observed_payload(
    client, anchored_engine
):
    """
    A row-count model of the production incident.

    ~47.6 MB came from roughly 10^5 rows: every week since 1980, times sectors,
    times exchanges. The bound now caps a bare request at 5000 rows, so that
    result set is unreachable without the caller explicitly asking for it.
    """
    anchored_engine(
        breadth_router,
        lambda sql, params: (
            [{"weekdate": ANCHOR_WEEKDATE, "wd": ANCHOR_WEEKDATE}]
            if "MAX(weekdate)" in sql
            else breadth_rows(params["limit"])
        ),
    )

    body = client.get("/v1/breadth/sector/history", headers=x402_headers()).json()

    observed_incident_rows = 100_000
    assert body["count"] == 5000
    assert body["count"] < observed_incident_rows / 10


def test_breadth_applied_bounds_reports_the_window_and_limit(client, anchored_engine):
    anchored_engine(
        breadth_router,
        lambda sql, params: (
            [{"weekdate": ANCHOR_WEEKDATE, "wd": ANCHOR_WEEKDATE}]
            if "MAX(weekdate)" in sql
            else breadth_rows(12)
        ),
    )

    bounds = client.get(
        "/v1/breadth/sector/history", headers=x402_headers()
    ).json()["applied_bounds"]

    assert bounds["start"] == "2024-09-06"
    assert bounds["end"] == ANCHOR_WEEKDATE
    assert bounds["window_source"] == "default_trailing_window"
    assert bounds["default_window_weeks"] == DEFAULT_HISTORY_WINDOW_WEEKS
    assert bounds["limit"] == 5000
    assert bounds["limit_source"] == "default"
    assert bounds["max_limit"] == 500000
    assert bounds["rows_returned"] == 12
    assert bounds["truncated_by_limit"] is False


def test_breadth_truncation_is_reported_to_the_caller(client, anchored_engine):
    """A caller that paid for the request must be able to see it was cut off."""
    anchored_engine(
        breadth_router,
        lambda sql, params: (
            [{"weekdate": ANCHOR_WEEKDATE, "wd": ANCHOR_WEEKDATE}]
            if "MAX(weekdate)" in sql
            else breadth_rows(params["limit"])  # limit + 1 rows available
        ),
    )

    bounds = client.get(
        "/v1/breadth/sector/history?limit=25", headers=x402_headers()
    ).json()["applied_bounds"]

    assert bounds["truncated_by_limit"] is True
    assert bounds["rows_returned"] == 25
    assert bounds["limit"] == 25
    assert bounds["limit_source"] == "caller_supplied"


def test_explicitly_bounded_breadth_request_is_unchanged(client, anchored_engine):
    engine = anchored_engine(breadth_router)

    response = client.get(
        "/v1/breadth/sector/history?start=2020-01-03&end=2020-12-25&limit=99",
        headers=x402_headers(),
    )

    assert response.status_code == 200
    _, params = engine.only_data_statement()
    assert params["start"] == "2020-01-03"
    assert params["end"] == "2020-12-25"
    assert params["limit"] == probe_limit(99)

    bounds = response.json()["applied_bounds"]
    assert bounds["window_source"] == "caller_supplied"
    assert bounds["limit_source"] == "caller_supplied"
    assert bounds["start"] == "2020-01-03"
    assert bounds["end"] == "2020-12-25"

    # An explicitly bounded request needs no anchor, so it must not pay for the
    # MAX(weekdate) lookup either: it issues exactly the queries it issued
    # before this bounding existed.
    assert engine.executed == engine.data_statements, (
        "an explicitly bounded request performed an unnecessary anchor lookup"
    )


# ---------------------------------------------------------------------------
# The all-exchange aggregation correction
# ---------------------------------------------------------------------------

def test_all_exchange_breadth_history_uses_the_raw_aggregation(client, anchored_engine):
    """
    st_sector_summary holds one row per (weekdate, sector, exchange, type) and
    the projection does not carry `ss.exchange`. Reading it without an exchange
    filter therefore emitted several unlabelled rows for the same
    (weekdate, sector_code) — the caller could not tell them apart, and could
    not aggregate them itself.

    An all-exchange request now aggregates st_data directly instead.
    """
    engine = anchored_engine(breadth_router)

    response = client.get("/v1/breadth/sector/history", headers=x402_headers())

    assert response.status_code == 200
    sql, _ = engine.only_data_statement()
    assert "st_sector_summary" not in sql
    assert "FROM st_data d" in sql


def test_all_exchange_breadth_cannot_emit_duplicate_weekdate_sector_rows(
    client, anchored_engine
):
    """
    Correctness is structural, not statistical: the query groups by
    (weekdate, sector), so at most one row per pair can exist. There is no
    intermediate per-exchange mean, and therefore no re-weighting step that
    could be got wrong — COUNT/SUM/AVG/MAX run once over the whole population.
    """
    engine = anchored_engine(breadth_router)

    client.get("/v1/breadth/sector/history", headers=x402_headers())

    sql, _ = engine.only_data_statement()
    normalized = " ".join(sql.split())
    assert "GROUP BY d.weekdate, s.sector_code, s.sector_name" in normalized
    # No exchange column is selected or grouped, because rows from every
    # exchange are folded into the one aggregate for that weekdate and sector.
    assert "d.exchange" not in normalized.split("GROUP BY")[0]


def test_all_exchange_history_matches_the_aggregation_latest_uses(
    client, anchored_engine
):
    """
    `/latest` and `/history` must mean the same thing by "all-exchange sector
    breadth". Both now build their aggregate from the same SQL builder.
    """
    history_engine = anchored_engine(breadth_router)
    client.get("/v1/breadth/sector/history", headers=x402_headers())
    history_sql, _ = history_engine.only_data_statement()

    latest_engine = anchored_engine(breadth_router)
    client.get("/v1/breadth/sector/latest", headers=x402_headers())
    latest_sql, _ = latest_engine.only_data_statement()

    def aggregate_expressions(sql: str) -> str:
        """
        The SELECT list only.

        The WHERE clauses differ by construction — /latest pins one weekdate
        while /history spans a range — so comparing them would prove nothing.
        What must match is the aggregate itself: the same COUNT/SUM/AVG/MAX
        expressions over the same grouping.
        """
        normalized = " ".join(sql.split())
        return normalized[normalized.index("SELECT"): normalized.index("FROM st_data")]

    assert aggregate_expressions(history_sql) == aggregate_expressions(latest_sql)

    # ...and the same grouping key, so neither can emit two rows per pair.
    for sql in (history_sql, latest_sql):
        assert "GROUP BY d.weekdate, s.sector_code, s.sector_name" in " ".join(sql.split())


def test_single_exchange_breadth_history_keeps_the_summary_fast_path(
    client, anchored_engine
):
    """
    Scoped to one exchange, the stored summary row *is* the aggregate over
    exactly the population requested, so the fast path stays available.
    """
    engine = anchored_engine(breadth_router)

    response = client.get(
        "/v1/breadth/sector/history?exchange=N", headers=x402_headers()
    )

    assert response.status_code == 200
    sql, params = engine.only_data_statement()
    assert "st_sector_summary" in sql
    assert params["exchange"] == "N"


# ===========================================================================
# 4. /v1/leadership/rotation/history — previously unbounded by construction
# ===========================================================================

def test_leadership_rotation_history_emits_a_real_sql_limit(client, anchored_engine):
    """
    This endpoint had no `limit` parameter and no LIMIT clause at all: its size
    was governed only by top_k multiplied by however many weeks existed.
    """
    engine = anchored_engine(leadership_router)

    response = client.get("/v1/leadership/rotation/history", headers=x402_headers())

    assert response.status_code == 200
    sql, params = engine.only_data_statement()
    assert "LIMIT :limit" in sql
    assert params["limit"] == probe_limit(2000)
    assert params["start"] == "2024-09-06"
    assert params["end"] == ANCHOR_WEEKDATE


def test_leadership_rotation_history_reports_applied_bounds(client, anchored_engine):
    anchored_engine(leadership_router)

    bounds = client.get(
        "/v1/leadership/rotation/history", headers=x402_headers()
    ).json()["applied_bounds"]

    assert bounds["window_source"] == "default_trailing_window"
    assert bounds["limit"] == 2000
    assert bounds["max_limit"] == 50000
    assert bounds["truncated_by_limit"] is False


def test_leadership_explicit_bounds_are_preserved(client, anchored_engine):
    engine = anchored_engine(leadership_router)

    client.get(
        "/v1/leadership/rotation/history?start=2019-01-04&limit=7",
        headers=x402_headers(),
    )

    _, params = engine.only_data_statement()
    assert params["start"] == "2019-01-04"
    assert "end" not in params
    assert params["limit"] == probe_limit(7)
    assert engine.executed == engine.data_statements, (
        "an explicitly bounded request performed an unnecessary anchor lookup"
    )


# ===========================================================================
# 5. /v1/stwr/reports/history — bounding plus the week-grouping correction
# ===========================================================================

def test_stwr_history_is_bounded(client, anchored_engine):
    engine = anchored_engine(stwr_router)

    response = client.get(
        "/v1/stwr/reports/history?rpt=bullcross&exchange=N", headers=x402_headers()
    )

    assert response.status_code == 200
    _, params = engine.only_data_statement()
    assert params["start"] == "2024-09-06"
    assert params["end"] == ANCHOR_WEEKDATE
    assert params["limit"] == probe_limit(500)


def test_stwr_history_orders_by_weekdate_before_the_report_ranking(
    client, anchored_engine
):
    """
    Report builders order purely by rank (`d.rsi DESC`, `mom_index DESC`, ...),
    which is right for the single-week `/latest` endpoint and wrong here: rows
    from every week interleave, so `limit` truncation cut an arbitrary
    cross-section instead of a time-ordered prefix.
    """
    engine = anchored_engine(stwr_router)

    client.get(
        "/v1/stwr/reports/history?rpt=bullcross&exchange=N", headers=x402_headers()
    )

    sql, _ = engine.only_data_statement()
    normalized = " ".join(sql.split())
    assert "ORDER BY d.weekdate ASC, d.rsi DESC, d.shortname ASC" in normalized


def test_stwr_week_ordering_helper_preserves_every_report_ranking():
    """The report's own ranking must survive intact as the secondary key."""
    for ranking in (
        " ORDER BY d.rsi DESC, d.shortname ASC",
        " ORDER BY mom_index DESC",
        " ORDER BY d.pr_change ASC, d.rsi ASC",
        " ORDER BY d.volume DESC",
    ):
        rewritten = stwr_router._week_primary_order(ranking)
        assert rewritten.strip().startswith("ORDER BY d.weekdate ASC,")
        assert ranking.replace("ORDER BY", "").strip() in rewritten


def test_stwr_grouped_output_cannot_contain_fragmented_week_buckets(
    client, anchored_engine
):
    """
    The bucketer closes a bucket only when the weekdate changes, so
    rank-interleaved rows produced the same weekdate as many separate buckets.
    With weekdate as the primary sort key, each weekdate can appear only once.
    """
    interleaved = [
        {"weekdate": wk, "symbol": sym, "exchange": "N", "volume": 10, "rsi": rsi}
        for wk, sym, rsi in [
            ("2026-08-14", "AAA", 130),
            ("2026-08-14", "BBB", 120),
            ("2026-08-21", "CCC", 118),
            ("2026-08-28", "DDD", 111),
            ("2026-08-28", "EEE", 105),
        ]
    ]
    anchored_engine(
        stwr_router,
        lambda sql, params: (
            [{"weekdate": ANCHOR_WEEKDATE, "wd": ANCHOR_WEEKDATE}]
            if "MAX(weekdate)" in sql
            else interleaved
        ),
    )

    body = client.get(
        "/v1/stwr/reports/history?rpt=bullcross&exchange=N", headers=x402_headers()
    ).json()

    weekdates = [week["weekdate"] for week in body["weeks"]]
    assert weekdates == sorted(set(weekdates)), (
        f"weekdate appears in more than one bucket: {weekdates}"
    )
    assert body["week_count"] == 3


# ===========================================================================
# 6. Endpoints that were already bounded gain disclosure only
# ===========================================================================

@pytest.mark.parametrize(
    "module,path,expected_limit,expected_max",
    [
        (selections_router, "/v1/selections/history", 520, 5200),
        (
            selections_published_router,
            "/v1/selections/published/history",
            5200,
            50000,
        ),
    ],
)
def test_already_bounded_selections_endpoints_keep_their_retrieval_semantics(
    client, anchored_engine, module, path, expected_limit, expected_max
):
    engine = anchored_engine(module)

    response = client.get(path, headers=x402_headers())

    assert response.status_code == 200
    _, params = engine.only_data_statement()
    assert params["limit"] == probe_limit(expected_limit)
    # No default window is invented for these: their row limit already bounded
    # them, and adding one would change what an existing caller receives.
    assert "start" not in params and "end" not in params

    bounds = response.json()["applied_bounds"]
    assert bounds["window_source"] == "no_default_window"
    assert bounds["default_window_weeks"] is None
    assert bounds["limit"] == expected_limit
    assert bounds["max_limit"] == expected_max


# ===========================================================================
# 7. Audit coverage — a new paid history endpoint must be classified
# ===========================================================================

# Every payment-governed history route, with the verdict this PR recorded for
# it. Pricing a new history endpoint fails this test until somebody records
# which class it falls into, the way the pre-payment suite enrols validators.
AUDITED_HISTORY_ROUTES: dict[str, str] = {
    "/v1/breadth/sector/history": "remediated — default window, default limit, applied_bounds, all-exchange aggregation corrected",
    "/v1/stwr/reports/history": "remediated — default window, default limit, applied_bounds, weekdate-primary ordering",
    "/v1/leadership/rotation/history": "remediated — default window, explicit limit with real SQL LIMIT, applied_bounds",
    "/v1/selections/history": "already bounded (520/5200) — applied_bounds disclosure only",
    "/v1/selections/published/history": "already bounded (5200/50000) — applied_bounds disclosure only",
    "/v1/indicators/history": "already bounded (260/2600), symbol-scoped — no runtime change",
    "/v1/prices/history": "already bounded (260/2600), symbol-scoped — static default corrected 52 -> 260",
    "/v1/stim/history": "already bounded (260/2600), symbol-scoped — static default corrected 52 -> 260",
    "/v1/market/regime/history": "already bounded (12/52) and self-describing — reference model, no change",
}


def test_every_paid_history_route_has_an_audit_verdict():
    governed = {
        v1_path(route)
        for route, method in payment_governed_routes()
        if method == "GET" and v1_path(route).endswith("/history")
    }
    unaudited = governed - set(AUDITED_HISTORY_ROUTES)
    assert not unaudited, (
        f"paid history routes with no recorded audit verdict: {sorted(unaudited)}. "
        "Record the verdict rather than deleting the assertion."
    )


def test_audit_list_names_no_route_that_left_the_paid_surface():
    governed = {
        v1_path(route)
        for route, method in payment_governed_routes()
        if method == "GET"
    }
    stale = set(AUDITED_HISTORY_ROUTES) - governed
    assert not stale, f"audit list names routes that are no longer paid: {sorted(stale)}"


def test_every_audited_route_is_in_the_bounds_table():
    missing = set(AUDITED_HISTORY_ROUTES) - set(HISTORY_ENDPOINT_BOUNDS)
    assert not missing, f"audited routes with no published bounds: {sorted(missing)}"


# ===========================================================================
# 8. historical_signal_validation — the workflow that consumes bounded history
# ===========================================================================

WORKFLOW_ID = "historical_signal_validation"


@pytest.fixture(scope="module")
def workflow():
    return next(w for w in WORKFLOW_REGISTRY if w["workflow_id"] == WORKFLOW_ID)


def test_workflow_is_discoverable_on_every_intended_surface(workflow, client):
    assert WORKFLOW_ID in WORKFLOW_ID_EXAMPLES

    manifest = ai_tools()
    assert any(w["workflow_id"] == WORKFLOW_ID for w in manifest["workflows"])

    main.v1.openapi_schema = None
    schema = client.get("/v1/openapi.json").json()
    parameters = schema["paths"]["/cost-estimate"]["get"]["parameters"]
    workflow_id = next(p for p in parameters if p["name"] == "workflow_id")
    assert WORKFLOW_ID in workflow_id["schema"]["enum"]


def _is_stim_inference_step(endpoint: str) -> bool:
    """
    An ST-IM *inference-layer* step.

    Matched by route rather than by the substring "stim", because
    /v1/selections/stim-select/outcomes/summary is published evidence about a
    signal rule, not a retrieval of ST-IM inference output.
    """
    path = endpoint.split(" ", 1)[1]
    return path.startswith("/v1/stim/") or path == "/v1/meta/stim"


def test_workflow_represents_the_framework_rather_than_only_stim(workflow):
    """
    ST-IM is one inference provider over the classification framework, not the
    framework itself. A workflow that reduced Stock Trends to ST-IM would
    misrepresent the product.
    """
    endpoints = [step["endpoint"] for step in workflow["steps"]]
    stim_steps = [e for e in endpoints if _is_stim_inference_step(e)]
    non_stim_steps = [e for e in endpoints if not _is_stim_inference_step(e)]

    assert len(non_stim_steps) > len(stim_steps)
    for layer in (
        "/v1/indicators/history",        # instrument
        "/v1/breadth/sector/history",    # cross-sectional participation
        "/v1/leadership/rotation/history",  # cross-sectional concentration
        "/v1/market/regime/history",     # market regime
        "/v1/meta/indicators",           # classification semantics
    ):
        assert any(layer in e for e in endpoints), f"workflow omits the {layer} layer"


def test_workflow_treats_stim_as_optional(workflow):
    for step in workflow["steps"]:
        if _is_stim_inference_step(step["endpoint"]):
            assert step["optional"] is True, (
                f"{step['step_id']} is mandatory; ST-IM must remain one optional "
                "inference layer over the framework"
            )
    mandatory = [s["endpoint"] for s in workflow["steps"] if not s["optional"]]
    assert "GET /v1/indicators/history" in mandatory


def test_workflow_references_only_routes_that_exist(workflow):
    """
    Asserted against the live route table rather than a list in the test, so a
    workflow naming an endpoint that was never built fails here.
    """
    live = {
        f"{method} {v1_path(route)}"
        for route in main.v1.routes
        if getattr(route, "methods", None)
        for method in route.methods
        if method not in {"HEAD", "OPTIONS"}
    }
    for step in workflow["steps"]:
        assert step["endpoint"] in live, f"{step['endpoint']} is not a live route"


def test_workflow_separates_what_stock_trends_supplies_from_researcher_work(workflow):
    supplies = " ".join(workflow["stock_trends_supplies"]).lower()
    assert "point-in-time" in supplies
    assert "weekdate" in supplies
    assert "semantics" in supplies
    assert "evidence" in supplies

    researcher = " ".join(workflow["researcher_supplied_steps"]).lower()
    for obligation in (
        "research period",
        "target",
        "join",
        "look-ahead",
        "baseline",
        "incremental contribution",
        "regimes",
        "out-of-sample",
        "recurring integration",
    ):
        assert obligation in researcher, (
            f"workflow does not state that the researcher performs: {obligation}"
        )


def test_workflow_declares_the_capabilities_the_api_does_not_have(workflow):
    absent = {item.lower() for item in workflow["not_provided_by_this_api"]}
    for capability in (
        "bulk dataset download",
        "model training or fitting",
        "automatic feature engineering",
        "joins to external targets or datasets",
        "backtesting or portfolio simulation",
        "statistical significance testing",
        "causal validation",
    ):
        assert capability in absent, f"workflow does not disclaim {capability}"


def test_workflow_success_condition_is_a_completed_evaluation_not_a_positive_one(
    workflow,
):
    condition = workflow["success_condition"].lower()
    assert "completed evaluation is the success condition" in condition
    assert "not asserted, implied, or required" in condition


IMPROVEMENT_DENYLIST = (
    "will improve",
    "improves your model",
    "adds alpha",
    "proven edge",
    "will outperform",
    "guaranteed",
    "demonstrates that stock trends",
    "confirms that stock trends",
)


def test_workflow_asserts_no_model_improvement_or_investment_performance(workflow):
    corpus = json.dumps(workflow).lower()
    hits = [phrase for phrase in IMPROVEMENT_DENYLIST if phrase in corpus]
    assert not hits, f"workflow acquired an improvement claim: {hits}"


def test_improvement_denylist_has_a_positive_control():
    planted = (
        "This workflow will improve your model and adds alpha, with a proven edge "
        "that is guaranteed."
    ).lower()
    hits = [phrase for phrase in IMPROVEMENT_DENYLIST if phrase in planted]
    assert len(hits) >= 4, f"denylist failed to fire on planted claims: {hits}"


def test_workflow_pricing_rule_ids_resolve_against_the_catalog(workflow, monkeypatch):
    """
    Every priced step must have an active pricing rule, or `/v1/workflows`
    returns 500 for the whole registry.
    """
    import routers.workflows as workflows_router

    rule_ids = {
        step["pricing_rule_id"]
        for step in workflow["steps"]
        if step.get("pricing_rule_id")
    }
    assert rule_ids, "workflow has no priced steps"

    cost_map = {rule_id: 0.10 for rule_id in _all_registry_rule_ids()}
    monkeypatch.setattr(
        workflows_router, "_fetch_active_pricing_costs", lambda: cost_map
    )

    response = workflows_router.get_workflows()
    body = json.loads(response.body)
    entry = next(w for w in body["workflows"] if w["workflow_id"] == WORKFLOW_ID)

    assert entry["total_stc_cost"] == pytest.approx(0.10 * len(
        [s for s in workflow["steps"] if s.get("pricing_rule_id")]
    ))
    assert entry["researcher_supplied_steps"]
    assert entry["not_provided_by_this_api"]
    assert entry["success_condition"]


def test_free_steps_are_declared_as_free(workflow, monkeypatch):
    import routers.workflows as workflows_router

    monkeypatch.setattr(
        workflows_router,
        "_fetch_active_pricing_costs",
        lambda: {rule_id: 0.10 for rule_id in _all_registry_rule_ids()},
    )
    body = json.loads(workflows_router.get_workflows().body)
    entry = next(w for w in body["workflows"] if w["workflow_id"] == WORKFLOW_ID)

    free_steps = [s for s in entry["steps"] if s["pricing_rule_id"] is None]
    assert free_steps, "the evaluation path must include public non-metered steps"
    for step in free_steps:
        assert step["stc_cost"] == 0.0
        assert "no STC charge" in step["pricing_note"]


def _all_registry_rule_ids() -> set[str]:
    return {
        step["pricing_rule_id"]
        for wf in WORKFLOW_REGISTRY
        for step in wf["steps"]
        if step.get("pricing_rule_id")
    }


def test_workflow_registry_and_cost_estimate_examples_cannot_drift():
    assert set(WORKFLOW_ID_EXAMPLES) == {w["workflow_id"] for w in WORKFLOW_REGISTRY}


# ===========================================================================
# 9. Nothing in this PR moved the payment boundary
# ===========================================================================

def test_bounding_did_not_become_pre_payment_validation():
    """
    A default window decides what slice of already-purchased work to perform. It
    is service shaping, so it must run behind the payment gate. Moving it into a
    pre-payment validator would put a data-shaping decision — and, via the
    anchor lookup, a database read — on the unpaid path.
    """
    from api.routing import get_pre_payment_semantic_validator

    for module, endpoint_name in (
        (breadth_router, "breadth_sector_history"),
        (leadership_router, "leadership_rotation_history"),
        (stwr_router, "stwr_reports_history"),
    ):
        endpoint = getattr(module, endpoint_name)
        validator = get_pre_payment_semantic_validator(endpoint)
        assert validator is not None, f"{endpoint_name} lost its semantic validator"

        source = validator.__doc__ or ""
        assert "window" not in source.lower(), (
            f"{endpoint_name}'s pre-payment validator now mentions windowing; "
            "bounding must stay behind the payment boundary"
        )


def test_bounded_endpoints_still_reject_invalid_input_before_payment(
    payment_harness, anchored_engine
):
    """
    The pre-payment rejection contract is unchanged: an exchange code that
    cannot exist is refused from the query string alone, with no settlement.
    """
    anchored_engine(breadth_router)

    response = payment_harness.client.get(
        "/v1/breadth/sector/history?exchange=ZZ", headers=x402_headers()
    )

    assert response.status_code == 400
    assert payment_harness.settle_count == 0
