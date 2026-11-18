import { TrendingUp, TrendingDown, Ban } from "lucide-react";
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
      <h2 className="panel-title">Positions</h2>
      {positions.length === 0 ? (
        <p className="empty">No open positions</p>
      ) : (
        positions.map((p) => (
          <div className="pos-row" key={p.ticket}>
            <div className="pos-main">
              <div className="pos-head">
                <span className={`side-tag ${p.type.toLowerCase()}`}>
                  {p.type === "BUY" ? <TrendingUp size={12} /> : <TrendingDown size={12} />}
                  {p.type}
                </span>
                <span className="pos-symbol">{p.symbol}</span>
                <span className="pos-vol">{p.volume.toFixed(2)}</span>
              </div>
              <div className="pos-sub">
                <span>Open {p.open_price}</span>
                <span className={p.profit >= 0 ? "profit" : "loss"}>
                  {p.profit >= 0 ? "+" : ""}
                  {p.profit.toFixed(2)}
                </span>
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
              {busy?.close === p.ticket ? "…" : "Close"}
            </button>
          </div>
        ))
      )}

      <h2 className="panel-title">Pending orders</h2>
      {pending.length === 0 ? (
        <p className="empty">No pending orders</p>
      ) : (
        pending.map((o) => (
          <div className="pos-row" key={o.ticket}>
            <div className="pos-main">
              <div className="pos-head">
                <span className="side-tag pending">{o.type.replace("_", " ")}</span>
                <span className="pos-symbol">{o.symbol}</span>
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
                  <Ban size={12} /> Cancel
                </>
              )}
            </button>
          </div>
        ))
      )}
      {blocked && <p className="notice-warn">Trading is blocked on this account.</p>}
    </div>
  );
}