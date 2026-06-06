from __future__ import annotations

import copy
import json
import os
import shutil
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import main
import middleware.api_key as api_key_module
import middleware.metering as metering_module
import pricing.classifier as classifier_module
from payments.enforcement import PaymentEnforcementResult
from payments.policy_provider import is_public_intelligence_path
from services.intelligence_artifact_store import (
    CONTRACT_SCHEMA_PATH,
    STORE_ENV_VAR,
    IntelligenceArtifactStore,
    IntelligenceArtifactStoreUnavailable,
    compute_public_artifact_content_hash,
)


REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURE_ROOT = REPO_ROOT / "tests" / "fixtures" / "intelligence" / "public_artifacts" / "v1"

GUIDANCE_ID = "market_guidance:N:2026-04-11:guidance:aff9aaeee1660a31"
RESEARCH_ID = "market_research_report:N:2026-04-11:research:2a7d870d628448a0"


def _copy_fixture_store(tmp_path: Path) -> Path:
    target = tmp_path / "public_artifacts" / "v1"
    shutil.copytree(FIXTURE_ROOT, target)
    return target


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _load_manifest(root: Path) -> dict:
    return _read_json(root / "manifest.json")


def _save_manifest(root: Path, manifest: dict) -> None:
    _write_json(root / "manifest.json", manifest)


def _entry_for(manifest: dict, artifact_type: str) -> dict:
    return next(entry for entry in manifest["artifacts"] if entry["artifact_type"] == artifact_type)


def _artifact_path(root: Path, entry: dict) -> Path:
    return root / Path(*entry["path"].split("/"))


def _rewrite_artifact(root: Path, artifact_type: str, updates: dict) -> dict:
    manifest = _load_manifest(root)
    entry = _entry_for(manifest, artifact_type)
    artifact_path = _artifact_path(root, entry)
    artifact = _read_json(artifact_path)
    artifact.update(updates)
    artifact["content_hash"] = compute_public_artifact_content_hash(artifact)

    for key in ("artifact_id", "artifact_type", "content_hash", "exchange", "published_at", "weekdate"):
        entry[key] = artifact[key]

    _write_json(artifact_path, artifact)
    _save_manifest(root, manifest)
    return artifact


def _stub_runtime_side_effects(monkeypatch, *, cost: Decimal = Decimal("0")):
    monkeypatch.setattr(metering_module, "log_api_request_event", lambda *args, **kwargs: None)
    monkeypatch.setattr(metering_module, "log_api_request_economics", lambda *args, **kwargs: None)
    monkeypatch.setattr(api_key_module, "log_auth_failure_event", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        metering_module,
        "resolve_economic_amounts",
        lambda *args, **kwargs: (cost, cost, cost),
    )


def _stub_paid_api_key(monkeypatch):
    def _authenticate_api_key(self, path: str, raw_key: str) -> tuple[bool, dict]:
        return True, {
            "api_key_id": "key_test",
            "customer_id": "cus_test",
            "subscription_id": "sub_test",
            "plan_code": "pro",
            "actor_type": "external_customer",
            "monthly_quota": 1000,
        }

    monkeypatch.setattr(
        api_key_module.ApiKeyMiddleware,
        "_authenticate_api_key",
        _authenticate_api_key,
    )


def _enable_agent_pay(monkeypatch):
    monkeypatch.setattr(api_key_module, "_ENABLE_AGENT_PAY", True)
    monkeypatch.setattr(metering_module, "ENABLE_AGENT_PAY", True)
    monkeypatch.setattr(metering_module, "ENFORCE_AGENT_PAY", True)
    monkeypatch.setattr(metering_module, "VALIDATE_AGENT_PAY_HEADERS", False)
    monkeypatch.setattr(classifier_module, "ENABLE_AGENT_PAY", True)
    monkeypatch.setattr(classifier_module, "ENFORCE_AGENT_PAY", True)


@pytest.fixture
def artifact_root(tmp_path):
    return _copy_fixture_store(tmp_path)


@pytest.fixture
def intelligence_client(monkeypatch, artifact_root):
    _stub_runtime_side_effects(monkeypatch)
    monkeypatch.setenv(STORE_ENV_VAR, str(artifact_root))
    with TestClient(main.app) as client:
        yield client


@pytest.fixture
def paid_subscription_client(monkeypatch, artifact_root):
    economics_rows: list[dict] = []
    cost_by_rule = {
        "intelligence_guidance_latest": Decimal("0.25"),
        "intelligence_guidance_by_id": Decimal("0.25"),
        "intelligence_research_latest": Decimal("0.50"),
        "intelligence_research_by_id": Decimal("0.50"),
    }

    _stub_runtime_side_effects(monkeypatch)
    _stub_paid_api_key(monkeypatch)
    monkeypatch.setenv(STORE_ENV_VAR, str(artifact_root))
    monkeypatch.setattr(
        metering_module,
        "resolve_economic_amounts",
        lambda rule_name: (
            cost_by_rule.get(rule_name, Decimal("0")),
            cost_by_rule.get(rule_name, Decimal("0")),
            cost_by_rule.get(rule_name, Decimal("0")),
        ),
    )
    monkeypatch.setattr(
        metering_module,
        "log_api_request_economics",
        lambda econ: economics_rows.append(dict(econ)),
    )

    with TestClient(main.app) as client:
        yield client, economics_rows


@pytest.fixture
def paid_machine_client(monkeypatch, artifact_root):
    economics_rows: list[dict] = []

    _stub_runtime_side_effects(monkeypatch, cost=Decimal("0.25"))
    _enable_agent_pay(monkeypatch)
    monkeypatch.setenv(STORE_ENV_VAR, str(artifact_root))
    monkeypatch.setattr(
        metering_module,
        "log_api_request_economics",
        lambda econ: economics_rows.append(dict(econ)),
    )

    def _fake_enforce_payment_rail(**kwargs):
        payment_rail = kwargs["payment_rail"]
        return PaymentEnforcementResult(
            outcome="proceed",
            payment_reference=f"{payment_rail}-paid-ref",
            payment_network="eip155:8453",
            payment_token="USDC",
            payment_amount_native=Decimal("250000"),
            payment_response={"success": True, "rail": payment_rail} if payment_rail == "x402" else None,
        )

    monkeypatch.setattr(metering_module, "enforce_payment_rail", _fake_enforce_payment_rail)

    with TestClient(main.app) as client:
        yield client, economics_rows


def test_api_loads_vendored_agent_fixtures(artifact_root):
    store = IntelligenceArtifactStore(artifact_root)

    artifacts = store.list_valid_artifacts()

    assert len(artifacts) == 4
    assert {artifact.artifact_type for artifact in artifacts} == {
        "discovery_metadata",
        "editorial_preview",
        "market_guidance",
        "market_research_report",
    }


def test_api_validates_artifacts_against_vendored_schema(artifact_root):
    schema = _read_json(CONTRACT_SCHEMA_PATH)
    assert schema["title"] == "PublicArtifactEnvelope.v1"
    assert schema["additionalProperties"] is False
    assert "canonical_url" not in schema["properties"]

    artifact = IntelligenceArtifactStore(artifact_root).get_latest("market_guidance")
    assert artifact is not None
    assert artifact.schema_version == "1"


def test_api_verifies_content_hash(artifact_root):
    manifest = _load_manifest(artifact_root)
    entry = _entry_for(manifest, "market_guidance")
    artifact = _read_json(_artifact_path(artifact_root, entry))

    assert compute_public_artifact_content_hash(artifact) == artifact["content_hash"]
    assert artifact["content_hash"] == entry["content_hash"]


def test_invalid_hash_fails_closed(artifact_root):
    manifest = _load_manifest(artifact_root)
    entry = _entry_for(manifest, "market_guidance")
    entry["content_hash"] = "sha256:" + ("0" * 64)
    _save_manifest(artifact_root, manifest)

    assert IntelligenceArtifactStore(artifact_root).get_latest("market_guidance") is None


def test_unsupported_schema_version_fails_closed(artifact_root):
    _rewrite_artifact(artifact_root, "market_guidance", {"schema_version": "2"})

    assert IntelligenceArtifactStore(artifact_root).get_latest("market_guidance") is None


def test_missing_manifest_fails_closed(tmp_path):
    root = tmp_path / "empty"
    root.mkdir()
    store = IntelligenceArtifactStore(root)

    with pytest.raises(IntelligenceArtifactStoreUnavailable):
        store.get_latest("market_guidance")


def test_malformed_manifest_fails_closed(artifact_root):
    (artifact_root / "manifest.json").write_text("{", encoding="utf-8")

    with pytest.raises(IntelligenceArtifactStoreUnavailable):
        IntelligenceArtifactStore(artifact_root).get_latest("market_guidance")


def test_artifact_path_traversal_is_rejected(artifact_root):
    manifest = _load_manifest(artifact_root)
    _entry_for(manifest, "market_guidance")["path"] = "../outside.json"
    _save_manifest(artifact_root, manifest)

    with pytest.raises(IntelligenceArtifactStoreUnavailable):
        IntelligenceArtifactStore(artifact_root).get_latest("market_guidance")


def test_files_not_referenced_by_manifest_are_ignored(artifact_root):
    rogue = artifact_root / "artifacts" / "market_guidance" / "rogue.json"
    rogue.write_text("{", encoding="utf-8")

    artifact = IntelligenceArtifactStore(artifact_root).get_latest("market_guidance")

    assert artifact is not None
    assert artifact.artifact_id == GUIDANCE_ID


def test_wrong_artifact_type_for_route_returns_404(paid_subscription_client):
    client, _economics_rows = paid_subscription_client

    response = client.get(
        f"/v1/intelligence/guidance/{RESEARCH_ID}",
        headers={"Authorization": "Bearer paid_test_key"},
    )

    assert response.status_code == 404
    assert response.json()["detail"]["error"] == "intelligence_artifact_not_found"


def test_latest_resolves_deterministically(artifact_root):
    manifest = _load_manifest(artifact_root)
    entry = _entry_for(manifest, "market_guidance")
    source_artifact = _read_json(_artifact_path(artifact_root, entry))

    newer = copy.deepcopy(source_artifact)
    newer.update(
        {
            "artifact_id": "market_guidance:N:2026-04-18:guidance:newer",
            "weekdate": "2026-04-18",
            "generated_at": "2026-04-19T06:00:00+00:00",
            "published_at": "2026-04-19T06:00:00+00:00",
        }
    )
    newer["content_hash"] = compute_public_artifact_content_hash(newer)
    newer_path = artifact_root / "artifacts" / "market_guidance" / "market_guidance-newer.json"
    _write_json(newer_path, newer)

    manifest["artifacts"].append(
        {
            "artifact_id": newer["artifact_id"],
            "artifact_type": newer["artifact_type"],
            "content_hash": newer["content_hash"],
            "exchange": newer["exchange"],
            "path": "artifacts/market_guidance/market_guidance-newer.json",
            "published_at": newer["published_at"],
            "weekdate": newer["weekdate"],
        }
    )
    manifest["artifact_count"] = len(manifest["artifacts"])
    _save_manifest(artifact_root, manifest)

    store = IntelligenceArtifactStore(artifact_root)

    assert store.get_latest("market_guidance").artifact_id == newer["artifact_id"]
    assert store.get_latest("market_guidance").artifact_id == newer["artifact_id"]


def test_static_published_at_rejected_for_dated_types(artifact_root):
    _rewrite_artifact(artifact_root, "market_guidance", {"published_at": "static"})

    assert IntelligenceArtifactStore(artifact_root).get_latest("market_guidance") is None


def test_static_published_at_accepted_for_discovery_metadata(artifact_root):
    artifact = IntelligenceArtifactStore(artifact_root).get_latest("discovery_metadata")

    assert artifact is not None
    assert artifact.published_at == "static"


def test_canonical_url_rejected_in_vendored_artifacts(artifact_root):
    _rewrite_artifact(
        artifact_root,
        "market_guidance",
        {"canonical_url": "https://api.stocktrends.com/v1/intelligence/guidance/example"},
    )

    assert IntelligenceArtifactStore(artifact_root).get_latest("market_guidance") is None


def test_expired_artifacts_fail_closed(artifact_root):
    _rewrite_artifact(artifact_root, "market_guidance", {"expires_at": "2026-04-13T00:00:00+00:00"})

    store = IntelligenceArtifactStore(
        artifact_root,
        now=datetime(2026, 4, 14, tzinfo=timezone.utc),
    )
    assert store.get_latest("market_guidance") is None


def test_store_cache_avoids_repeated_full_reload_on_unchanged_manifest(monkeypatch, artifact_root):
    store = IntelligenceArtifactStore(artifact_root)
    calls = 0
    original = store._load_valid_manifest_entry

    def _counting_load(entry):
        nonlocal calls
        calls += 1
        return original(entry)

    monkeypatch.setattr(store, "_load_valid_manifest_entry", _counting_load)

    assert store.get_latest("market_guidance") is not None
    first_call_count = calls
    assert first_call_count == 4

    assert store.get_latest("market_research_report") is not None
    assert calls == first_call_count


def test_store_cache_invalidates_when_manifest_changes(artifact_root):
    store = IntelligenceArtifactStore(artifact_root)
    assert store.get_latest("market_guidance").artifact_id == GUIDANCE_ID

    manifest = _load_manifest(artifact_root)
    entry = _entry_for(manifest, "market_guidance")
    source_artifact = _read_json(_artifact_path(artifact_root, entry))
    newer = copy.deepcopy(source_artifact)
    newer.update(
        {
            "artifact_id": "market_guidance:N:2026-04-18:guidance:cache-newer",
            "weekdate": "2026-04-18",
            "generated_at": "2026-04-19T06:00:00+00:00",
            "published_at": "2026-04-19T06:00:00+00:00",
        }
    )
    newer["content_hash"] = compute_public_artifact_content_hash(newer)
    newer_path = artifact_root / "artifacts" / "market_guidance" / "market_guidance-cache-newer.json"
    _write_json(newer_path, newer)
    manifest["artifacts"].append(
        {
            "artifact_id": newer["artifact_id"],
            "artifact_type": newer["artifact_type"],
            "content_hash": newer["content_hash"],
            "exchange": newer["exchange"],
            "path": "artifacts/market_guidance/market_guidance-cache-newer.json",
            "published_at": newer["published_at"],
            "weekdate": newer["weekdate"],
        }
    )
    manifest["artifact_count"] = len(manifest["artifacts"])
    _save_manifest(artifact_root, manifest)
    os.utime(artifact_root / "manifest.json", None)

    assert store.get_latest("market_guidance").artifact_id == newer["artifact_id"]


def test_store_cache_does_not_bypass_hash_validation_after_artifact_change(artifact_root):
    store = IntelligenceArtifactStore(artifact_root)
    assert store.get_latest("market_guidance") is not None

    manifest = _load_manifest(artifact_root)
    entry = _entry_for(manifest, "market_guidance")
    artifact_path = _artifact_path(artifact_root, entry)
    artifact = _read_json(artifact_path)
    artifact["payload"] = {"tampered": True}
    _write_json(artifact_path, artifact)
    os.utime(artifact_path, None)

    assert store.get_latest("market_guidance") is None


@pytest.mark.parametrize(
    ("artifact_type", "status", "expected_available"),
    [
        ("discovery_metadata", "publish_ready", True),
        ("editorial_preview", "publish_ready", True),
        ("market_guidance", "published", True),
        ("market_guidance", "product_grade", True),
        ("market_guidance", "publish_ready", False),
        ("market_guidance", "agent_actionable", False),
        ("market_research_report", "published", True),
        ("market_research_report", "product_grade", True),
        ("market_research_report", "publish_ready", False),
    ],
)
def test_publication_status_allowlist_by_artifact_type(
    artifact_root,
    artifact_type,
    status,
    expected_available,
):
    _rewrite_artifact(artifact_root, artifact_type, {"publication_status": status})

    artifact = IntelligenceArtifactStore(artifact_root).get_latest(artifact_type)

    assert (artifact is not None) is expected_available


def test_no_agent_repo_imports():
    checked_paths = [
        REPO_ROOT / "services" / "intelligence_artifact_store.py",
        REPO_ROOT / "routers" / "intelligence.py",
    ]
    for path in checked_paths:
        source = path.read_text(encoding="utf-8")
        assert "stocktrends-intelligence-agent" not in source
        assert "app.models.public" not in source
        assert "app.services.public_artifacts" not in source


def test_openapi_includes_intelligence_routes(intelligence_client):
    main.v1.openapi_schema = None

    response = intelligence_client.get("/v1/openapi.json")
    assert response.status_code == 200
    paths = response.json()["paths"]

    for path in (
        "/intelligence/discovery",
        "/intelligence/guidance/latest",
        "/intelligence/guidance/{artifact_id}",
        "/intelligence/research/latest",
        "/intelligence/research/{artifact_id}",
        "/intelligence/editorial/latest/preview",
    ):
        assert path in paths


@pytest.mark.parametrize(
    "path",
    [
        "/v1/intelligence/discovery",
        "/v1/intelligence/editorial/latest/preview",
    ],
)
def test_public_intelligence_routes_return_200_without_api_key(intelligence_client, path):
    response = intelligence_client.get(path)

    assert response.status_code == 200
    assert response.headers["x-stocktrends-payment-required"] == "false"
    assert response.headers["x-stocktrends-accepted-payment-methods"] == "none"


@pytest.mark.parametrize(
    "path",
    [
        "/v1/intelligence/guidance/latest",
        f"/v1/intelligence/guidance/{GUIDANCE_ID}",
        "/v1/intelligence/research/latest",
        f"/v1/intelligence/research/{RESEARCH_ID}",
    ],
)
def test_paid_intelligence_routes_are_not_public_without_api_key(intelligence_client, path):
    response = intelligence_client.get(path)

    assert response.status_code == 401
    assert response.json()["detail"] == "Missing API key"


@pytest.mark.parametrize(
    ("path", "pricing_rule_id", "stc_cost"),
    [
        ("/v1/intelligence/guidance/latest", "intelligence_guidance_latest", Decimal("0.25")),
        (f"/v1/intelligence/guidance/{GUIDANCE_ID}", "intelligence_guidance_by_id", Decimal("0.25")),
        ("/v1/intelligence/research/latest", "intelligence_research_latest", Decimal("0.50")),
        (f"/v1/intelligence/research/{RESEARCH_ID}", "intelligence_research_by_id", Decimal("0.50")),
    ],
)
def test_paid_intelligence_subscription_access_logs_economics(
    paid_subscription_client,
    path,
    pricing_rule_id,
    stc_cost,
):
    client, economics_rows = paid_subscription_client

    response = client.get(path, headers={"Authorization": "Bearer paid_test_key"})

    assert response.status_code == 200
    assert response.headers["x-stocktrends-pricing-rule"] == pricing_rule_id
    assert response.headers["x-stocktrends-payment-required"] == "false"
    assert response.headers["x-stocktrends-accepted-payment-methods"] == "subscription,x402,mpp"
    assert response.headers["cache-control"] == "no-store, private"

    row = economics_rows[-1]
    assert row["request_id"]
    assert row["pricing_rule_id"] == pricing_rule_id
    assert row["stc_cost"] == stc_cost
    assert row["payment_required"] == 0
    assert row["payment_rail"] == "subscription"
    assert row["payment_status"] == "not_required"


@pytest.mark.parametrize("payment_method", ["x402", "mpp"])
def test_paid_intelligence_machine_payment_rails_work(paid_machine_client, payment_method):
    client, economics_rows = paid_machine_client
    headers = {
        "X-StockTrends-Agent-Id": "intelligence-agent-test",
        "X-StockTrends-Payment-Method": payment_method,
        "X-StockTrends-Payment-Reference": f"{payment_method}-ref",
        "X-StockTrends-Session-Id": f"{payment_method}-session",
    }

    response = client.get("/v1/intelligence/guidance/latest", headers=headers)

    assert response.status_code == 200
    assert response.headers["x-stocktrends-pricing-rule"] == "intelligence_guidance_latest"
    assert response.headers["x-stocktrends-payment-required"] == "true"
    assert response.headers["x-stocktrends-accepted-payment-methods"] == "subscription,x402,mpp"

    row = economics_rows[-1]
    assert row["pricing_rule_id"] == "intelligence_guidance_latest"
    assert row["stc_cost"] == Decimal("0.25")
    assert row["payment_required"] == 1
    assert row["payment_rail"] == payment_method
    assert row["payment_method"] == payment_method
    assert row["request_id"]


def test_missing_store_returns_503(monkeypatch):
    _stub_runtime_side_effects(monkeypatch)
    monkeypatch.delenv(STORE_ENV_VAR, raising=False)

    with TestClient(main.app) as client:
        response = client.get("/v1/intelligence/discovery")

    assert response.status_code == 503
    assert response.json()["detail"]["error"] == "intelligence_artifact_store_unavailable"


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("/v1/intelligence/discovery", True),
        ("/v1/intelligence/guidance/latest", False),
        ("/v1/intelligence/guidance/example", False),
        ("/v1/intelligence/guidance/example/extra", False),
        ("/v1/intelligence/research/latest", False),
        ("/v1/intelligence/research/example", False),
        ("/v1/intelligence/research/example/extra", False),
        ("/v1/intelligence/editorial/latest/preview", True),
        ("/v1/intelligence/editorial/latest", False),
    ],
)
def test_intelligence_public_classification_is_exact(path, expected):
    assert is_public_intelligence_path(path) is expected
