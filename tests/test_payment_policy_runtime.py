import unittest
from unittest.mock import patch
from urllib import error as urllib_error

import payments.policy_provider as policy_provider
import pricing.classifier as classifier


LIVE_STYLE_CONFIG_PAYLOAD = {
    "environment": "production",
    "environment_rail_enablement": {
        "subscription": True,
        "x402": True,
        "mpp": False,
    },
    "pricing_rules": [
        {"rule_name": "indicators-latest-default"},
    ],
    "endpoint_payment_policies": [
        {
            "endpoint_id": "indicators.latest",
            "path_pattern": "/v1/indicators/latest",
            "method": "GET",
            "allowed_rails": ["subscription", "x402"],
            "pricing_rule_id": "indicators-latest-default",
        }
    ],
}


class PaymentPolicyRuntimeTests(unittest.TestCase):
    def setUp(self):
        self._reset_policy_cache()

    def tearDown(self):
        self._reset_policy_cache()

    def _reset_policy_cache(self):
        policy_provider._cached_config = None
        policy_provider._cached_at = 0.0
        policy_provider._last_known_good_config = None
        policy_provider._last_reported_fallback_reason = None

    def test_control_plane_exact_policy_is_used_for_accepted_rails(self):
        config = policy_provider._parse_config_payload(LIVE_STYLE_CONFIG_PAYLOAD)

        with patch.object(policy_provider, "get_runtime_payment_policy_config", return_value=config):
            accepted = policy_provider.get_accepted_payment_methods_for_path(
                "/v1/indicators/latest",
                "default_subscription",
                method="GET",
            )

        self.assertEqual(accepted, "subscription,x402")

    def test_control_plane_unavailable_falls_back_to_defaults(self):
        with patch.object(
            policy_provider,
            "_fetch_runtime_payment_policy_config",
            side_effect=urllib_error.URLError("control plane unavailable"),
        ):
            config = policy_provider.get_runtime_payment_policy_config(force_refresh=True)

        self.assertEqual(config.source, "defaults")
        self.assertTrue(policy_provider.is_free_metered_path("/v1/ai/context"))
        self.assertEqual(
            policy_provider.get_accepted_payment_methods_for_path(
                "/v1/stim/latest",
                "agent_pay_required",
                method="GET",
            ),
            "subscription,x402,mpp",
        )

    def test_configured_endpoint_requires_agent_pay_without_subscription_auth(self):
        config = policy_provider._parse_config_payload(LIVE_STYLE_CONFIG_PAYLOAD)

        with patch.object(policy_provider, "get_runtime_payment_policy_config", return_value=config), patch.object(
            classifier, "ENABLE_AGENT_PAY", True
        ):
            decision = classifier.classify_request(
                path="/v1/indicators/latest",
                method="GET",
                has_paid_auth=False,
                payment_method_header="x402",
                agent_identifier="agent-123",
            )

        self.assertTrue(decision.access_granted)
        self.assertEqual(decision.econ_payment_required, 1)
        self.assertEqual(decision.econ_payment_method, "x402")

    def test_configured_endpoint_stays_subscription_for_authenticated_callers(self):
        config = policy_provider._parse_config_payload(LIVE_STYLE_CONFIG_PAYLOAD)

        with patch.object(policy_provider, "get_runtime_payment_policy_config", return_value=config), patch.object(
            classifier, "ENABLE_AGENT_PAY", True
        ):
            decision = classifier.classify_request(
                path="/v1/indicators/latest",
                method="GET",
                has_paid_auth=True,
                plan_code="sandbox",
                payment_method_header="x402",
                agent_identifier="agent-123",
            )

        self.assertTrue(decision.access_granted)
        self.assertEqual(decision.econ_payment_required, 0)
        self.assertEqual(decision.econ_payment_method, "subscription")

    def test_stim_fallback_behavior_is_unchanged(self):
        default_config = policy_provider._default_policy_config()

        with patch.object(policy_provider, "get_runtime_payment_policy_config", return_value=default_config), patch.object(
            classifier, "ENABLE_AGENT_PAY", True
        ):
            decision = classifier.classify_request(
                path="/v1/stim/latest",
                method="GET",
                has_paid_auth=False,
                payment_method_header="x402",
                agent_identifier="agent-123",
            )

        self.assertTrue(decision.access_granted)
        self.assertEqual(decision.econ_payment_required, 1)
        self.assertEqual(decision.econ_payment_method, "x402")

    def test_stocktrends_portfolio_detail_policy_matches_path_template(self):
        default_config = policy_provider._default_policy_config()

        with patch.object(policy_provider, "get_runtime_payment_policy_config", return_value=default_config):
            effective = policy_provider.get_effective_endpoint_payment_policy(
                "/v1/stocktrends/portfolios/1",
                "GET",
            )
            accepted = policy_provider.get_accepted_payment_methods_for_path(
                "/v1/stocktrends/portfolios/1?include=ignored",
                "default_subscription",
                method="GET",
            )

        self.assertIsNotNone(effective)
        self.assertEqual(effective.pricing_rule_id, "stocktrends_portfolios_detail_paid")
        self.assertEqual(effective.allowed_rails, ("subscription", "x402", "mpp"))
        self.assertEqual(accepted, "subscription,x402,mpp")

    def test_stocktrends_portfolio_list_policy_is_registered(self):
        default_config = policy_provider._default_policy_config()

        with patch.object(policy_provider, "get_runtime_payment_policy_config", return_value=default_config):
            effective = policy_provider.get_effective_endpoint_payment_policy(
                "/v1/stocktrends/portfolios",
                "GET",
            )

        self.assertIsNotNone(effective)
        self.assertEqual(effective.pricing_rule_id, "stocktrends_portfolios_list_paid")
        self.assertEqual(effective.machine_payment_rails, ("x402", "mpp"))


if __name__ == "__main__":
    unittest.main()
