"""Sandbox is a full subscription plan, not a restricted endpoint subset.

Product contract: `sandbox`, `research`, `pro`, and `enterprise` are all active
subscription plans.  An active subscription on any of them receives API-key
access to the full protected endpoint set, including the STIM family.  Plans
differ through quota / rate / burst / commercial envelope, not through an
arbitrary exclusion of an endpoint family.

These tests pin the two layers that previously encoded the legacy
"Sandbox is denied STIM" rule:

1. `ApiKeyMiddleware.is_plan_allowed` -- the subscription entitlement gate.
2. `pricing.classifier` -- the economic / payment classification layer.

They also pin the behaviour that must NOT change: anonymous callers gain
nothing, the x402 / agent-pay lane is untouched, free/trial/test plan codes stay
non-paid, and Sandbox STIM traffic remains metered.
"""

import unittest
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import patch

from middleware import api_key as api_key_module
from middleware.api_key import ApiKeyMiddleware
from payments import policy_provider
from pricing import classifier


SUBSCRIPTION_PLANS = ("sandbox", "research", "pro", "enterprise")

STIM_PATHS = ("/v1/stim/latest", "/v1/stim/history")

# Registered per-endpoint pricing rules for the STIM GET routes.  Entitlement
# must not move Sandbox onto a different rule.
STIM_PRICING_RULES = {
    "/v1/stim/latest": "stim_latest_paid",
    "/v1/stim/history": "stim_history_paid",
}

ORDINARY_PROTECTED_PATHS = ("/v1/prices/latest", "/v1/indicators/latest")


def _middleware() -> ApiKeyMiddleware:
    # is_plan_allowed is pure policy; bypass BaseHTTPMiddleware.__init__ so the
    # entitlement gate can be exercised without an ASGI app.
    return ApiKeyMiddleware.__new__(ApiKeyMiddleware)


class _FakeConn:
    """Returns one api_keys/api_subscriptions/api_plans row for the lookup."""

    def __init__(self, row):
        self.row = row
        # Bound parameters per execute().  Asserted instead of SQL text because
        # tests/conftest.py stubs sqlalchemy, so `text(...)` is a MagicMock.
        self.params: list[dict] = []

    def execute(self, statement, params=None):
        self.params.append(dict(params or {}))
        return SimpleNamespace(fetchone=lambda: self.row)


class _FakeEngine:
    def __init__(self, conn):
        self.conn = conn

    @contextmanager
    def begin(self):
        yield self.conn


def _auth_row(plan_code: str):
    # Column order mirrors the SELECT in ApiKeyMiddleware._authenticate_api_key:
    # id, customer_id, subscription_id, status, revoked_at,
    # subscription_status, plan_code, plan_active, monthly_quota
    return (1, 10, 100, "active", None, "active", plan_code, 1, 1000)


class SandboxApiKeyEntitlementTests(unittest.TestCase):
    """Layer 1: the API-key middleware plan gate."""

    def test_sandbox_is_allowed_on_stim_endpoints(self):
        mw = _middleware()
        for path in STIM_PATHS:
            with self.subTest(path=path):
                self.assertTrue(
                    mw.is_plan_allowed(path, "sandbox"),
                    f"active sandbox subscription must reach {path}",
                )

    def test_sandbox_retains_ordinary_protected_routes(self):
        mw = _middleware()
        for path in ORDINARY_PROTECTED_PATHS:
            with self.subTest(path=path):
                self.assertTrue(mw.is_plan_allowed(path, "sandbox"))

    def test_all_subscription_plans_reach_stim(self):
        mw = _middleware()
        for plan in SUBSCRIPTION_PLANS:
            for path in STIM_PATHS:
                with self.subTest(plan=plan, path=path):
                    self.assertTrue(mw.is_plan_allowed(path, plan))

    def test_research_pro_enterprise_retain_full_access(self):
        mw = _middleware()
        for plan in ("research", "pro", "enterprise"):
            for path in STIM_PATHS + ORDINARY_PROTECTED_PATHS:
                with self.subTest(plan=plan, path=path):
                    self.assertTrue(mw.is_plan_allowed(path, plan))

    def test_non_subscription_plan_codes_remain_denied(self):
        """free/trial/test are not subscription plan codes and stay out."""
        mw = _middleware()
        for plan in ("free", "trial", "test", "unknown-plan", ""):
            for path in STIM_PATHS + ORDINARY_PROTECTED_PATHS:
                with self.subTest(plan=plan, path=path):
                    self.assertFalse(mw.is_plan_allowed(path, plan))

    def test_non_v1_paths_are_unaffected(self):
        mw = _middleware()
        self.assertTrue(mw.is_plan_allowed("/health", "sandbox"))
        self.assertTrue(mw.is_plan_allowed("/health", "free"))


class SandboxAuthenticateApiKeyTests(unittest.TestCase):
    """Layer 1 end-to-end: the real key lookup that produced the production 403.

    Other suites monkeypatch `_authenticate_api_key` away entirely, so without
    this the `Plan '<code>' does not allow this endpoint` branch is only ever
    exercised indirectly through `is_plan_allowed`.
    """

    def _authenticate(self, path: str, plan_code: str):
        conn = _FakeConn(_auth_row(plan_code))
        with patch.object(api_key_module, "get_auth_engine", return_value=_FakeEngine(conn)):
            ok, auth = _middleware()._authenticate_api_key(path, "raw-test-key")
        return ok, auth, conn

    def test_active_sandbox_key_authenticates_on_stim(self):
        for path in STIM_PATHS:
            with self.subTest(path=path):
                ok, auth, conn = self._authenticate(path, "sandbox")
                self.assertTrue(ok, auth)
                self.assertEqual(auth["plan_code"], "sandbox")
                self.assertEqual(auth["monthly_quota"], 1000)
                # Successful auth runs the key lookup, then the last_used_at
                # UPDATE for that key id.
                self.assertEqual(len(conn.params), 2)
                self.assertIn("key_hash", conn.params[0])
                self.assertEqual(conn.params[1], {"key_id": 1})

    def test_non_subscription_plan_key_still_forbidden_on_stim(self):
        for plan in ("free", "trial", "test"):
            for path in STIM_PATHS:
                with self.subTest(plan=plan, path=path):
                    ok, auth, conn = self._authenticate(path, plan)
                    self.assertFalse(ok)
                    self.assertEqual(auth["status_code"], 403)
                    self.assertEqual(
                        auth["detail"], f"Plan '{plan}' does not allow this endpoint"
                    )
                    # Denied keys are not stamped as used: lookup only.
                    self.assertEqual(len(conn.params), 1)
                    self.assertIn("key_hash", conn.params[0])


class SandboxClassifierEntitlementTests(unittest.TestCase):
    """Layer 2: pricing / payment classification."""

    def setUp(self):
        self.config = policy_provider._default_policy_config()
        patcher = patch.object(
            policy_provider,
            "get_runtime_payment_policy_config",
            return_value=self.config,
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def _classify(self, **kwargs):
        return classifier.classify_request(**kwargs)

    def test_sandbox_stim_resolves_to_subscription_not_denial(self):
        """The registered-policy lane: /v1/stim/* with a real GET request."""
        for path in STIM_PATHS:
            with self.subTest(path=path):
                decision = self._classify(
                    path=path,
                    method="GET",
                    has_paid_auth=True,
                    plan_code="sandbox",
                )
                self.assertTrue(decision.access_granted)
                self.assertIsNone(decision.deny_reason)
                self.assertEqual(decision.econ_payment_method, "subscription")
                self.assertEqual(decision.econ_payment_required, 0)

    def test_sandbox_matches_other_subscription_plans_on_stim(self):
        """Sandbox must land on the same lane as pro/research/enterprise.

        Covers both the registered-policy lane (GET) and the /v1/stim prefix
        fallback lane (method=None) -- the fallback is where the removed
        `sandbox_plan_denied` branch was actually reachable.
        """
        for path in STIM_PATHS:
            for method in ("GET", None):
                baseline = self._classify(
                    path=path, method=method, has_paid_auth=True, plan_code="pro"
                )
                self.assertTrue(baseline.access_granted)
                for plan in SUBSCRIPTION_PLANS:
                    with self.subTest(plan=plan, path=path, method=method):
                        decision = self._classify(
                            path=path, method=method, has_paid_auth=True, plan_code=plan
                        )
                        self.assertEqual(decision, baseline)

    def test_sandbox_stim_is_never_denied_as_sandbox_plan(self):
        """`sandbox_plan_denied` must not be reachable on either STIM lane."""
        for path in STIM_PATHS:
            for method in ("GET", None):  # None exercises the prefix fallback lane
                with self.subTest(path=path, method=method):
                    decision = self._classify(
                        path=path,
                        method=method,
                        has_paid_auth=True,
                        plan_code="sandbox",
                    )
                    self.assertNotEqual(decision.deny_reason, "sandbox_plan_denied")
                    self.assertTrue(decision.access_granted)
                    self.assertEqual(decision.econ_payment_method, "subscription")

    def test_sandbox_stim_remains_metered(self):
        """Entitlement must not become a metering/quota bypass."""
        for path in STIM_PATHS:
            with self.subTest(path=path):
                decision = self._classify(
                    path=path,
                    method="GET",
                    has_paid_auth=True,
                    plan_code="sandbox",
                )
                self.assertEqual(decision.is_metered, 1)
                self.assertEqual(decision.econ_pricing_rule_id, STIM_PRICING_RULES[path])
                self.assertEqual(decision.log_pricing_rule_id, STIM_PRICING_RULES[path])

    def test_sandbox_is_treated_as_a_paid_subscription_plan(self):
        self.assertTrue(classifier._is_paid_plan("sandbox"))
        self.assertTrue(classifier._is_paid_plan("  Sandbox  "))

    def test_free_trial_test_plan_codes_remain_non_paid(self):
        for plan in ("free", "trial", "test", None, ""):
            with self.subTest(plan=plan):
                self.assertFalse(classifier._is_paid_plan(plan))

    def test_free_plan_stim_fallback_behaviour_unchanged(self):
        """Non-subscription plan codes stay denied on the STIM fallback lane."""
        with patch.object(classifier, "ENABLE_AGENT_PAY", False):
            for plan in ("free", "trial", "test"):
                with self.subTest(plan=plan):
                    decision = self._classify(
                        path="/v1/stim/latest",
                        method=None,
                        has_paid_auth=True,
                        plan_code=plan,
                    )
                    self.assertFalse(decision.access_granted)
                    self.assertEqual(
                        decision.deny_reason, "stim_access_not_permitted"
                    )


class AnonymousAndAgentPayUnchangedTests(unittest.TestCase):
    """Nothing about the anonymous / machine-payment lanes may shift."""

    def setUp(self):
        self.config = policy_provider._default_policy_config()
        patcher = patch.object(
            policy_provider,
            "get_runtime_payment_policy_config",
            return_value=self.config,
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_anonymous_caller_gains_no_subscription_access(self):
        with patch.object(classifier, "ENABLE_AGENT_PAY", False):
            for path in STIM_PATHS:
                with self.subTest(path=path):
                    decision = classifier.classify_request(
                        path=path,
                        method="GET",
                        has_paid_auth=False,
                        plan_code=None,
                    )
                    self.assertFalse(decision.access_granted)
                    self.assertEqual(decision.deny_reason, "authentication_required")

    def test_anonymous_caller_cannot_claim_sandbox_plan(self):
        """A plan code without authenticated auth must not grant access.

        Note: `_deny_decision` always stamps econ_payment_method="subscription"
        on the *denial* record, so entitlement is asserted via access_granted /
        deny_reason rather than that logging field.
        """
        with patch.object(classifier, "ENABLE_AGENT_PAY", False):
            for path in STIM_PATHS:
                with self.subTest(path=path):
                    decision = classifier.classify_request(
                        path=path,
                        method="GET",
                        has_paid_auth=False,
                        plan_code="sandbox",
                    )
                    self.assertFalse(decision.access_granted)
                    self.assertEqual(decision.deny_reason, "authentication_required")

    def test_anonymous_stim_still_receives_x402_challenge(self):
        with patch.object(classifier, "ENABLE_AGENT_PAY", True):
            for path in STIM_PATHS:
                with self.subTest(path=path):
                    decision = classifier.classify_request(
                        path=path,
                        method="GET",
                        has_paid_auth=False,
                        plan_code=None,
                    )
                    self.assertTrue(decision.access_granted)
                    self.assertEqual(decision.econ_payment_required, 1)
                    self.assertEqual(decision.econ_payment_method, "x402")
                    self.assertEqual(decision.econ_pricing_rule_id, STIM_PRICING_RULES[path])

    def test_identified_agent_pay_intent_still_routes_to_machine_rail(self):
        with patch.object(classifier, "ENABLE_AGENT_PAY", True):
            for method_header in ("x402", "mpp"):
                with self.subTest(payment_method=method_header):
                    decision = classifier.classify_request(
                        path="/v1/stim/latest",
                        method="GET",
                        has_paid_auth=False,
                        payment_method_header=method_header,
                        agent_identifier="agent-123",
                    )
                    self.assertTrue(decision.access_granted)
                    self.assertEqual(decision.econ_payment_required, 1)
                    self.assertEqual(decision.econ_payment_method, method_header)

    def test_stim_endpoints_still_allow_all_three_rails(self):
        for path in STIM_PATHS:
            with self.subTest(path=path):
                policy = policy_provider.get_effective_endpoint_payment_policy_from_config(
                    self.config, path, "GET"
                )
                self.assertIsNotNone(policy)
                self.assertTrue(policy.allows_subscription)
                self.assertEqual(policy.machine_payment_rails, ("x402", "mpp"))


if __name__ == "__main__":
    unittest.main()
