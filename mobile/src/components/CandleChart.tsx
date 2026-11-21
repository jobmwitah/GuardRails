import { useEffect, useRef, useState, useCallback } from "react";
import { createChart, darkTheme } from "openalgo-charts";
import type { Chart } from "openalgo-charts";
import type { CandleLike, Position, Timeframe } from "../types";

const UP = "#26a69a";
const DOWN = "#ef5350";
const SL_COLOR = "#ef5350";
const TP_COLOR = "#26a69a";
const ENTRY_BUY_COLOR = "#4f8cff";
const ENTRY_SELL_COLOR = "#ff9800";

type OrderType =
  | "BUY_LIMIT"
  | "SELL_LIMIT"
  | "BUY_STOP"
  | "SELL_STOP"
  | "BUY_MARKET"
  | "SELL_MARKET"
  | null;

interface Props {
  candles: CandleLike[];
  timeframe: Timeframe;
  symbol?: string;
  positions?: Position[];
  onTimeframe: (tf: Timeframe) => void;
  loading: boolean;
  onOrderSubmit?: (order: {
    type: OrderType;
    entry: number;
    sl: number | null;
    tp: number | null;
  }) => void;
}

const TIMEFRAMES: Timeframe[] = ["M1", "M5", "M15", "M30", "H1", "H4", "D1"];

const ORDER_TYPES: { type: OrderType; label: string; color: string }[] = [
  { type: "BUY_LIMIT", label: "Buy Limit", color: ENTRY_BUY_COLOR },
  { type: "SELL_LIMIT", label: "Sell Limit", color: ENTRY_SELL_COLOR },
  { type: "BUY_STOP", label: "Buy Stop", color: ENTRY_BUY_COLOR },
  { type: "SELL_STOP", label: "Sell Stop", color: ENTRY_SELL_COLOR },
  { type: "BUY_MARKET", label: "Buy", color: ENTRY_BUY_COLOR },
  { type: "SELL_MARKET", label: "Sell", color: ENTRY_SELL_COLOR },
];

// Decimal places for a price, so FX (1.10047) and indices (19000) both read cleanly.
function digits(p: number): number {
  if (!isFinite(p)) return 5;
  const abs = Math.abs(p);
  if (abs >= 1000) return 1;
  if (abs >= 100) return 2;
  if (abs >= 10) return 3;
  if (abs >= 1) return 4;
  return 5;
}

export default function CandleChart({
  candles,
  timeframe,
  symbol,
  positions,
  onTimeframe,
  loading,
  onOrderSubmit,
}: Props) {
  const holderRef = useRef<HTMLDivElement | null>(null);
  const chartRef = useRef<Chart | null>(null);
  const seriesRef = useRef<ReturnType<Chart["addSeries"]> | null>(null);
  const candlesRef = useRef(candles);
  candlesRef.current = candles;

  // Order placement state
  const [selectedOrderType, setSelectedOrderType] = useState<OrderType>(null);
  const [entryPrice, setEntryPrice] = useState<number | null>(null);
  const [slPrice, setSlPrice] = useState<number | null>(null);
  const [tpPrice, setTpPrice] = useState<number | null>(null);

  // When an order type is picked, seed entry near the last close and SL a few
  // ticks away, on the correct side.
  useEffect(() => {
    if (selectedOrderType && candles.length > 0) {
      const last = candles[candles.length - 1];
      const isBuy = selectedOrderType.startsWith("BUY");
      const offset = (last.high - last.low) * 2 || Math.max(last.close * 0.0005, 0.001);
      const entry = isBuy ? last.close - offset * 0.2 : last.close + offset * 0.2;
      setEntryPrice(entry);
      setSlPrice(isBuy ? entry - offset : entry + offset);
    } else {
      setEntryPrice(null);
      setSlPrice(null);
      setTpPrice(null);
    }
  }, [selectedOrderType, candles]);

  // Take profit is never client-controlled: fixed 3.25R, matching the backend/EA.
  useEffect(() => {
    if (entryPrice !== null && slPrice !== null && selectedOrderType) {
      const isBuy = selectedOrderType.startsWith("BUY");
      const distance = Math.abs(entryPrice - slPrice);
      setTpPrice(isBuy ? entryPrice + distance * 3.25 : entryPrice - distance * 3.25);
    } else {
      setTpPrice(null);
    }
  }, [entryPrice, slPrice, selectedOrderType]);

  // Create the OpenAlgo chart once. It owns its own resize observer.
  useEffect(() => {
    const holder = holderRef.current;
    if (!holder) return;

    const theme = {
      ...darkTheme,
      background: "#0f141e",
      grid: "#1a2230",
      axisText: "#8a94a6",
      axisLine: "#1a2230",
      paneSeparator: "#1a2230",
      crosshair: "#3a4658",
      crosshairStyle: "dashed" as const,
      upColor: UP,
      downColor: DOWN,
      wickUpColor: UP,
      wickDownColor: DOWN,
      lastPriceUp: UP,
      lastPriceDown: DOWN,
      lastPriceText: "#e8ecf2",
    };

    let zone = "UTC";
    try {
      zone = Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";
    } catch {
      zone = "UTC";
    }

    const chart = createChart(holder, { theme, timezone: zone });
    const prec = candlesRef.current.length
      ? digits(candlesRef.current[candlesRef.current.length - 1].close)
      : 5;
    const series = chart.addSeries("candlestick", {
      style: {
        precision: prec,
        priceLineVisible: true,
        lastValueVisible: true,
      },
    });
    series.setData(candlesRef.current);
    chart.fitContent();

    chartRef.current = chart;
    seriesRef.current = series;

    // Wire draggable order / bracket lines back into React state.
    const trading = chart.trading;
    const offModify = trading.on("trading:order_modify", (e) => {
      const ev = e as { orderId: string; newPrice: number };
      if (ev.orderId === "entry") setEntryPrice(ev.newPrice);
    });
    const offBracket = trading.on("trading:bracket_modify", (e) => {
      const ev = e as { bracketRole: string; newPrice: number };
      if (ev.bracketRole === "sl") setSlPrice(ev.newPrice);
    });

    return () => {
      offModify();
      offBracket();
      chart.destroy();
      chartRef.current = null;
      seriesRef.current = null;
    };
  }, []);

  // Push fresh bars into the series as they arrive.
  useEffect(() => {
    const series = seriesRef.current;
    if (!series) return;
    if (candles.length) {
      const prec = digits(candles[candles.length - 1].close);
      series.applyOptions({ precision: prec });
    }
    series.setData(candles);
  }, [candles]);

  // Re-fit when the symbol or timeframe changes so the new history fills the pane.
  useEffect(() => {
    chartRef.current?.fitContent();
  }, [symbol, timeframe]);

  // Push the entry/SL/TP draft as on-chart order lines.
  useEffect(() => {
    const chart = chartRef.current;
    if (!chart) return;
    if (!selectedOrderType || entryPrice === null || slPrice === null || tpPrice === null) {
      chart.trading.setOrders([]);
      return;
    }
    const isBuy = selectedOrderType.startsWith("BUY");
    const side = isBuy ? ("buy" as const) : ("sell" as const);
    const kind = selectedOrderType.endsWith("_STOP") ? ("stop" as const) : ("limit" as const);
    chart.trading.setOrders([
      {
        id: "entry",
        type: kind,
        side,
        price: entryPrice,
        size: 0.01,
        draggable: true,
        color: isBuy ? ENTRY_BUY_COLOR : ENTRY_SELL_COLOR,
      },
      {
        id: "sl",
        type: kind,
        side,
        price: slPrice,
        size: 0.01,
        draggable: true,
        bracketRole: "sl",
        parentId: "entry",
        color: SL_COLOR,
      },
      {
        id: "tp",
        type: kind,
        side,
        price: tpPrice,
        size: 0.01,
        draggable: false,
        readOnly: true,
        bracketRole: "tp",
        parentId: "entry",
        color: TP_COLOR,
      },
    ]);
  }, [selectedOrderType, entryPrice, slPrice, tpPrice]);

  // Show open positions on the chart for this symbol.
  useEffect(() => {
    const chart = chartRef.current;
    if (!chart) return;
    const scoped = (positions || []).filter((p) => !symbol || p.symbol === symbol);
    chart.trading.setPositions(
      scoped.map((p) => ({
        id: `pos:${p.ticket}`,
        side: p.type === "BUY" ? ("long" as const) : ("short" as const),
        entryPrice: p.open_price,
        size: p.volume,
        pnlText: p.profit.toFixed(2),
      })),
    );
  }, [positions, symbol]);

  const handleSubmitOrder = useCallback(() => {
    if (selectedOrderType && entryPrice !== null) {
      if (onOrderSubmit) {
        onOrderSubmit({ type: selectedOrderType, entry: entryPrice, sl: slPrice, tp: tpPrice });
      }
      setSelectedOrderType(null);
    }
  }, [selectedOrderType, entryPrice, slPrice, tpPrice, onOrderSubmit]);

  const handleCancelOrder = useCallback(() => {
    setSelectedOrderType(null);
  }, []);

  return (
    <div className="chart-card">
      <div className="order-type-bar">
        {ORDER_TYPES.map((ot) => (
          <button
            key={ot.type}
            className={`order-type-btn ${selectedOrderType === ot.type ? "active" : ""}`}
            style={{ borderColor: ot.color, color: selectedOrderType === ot.type ? "#fff" : ot.color }}
            onClick={() => setSelectedOrderType(selectedOrderType === ot.type ? null : ot.type)}
          >
            {ot.label}
          </button>
        ))}
      </div>

      <div className="tf-switcher">
        {TIMEFRAMES.map((tf) => (
          <button
            key={tf}
            className={`tf-btn ${tf === timeframe ? "active" : ""}`}
            onClick={() => onTimeframe(tf)}
          >
            {tf}
          </button>
        ))}
      </div>

      <div className="chart-holder" ref={holderRef} style={{ position: "relative", flex: 1, minHeight: 260 }}>
        {loading && candles.length === 0 && (
          <div className="chart-loading">Loading candles…</div>
        )}
        {!loading && candles.length === 0 && (
          <div className="chart-loading">No candles yet</div>
        )}
      </div>

      {selectedOrderType && (
        <div className="order-info-bar">
          <span>Drag entry / SL — TP auto at 3.25R</span>
          <span className="order-prices">
            Entry: {entryPrice?.toFixed(5)} | SL: {slPrice?.toFixed(5)} | TP: {tpPrice?.toFixed(5)}
          </span>
          <div className="order-submit-actions">
            <button className="submit-order-btn" onClick={handleSubmitOrder} disabled={slPrice === null}>
              Place{" "}
              {ORDER_TYPES.find((ot) => ot.type === selectedOrderType)?.label ||
                selectedOrderType.replace("_", " ")}
            </button>
            <button className="cancel-order-btn" onClick={handleCancelOrder}>
              ✕
            </button>
          </div>
        </div>
      )}
    </div>
  );
}