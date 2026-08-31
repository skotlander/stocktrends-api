# x402 Resource Discovery

## Purpose

x402 resource discovery and paid resource execution are separate concerns.
An anonymous crawler or agent must be able to learn which resources are
payable, how to construct a serviceable request, which payment rails are
supported, and which STC pricing rule applies without executing paid work.

The canonical discovery representation is served at:

```text
/.well-known/x402
```

Compatibility aliases serve the same semantic JSON at
`/.well-known/x402.json`, `/.well-known/x402-discovery`, and
`/.well-known/x402-services.json`. The representation identifies itself as
`stocktrends.x402-discovery.v1`; this is a Stock Trends-owned schema, not a
claim of ratified x402 manifest standardization.

## Canonical Sources

The manifest is assembled at request time from existing sources of truth:

* runtime endpoint payment policy supplies the payment-governed surface,
  pricing-rule identifiers, and enabled rails;
* `discovery.endpoint_metadata` supplies descriptions, analytical roles,
  request schemas, and safe examples;
* x402 payment configuration supplies version, network, scheme, and token
  metadata;
* `/v1/pricing/catalog` remains the live STC price source.

The manifest does not maintain endpoint prices or a separate endpoint catalog.
A governed route that lacks canonical metadata or a safe example causes
manifest construction and completeness tests to fail unless a deliberate,
audited discovery exception is recorded.

## Discovery Is Not Execution

Reading the manifest is public/free and must not:

* invoke a paid endpoint or market-data query;
* contact the x402 facilitator;
* authorize or settle an MPP session;
* verify or settle an x402 payment.

A `402 Payment Required` response belongs to execution. It is emitted only for
an otherwise-serviceable paid request after routing, parsing, Pydantic/schema
validation, and request-only semantic validation. A crawler should never need
to send a malformed paid request to discover a resource or its parameters.

The integration sequence is:

1. read `/.well-known/x402` for payable-resource discovery;
2. use `/v1/ai/tools` for task/tool discovery;
3. confirm exact contracts in `/v1/openapi.json`;
4. use `/v1/workflows` for workflow planning;
5. resolve current cost through `/v1/pricing/catalog`;
6. construct a serviceable request;
7. receive an execution-time 402 when using anonymous x402;
8. pay and retry with a supported payment proof.

## Multi-Rail Boundary

The manifest improves x402 compatibility without changing the multi-rail
architecture. STC remains the pricing unit. Subscription, x402, and MPP remain
distinct transports over that price. MPP remains session-based and is not
described as an x402 challenge flow.

Paid Intelligence artifact routes retain their earlier availability boundary.
They are discoverable, but an example may return `404` or `503` before payment
when no validated, serveable published artifact exists. The discovery layer
does not weaken or bypass that fail-closed behavior.
