import { useEffect, useRef, useState, useCallback } from "react";
import type { CandleLike, Timeframe } from "../types";

const UP = "#26a69a";
const DOWN = "#ef5350";
const SL_COLOR = "#ef5350";
const TP_COLOR = "#26a69a";
const ENTRY_BUY_COLOR = "#4f8cff";
const ENTRY_SELL_COLOR = "#ff9800";
const ORDER_BTN_COLOR = "#4f8cff";

type OrderType = "BUY_LIMIT" | "SELL_LIMIT" | "BUY_STOP" | "SELL_STOP" | "BUY_MARKET" | "SELL_MARKET" | null;

interface Props {
  candles: CandleLike[];
  timeframe: Timeframe;
  onTimeframe: (tf: Timeframe) => void;
  loading: boolean;
  // Callback when order is submitted
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

export default function CandleChart({ candles, timeframe, onTimeframe, loading, onOrderSubmit }: Props) {
  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  const containerRef = useRef<HTMLDivElement | null>(null);

  // Order placement state
  const [selectedOrderType, setSelectedOrderType] = useState<OrderType>(null);
  const [entryPrice, setEntryPrice] = useState<number | null>(null);
  const [slPrice, setSlPrice] = useState<number | null>(null);
  const [tpPrice, setTpPrice] = useState<number | null>(null);
  const [dragging, setDragging] = useState<"entry" | "sl" | "tp" | null>(null);
  const [hoverLine, setHoverLine] = useState<"entry" | "sl" | "tp" | null>(null);
  const [showSubmitBtn, setShowSubmitBtn] = useState(false);
  const [submitBtnPos, setSubmitBtnPos] = useState({ x: 0, y: 0 });

  // Refs for drawing
  const linesRef = useRef<{ entry: number | null; sl: number | null; tp: number | null }>({
    entry: null,
    sl: null,
    tp: null,
  });

  // Update lines ref
  useEffect(() => {
    linesRef.current = { entry: entryPrice, sl: slPrice, tp: tpPrice };
  }, [entryPrice, slPrice, tpPrice]);

  // When order type is selected, set initial entry price to current price
  useEffect(() => {
    if (selectedOrderType && candles.length > 0) {
      const lastCandle = candles[candles.length - 1];
      const mid = (lastCandle.high + lastCandle.low) / 2;
      setEntryPrice(mid);
      // Set default SL/TP based on order type
      const isBuy = selectedOrderType.startsWith("BUY");
      const offset = (lastCandle.high - lastCandle.low) * 2 || 0.001;
      setSlPrice(isBuy ? mid - offset : mid + offset);
      setTpPrice(isBuy ? mid + offset * 2 : mid - offset * 2);
    } else {
      setEntryPrice(null);
      setSlPrice(null);
      setTpPrice(null);
    }
  }, [selectedOrderType, candles]);

  const getPriceRange = useCallback(() => {
    if (!candles.length) return { min: 0, max: 1 };
    let min = Infinity, max = -Infinity;
    for (const c of candles) {
      if (c.low < min) min = c.low;
      if (c.high > max) max = c.high;
    }
    const lines = linesRef.current;
    if (lines.entry !== null) { min = Math.min(min, lines.entry); max = Math.max(max, lines.entry); }
    if (lines.sl !== null) { min = Math.min(min, lines.sl); max = Math.max(max, lines.sl); }
    if (lines.tp !== null) { min = Math.min(min, lines.tp); max = Math.max(max, lines.tp); }
    const pad = (max - min) * 0.08 || 1;
    return { min: min - pad, max: max + pad };
  }, [candles]);

  const yAt = useCallback((price: number, min: number, max: number, plotH: number, padT: number) => {
    return padT + plotH * (1 - (price - min) / (max - min));
  }, []);

  const getPriceAtY = useCallback((y: number) => {
    const canvas = canvasRef.current;
    if (!canvas) return 0;
    const rect = canvas.getBoundingClientRect();
    const H = rect.height;
    const padT = 10, padB = 8;
    const plotH = H - padT - padB;
    const { min, max } = getPriceRange();
    const ratio = 1 - (y - padT) / plotH;
    return min + ratio * (max - min);
  }, [getPriceRange]);

  const draw = useCallback(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const dpr = window.devicePixelRatio || 1;
    const rect = canvas.getBoundingClientRect();
    canvas.width = Math.round(rect.width * dpr);
    canvas.height = Math.round(rect.height * dpr);
    const ctx = canvas.getContext("2d");
    if (!ctx) return;
    ctx.scale(dpr, dpr);

    const W = rect.width;
    const H = rect.height;
    ctx.clearRect(0, 0, W, H);
    ctx.fillStyle = "#0f141e";
    ctx.fillRect(0, 0, W, H);

    if (loading) {
      ctx.fillStyle = "#8a94a6";
      ctx.font = "13px system-ui";
      ctx.textAlign = "center";
      ctx.fillText("Loading candles…", W / 2, H / 2);
      return;
    }
    if (!candles.length) {
      ctx.fillStyle = "#8a94a6";
      ctx.font = "13px system-ui";
      ctx.textAlign = "center";
      ctx.fillText("No candles yet", W / 2, H / 2);
      return;
    }

    const data = candles;
    const { min, max } = getPriceRange();
    const padL = 8;
    const padR = 60;
    const padT = 10;
    const padB = 8;
    const plotW = W - padL - padR;
    const plotH = H - padT - padB;
    const bodyW = Math.max(2, Math.min(18, plotW / data.length - 2));
    const halfBody = bodyW / 2;

    const xAt = (i: number) => padL + (plotW * (i + 0.5)) / data.length;
    const yAtPrice = (v: number) => padT + plotH * (1 - (v - min) / (max - min));

    // Grid
    ctx.strokeStyle = "#1a2230";
    ctx.lineWidth = 1;
    ctx.beginPath();
    const steps = 5;
    for (let i = 0; i <= steps; i++) {
      const y = padT + (plotH * i) / steps;
      ctx.moveTo(padL, y);
      ctx.lineTo(W - padR, y);
    }
    ctx.stroke();

    // Candle bodies + wicks
    for (let i = 0; i < data.length; i++) {
      const c = data[i];
      const up = c.close >= c.open;
      const color = up ? UP : DOWN;
      const x = xAt(i);
      ctx.strokeStyle = color;
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.moveTo(x, yAtPrice(c.high));
      ctx.lineTo(x, yAtPrice(c.low));
      ctx.stroke();

      const openY = yAtPrice(c.open);
      const closeY = yAtPrice(c.close);
      const top = Math.min(openY, closeY);
      const h = Math.max(1, Math.abs(closeY - openY));
      ctx.fillStyle = color;
      ctx.fillRect(x - halfBody, top, bodyW, h);
    }

    const lines = linesRef.current;
    const isBuy = selectedOrderType?.startsWith("BUY");

    // Draw entry line
    if (lines.entry !== null) {
      const y = yAtPrice(lines.entry);
      const color = isBuy ? ENTRY_BUY_COLOR : ENTRY_SELL_COLOR;
      ctx.strokeStyle = color;
      ctx.lineWidth = 2;
      ctx.setLineDash([]);
      ctx.beginPath();
      ctx.moveTo(padL, y);
      ctx.lineTo(W - padR, y);
      ctx.stroke();

      // Label
      ctx.fillStyle = color;
      ctx.font = "bold 11px system-ui";
      ctx.textAlign = "left";
      ctx.fillText(`${selectedOrderType?.replace("_", " ")} @ ${lines.entry.toFixed(5)}`, padL + 4, y - 6);

      // Draggable handle
      ctx.fillStyle = color;
      ctx.beginPath();
      ctx.arc(padL + 20, y, 5, 0, Math.PI * 2);
      ctx.fill();
      ctx.strokeStyle = "#0f141e";
      ctx.lineWidth = 1;
      ctx.stroke();
    }

    // Draw SL line
    if (lines.sl !== null) {
      const y = yAtPrice(lines.sl);
      ctx.strokeStyle = SL_COLOR;
      ctx.lineWidth = 2;
      ctx.setLineDash([6, 4]);
      ctx.beginPath();
      ctx.moveTo(padL, y);
      ctx.lineTo(W - padR, y);
      ctx.stroke();
      ctx.setLineDash([]);

      ctx.fillStyle = SL_COLOR;
      ctx.font = "bold 11px system-ui";
      ctx.textAlign = "left";
      ctx.fillText(`SL ${lines.sl.toFixed(5)}`, padL + 4, y - 6);

      ctx.fillStyle = SL_COLOR;
      ctx.beginPath();
      ctx.arc(padL + 20, y, 5, 0, Math.PI * 2);
      ctx.fill();
      ctx.strokeStyle = "#0f141e";
      ctx.lineWidth = 1;
      ctx.stroke();
    }

    // Draw TP line
    if (lines.tp !== null) {
      const y = yAtPrice(lines.tp);
      ctx.strokeStyle = TP_COLOR;
      ctx.lineWidth = 2;
      ctx.setLineDash([6, 4]);
      ctx.beginPath();
      ctx.moveTo(padL, y);
      ctx.lineTo(W - padR, y);
      ctx.stroke();
      ctx.setLineDash([]);

      ctx.fillStyle = TP_COLOR;
      ctx.font = "bold 11px system-ui";
      ctx.textAlign = "left";
      ctx.fillText(`TP ${lines.tp.toFixed(5)}`, padL + 4, y - 6);

      ctx.fillStyle = TP_COLOR;
      ctx.beginPath();
      ctx.arc(padL + 20, y, 5, 0, Math.PI * 2);
      ctx.fill();
      ctx.strokeStyle = "#0f141e";
      ctx.lineWidth = 1;
      ctx.stroke();
    }

    // Price labels on right axis
    ctx.fillStyle = "#8a94a6";
    ctx.font = "10px system-ui";
    ctx.textAlign = "right";
    ctx.fillText(max.toFixed(5), W - padR - 4, 12);
    ctx.fillText(min.toFixed(5), W - padR - 4, H - 6);

    // Draw submit button position
    if (lines.entry !== null) {
      const y = yAtPrice(lines.entry);
      setShowSubmitBtn(true);
      setSubmitBtnPos({ x: padL + 10, y: y - 30 });
    } else {
      setShowSubmitBtn(false);
    }
  }, [candles, loading, timeframe, selectedOrderType, entryPrice, slPrice, tpPrice]);

  const findNearestLine = useCallback((y: number): "entry" | "sl" | "tp" | null => {
    const canvas = canvasRef.current;
    if (!canvas) return null;
    const rect = canvas.getBoundingClientRect();
    const H = rect.height;
    const padT = 10, padB = 8;
    const plotH = H - padT - padB;
    const { min, max } = getPriceRange();
    const toY = (price: number) => padT + plotH * (1 - (price - min) / (max - min));

    let nearest: "entry" | "sl" | "tp" | null = null;
    let minDist = 12;
    const lines = linesRef.current;
    if (lines.entry !== null) {
      const dist = Math.abs(y - toY(lines.entry));
      if (dist < minDist) { minDist = dist; nearest = "entry"; }
    }
    if (lines.sl !== null) {
      const dist = Math.abs(y - toY(lines.sl));
      if (dist < minDist) { minDist = dist; nearest = "sl"; }
    }
    if (lines.tp !== null) {
      const dist = Math.abs(y - toY(lines.tp));
      if (dist < minDist) { minDist = dist; nearest = "tp"; }
    }
    return nearest;
  }, [getPriceRange]);

  const handleMouseDown = useCallback((e: React.MouseEvent) => {
    const canvas = canvasRef.current;
    if (!canvas || !selectedOrderType) return;
    const rect = canvas.getBoundingClientRect();
    const y = e.clientY - rect.top;
    const line = findNearestLine(y);
    if (line) {
      setDragging(line);
      canvas.style.cursor = "ns-resize";
    }
  }, [selectedOrderType, findNearestLine]);

  const handleMouseMove = useCallback((e: React.MouseEvent) => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const rect = canvas.getBoundingClientRect();
    const y = e.clientY - rect.top;

    if (dragging) {
      const price = getPriceAtY(y);
      if (dragging === "entry") setEntryPrice(price);
      else if (dragging === "sl") setSlPrice(price);
      else if (dragging === "tp") setTpPrice(price);
    } else {
      const nearest = findNearestLine(y);
      setHoverLine(nearest);
      canvas.style.cursor = nearest ? "ns-resize" : "default";
    }
  }, [dragging, getPriceAtY, findNearestLine]);

  const handleMouseUp = useCallback(() => {
    if (dragging) {
      setDragging(null);
      const canvas = canvasRef.current;
      if (canvas) canvas.style.cursor = "default";
    }
  }, [dragging]);

  const handleTouchStart = useCallback((e: React.TouchEvent) => {
    const canvas = canvasRef.current;
    if (!canvas || !selectedOrderType || !e.touches.length) return;
    const rect = canvas.getBoundingClientRect();
    const y = e.touches[0].clientY - rect.top;
    const line = findNearestLine(y);
    if (line) setDragging(line);
  }, [selectedOrderType, findNearestLine]);

  const handleTouchMove = useCallback((e: React.TouchEvent) => {
    if (!dragging || !e.touches.length) return;
    e.preventDefault();
    const canvas = canvasRef.current;
    if (!canvas) return;
    const rect = canvas.getBoundingClientRect();
    const y = e.touches[0].clientY - rect.top;
    const price = getPriceAtY(y);
    if (dragging === "entry") setEntryPrice(price);
    else if (dragging === "sl") setSlPrice(price);
    else if (dragging === "tp") setTpPrice(price);
  }, [dragging, getPriceAtY]);

  const handleTouchEnd = useCallback(() => {
    setDragging(null);
  }, []);

  const handleSubmitOrder = useCallback(() => {
    if (selectedOrderType && entryPrice !== null) {
      if (onOrderSubmit) {
        onOrderSubmit({
          type: selectedOrderType,
          entry: entryPrice,
          sl: slPrice,
          tp: tpPrice,
        });
      }
      // Reset after submit
      setSelectedOrderType(null);
    }
  }, [selectedOrderType, entryPrice, slPrice, tpPrice, onOrderSubmit]);

  const handleCancelOrder = useCallback(() => {
    setSelectedOrderType(null);
  }, []);

  useEffect(() => {
    draw();
  }, [draw]);

  return (
    <div className="chart-card">
      {/* Order type selector */}
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

      <div className="chart-holder" style={{ position: "relative" }}>
        <canvas
          ref={canvasRef}
          className="candle-canvas"
          onMouseDown={handleMouseDown}
          onMouseMove={handleMouseMove}
          onMouseUp={handleMouseUp}
          onMouseLeave={handleMouseUp}
          onTouchStart={handleTouchStart}
          onTouchMove={handleTouchMove}
          onTouchEnd={handleTouchEnd}
        />

        {/* Submit/Cancel buttons overlay */}
        {showSubmitBtn && selectedOrderType && (
          <div
            className="order-submit-overlay"
            style={{ left: submitBtnPos.x, top: submitBtnPos.y }}
          >
            <button className="submit-order-btn" onClick={handleSubmitOrder}>
              Place {selectedOrderType.replace("_", " ")}
            </button>
            <button className="cancel-order-btn" onClick={handleCancelOrder}>
              ✕
            </button>
          </div>
        )}
      </div>

      {selectedOrderType && (
        <div className="order-info-bar">
          <span>Drag lines to adjust SL/TP</span>
          <span className="order-prices">
            Entry: {entryPrice?.toFixed(5)} | SL: {slPrice?.toFixed(5)} | TP: {tpPrice?.toFixed(5)}
          </span>
        </div>
      )}
    </div>
  );
}