import unittest
from dataclasses import replace
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

    def test_stocktrends_portfolio_detail_metadata_path_is_public_not_endpoint_policy(self):
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

        self.assertIsNone(effective)
        self.assertTrue(policy_provider.is_public_stocktrends_portfolio_metadata_path("/v1/stocktrends/portfolios/1"))
        self.assertEqual(accepted, "none")

    def test_stocktrends_portfolio_list_metadata_path_is_public_not_endpoint_policy(self):
        default_config = policy_provider._default_policy_config()

        with patch.object(policy_provider, "get_runtime_payment_policy_config", return_value=default_config):
            effective = policy_provider.get_effective_endpoint_payment_policy(
                "/v1/stocktrends/portfolios",
                "GET",
            )
            accepted = policy_provider.get_accepted_payment_methods_for_path(
                "/v1/stocktrends/portfolios",
                "default_free",
                method="GET",
            )

        self.assertIsNone(effective)
        self.assertTrue(policy_provider.is_public_stocktrends_portfolio_metadata_path("/v1/stocktrends/portfolios"))
        self.assertEqual(accepted, "none")

    def test_stocktrends_portfolio_returns_path_is_public_not_endpoint_policy(self):
        paid_returns_policy = policy_provider.EndpointPaymentPolicy(
            endpoint_id="stocktrends_portfolio_returns_paid",
            path_pattern="/v1/stocktrends/portfolios/{port_id}/returns",
            method="GET",
            allowed_rails=("subscription", "x402", "mpp"),
            pricing_rule_id="stocktrends_portfolio_returns_paid",
        )
        future_config = replace(
            policy_provider._default_policy_config(),
            endpoint_payment_policies=(paid_returns_policy,),
        )

        with patch.object(policy_provider, "get_runtime_payment_policy_config", return_value=future_config), patch.object(
            classifier, "ENABLE_AGENT_PAY", True
        ):
            effective = policy_provider.get_effective_endpoint_payment_policy(
                "/v1/stocktrends/portfolios/1/returns",
                "GET",
            )
            accepted = policy_provider.get_accepted_payment_methods_for_path(
                "/v1/stocktrends/portfolios/1/returns",
                "default_free",
                method="GET",
            )
            decision = classifier.classify_request(
                path="/v1/stocktrends/portfolios/1/returns",
                method="GET",
                has_paid_auth=False,
                payment_method_header="x402",
                agent_identifier="agent-123",
            )

        self.assertFalse(
            policy_provider.is_public_stocktrends_portfolio_metadata_path(
                "/v1/stocktrends/portfolios/1/returns"
            )
        )
        self.assertTrue(
            policy_provider.is_public_stocktrends_portfolio_returns_path(
                "/v1/stocktrends/portfolios/1/returns"
            )
        )
        self.assertTrue(
            policy_provider.is_public_stocktrends_portfolio_path(
                "/v1/stocktrends/portfolios/1/returns"
            )
        )
        self.assertIsNone(effective)
        self.assertTrue(decision.access_granted)
        self.assertEqual(decision.is_metered, 0)
        self.assertEqual(decision.econ_payment_required, 0)
        self.assertEqual(accepted, "none")

    def test_stocktrends_portfolio_positions_history_path_is_public_not_endpoint_policy(self):
        paid_positions_history_policy = policy_provider.EndpointPaymentPolicy(
            endpoint_id="stocktrends_portfolio_positions_history_paid",
            path_pattern="/v1/stocktrends/portfolios/{port_id}/positions/history",
            method="GET",
            allowed_rails=("subscription", "x402", "mpp"),
            pricing_rule_id="stocktrends_portfolio_positions_history_paid",
        )
        future_config = replace(
            policy_provider._default_policy_config(),
            endpoint_payment_policies=(paid_positions_history_policy,),
        )

        with patch.object(policy_provider, "get_runtime_payment_policy_config", return_value=future_config), patch.object(
            classifier, "ENABLE_AGENT_PAY", True
        ):
            effective = policy_provider.get_effective_endpoint_payment_policy(
                "/v1/stocktrends/portfolios/1/positions/history",
                "GET",
            )
            accepted = policy_provider.get_accepted_payment_methods_for_path(
                "/v1/stocktrends/portfolios/1/positions/history?start_date=2024-01-01",
                "default_free",
                method="GET",
            )
            decision = classifier.classify_request(
                path="/v1/stocktrends/portfolios/1/positions/history",
                method="GET",
                has_paid_auth=False,
                payment_method_header="x402",
                agent_identifier="agent-123",
            )

        self.assertFalse(
            policy_provider.is_public_stocktrends_portfolio_metadata_path(
                "/v1/stocktrends/portfolios/1/positions/history"
            )
        )
        self.assertFalse(
            policy_provider.is_public_stocktrends_portfolio_returns_path(
                "/v1/stocktrends/portfolios/1/positions/history"
            )
        )
        self.assertTrue(
            policy_provider.is_public_stocktrends_portfolio_positions_history_path(
                "/v1/stocktrends/portfolios/1/positions/history"
            )
        )
        self.assertTrue(
            policy_provider.is_public_stocktrends_portfolio_path(
                "/v1/stocktrends/portfolios/1/positions/history"
            )
        )
        self.assertIsNone(effective)
        self.assertTrue(decision.access_granted)
        self.assertEqual(decision.is_metered, 0)
        self.assertEqual(decision.econ_payment_required, 0)
        self.assertEqual(accepted, "none")

    def test_stocktrends_portfolio_public_match_does_not_cover_other_children(self):
        future_child_policy = policy_provider.EndpointPaymentPolicy(
            endpoint_id="stocktrends_portfolio_positions_paid",
            path_pattern="/v1/stocktrends/portfolios/{port_id}/positions",
            method="GET",
            allowed_rails=("subscription", "x402", "mpp"),
            pricing_rule_id="stocktrends_portfolio_positions_paid",
        )
        future_config = replace(
            policy_provider._default_policy_config(),
            endpoint_payment_policies=(future_child_policy,),
        )

        with patch.object(policy_provider, "get_runtime_payment_policy_config", return_value=future_config), patch.object(
            classifier, "ENABLE_AGENT_PAY", True
        ):
            effective = policy_provider.get_effective_endpoint_payment_policy(
                "/v1/stocktrends/portfolios/1/positions",
                "GET",
            )
            decision = classifier.classify_request(
                path="/v1/stocktrends/portfolios/1/positions",
                method="GET",
                has_paid_auth=False,
                payment_method_header="x402",
                agent_identifier="agent-123",
            )

        self.assertFalse(
            policy_provider.is_public_stocktrends_portfolio_path(
                "/v1/stocktrends/portfolios/1/positions"
            )
        )
        self.assertIsNotNone(effective)
        self.assertEqual(effective.pricing_rule_id, "stocktrends_portfolio_positions_paid")
        self.assertEqual(effective.allowed_rails, ("subscription", "x402", "mpp"))
        self.assertTrue(decision.access_granted)
        self.assertEqual(decision.econ_payment_required, 1)
        self.assertEqual(decision.econ_payment_method, "x402")

    def test_stocktrends_current_positions_path_is_not_public(self):
        future_current_policy = policy_provider.EndpointPaymentPolicy(
            endpoint_id="stocktrends_portfolio_current_positions_paid",
            path_pattern="/v1/stocktrends/portfolios/{port_id}/positions/current",
            method="GET",
            allowed_rails=("subscription", "x402", "mpp"),
            pricing_rule_id="stocktrends_portfolio_current_positions_paid",
        )
        future_config = replace(
            policy_provider._default_policy_config(),
            endpoint_payment_policies=(future_current_policy,),
        )

        with patch.object(policy_provider, "get_runtime_payment_policy_config", return_value=future_config), patch.object(
            classifier, "ENABLE_AGENT_PAY", True
        ):
            effective = policy_provider.get_effective_endpoint_payment_policy(
                "/v1/stocktrends/portfolios/1/positions/current",
                "GET",
            )
            decision = classifier.classify_request(
                path="/v1/stocktrends/portfolios/1/positions/current",
                method="GET",
                has_paid_auth=False,
                payment_method_header="x402",
                agent_identifier="agent-123",
            )

        self.assertFalse(
            policy_provider.is_public_stocktrends_portfolio_path(
                "/v1/stocktrends/portfolios/1/positions/current"
            )
        )
        self.assertIsNotNone(effective)
        self.assertEqual(effective.pricing_rule_id, "stocktrends_portfolio_current_positions_paid")
        self.assertEqual(effective.allowed_rails, ("subscription", "x402", "mpp"))
        self.assertTrue(decision.access_granted)
        self.assertEqual(decision.econ_payment_required, 1)
        self.assertEqual(decision.econ_payment_method, "x402")

    def test_stocktrends_strategy_metadata_paths_are_public_not_endpoint_policies(self):
        future_strategy_policy = policy_provider.EndpointPaymentPolicy(
            endpoint_id="stocktrends_strategy_detail_paid",
            path_pattern="/v1/stocktrends/strategies/{strategy_id}",
            method="GET",
            allowed_rails=("subscription", "x402", "mpp"),
            pricing_rule_id="stocktrends_strategy_detail_paid",
        )
        future_portfolio_strategy_policy = policy_provider.EndpointPaymentPolicy(
            endpoint_id="stocktrends_portfolio_strategy_paid",
            path_pattern="/v1/stocktrends/portfolios/{port_id}/strategy",
            method="GET",
            allowed_rails=("subscription", "x402", "mpp"),
            pricing_rule_id="stocktrends_portfolio_strategy_paid",
        )
        future_config = replace(
            policy_provider._default_policy_config(),
            endpoint_payment_policies=(future_strategy_policy, future_portfolio_strategy_policy),
        )

        with patch.object(policy_provider, "get_runtime_payment_policy_config", return_value=future_config), patch.object(
            classifier, "ENABLE_AGENT_PAY", True
        ):
            for path in (
                "/v1/stocktrends/strategies",
                "/v1/stocktrends/strategies/3",
                "/v1/stocktrends/portfolios/2/strategy",
            ):
                effective = policy_provider.get_effective_endpoint_payment_policy(path, "GET")
                accepted = policy_provider.get_accepted_payment_methods_for_path(
                    f"{path}?include=ignored",
                    "default_free",
                    method="GET",
                )
                decision = classifier.classify_request(
                    path=path,
                    method="GET",
                    has_paid_auth=False,
                    payment_method_header="x402",
                    agent_identifier="agent-123",
                )

                self.assertIsNone(effective)
                self.assertTrue(policy_provider.is_public_stocktrends_path(path))
                self.assertTrue(decision.access_granted)
                self.assertEqual(decision.is_metered, 0)
                self.assertEqual(decision.econ_payment_required, 0)
                self.assertEqual(accepted, "none")

    def test_stocktrends_strategy_child_paths_are_not_public(self):
        future_matches_policy = policy_provider.EndpointPaymentPolicy(
            endpoint_id="stocktrends_strategy_matches_paid",
            path_pattern="/v1/stocktrends/strategies/{strategy_id}/matches",
            method="GET",
            allowed_rails=("subscription", "x402", "mpp"),
            pricing_rule_id="stocktrends_strategy_matches_paid",
        )
        future_portfolio_strategy_current_policy = policy_provider.EndpointPaymentPolicy(
            endpoint_id="stocktrends_portfolio_strategy_current_paid",
            path_pattern="/v1/stocktrends/portfolios/{port_id}/strategy/current",
            method="GET",
            allowed_rails=("subscription", "x402", "mpp"),
            pricing_rule_id="stocktrends_portfolio_strategy_current_paid",
        )
        future_config = replace(
            policy_provider._default_policy_config(),
            endpoint_payment_policies=(future_matches_policy, future_portfolio_strategy_current_policy),
        )

        with patch.object(policy_provider, "get_runtime_payment_policy_config", return_value=future_config), patch.object(
            classifier, "ENABLE_AGENT_PAY", True
        ):
            for path, rule_id in (
                ("/v1/stocktrends/strategies/3/matches", "stocktrends_strategy_matches_paid"),
                (
                    "/v1/stocktrends/portfolios/2/strategy/current",
                    "stocktrends_portfolio_strategy_current_paid",
                ),
            ):
                effective = policy_provider.get_effective_endpoint_payment_policy(path, "GET")
                decision = classifier.classify_request(
                    path=path,
                    method="GET",
                    has_paid_auth=False,
                    payment_method_header="x402",
                    agent_identifier="agent-123",
                )

                self.assertFalse(policy_provider.is_public_stocktrends_path(path))
                self.assertIsNotNone(effective)
                self.assertEqual(effective.pricing_rule_id, rule_id)
                self.assertEqual(effective.allowed_rails, ("subscription", "x402", "mpp"))
                self.assertTrue(decision.access_granted)
                self.assertEqual(decision.econ_payment_required, 1)
                self.assertEqual(decision.econ_payment_method, "x402")

    def test_intelligence_public_paths_do_not_receive_endpoint_policy(self):
        default_config = policy_provider._default_policy_config()

        with patch.object(policy_provider, "get_runtime_payment_policy_config", return_value=default_config):
            for path in (
                "/v1/intelligence/discovery",
                "/v1/intelligence/editorial/latest/preview",
            ):
                effective = policy_provider.get_effective_endpoint_payment_policy(path, "GET")
                accepted = policy_provider.get_accepted_payment_methods_for_path(
                    path,
                    "default_free",
                    method="GET",
                )

                self.assertIsNone(effective)
                self.assertTrue(policy_provider.is_public_intelligence_path(path))
                self.assertEqual(accepted, "none")

    def test_intelligence_paid_paths_use_exact_multi_rail_policy(self):
        default_config = policy_provider._default_policy_config()

        with patch.object(policy_provider, "get_runtime_payment_policy_config", return_value=default_config), patch.object(
            classifier, "ENABLE_AGENT_PAY", True
        ):
            for path, rule_id in (
                ("/v1/intelligence/guidance/latest", "intelligence_guidance_latest"),
                ("/v1/intelligence/guidance/artifact-1", "intelligence_guidance_by_id"),
                ("/v1/intelligence/research/latest", "intelligence_research_latest"),
                ("/v1/intelligence/research/artifact-1", "intelligence_research_by_id"),
            ):
                effective = policy_provider.get_effective_endpoint_payment_policy(path, "GET")
                x402_decision = classifier.classify_request(
                    path=path,
                    method="GET",
                    has_paid_auth=False,
                    payment_method_header=None,
                    agent_identifier=None,
                )
                mpp_decision = classifier.classify_request(
                    path=path,
                    method="GET",
                    has_paid_auth=False,
                    payment_method_header="mpp",
                    agent_identifier="agent-123",
                )
                subscription_decision = classifier.classify_request(
                    path=path,
                    method="GET",
                    has_paid_auth=True,
                    plan_code="pro",
                )

                self.assertFalse(policy_provider.is_public_intelligence_path(path))
                self.assertIsNotNone(effective)
                self.assertEqual(effective.pricing_rule_id, rule_id)
                self.assertEqual(effective.allowed_rails, ("subscription", "x402", "mpp"))
                self.assertEqual(effective.machine_payment_rails, ("x402", "mpp"))
                self.assertTrue(x402_decision.access_granted)
                self.assertEqual(x402_decision.econ_pricing_rule_id, rule_id)
                self.assertEqual(x402_decision.econ_payment_required, 1)
                self.assertEqual(x402_decision.econ_payment_method, "x402")
                self.assertTrue(mpp_decision.access_granted)
                self.assertEqual(mpp_decision.econ_pricing_rule_id, rule_id)
                self.assertEqual(mpp_decision.econ_payment_method, "mpp")
                self.assertTrue(subscription_decision.access_granted)
                self.assertEqual(subscription_decision.econ_pricing_rule_id, rule_id)
                self.assertEqual(subscription_decision.econ_payment_method, "subscription")

    def test_intelligence_paid_child_paths_are_not_matched_by_prefix(self):
        default_config = policy_provider._default_policy_config()

        with patch.object(policy_provider, "get_runtime_payment_policy_config", return_value=default_config):
            for path in (
                "/v1/intelligence/guidance/artifact-1/extra",
                "/v1/intelligence/research/artifact-1/extra",
            ):
                self.assertIsNone(policy_provider.get_effective_endpoint_payment_policy(path, "GET"))
                self.assertFalse(policy_provider.is_public_intelligence_path(path))


if __name__ == "__main__":
    unittest.main()
