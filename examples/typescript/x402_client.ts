/**
 * x402 Client
 * ============
 * Purpose:
 *   Demonstrate how an autonomous agent handles the Stock Trends x402
 *   per-request payment protocol: detecting the payment challenge,
 *   inspecting pricing, completing on-chain payment, and resubmitting
 *   the request with a payment proof header.
 *
 * What it demonstrates:
 *   - Detecting an HTTP 402 response and extracting the x402 payment challenge
 *   - Parsing the pricing object: amount_usd, network (Base / eip155:8453),
 *     token (USDC), and payment requirements
 *   - The request → challenge → pay → resubmit pattern that x402 agents follow
 *   - How to structure a reusable x402-aware fetch wrapper for any endpoint
 *
 * Why a developer would use this:
 *   - Building an agent that uses x402 for per-request payment without a
 *     subscription account — agents discover pricing and pay autonomously
 *   - MCP tool authors who need to handle x402 payment flows behind a tool call
 *   - Understanding the Stock Trends x402 protocol before integrating with a
 *     CDP SDK or other x402 payment library
 *
 * x402 flow:
 *   1. Agent sends request (no payment header)
 *   2. API returns 402 with payment challenge body and X-Payment-Required header
 *   3. Agent reads challenge: inspects amount, network, token, payTo address
 *   4. Agent completes on-chain USDC payment on Base (via CDP SDK or web3 lib)
 *   5. Agent encodes payment proof as base64 JSON
 *   6. Agent resubmits original request with X-Payment header
 *   7. API verifies payment with facilitator and returns response
 *
 * Payment amounts:
 *   The challenge body includes pricing.amount_usd for human readability.
 *   The on-chain amount in payment_required.accepts[0].amount is in token
 *   base units (6 decimal places for USDC):
 *     "500000" = 0.5 USDC = $0.50
 *     "2500"   = 0.0025 USDC = $0.0025
 *   Never interpret the raw token amount as a dollar value.
 *
 * On-chain payment integration:
 *   This example stubs the payment step. To complete real payments, integrate
 *   with the Coinbase CDP SDK (coinbase-sdk) or an EIP-3009 transfer library.
 *   The payment proof is a base64-encoded JSON object containing:
 *     - x402Version
 *     - scheme, network, payload (transaction details, signature)
 *
 * Environment variables:
 *   ST_API_BASE_URL       API base URL (default: https://api.stocktrends.com)
 *   WALLET_PRIVATE_KEY    EVM private key for x402 payments (hex, 0x-prefixed)
 *   WALLET_ADDRESS        EVM wallet address funding the payments
 *
 * Run (requires ts-node or tsx):
 *   npx tsx examples/typescript/x402_client.ts
 */

const BASE_URL = (process.env.ST_API_BASE_URL ?? "https://api.stocktrends.com").replace(/\/$/, "");

// ---------------------------------------------------------------------------
// x402 protocol types
// ---------------------------------------------------------------------------

interface X402PaymentRequirement {
  scheme: string;
  network: string;
  amount: string;          // token base units (USDC: 6 decimals)
  asset: string;           // ERC-20 contract address
  payTo: string;           // recipient wallet address
  maxTimeoutSeconds: number;
  extra?: {
    name?: string;
    description?: string;
    amount_usd?: string;
  };
}

interface X402PaymentRequirements {
  x402Version: number;
  resource: { url: string; method?: string };
  accepts: X402PaymentRequirement[];
  extensions?: unknown;
}

interface X402Challenge {
  error: "payment_required";
  detail: string;
  protocol: "x402";
  resource: string;
  pricing: {
    amount_usd: string;   // human-readable USD (e.g. "0.002500")
    unit: string;         // "request"
    network: string;      // "eip155:8453"
    token: string;        // ERC-20 contract address
    scheme: string;       // "exact"
  };
  accepted_payment_methods: string[];
  payment_required: X402PaymentRequirements;
}

// Payment proof submitted as the X-Payment header (base64-encoded JSON)
interface X402PaymentProof {
  x402Version: number;
  scheme: string;
  network: string;
  payload: {
    signature: string;
    authorization?: string;   // EIP-3009 authorization
    transaction_hash?: string;
    [key: string]: unknown;
  };
}

// ---------------------------------------------------------------------------
// Payment proof builder (stub — replace with CDP SDK or web3 library)
// ---------------------------------------------------------------------------

/**
 * Build an x402 payment proof for the given requirement.
 *
 * This function is a stub. In production:
 *   1. Use the Coinbase CDP SDK to fund the session or sign the payment
 *   2. Or use an EIP-3009 receiveWithAuthorization transaction on Base
 *   3. Encode the resulting proof as base64 JSON for the X-Payment header
 *
 * The proof structure depends on the scheme ("exact") and the facilitator
 * used (Coinbase CDP by default in Stock Trends).
 */
async function buildPaymentProof(
  requirement: X402PaymentRequirement,
  _walletPrivateKey: string,
  _walletAddress: string,
): Promise<string> {
  const amountUsdc = (parseInt(requirement.amount, 10) / 1_000_000).toFixed(6);

  console.log(`  [Payment] Network: ${requirement.network}`);
  console.log(`  [Payment] Token:   ${requirement.asset} (USDC)`);
  console.log(`  [Payment] Amount:  ${amountUsdc} USDC  (${requirement.amount} base units)`);
  console.log(`  [Payment] Pay to:  ${requirement.payTo}`);
  console.log();
  console.log("  [Stub] Replace this function with CDP SDK or EIP-3009 integration.");
  console.log("  See: https://docs.cdp.coinbase.com/");
  console.log();

  // In a real implementation this would be a signed EIP-3009 authorization
  // or a CDP-signed payment payload. The proof below is illustrative only.
  const proof: X402PaymentProof = {
    x402Version: 2,
    scheme: requirement.scheme,
    network: requirement.network,
    payload: {
      signature: "0x_REPLACE_WITH_REAL_SIGNED_PAYMENT",
      authorization: "EIP_3009_AUTHORIZATION_HERE",
    },
  };

  return btoa(JSON.stringify(proof));
}

// ---------------------------------------------------------------------------
// x402-aware fetch wrapper
// ---------------------------------------------------------------------------

interface FetchWithPaymentOptions {
  walletPrivateKey?: string;
  walletAddress?: string;
  maxRetries?: number;
}

/**
 * Fetch a Stock Trends endpoint with automatic x402 payment handling.
 *
 * On first call: sends request without payment header.
 * On 402: parses challenge, completes payment, resubmits with X-Payment header.
 * On success: returns parsed JSON response body.
 */
async function fetchWithX402<T>(
  path: string,
  params?: Record<string, string>,
  options: FetchWithPaymentOptions = {},
): Promise<T> {
  const {
    walletPrivateKey = process.env.WALLET_PRIVATE_KEY ?? "",
    walletAddress = process.env.WALLET_ADDRESS ?? "",
    maxRetries = 1,
  } = options;

  const url = new URL(`${BASE_URL}${path}`);
  if (params) {
    for (const [k, v] of Object.entries(params)) {
      url.searchParams.set(k, v);
    }
  }

  let attempt = 0;
  let paymentHeader: string | undefined;

  while (attempt <= maxRetries) {
    const headers: Record<string, string> = { Accept: "application/json" };
    if (paymentHeader) {
      headers["X-Payment"] = paymentHeader;
    }

    const resp = await fetch(url.toString(), { headers });

    // -----------------------------------------------------------------------
    // Success path
    // -----------------------------------------------------------------------
    if (resp.ok) {
      if (paymentHeader) {
        console.log(`  [x402] Payment accepted — request succeeded (attempt ${attempt + 1})`);
      }
      return resp.json() as Promise<T>;
    }

    // -----------------------------------------------------------------------
    // x402 Payment Required
    // -----------------------------------------------------------------------
    if (resp.status === 402) {
      const challenge = (await resp.json()) as X402Challenge;
      const pricing = challenge.pricing;

      console.log(`\n[x402] Payment required for ${path}`);
      console.log(`  Amount: $${pricing.amount_usd} USD  (${pricing.unit})`);
      console.log(`  Network: ${pricing.network}`);
      console.log();

      if (attempt >= maxRetries) {
        throw new Error(
          `x402 payment required but max retries (${maxRetries}) reached. ` +
          `Ensure wallet credentials are set and buildPaymentProof is implemented.`
        );
      }

      if (!walletPrivateKey || !walletAddress) {
        throw new Error(
          `x402 payment required but WALLET_PRIVATE_KEY / WALLET_ADDRESS are not set.\n` +
          `Amount: $${pricing.amount_usd} USD on ${pricing.network}\n\n` +
          `Alternatively, set ST_API_KEY for subscription access.`
        );
      }

      // Select the first accepted payment requirement
      const requirements = challenge.payment_required;
      const requirement = requirements?.accepts?.[0];
      if (!requirement) {
        throw new Error("x402 challenge contained no accepted payment requirements.");
      }

      console.log(`  [x402] Completing payment (attempt ${attempt + 1} of ${maxRetries})...`);
      paymentHeader = await buildPaymentProof(requirement, walletPrivateKey, walletAddress);

      attempt++;
      continue;
    }

    // -----------------------------------------------------------------------
    // Other error
    // -----------------------------------------------------------------------
    throw new Error(`HTTP ${resp.status} on ${path}: ${await resp.text()}`);
  }

  throw new Error(`Exhausted retries for ${path}`);
}

// ---------------------------------------------------------------------------
// Demonstration
// ---------------------------------------------------------------------------

interface StimResponse {
  symbol: string;
  exchange: string;
  symbol_exchange: string;
  weekdate: string;
  x4wk: number;
  x4wksd: number;
  x13wk: number;
  x13wksd: number;
  x40wk: number;
  x40wksd: number;
  x13wk1: number;
  x13wk2: number;
  is_stale: boolean;
}

async function demonstrateX402Flow(): Promise<void> {
  console.log("x402 Client Demonstration");
  console.log("=".repeat(56));
  console.log();
  console.log("This example demonstrates the x402 per-request payment flow");
  console.log("used by Stock Trends for autonomous agent access.");
  console.log();
  console.log("Flow:");
  console.log("  1. Request sent without payment header");
  console.log("  2. API returns 402 with payment challenge");
  console.log("  3. Agent inspects challenge and completes payment");
  console.log("  4. Request resubmitted with X-Payment header");
  console.log("  5. API verifies payment and returns response");
  console.log();

  // Demonstrate the x402 flow for a paid endpoint (ST-IM for NVDA)
  // In production, provide WALLET_PRIVATE_KEY and WALLET_ADDRESS,
  // and replace buildPaymentProof() with your CDP SDK integration.
  try {
    console.log("Requesting ST-IM data for NVDA-Q via x402...\n");
    const stim = await fetchWithX402<StimResponse>(
      "/v1/stim/latest",
      { symbol_exchange: "NVDA-Q" },
    );

    // ST-IM forward return distribution results
    console.log("ST-IM Response:");
    console.log(`  Symbol:       ${stim.symbol_exchange}`);
    console.log(`  Week:         ${stim.weekdate}`);
    console.log(`  4wk return:   ${stim.x4wk >= 0 ? "+" : ""}${stim.x4wk?.toFixed(2)}% ± ${stim.x4wksd?.toFixed(2)}%`);
    console.log(`  13wk return:  ${stim.x13wk >= 0 ? "+" : ""}${stim.x13wk?.toFixed(2)}% ± ${stim.x13wksd?.toFixed(2)}%`);
    console.log(`    CI:         [${stim.x13wk1?.toFixed(2)}% → ${stim.x13wk2?.toFixed(2)}%]`);
    console.log(`  40wk return:  ${stim.x40wk >= 0 ? "+" : ""}${stim.x40wk?.toFixed(2)}% ± n/a`);
    if (stim.is_stale) {
      console.log("  [!] ST-IM estimate is stale — insufficient sample for latest week");
    }
    console.log();
    console.log("Note: ST-IM outputs are conditional historical tendencies,");
    console.log("not guarantees, price targets, or buy/sell commands.");

  } catch (err: unknown) {
    const message = err instanceof Error ? err.message : String(err);
    // Expected in this demo environment — the stub payment proof will be rejected.
    // The important part is observing the payment challenge detection and flow.
    console.log(`[Expected in demo] ${message}`);
    console.log();
    console.log("To complete real payments:");
    console.log("  1. Set WALLET_PRIVATE_KEY and WALLET_ADDRESS");
    console.log("  2. Replace buildPaymentProof() with CDP SDK integration");
    console.log("  3. See: https://docs.cdp.coinbase.com/");
  }

  console.log();
  console.log("=".repeat(56));
  console.log("[x402 demonstration complete]");
}

demonstrateX402Flow().catch((err: unknown) => {
  console.error("Fatal error:", err instanceof Error ? err.message : String(err));
  process.exit(1);
});
