import { ChartLineUp, Coins, Scales, Wallet } from "@phosphor-icons/react";
import type { Account } from "../types";

interface Props {
  account?: Account;
}

function fmt(n: number): string {
  return n.toLocaleString(undefined, {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  });
}

/** Balance / equity / margin cards, shown on the Positions and History tabs. */
export default function AccountStats({ account }: Props) {
  if (!account) return null;
  return (
    <div className="acc-summary-grid">
      <div className="sum-item">
        <span className="sum-label">
          <Wallet size={12} weight="duotone" className="sum-icon" />
          Balance
        </span>
        <span className="sum-value">{fmt(account.balance)}</span>
      </div>
      <div className="sum-item">
        <span className="sum-label">
          <ChartLineUp size={12} weight="duotone" className="sum-icon" />
          Equity
        </span>
        <span className="sum-value">{fmt(account.equity)}</span>
      </div>
      <div className="sum-item">
        <span className="sum-label">
          <Scales size={12} weight="duotone" className="sum-icon" />
          Margin
        </span>
        <span className="sum-value">{fmt(account.margin)}</span>
      </div>
      <div className="sum-item">
        <span className="sum-label">
          <Coins size={12} weight="duotone" className="sum-icon" />
          Free
        </span>
        <span className="sum-value">{fmt(account.free_margin)}</span>
      </div>
    </div>
  );
}
