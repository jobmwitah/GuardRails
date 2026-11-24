// Shared domain types mirroring the backend /api/mobile responses.

export type Platform = "mt5" | "ctrader";
export type AccountPhase = "challenge" | "funded" | null;

export interface Account {
  login: string;
  platform: Platform;
  phase: AccountPhase;
  display_name: string;
  server: string;
  instance_status: string;
  balance: number;
  equity: number;
  margin: number;
  free_margin: number;
  blocked: boolean;
  market_closed: boolean;
  read_only: boolean;
  risk_pct: number;
  max_positions: number;
  positions: number;
  pending: number;
  symbols: string[];
  last_report_ts: number;
}

export type PositionType = "BUY" | "SELL";

export interface Position {
  ticket: string;
  symbol: string;
  type: PositionType;
  volume: number;
  open_price: number;
  current: number;
  profit: number;
  sl: number | null;
  tp: number | null;
}

export type OrderType =
  | "BUY_LIMIT"
  | "SELL_LIMIT"
  | "BUY_STOP"
  | "SELL_STOP"
  | "OTHER";

export interface PendingOrder {
  ticket: string;
  symbol: string;
  type: OrderType;
  price: number;
  sl: number | null;
  tp: number | null;
}

export interface AccountsResponse {
  accounts: Account[];
  read_only_accounts: string[];
  trade_accounts: string[];
  copy?: {
    enabled: boolean;
    master: string | null;
    slaves: string[];
  };
}

export interface CopyLeg {
  slave_login: string;
  slave_ticket: string;
  status: string;
  detail: string;
}

export interface CopyLink {
  id: number;
  master_ticket: string;
  master_order_ticket: string;
  kind: "position" | "pending" | string;
  symbol: string;
  side: string;
  entry: string;
  sl: string;
  status: string;
  reason: string;
  created_ts: number;
  legs: CopyLeg[];
}

export interface CopyStatus {
  enabled: boolean;
  baselined: boolean;
  master: string | null;
  slaves: string[];
  links: CopyLink[];
  pending_min_age: number;
}

export interface PositionsResponse {
  account: string;
  positions: Position[];
  pending: PendingOrder[];
  blocked: boolean;
  read_only: boolean;
}

export interface TradeHistoryItem {
  time: number;
  symbol: string;
  ticket: string;
  reason: string;
  volume: string;
  profit: number;
  source?: "bot" | "broker";
}

export interface HistoryResponse {
  account: string;
  trades: TradeHistoryItem[];
  summary: {
    trades: number;
    wins: number;
    losses: number;
    net_profit: number;
    broker_trades?: number;
  };
}

export type Timeframe = "M1" | "M5" | "M15" | "M30" | "H1" | "H4" | "D1";

export interface Candle {
  time: number;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
}

export interface CandlesResponse {
  account: string;
  symbol: string;
  timeframe: string;
  candles: Candle[];
  bid?: number | null;
  ask?: number | null;
  spread?: number | null;
}

export interface QuotesResponse {
  account: string;
  symbol: string;
  bid: number | null;
  ask: number | null;
  spread: number | null;
  report_age: number | null;
}

export interface MarketCandlesResponse {
  symbol: string;
  instrument?: string;
  timeframe: string;
  candles: Candle[];
  bid?: number | null;
  ask?: number | null;
  spread?: number | null;
  last?: number | null;
  source: string;
  quote_source?: string;
  cached_at?: number;
}

export type TradeSide = "BUY" | "SELL";
export type OrderKind = "market" | "limit";

export interface PlaceOrderPayload {
  account: string;
  symbol: string;
  side: TradeSide;
  kind: OrderKind;
  sl: number | null;
  tp: number | null;
  price: number | null; // required for limit orders
}

export interface TradeResult {
  ok: boolean;
  message: string;
  ticket?: string;
  confirmed?: boolean;
  queued?: boolean;
}

export interface CandleLike {
  time: number;
  open: number;
  high: number;
  low: number;
  close: number;
}