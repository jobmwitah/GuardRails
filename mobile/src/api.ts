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
 * Authorization header.
 *
 * The deployed PWA is served by the backend, so relative paths are same-origin.
 * The Android/iOS shell (Capacitor) serves the bundle from https://localhost;
 * a relative /api request there hits the app's own asset server and returns
 * index.html, which looks like an empty account list. Native production builds
 * therefore call the public backend directly.
 */
const isNativeShell =
  import.meta.env.PROD &&
  (window.location.hostname === "localhost" ||
    window.location.protocol === "capacitor:");
const BASE = isNativeShell
  ? `${__MOBILE_BACKEND_ORIGIN__.replace(/\/+$/, "")}/api/mobile`
  : "/api/mobile";

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
  const isJson = contentType.includes("application/json");
  if (!res.ok) {
    let message = `HTTP ${res.status}`;
    if (isJson) {
      const body = (await res.json()) as { message?: string; detail?: string };
      message = body.message || body.detail || message;
    }
    throw new Error(message);
  }
  // Never treat an HTML shell/404 page as a successful empty response — that
  // was what made a misconfigured APK look like "no accounts configured".
  if (!isJson) {
    throw new Error(
      `Unexpected non-JSON reply from ${BASE}${path} (HTTP ${res.status})`,
    );
  }
  return (await res.json()) as T;
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