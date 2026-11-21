import {
  CheckCircle,
  ClockCounterClockwise,
  Hand,
  Receipt,
  ShieldWarning,
} from "@phosphor-icons/react";
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

function reasonIcon(reason: string) {
  if (reason === "TP") return <CheckCircle size={18} weight="duotone" />;
  if (reason === "SL") return <ShieldWarning size={18} weight="duotone" />;
  if (reason === "MANUAL") return <Hand size={18} weight="duotone" />;
  return <Receipt size={18} weight="duotone" />;
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
        <h2 className="panel-title">Trade history</h2>
        {loading && trades.length === 0 ? (
          <div className="empty-state">
            <span className="empty-icon">
              <ClockCounterClockwise size={20} weight="duotone" />
            </span>
            <span>Loading history…</span>
          </div>
        ) : trades.length === 0 ? (
          <div className="empty-state">
            <span className="empty-icon">
              <ClockCounterClockwise size={20} weight="duotone" />
            </span>
            <span>No closed trades yet</span>
            <small>Closed positions will be listed here.</small>
          </div>
        ) : (
          trades.map((t) => {
            const kind = t.reason === "TP" ? "buy" : t.reason === "SL" ? "sell" : "pending";
            return (
              <div className="pos-row" key={`${t.ticket}-${t.time}`}>
                <span className={`pos-icon ${kind}`}>{reasonIcon(t.reason)}</span>
                <div className="pos-main">
                  <div className="pos-head">
                    <span className="pos-symbol">{t.symbol}</span>
                    <span className={`side-tag ${kind}`}>
                      {REASON_LABELS[t.reason] || t.reason || "Closed"}
                    </span>
                    <span className="pos-vol">{t.volume}</span>
                    <span className={`pos-pnl ${t.profit >= 0 ? "profit" : "loss"}`}>
                      {t.profit >= 0 ? "+" : ""}
                      {fmtMoney(t.profit)}
                    </span>
                  </div>
                  <div className="pos-sub">
                    <span>{fmtTime(t.time)}</span>
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
