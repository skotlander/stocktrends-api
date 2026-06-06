-- Active pricing rules for paid Intelligence Artifact routes.
-- Conservative PR 3 launch pricing:
--   guidance artifacts: 0.25 STC per request
--   research artifacts: 0.50 STC per request
--
-- STC remains the source of truth. Payment rails translate these STC costs;
-- rails must not define independent endpoint prices.
--
-- Deployment note:
--   Production api_pricing_rules has no UNIQUE(rule_name), so
--   ON DUPLICATE KEY UPDATE is unsafe and would duplicate rows. This seed
--   intentionally deletes the four rule_name rows first, then inserts them.
--
-- Route precedence:
--   Exact /latest routes use priority=10.
--   By-id template routes use priority=20 so exact routes sort first if
--   a deployment loader applies priority-based endpoint matching.

START TRANSACTION;

DELETE FROM api_pricing_rules
WHERE rule_name IN (
    'intelligence_guidance_latest',
    'intelligence_guidance_by_id',
    'intelligence_research_latest',
    'intelligence_research_by_id'
);

INSERT INTO api_pricing_rules (
    rule_name,
    endpoint_pattern,
    endpoint_family,
    api_version,
    priority,
    access_type,
    cost_per_request,
    cost_unit,
    requires_subscription,
    requires_payment,
    is_active
) VALUES
    (
        'intelligence_guidance_latest',
        '/v1/intelligence/guidance/latest',
        'intelligence',
        'v1',
        10,
        'paid',
        0.250000,
        'STC',
        0,
        1,
        1
    ),
    (
        'intelligence_guidance_by_id',
        '/v1/intelligence/guidance/{artifact_id}',
        'intelligence',
        'v1',
        20,
        'paid',
        0.250000,
        'STC',
        0,
        1,
        1
    ),
    (
        'intelligence_research_latest',
        '/v1/intelligence/research/latest',
        'intelligence',
        'v1',
        10,
        'paid',
        0.500000,
        'STC',
        0,
        1,
        1
    ),
    (
        'intelligence_research_by_id',
        '/v1/intelligence/research/{artifact_id}',
        'intelligence',
        'v1',
        20,
        'paid',
        0.500000,
        'STC',
        0,
        1,
        1
    );

COMMIT;
