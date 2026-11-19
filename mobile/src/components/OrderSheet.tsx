import { useMemo, useState } from "react";
import { X, ShieldOff } from "lucide-react";
import type { Account, TradeSide, OrderKind, TradeResult } from "../types";

interface Props {
  account: Account;
  symbol: string;
  bid: number;
  ask: number;
  busy: boolean;
  onPlace: (
    side: TradeSide,
    kind: OrderKind,
    price: number | null,
    sl: number | null,
    tp: number | null,
    riskPct: number | null,
    lotSize: number | null,
  ) => Promise<TradeResult>;
  onCloseSheet: () => void;
}

export default function OrderSheet({
  account,
  symbol,
  bid,
  ask,
  busy,
  onPlace,
  onCloseSheet,
}: Props) {
  const [side, setSide] = useState<TradeSide>("BUY");
  const [kind, setKind] = useState<OrderKind>("market");
  const [riskPctStr, setRiskPctStr] = useState<string>(account.risk_pct ? String(account.risk_pct) : "1");
  const [lot, setLot] = useState<string>("0.01");
  const [sl, setSl] = useState<string>("");
  const [entry, setEntry] = useState<string>("");
  const [useRisk, setUseRisk] = useState<boolean>(true);
  const [result, setResult] = useState<string>("");

  const mid = (bid + ask) / 2;

  const slNum = parseFloat(sl);
  const entryNum = parseFloat(entry) || mid;
  const slOk = !isNaN(slNum) && Math.abs(slNum - entryNum) > 0.0001;
  const sideOk =
    kind !== "limit" || (side === "BUY" ? slNum < entryNum : slNum > entryNum);
  const valid = !!side && slOk && sideOk;
  const canSubmit =
    account && !account.read_only && !account.blocked && !busy && valid;

  const computedTp = useMemo(() => {
    if (isNaN(slNum) || Math.abs(slNum - entryNum) < 1e-9) return null;
    const distance = Math.abs(entryNum - slNum) * 3.25;
    return side === "BUY" ? entryNum + distance : entryNum - distance;
  }, [entryNum, slNum, side]);

  async function submit() {
    if (!canSubmit) return;
    setResult("");
    try {
      const kindName: OrderKind = kind;
      const slNum = parseFloat(sl);
      const resolvedRiskPct = useRisk ? parseFloat(riskPctStr) : null;
      const lotNum = useRisk ? null : parseFloat(lot);
      const res = await onPlace(
        side,
        kindName,
        kindName === "limit" ? entryNum : null,
        isNaN(slNum) ? null : slNum,
        null,
        resolvedRiskPct,
        lotNum !== null && !isNaN(lotNum) ? lotNum : null,
      );
      setResult(res.message || "Order sent");
    } catch (e) {
      setResult((e as Error).message);
    }
  }

  if (account.read_only) {
    return (
      <div className="sheet-overlay" onClick={onCloseSheet}>
        <div className="sheet" onClick={(e) => e.stopPropagation()}>
          <div className="sheet-head">
            <h2>Place order · {symbol}</h2>
            <button className="icon-btn" onClick={onCloseSheet}>
              <X size={18} />
            </button>
          </div>
          <div className="ro-sheet">
            <ShieldOff size={40} />
            <p>This is the FTMO account and is read-only. Trading is disabled here.</p>
            <button className="btn-primary" onClick={onCloseSheet}>
              Done
            </button>
          </div>
        </div>
      </div>
    );
  }

  return (
    <div className="sheet-overlay" onClick={onCloseSheet}>
      <div className="sheet" onClick={(e) => e.stopPropagation()}>
        <div className="sheet-head">
          <h2>
            Place order · {symbol} <span className="sheet-mid">mid {mid.toFixed(5)}</span>
          </h2>
          <button className="icon-btn" onClick={onCloseSheet}>
            <X size={18} />
          </button>
        </div>

        <div className="seg">
          <button
            className={`seg-btn ${side === "BUY" ? "buy active" : ""}`}
            onClick={() => setSide("BUY")}
          >
            Buy
          </button>
          <button
            className={`seg-btn ${side === "SELL" ? "sell active" : ""}`}
            onClick={() => setSide("SELL")}
          >
            Sell
          </button>
        </div>

        <div className="seg">
          <button
            className={`seg-btn ${kind === "market" ? "active" : ""}`}
            onClick={() => setKind("market")}
          >
            Market
          </button>
          <button
            className={`seg-btn ${kind === "limit" ? "active" : ""}`}
            onClick={() => setKind("limit")}
          >
            Limit
          </button>
        </div>

        {kind === "limit" && (
          <label className="field">
            <span>Entry price</span>
            <input
              type="number"
              step="0.00001"
              placeholder={mid.toFixed(5)}
              value={entry}
              onChange={(e) => setEntry(e.target.value)}
            />
          </label>
        )}

        <label className="field">
          <span>Size</span>
          <div className="size-toggle">
            <button className={useRisk ? "active" : ""} onClick={() => setUseRisk(true)}>
              Risk %
            </button>
            <button className={!useRisk ? "active" : ""} onClick={() => setUseRisk(false)}>
              Lots
            </button>
          </div>
          {useRisk ? (
            <input
              type="number"
              min="0.1"
              max="5"
              step="0.1"
              value={riskPctStr}
              onChange={(e) => setRiskPctStr(e.target.value)}
              placeholder="1.0"
            />
          ) : (
            <input
              type="number"
              min="0.01"
              step="0.01"
              value={lot}
              onChange={(e) => setLot(e.target.value)}
              placeholder="0.01"
            />
          )}
        </label>

        <label className="field">
          <span>Stop loss</span>
          <input
            type="number"
            step="0.00001"
            value={sl}
            onChange={(e) => setSl(e.target.value)}
            placeholder="Optional"
          />
        </label>

        <div className="field">
          <span>
            Take profit <em>auto · 3.25R</em>
          </span>
          <div className="tp-auto">
            {computedTp !== null ? computedTp.toFixed(5) : "Set a stop loss"}
          </div>
        </div>

        {computedTp !== null && (
          <p className="rrr-hint">
            TP is fixed at 3.25× the risk distance · risk{" "}
            {useRisk ? `${riskPctStr}%` : `${lot} lots`} · {account.risk_pct}% account cap
          </p>
        )}

        {result && <p className={result.startsWith("Error") ? "notice-error" : "notice-ok"}>{result}</p>}

        <button
          className={`btn-primary ${side === "BUY" ? "buy" : "sell"}`}
          onClick={submit}
          disabled={!canSubmit}
        >
          {busy ? "Sending…" : `Place ${side} ${kind} order`}
        </button>
      </div>
    </div>
  );
}