import type {
  AccountsResponse,
  CandlesResponse,
  CandleLike,
  HistoryResponse,
  MarketCandlesResponse,
  PlaceOrderPayload,
  PositionsResponse,
  QuotesResponse,
  Timeframe,
  TradeResult,
} from "./types";

/**
 * Backend mobile API client. The app is token-gated like the rest of the
 * backend: the bearer token is stored in localStorage and sent as an
 * Authorization header (the Vite dev proxy forwards /api to the FastAPI
 * backend, so relative paths work in both dev and production behind the same
 * origin).
 */
const BASE = "/api/mobile";

function authHeaders(): Record<string, string> {
  const headers: Record<string, string> = {
    "Content-Type": "application/json",
    // ngrok's free tier serves an HTML interstitial to browser-like clients
    // without this header, which would otherwise look like an empty API reply.
    "ngrok-skip-browser-warning": "true",
  };
  const token = localStorage.getItem("guardrails.token");
  if (token) headers["Authorization"] = `Bearer ${token}`;
  return headers;
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${BASE}${path}`, {
    ...init,
    headers: { ...authHeaders(), ...(init?.headers || {}) },
  });
  const contentType = res.headers.get("content-type") || "";
  const body = contentType.includes("application/json")
    ? await res.json()
    : { message: await res.text() };
  if (!res.ok) {
    throw new Error((body as { message?: string }).message || `HTTP ${res.status}`);
  }
  return body as T;
}

export const api = {
  accounts: () => request<AccountsResponse>("/accounts"),
  positions: (account: string) =>
    request<PositionsResponse>(`/positions?account=${encodeURIComponent(account)}`),
  history: (account: string) =>
    request<HistoryResponse>(`/history?account=${encodeURIComponent(account)}`),
  candles: (account: string, symbol: string, timeframe: Timeframe) =>
    request<CandlesResponse>(
      `/candles?account=${encodeURIComponent(account)}&symbol=${encodeURIComponent(
        symbol,
      )}&timeframe=${timeframe}`,
    ),
  quotes: (account: string, symbol: string) =>
    request<QuotesResponse>(
      `/quotes?account=${encodeURIComponent(account)}&symbol=${encodeURIComponent(symbol)}`,
    ),
  marketCandles: (symbol: string, timeframe: Timeframe, account?: string) =>
    request<MarketCandlesResponse>(
      `/market/candles?symbol=${encodeURIComponent(symbol)}&timeframe=${timeframe}` +
        (account ? `&account=${encodeURIComponent(account)}` : ""),
    ),
  trade: (payload: PlaceOrderPayload) =>
    request<TradeResult>("/trade", {
      method: "POST",
      body: JSON.stringify(payload),
    }),
  close: (account: string, ticket: string) =>
    request<TradeResult>(`/trade/close`, {
      method: "POST",
      body: JSON.stringify({ account, ticket }),
    }),
  cancel: (account: string, ticket: string) =>
    request<TradeResult>(`/trade/cancel`, {
      method: "POST",
      body: JSON.stringify({ account, ticket }),
    }),
};

/** Simple exponential backoff poll for live positions while app is foregrounded. */
export function poll<T>(fn: () => Promise<T>, intervalMs: number): () => void {
  let alive = true;
  let timer: number;
  let last = 0;
  const tick = async () => {
    if (!alive) return;
    const now = Date.now();
    if (now - last >= intervalMs) {
      last = now;
      try {
        await fn();
      } catch {
        /* transient — keep polling */
      }
    }
    timer = window.setTimeout(tick, intervalMs);
  };
  timer = window.setTimeout(tick, intervalMs);
  return () => {
    alive = false;
    window.clearTimeout(timer);
  };
}

export type { CandleLike };