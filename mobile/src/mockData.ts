// Mock data for local testing without a backend
import type { Account, Position, PendingOrder, CandleLike, TradeHistoryItem } from "./types";

export const MOCK_ACCOUNTS: Account[] = [
  {
    login: "1345721557",
    platform: "mt5",
    phase: "funded",
    display_name: "Pepperstone Demo",
    server: "Pepperstone-Demo",
    instance_status: "connected",
    balance: 10450.25,
    equity: 10680.50,
    margin: 420.00,
    free_margin: 10260.50,
    blocked: false,
    market_closed: false,
    read_only: false,
    risk_pct: 1.0,
    max_positions: 2,
    positions: 2,
    pending: 1,
    symbols: ["EURUSD", "GBPUSD", "XAUUSD", "USDJPY"],
    last_report_ts: Date.now() / 1000,
  },
  {
    login: "1515282832",
    platform: "mt5",
    phase: "funded",
    display_name: "FTMO Demo",
    server: "FTMO-Demo",
    instance_status: "connected",
    balance: 100000.00,
    equity: 99850.00,
    margin: 1200.00,
    free_margin: 98650.00,
    blocked: false,
    market_closed: false,
    read_only: true,
    risk_pct: 0.5,
    max_positions: 2,
    positions: 1,
    pending: 0,
    symbols: ["EURUSD", "GBPUSD", "US30", "NAS100"],
    last_report_ts: Date.now() / 1000,
  },
  {
    login: "61603648",
    platform: "mt5",
    phase: "challenge",
    display_name: "Pepperstone Demo 2",
    server: "Pepperstone-Demo",
    instance_status: "connected",
    balance: 5000.00,
    equity: 5120.30,
    margin: 280.00,
    free_margin: 4840.30,
    blocked: false,
    market_closed: false,
    read_only: false,
    risk_pct: 1.0,
    max_positions: 2,
    positions: 0,
    pending: 2,
    symbols: ["EURUSD", "GBPUSD", "AUDUSD"],
    last_report_ts: Date.now() / 1000,
  },
];

export const MOCK_POSITIONS: Record<string, Position[]> = {
  "1345721557": [
    {
      ticket: "12345678",
      symbol: "EURUSD",
      type: "BUY",
      volume: 0.10,
      open_price: 1.08450,
      current: 1.08620,
      profit: 17.00,
      sl: 1.08200,
      tp: 1.09000,
    },
    {
      ticket: "12345679",
      symbol: "GBPUSD",
      type: "SELL",
      volume: 0.05,
      open_price: 1.26500,
      current: 1.26350,
      profit: 7.50,
      sl: 1.26800,
      tp: 1.26000,
    },
  ],
  "1515282832": [
    {
      ticket: "98765432",
      symbol: "US30",
      type: "BUY",
      volume: 0.01,
      open_price: 38500.00,
      current: 38620.00,
      profit: 12.00,
      sl: 38400.00,
      tp: 38800.00,
    },
  ],
  "61603648": [],
};

export const MOCK_PENDING: Record<string, PendingOrder[]> = {
  "1345721557": [
    {
      ticket: "87654321",
      symbol: "XAUUSD",
      type: "BUY_LIMIT",
      price: 2340.50,
      sl: 2335.00,
      tp: 2350.00,
    },
  ],
  "1515282832": [],
  "61603648": [
    {
      ticket: "11223344",
      symbol: "EURUSD",
      type: "SELL_LIMIT",
      price: 1.09000,
      sl: 1.09200,
      tp: 1.08500,
    },
    {
      ticket: "11223345",
      symbol: "GBPUSD",
      type: "BUY_STOP",
      price: 1.27000,
      sl: 1.26700,
      tp: 1.27500,
    },
  ],
};

// Generate mock candle data
export function generateMockCandles(symbol: string, count: number = 60): CandleLike[] {
  const candles: CandleLike[] = [];
  const basePrice = symbol === "EURUSD" ? 1.085 : symbol === "GBPUSD" ? 1.265 : symbol === "XAUUSD" ? 2345 : 1.5;
  let price = basePrice;
  const now = Math.floor(Date.now() / 1000);
  const interval = 900; // 15 minutes

  for (let i = count - 1; i >= 0; i--) {
    const time = now - i * interval;
    const change = (Math.random() - 0.5) * 0.002;
    const open = price;
    const close = price + change;
    const high = Math.max(open, close) + Math.random() * 0.001;
    const low = Math.min(open, close) - Math.random() * 0.001;
    candles.push({
      time,
      open: parseFloat(open.toFixed(5)),
      high: parseFloat(high.toFixed(5)),
      low: parseFloat(low.toFixed(5)),
      close: parseFloat(close.toFixed(5)),
    });
    price = close;
  }

  return candles;
}

export const MOCK_CANDLES: Record<string, CandleLike[]> = {
  EURUSD: generateMockCandles("EURUSD", 80),
  GBPUSD: generateMockCandles("GBPUSD", 80),
  XAUUSD: generateMockCandles("XAUUSD", 80),
  USDJPY: generateMockCandles("USDJPY", 80),
  US30: generateMockCandles("US30", 80),
  NAS100: generateMockCandles("NAS100", 80),
  AUDUSD: generateMockCandles("AUDUSD", 80),
};

const MOCK_NOW = Math.floor(Date.now() / 1000);

export const MOCK_HISTORY: Record<string, TradeHistoryItem[]> = {
  "1345721557": [
    { time: MOCK_NOW - 3600, symbol: "EURUSD", ticket: "88421001", reason: "TP", volume: "0.10", profit: 17.0 },
    { time: MOCK_NOW - 9900, symbol: "GBPUSD", ticket: "88421002", reason: "SL", volume: "0.05", profit: -12.5 },
    { time: MOCK_NOW - 21600, symbol: "EURUSD", ticket: "88421003", reason: "MANUAL", volume: "0.08", profit: 6.4 },
  ],
  "1515282832": [
    { time: MOCK_NOW - 5400, symbol: "US30", ticket: "98765001", reason: "TP", volume: "0.01", profit: 12.0 },
    { time: MOCK_NOW - 18000, symbol: "NAS100", ticket: "98765002", reason: "SL", volume: "0.02", profit: -8.25 },
  ],
  "61603648": [
    { time: MOCK_NOW - 7200, symbol: "EURUSD", ticket: "71603001", reason: "MANUAL", volume: "0.10", profit: 4.8 },
  ],
};