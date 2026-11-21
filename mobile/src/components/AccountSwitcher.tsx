import { ChevronDown } from "lucide-react";
import type { Account } from "../types";

interface Props {
  accounts: Account[];
  selected: string;
  onSelect: (login: string) => void;
}

export default function AccountSwitcher({ accounts, selected, onSelect }: Props) {
  const current = accounts.find((a) => a.login === selected);

  return (
    <div className="acc-switcher">
      <label className="select-wrap">
        <select
          value={selected}
          onChange={(e) => onSelect(e.target.value)}
          className="acc-select"
          aria-label="Account"
        >
          {accounts.map((a) => (
            <option key={a.login} value={a.login}>
              {a.display_name || a.login} · {a.platform}
            </option>
          ))}
        </select>
        <ChevronDown size={16} className="select-chevron" />
      </label>

      {current?.read_only && (
        <div className="acc-meta">
          <span className="ro-badge">read-only</span>
        </div>
      )}
    </div>
  );
}
