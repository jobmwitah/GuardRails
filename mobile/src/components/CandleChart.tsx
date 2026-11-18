import { useEffect, useRef } from "react";
import type { CandleLike, Timeframe } from "../types";

const UP = "#26a69a";
const DOWN = "#ef5350";

interface Props {
  candles: CandleLike[];
  timeframe: Timeframe;
  onTimeframe: (tf: Timeframe) => void;
  loading: boolean;
}

const TIMEFRAMES: Timeframe[] = ["M1", "M5", "M15", "M30", "H1", "H4", "D1"];

export default function CandleChart({ candles, timeframe, onTimeframe, loading }: Props) {
  const ref = useRef<HTMLCanvasElement | null>(null);

  useEffect(() => {
    const canvas = ref.current;
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
    let min = Infinity;
    let max = -Infinity;
    for (const c of data) {
      if (c.low < min) min = c.low;
      if (c.high > max) max = c.high;
    }
    const pad = (max - min) * 0.08 || 1;
    min -= pad;
    max += pad;
    const padL = 8;
    const padR = 8;
    const padT = 10;
    const padB = 8;
    const plotW = W - padL - padR;
    const plotH = H - padT - padB;
    const bodyW = Math.max(2, Math.min(18, plotW / data.length - 2));
    const halfBody = bodyW / 2;

    const xAt = (i: number) => padL + (plotW * (i + 0.5)) / data.length;
    const yAt = (v: number) => padT + plotH * (1 - (v - min) / (max - min));

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
      ctx.moveTo(x, yAt(c.high));
      ctx.lineTo(x, yAt(c.low));
      ctx.stroke();

      const openY = yAt(c.open);
      const closeY = yAt(c.close);
      const top = Math.min(openY, closeY);
      const h = Math.max(1, Math.abs(closeY - openY));
      ctx.fillStyle = color;
      ctx.fillRect(x - halfBody, top, bodyW, h);
    }

    // Price labels
    ctx.fillStyle = "#8a94a6";
    ctx.font = "10px system-ui";
    ctx.textAlign = "right";
    ctx.fillText(max.toFixed(2), W - padR - 4, 12);
    ctx.fillText(min.toFixed(2), W - padR - 4, H - 6);
  }, [candles, loading, timeframe]);

  return (
    <div className="chart-card">
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
      <div className="chart-holder">
        <canvas ref={ref} className="candle-canvas" />
      </div>
    </div>
  );
}