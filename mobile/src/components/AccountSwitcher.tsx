import { ChevronDown, RefreshCw, ShieldOff, ShieldCheck } from "lucide-react";
import type { Account } from "../types";

interface Props {
  accounts: Account[];
  selected: string;
  onSelect: (login: string) => void;
  onRefresh: () => void;
  refreshing: boolean;
}

function fmt(n: number): string {
  return n.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

export default function AccountSwitcher({
  accounts,
  selected,
  onSelect,
  onRefresh,
  refreshing,
}: Props) {
  const current = accounts.find((a) => a.login === selected);
  const status = current
    ? current.blocked
      ? "blocked"
      : current.market_closed
        ? "closed"
        : "live"
    : "none";

  return (
    <div className="acc-switcher">
      <div className="acc-switcher-row">
        <label className="select-wrap">
          <select
            value={selected}
            onChange={(e) => onSelect(e.target.value)}
            className="acc-select"
          >
            {accounts.map((a) => (
              <option key={a.login} value={a.login}>
                {a.display_name || a.login} · {a.platform}
              </option>
            ))}
          </select>
          <ChevronDown size={16} className="select-chevron" />
        </label>
        <button className="icon-btn" onClick={onRefresh} aria-label="Refresh" disabled={refreshing}>
          <RefreshCw size={18} className={refreshing ? "spin" : ""} />
        </button>
      </div>

      {current && (
        <div className="acc-summary">
          <div className="acc-summary-grid">
            <div className="sum-item">
              <span className="sum-label">Balance</span>
              <span className="sum-value">{fmt(current.balance)}</span>
            </div>
            <div className="sum-item">
              <span className="sum-label">Equity</span>
              <span className="sum-value">{fmt(current.equity)}</span>
            </div>
            <div className="sum-item">
              <span className="sum-label">Margin</span>
              <span className="sum-value">{fmt(current.margin)}</span>
            </div>
            <div className="sum-item">
              <span className="sum-label">Free</span>
              <span className="sum-value">{fmt(current.free_margin)}</span>
            </div>
          </div>
          <div className="acc-meta">
            <span className={`status-pill status-${status}`}>
              {current.blocked ? (
                <ShieldOff size={12} />
              ) : (
                <ShieldCheck size={12} />
              )}
              {status}
            </span>
            <span className="acc-phase">{current.phase || "—"} · {current.risk_pct || 0}% risk</span>
            {current.read_only && <span className="ro-badge">read-only</span>}
          </div>
        </div>
      )}
    </div>
  );
}