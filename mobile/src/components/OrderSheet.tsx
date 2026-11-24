import { useMemo, useState } from "react";
import {
  ArrowDownRight,
  ArrowUpRight,
  Clock,
  Lightning,
  Lock,
  ShieldSlash,
  X,
} from "@phosphor-icons/react";
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
  const [sl, setSl] = useState<string>("");
  const [entry, setEntry] = useState<string>("");
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
      const res = await onPlace(
        side,
        kindName,
        kindName === "limit" ? entryNum : null,
        isNaN(slNum) ? null : slNum,
      );
      setResult(
        res.confirmed === false
          ? `Not confirmed: ${res.message}`
          : res.message || "Order sent",
      );
    } catch (e) {
      setResult(`Error: ${(e as Error).message}`);
    }
  }

  if (account.read_only) {
    return (
      <div className="sheet-overlay" onClick={onCloseSheet}>
        <div className="sheet" onClick={(e) => e.stopPropagation()}>
          <div className="sheet-head">
            <h2>
              Place order <span className="sheet-symbol">{symbol}</span>
            </h2>
            <button className="icon-btn" onClick={onCloseSheet} aria-label="Close">
              <X size={17} />
            </button>
          </div>
          <div className="ro-sheet">
            <ShieldSlash size={38} weight="duotone" />
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
            Place order <span className="sheet-symbol">{symbol}</span>
          </h2>
          <button className="icon-btn" onClick={onCloseSheet} aria-label="Close">
            <X size={17} />
          </button>
        </div>

        <div className="sheet-quote">
          <span className="sheet-quote-label">Mid market</span>
          <span className="sheet-mid">{mid.toFixed(5)}</span>
        </div>

        <div className="seg">
          <button
            className={`seg-btn ${side === "BUY" ? "buy active" : ""}`}
            onClick={() => setSide("BUY")}
          >
            <ArrowUpRight size={15} weight="bold" />
            Buy
          </button>
          <button
            className={`seg-btn ${side === "SELL" ? "sell active" : ""}`}
            onClick={() => setSide("SELL")}
          >
            <ArrowDownRight size={15} weight="bold" />
            Sell
          </button>
        </div>

        <div className="seg">
          <button
            className={`seg-btn ${kind === "market" ? "active" : ""}`}
            onClick={() => setKind("market")}
          >
            <Lightning size={15} weight={kind === "market" ? "fill" : "regular"} />
            Market
          </button>
          <button
            className={`seg-btn ${kind === "limit" ? "active" : ""}`}
            onClick={() => setKind("limit")}
          >
            <Clock size={15} weight={kind === "limit" ? "fill" : "regular"} />
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

        <div className="field">
          <span>
            <Lock size={11} weight="bold" className="field-lock" />
            Risk per trade <em>fixed</em>
          </span>
          <div className="tp-auto">
            {account.risk_pct ? `${account.risk_pct}%` : "Not configured"}
          </div>
        </div>

        <label className="field">
          <span>Stop loss</span>
          <input
            type="number"
            step="0.00001"
            value={sl}
            onChange={(e) => setSl(e.target.value)}
            placeholder="Required"
          />
        </label>

        <div className="field">
          <span>
            <Lock size={11} weight="bold" className="field-lock" />
            Take profit <em>auto · 3.25R</em>
          </span>
          <div className={`tp-auto${computedTp === null ? " is-empty" : ""}`}>
            {computedTp !== null ? computedTp.toFixed(5) : "Set a stop loss"}
          </div>
        </div>

        {computedTp !== null && (
          <p className="rrr-hint">
            TP is fixed at 3.25× the risk distance · risk {account.risk_pct}% fixed
            by phase
          </p>
        )}

        {result && (
          <p
            className={
              result.startsWith("Error") || result.startsWith("Not confirmed")
                ? "notice-error"
                : "notice-ok"
            }
          >
            {result}
          </p>
        )}

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