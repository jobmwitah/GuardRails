import {
  ArrowDownRight,
  ArrowUpRight,
  ClockCountdown,
  Crosshair,
  Prohibit,
  ShieldWarning,
  X,
} from "@phosphor-icons/react";
import type { PendingOrder, Position } from "../types";

interface Props {
  positions: Position[];
  pending: PendingOrder[];
  blocked: boolean;
  readOnly: boolean;
  busy: { close: string; cancel: string } | null;
  onClose: (ticket: string) => void;
  onCancel: (ticket: string) => void;
}

export default function PositionsList({
  positions,
  pending,
  blocked,
  readOnly,
  busy,
  onClose,
  onCancel,
}: Props) {
  const canTrade = !readOnly && !blocked;

  return (
    <div className="panel">
      <h2 className="panel-title">Open positions</h2>
      {positions.length === 0 ? (
        <div className="empty-state">
          <span className="empty-icon">
            <Crosshair size={20} weight="duotone" />
          </span>
          <span>No open positions</span>
          <small>New trades will appear here.</small>
        </div>
      ) : (
        positions.map((p) => (
          <div className="pos-row" key={p.ticket}>
            <span className={`pos-icon ${p.type.toLowerCase()}`}>
              {p.type === "BUY" ? (
                <ArrowUpRight size={18} weight="bold" />
              ) : (
                <ArrowDownRight size={18} weight="bold" />
              )}
            </span>
            <div className="pos-main">
              <div className="pos-head">
                <span className="pos-symbol">{p.symbol}</span>
                <span className={`side-tag ${p.type.toLowerCase()}`}>{p.type}</span>
                <span className="pos-vol">{p.volume.toFixed(2)}</span>
                <span className={`pos-pnl ${p.profit >= 0 ? "profit" : "loss"}`}>
                  {p.profit >= 0 ? "+" : ""}
                  {p.profit.toFixed(2)}
                </span>
              </div>
              <div className="pos-sub">
                <span>Open {p.open_price}</span>
                {p.current ? <span>Now {p.current}</span> : null}
              </div>
              {(p.sl !== null || p.tp !== null) && (
                <div className="pos-stops">
                  {p.sl !== null && <span>SL {p.sl}</span>}
                  {p.tp !== null && <span>TP {p.tp}</span>}
                </div>
              )}
            </div>
            <button
              className="close-btn"
              onClick={() => onClose(p.ticket)}
              disabled={!canTrade || busy?.close === p.ticket}
            >
              {busy?.close === p.ticket ? (
                "…"
              ) : (
                <>
                  <X size={12} weight="bold" /> Close
                </>
              )}
            </button>
          </div>
        ))
      )}

      <h2 className="panel-title">Pending orders</h2>
      {pending.length === 0 ? (
        <div className="empty-state">
          <span className="empty-icon">
            <ClockCountdown size={20} weight="duotone" />
          </span>
          <span>No pending orders</span>
          <small>Limits and stops waiting for price will appear here.</small>
        </div>
      ) : (
        pending.map((o) => (
          <div className="pos-row" key={o.ticket}>
            <span className="pos-icon pending">
              <ClockCountdown size={18} weight="duotone" />
            </span>
            <div className="pos-main">
              <div className="pos-head">
                <span className="pos-symbol">{o.symbol}</span>
                <span className="side-tag pending">{o.type.replace(/_/g, " ")}</span>
                <span className="pos-vol">{o.price}</span>
              </div>
              {(o.sl !== null || o.tp !== null) && (
                <div className="pos-stops">
                  {o.sl !== null && <span>SL {o.sl}</span>}
                  {o.tp !== null && <span>TP {o.tp}</span>}
                </div>
              )}
            </div>
            <button
              className="close-btn cancel"
              onClick={() => onCancel(o.ticket)}
              disabled={!canTrade || busy?.cancel === o.ticket}
            >
              {busy?.cancel === o.ticket ? (
                "…"
              ) : (
                <>
                  <Prohibit size={12} weight="bold" /> Cancel
                </>
              )}
            </button>
          </div>
        ))
      )}
      {blocked && (
        <p className="notice-warn">
          <ShieldWarning size={14} weight="duotone" /> Trading is blocked on this account.
        </p>
      )}
    </div>
  );
}
