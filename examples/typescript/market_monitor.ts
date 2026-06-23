/**
 * Market Monitor (TypeScript)
 * ============================
 * Purpose:
 *   A market-monitoring agent that retrieves market regime, sector breadth,
 *   and sector leadership signals from Stock Trends, then synthesizes them
 *   into a structured market intelligence summary.
 *
 * What it demonstrates:
 *   - How to compose Stock Trends intelligence layers in TypeScript using
 *     native fetch with proper typing
 *   - How to handle x402 payment challenges inline — detect 402, surface
 *     pricing, and exit cleanly (x402 payment completion is in x402_client.ts)
 *   - How to type Stock Trends responses for downstream agent use
 *
 * Why a developer would use this:
 *   - TypeScript-native market monitoring agent or scheduled job
 *   - Foundation for an MCP tool that wraps Stock Trends market intelligence
 *   - Starting point for a Next.js or Node.js agent that needs regime context
 *
 * Environment variables:
 *   ST_API_BASE_URL   API base URL (default: https://api.stocktrends.com)
 *   ST_API_KEY        API key for subscription access
 *
 * Run (requires ts-node or tsx):
 *   npx tsx examples/typescript/market_monitor.ts
 */

const BASE_URL = (process.env.ST_API_BASE_URL ?? "https://api.stocktrends.com").replace(/\/$/, "");
const API_KEY = process.env.ST_API_KEY ?? "";

// ---------------------------------------------------------------------------
// Response types
// ---------------------------------------------------------------------------

interface RegimeResponse {
  regime: "bullish" | "bearish" | "mixed";
  confidence: "high" | "medium" | "low";
  regime_score: number;
  bullish_pct: number;
  bearish_pct: number;
  signal_count: number;
  weekdate: string;
}

interface SectorBreadthEntry {
  sector_code: string;
  sector_name: string;
  total: number;
  bullish_count: number;
  bearish_count: number;
  bullish_pct: number;
  bearish_pct: number;
  net_breadth: number;
  avg_rsi: number;
}

interface BreadthResponse {
  weekdate: string;
  count: number;
  data: SectorBreadthEntry[];
}

interface LeadershipEntry {
  symbol: string;
  exchange: string;
  rsi: number;
  mt_cnt: number;
  trend: string;
  trend_cnt: number;
  rsi_updn: string;
  sector_name: string;
}

interface LeadershipResponse {
  weekdate: string;
  overall_leaders: LeadershipEntry[];
  sector_leaders: LeadershipEntry[];
}

interface X402Challenge {
  error: string;
  protocol: string;
  pricing: {
    amount_usd: string;
    unit: string;
    network: string;
    token: string;
  };
}

// ---------------------------------------------------------------------------
// HTTP helpers
// ---------------------------------------------------------------------------

function buildHeaders(): Record<string, string> {
  const headers: Record<string, string> = { Accept: "application/json" };
  if (API_KEY) {
    headers["X-API-Key"] = API_KEY;
  }
  return headers;
}

async function get<T>(path: string, params?: Record<string, string>): Promise<T> {
  const url = new URL(`${BASE_URL}${path}`);
  if (params) {
    for (const [key, value] of Object.entries(params)) {
      url.searchParams.set(key, value);
    }
  }

  const resp = await fetch(url.toString(), { headers: buildHeaders() });

  if (resp.status === 402) {
    const challenge = (await resp.json()) as X402Challenge;
    const { pricing } = challenge;
    console.error(
      `\n[402 Payment Required]\n` +
      `  Endpoint: ${path}\n` +
      `  Amount:   ${pricing?.amount_usd ?? "?"} USD\n` +
      `  Network:  ${pricing?.network ?? "?"}\n\n` +
      `Set ST_API_KEY for subscription access, or implement x402 payment\n` +
      `(see examples/typescript/x402_client.ts).`
    );
    process.exit(1);
  }

  if (!resp.ok) {
    throw new Error(`HTTP ${resp.status} on ${path}: ${await resp.text()}`);
  }

  return resp.json() as Promise<T>;
}

// ---------------------------------------------------------------------------
// Intelligence retrieval
// ---------------------------------------------------------------------------

async function fetchMarketRegime(): Promise<RegimeResponse> {
  return get<RegimeResponse>("/v1/market/regime/latest");
}

async function fetchSectorBreadth(): Promise<BreadthResponse> {
  return get<BreadthResponse>("/v1/breadth/sector/latest");
}

async function fetchLeadership(): Promise<LeadershipResponse> {
  return get<LeadershipResponse>("/v1/leadership/summary/latest");
}

// ---------------------------------------------------------------------------
// Classification helpers
// ---------------------------------------------------------------------------

interface BreadthClassification {
  improving: string[];
  deteriorating: string[];
  neutral: string[];
}

function classifySectors(data: SectorBreadthEntry[]): BreadthClassification {
  const improving: string[] = [];
  const deteriorating: string[] = [];
  const neutral: string[] = [];

  for (const sector of data) {
    const name = sector.sector_name || sector.sector_code;
    if (sector.bullish_pct >= 0.60) {
      improving.push(name);
    } else if (sector.bearish_pct >= 0.55) {
      deteriorating.push(name);
    } else {
      neutral.push(name);
    }
  }

  return { improving, deteriorating, neutral };
}

// Represents one sector's share of the leadership constituent list returned by
// /v1/leadership/summary/latest — NOT a sector-wide RSI aggregate.
interface SectorLeadershipGroup {
  sector: string;
  count: number;
  avgConstituentRsi: number;
  topSymbols: string[];
}

function groupLeadershipBySector(leaders: LeadershipEntry[]): SectorLeadershipGroup[] {
  const buckets = new Map<string, { rsis: number[]; symbols: string[] }>();

  for (const leader of leaders) {
    const sector = leader.sector_name ?? "Unknown";
    const bucket = buckets.get(sector) ?? { rsis: [], symbols: [] };
    bucket.rsis.push(leader.rsi);
    // overall_leaders is returned sorted by RSI desc, so first symbols per
    // sector are the strongest constituents in that sector.
    const symEx = leader.exchange ? `${leader.symbol}-${leader.exchange}` : leader.symbol;
    bucket.symbols.push(symEx);
    buckets.set(sector, bucket);
  }

  return Array.from(buckets.entries())
    .map(([sector, bucket]) => ({
      sector,
      count: bucket.rsis.length,
      avgConstituentRsi: bucket.rsis.reduce((a, b) => a + b, 0) / bucket.rsis.length,
      topSymbols: bucket.symbols.slice(0, 3),
    }))
    .sort((a, b) => b.count - a.count || b.avgConstituentRsi - a.avgConstituentRsi);
}

// ---------------------------------------------------------------------------
// Output
// ---------------------------------------------------------------------------

function printSummary(
  regime: RegimeResponse,
  breadthData: BreadthResponse,
  leadership: LeadershipResponse,
): void {
  const breadth = classifySectors(breadthData.data);
  const sectorGroups = groupLeadershipBySector(leadership.overall_leaders);

  console.log();
  console.log("Stock Trends Market Intelligence Summary");
  console.log("=".repeat(56));

  // Regime
  console.log(`\nMarket Regime:  ${regime.regime.toUpperCase()} (${regime.confidence} confidence)`);
  console.log(`  Regime score: ${regime.regime_score >= 0 ? "+" : ""}${regime.regime_score.toFixed(3)}  (range: −1 bearish → +1 bullish)`);
  console.log(`  Bullish:      ${(regime.bullish_pct * 100).toFixed(1)}% of ${regime.signal_count.toLocaleString()} instruments`);
  console.log(`  Bearish:      ${(regime.bearish_pct * 100).toFixed(1)}%`);
  console.log(`  Week:         ${regime.weekdate}`);

  // Breadth
  console.log(`\nSector Breadth  (week: ${breadthData.weekdate})`);
  if (breadth.improving.length > 0) {
    console.log(`  Improving (≥60% bullish):     ${breadth.improving.join(", ")}`);
  } else {
    console.log(`  Improving:  none meeting threshold`);
  }
  if (breadth.deteriorating.length > 0) {
    console.log(`  Deteriorating (≥55% bearish): ${breadth.deteriorating.join(", ")}`);
  }
  if (breadth.neutral.length > 0) {
    console.log(`  Neutral:                      ${breadth.neutral.join(", ")}`);
  }

  // Leadership
  console.log(`\nSector Leadership  (week: ${leadership.weekdate})`);
  console.log(`  Source: /v1/leadership/summary/latest — leadership constituents only`);
  console.log(`  (Filtered: RSI >= 110 and mt_cnt >= 4; not sector-wide RSI averages)`);
  console.log(`  (RSI = relative strength vs benchmark; 100 = baseline, >100 = outperforming)`);
  if (sectorGroups.length > 0) {
    console.log("  Leadership constituents by sector:");
    for (const { sector, count, avgConstituentRsi, topSymbols } of sectorGroups.slice(0, 6)) {
      const examples = topSymbols.length > 0 ? topSymbols.join(", ") : "—";
      console.log(
        `    ${sector.padEnd(32)}  ${String(count).padStart(3)} leader(s)` +
        `  avg constituent RSI ${avgConstituentRsi.toFixed(1).padStart(7)}` +
        `  e.g. ${examples}`
      );
    }
  }

  console.log("\n" + "=".repeat(56));
  console.log("[Stock Trends market intelligence summary complete]");
  console.log();
}

// ---------------------------------------------------------------------------
// Main
// ---------------------------------------------------------------------------

async function run(): Promise<void> {
  console.log("Fetching market regime...");
  const regime = await fetchMarketRegime();

  console.log("Fetching sector breadth...");
  const breadth = await fetchSectorBreadth();

  console.log("Fetching sector leadership...");
  const leadership = await fetchLeadership();

  printSummary(regime, breadth, leadership);
}

run().catch((err: unknown) => {
  console.error("Error:", err instanceof Error ? err.message : String(err));
  process.exit(1);
});
