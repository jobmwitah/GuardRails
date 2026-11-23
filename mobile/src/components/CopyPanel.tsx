import { useEffect, useState } from "react";
import {
  ArrowsLeftRight,
  CheckCircle,
  WarningCircle,
} from "@phosphor-icons/react";
import type { Account, CopyStatus } from "../types";

interface Props {
  accounts: Account[];
  status: CopyStatus | null;
  loading: boolean;
  saving: boolean;
  onSave: (master: string, slaves: string[], enabled: boolean) => Promise<void>;
  onRefresh: () => void;
}

function label(account: Account): string {
  return account.display_name || account.login;
}

function accountMeta(account: Account): string {
  const phase = account.phase ? account.phase.toUpperCase() : "NO PHASE";
  const risk = account.risk_pct ? `${account.risk_pct}%` : "—";
  return `${account.login} · ${account.platform.toUpperCase()} · ${phase} · ${risk}`;
}

export default function CopyPanel({
  accounts,
  status,
  loading,
  saving,
  onSave,
  onRefresh,
}: Props) {
  const [master, setMaster] = useState("");
  const [slaves, setSlaves] = useState<string[]>([]);
  const [enabled, setEnabled] = useState(false);
  const [dirty, setDirty] = useState(false);

  // The parent polls copy status. Only adopt server state while the operator
  // has no unsaved edits, otherwise a poll could silently undo a selection.
  useEffect(() => {
    if (dirty || !status) return;
    setMaster(status.master || "");
    setSlaves(status.slaves || []);
    setEnabled(status.enabled);
  }, [status, dirty]);

  const toggleSlave = (login: string) => {
    setDirty(true);
    setSlaves((current) =>
      current.includes(login)
        ? current.filter((item) => item !== login)
        : [...current, login],
    );
  };

  const save = async (nextEnabled: boolean) => {
    if (!master) return;
    setDirty(true);
    try {
      await onSave(master, slaves, nextEnabled);
      setEnabled(nextEnabled);
      setDirty(false);
    } catch {
      /* parent surfaces the error */
    }
  };

  const candidates = accounts.filter((account) => account.login !== master);
  const masterAccount = accounts.find((account) => account.login === master);

  return (
    <div className="panel copy-panel">
      <div className="copy-head">
        <h2 className="copy-title">
          <ArrowsLeftRight size={16} weight="duotone" /> Copy trading
        </h2>
        <span className={`copy-state ${enabled ? "on" : ""}`}>
          {enabled ? "ON" : "OFF"}
        </span>
      </div>

      <p className="copy-note">
        The master's entries and stop loss are copied. Every slave keeps its own
        fixed phase risk, lot sizing, TP/RR, position limit and symbol list.
      </p>

      {loading && !status ? (
        <p className="copy-muted">Loading copy configuration…</p>
      ) : (
        <>
          <label className="copy-field">
            <span>Master account</span>
            <select
              value={master}
              onChange={(event) => {
                const next = event.target.value;
                setDirty(true);
                setMaster(next);
                setSlaves((current) => current.filter((item) => item !== next));
              }}
            >
              <option value="">Select a master…</option>
              {accounts.map((account) => (
                <option key={account.login} value={account.login}>
                  {label(account)} — {accountMeta(account)}
                </option>
              ))}
            </select>
          </label>

          {masterAccount && (
            <div className="copy-master-chip">
              <CheckCircle size={14} weight="fill" />
              <span>{label(masterAccount)}</span>
              <small>{masterAccount.server || masterAccount.platform.toUpperCase()}</small>
            </div>
          )}

          <div className="copy-field">
            <span>Slave accounts</span>
            <div className="copy-slaves">
              {candidates.length === 0 && (
                <small className="copy-muted">Add another account to copy to.</small>
              )}
              {candidates.map((account) => {
                const checked = slaves.includes(account.login);
                const disabled = account.read_only;
                return (
                  <label
                    key={account.login}
                    className={`copy-slave ${checked ? "checked" : ""} ${
                      disabled ? "disabled" : ""
                    }`}
                  >
                    <input
                      type="checkbox"
                      checked={checked}
                      disabled={disabled}
                      onChange={() => toggleSlave(account.login)}
                    />
                    <span className="copy-slave-main">
                      <span className="copy-slave-name">{label(account)}</span>
                      <span className="copy-slave-meta">{accountMeta(account)}</span>
                    </span>
                    {disabled && <span className="ro-badge">Read-only</span>}
                  </label>
                );
              })}
            </div>
          </div>

          <div className="copy-actions">
            <button
              className="btn-primary"
              disabled={!master || saving || (enabled && slaves.length === 0)}
              onClick={() => save(true)}
            >
              {saving ? "Saving…" : enabled ? "Save changes" : "Enable copy"}
            </button>
            {enabled && (
              <button
                className="btn-ghost"
                disabled={saving}
                onClick={() => save(false)}
              >
                Disable
              </button>
            )}
            <button className="btn-ghost" onClick={onRefresh} disabled={loading}>
              Refresh
            </button>
          </div>

          {enabled && status && !status.baselined && (
            <div className="copy-warning">
              <WarningCircle size={15} weight="duotone" />
              Waiting for the master's next report. Trades already open at that
              moment are baselined and never copied retroactively.
            </div>
          )}

          {enabled && slaves.length === 0 && (
            <div className="copy-warning">
              <WarningCircle size={15} weight="duotone" />
              Choose at least one slave before enabling.
            </div>
          )}

          <div className="copy-links-head">
            <span>Recent links</span>
            <small>{status?.links.length || 0}</small>
          </div>

          {!status || status.links.length === 0 ? (
            <div className="copy-empty">No copied trades yet.</div>
          ) : (
            <div className="copy-links">
              {status.links.map((link) => (
                <div className="copy-link" key={link.id}>
                  <div className="copy-link-head">
                    <span className="copy-link-title">
                      #{link.master_ticket} · {link.symbol || "—"}
                    </span>
                    <span className={`copy-link-status ${link.status}`}>
                      {link.status}
                    </span>
                  </div>
                  <div className="copy-link-sub">
                    {link.kind} {link.side}
                    {link.entry ? ` @ ${link.entry}` : ""}
                    {link.sl ? ` · SL ${link.sl}` : ""}
                    {link.reason ? ` · ${link.reason}` : ""}
                  </div>
                  <div className="copy-legs">
                    {link.legs.length === 0 && (
                      <span className="copy-muted">No slave legs</span>
                    )}
                    {link.legs.map((leg) => (
                      <span
                        className={`copy-leg ${leg.status}`}
                        key={`${link.id}-${leg.slave_login}`}
                        title={leg.detail || leg.status}
                      >
                        {leg.slave_login}: {leg.status}
                      </span>
                    ))}
                  </div>
                </div>
              ))}
            </div>
          )}
        </>
      )}
    </div>
  );
}
