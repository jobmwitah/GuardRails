import { TrendingDown, TrendingUp } from "lucide-react";
import type { TradeHistoryItem } from "../types";

interface Props {
  trades: TradeHistoryItem[];
  loading?: boolean;
}

const REASON_LABELS: Record<string, string> = {
  SL: "Stop loss",
  TP: "Take profit",
  MANUAL: "Manual",
};

function fmtTime(ts: number): string {
  if (!ts) return "—";
  return new Date(ts * 1000).toLocaleString(undefined, {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

function fmtMoney(n: number): string {
  return n.toLocaleString(undefined, {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  });
}

export default function HistoryList({ trades, loading }: Props) {
  const wins = trades.filter((t) => t.profit > 0).length;
  const net = trades.reduce((sum, t) => sum + t.profit, 0);
  const winRate = trades.length ? Math.round((wins / trades.length) * 100) : 0;

  return (
    <>
      <div className="acc-summary-grid history-summary">
        <div className="sum-item">
          <span className="sum-label">Closed</span>
          <span className="sum-value">{trades.length}</span>
        </div>
        <div className="sum-item">
          <span className="sum-label">Wins</span>
          <span className="sum-value">{wins}</span>
        </div>
        <div className="sum-item">
          <span className="sum-label">Win rate</span>
          <span className="sum-value">{winRate}%</span>
        </div>
        <div className="sum-item">
          <span className="sum-label">Net P/L</span>
          <span className={`sum-value ${net >= 0 ? "profit" : "loss"}`}>
            {net >= 0 ? "+" : ""}
            {fmtMoney(net)}
          </span>
        </div>
      </div>

      <div className="panel">
        <h2 className="panel-title">History</h2>
        {loading && trades.length === 0 ? (
          <p className="empty">Loading…</p>
        ) : trades.length === 0 ? (
          <p className="empty">No closed trades yet</p>
        ) : (
          trades.map((t) => {
            const kind = t.reason === "TP" ? "buy" : t.reason === "SL" ? "sell" : "pending";
            return (
              <div className="pos-row" key={`${t.ticket}-${t.time}`}>
                <div className="pos-main">
                  <div className="pos-head">
                    <span className={`side-tag ${kind}`}>
                      {t.reason === "TP" ? (
                        <TrendingUp size={12} />
                      ) : t.reason === "SL" ? (
                        <TrendingDown size={12} />
                      ) : null}
                      {REASON_LABELS[t.reason] || t.reason || "Closed"}
                    </span>
                    <span className="pos-symbol">{t.symbol}</span>
                    <span className="pos-vol">{t.volume}</span>
                  </div>
                  <div className="pos-sub">
                    <span>{fmtTime(t.time)}</span>
                    <span className={t.profit >= 0 ? "profit" : "loss"}>
                      {t.profit >= 0 ? "+" : ""}
                      {fmtMoney(t.profit)}
                    </span>
                  </div>
                </div>
              </div>
            );
          })
        )}
      </div>
    </>
  );
}
