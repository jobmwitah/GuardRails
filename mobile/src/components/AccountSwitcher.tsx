import { CheckCircle } from "@phosphor-icons/react";
import type { Account } from "../types";

interface Props {
  accounts: Account[];
  selected: string;
  onSelect: (login: string) => void;
}

function initials(account: Account): string {
  const name = (account.display_name || "").trim();
  if (name) {
    return name
      .split(/\s+/)
      .slice(0, 2)
      .map((part) => part[0])
      .join("")
      .toUpperCase();
  }
  return account.login.slice(-4);
}

export default function AccountSwitcher({ accounts, selected, onSelect }: Props) {
  return (
    <div className="acc-switcher" role="dialog" aria-label="Switch account">
      <div className="acc-switcher-head">
        <span>Accounts</span>
        <span className="acc-count">{accounts.length}</span>
      </div>

      <div className="acc-list">
        {accounts.map((account) => {
          const active = account.login === selected;
          return (
            <button
              key={account.login}
              type="button"
              className={`acc-item ${active ? "selected" : ""}`}
              onClick={() => onSelect(account.login)}
              aria-pressed={active}
            >
              <span className="acc-avatar">{initials(account)}</span>
              <span className="acc-copy">
                <span className="acc-name">{account.display_name || account.login}</span>
                <span className="acc-sub">
                  {account.login} · {account.platform.toUpperCase()}
                  {account.server ? ` · ${account.server}` : ""}
                </span>
              </span>
              {account.read_only ? (
                <span className="ro-badge">Read-only</span>
              ) : active ? (
                <CheckCircle size={18} weight="fill" className="acc-check" />
              ) : null}
            </button>
          );
        })}
      </div>

      <div className="acc-switcher-foot">Sets the account for chart, orders and history.</div>
    </div>
  );
}
