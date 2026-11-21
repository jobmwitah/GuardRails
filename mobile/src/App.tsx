import { useCallback, useEffect, useState, lazy, Suspense } from "react";
import {
  History,
  BarChart3,
  ListOrdered,
  PlusCircle,
  KeyRound,
  ShieldAlert,
  Database,
} from "lucide-react";
import { api, poll } from "./api";
import {
  MOCK_ACCOUNTS,
  MOCK_POSITIONS,
  MOCK_PENDING,
  MOCK_CANDLES,
  MOCK_HISTORY,
} from "./mockData";
import type {
  Account,
  CandleLike,
  OrderKind,
  PendingOrder,
  Position,
  Timeframe,
  TradeHistoryItem,
  TradeResult,
  TradeSide,
} from "./types";
import AccountSwitcher from "./components/AccountSwitcher";
import AccountStats from "./components/AccountStats";
import HistoryList from "./components/HistoryList";
import PositionsList from "./components/PositionsList";
import OrderSheet from "./components/OrderSheet";

// openalgo-charts is large (~170 kB gzip), so only load it when the Chart tab is opened.
const CandleChart = lazy(() => import("./components/CandleChart"));

type Tab = "chart" | "positions" | "history";

export default function App() {
  const [token, setToken] = useState<string>(
    () => localStorage.getItem("guardrails.token") || "",
  );
  const [accounts, setAccounts] = useState<Account[]>([]);
  const [selectedAcc, setSelectedAcc] = useState<string>("");
  const [positions, setPositions] = useState<Position[]>([]);
  const [pending, setPending] = useState<PendingOrder[]>([]);
  const [history, setHistory] = useState<TradeHistoryItem[]>([]);
  const [loadingHistory, setLoadingHistory] = useState(false);
  const [candles, setCandles] = useState<CandleLike[]>([]);
  const [bid, setBid] = useState<number | null>(null);
  const [ask, setAsk] = useState<number | null>(null);
  const [timeframe, setTimeframe] = useState<Timeframe>("M15");
  const [tab, setTab] = useState<Tab>("chart");
  const [symbol, setSymbol] = useState<string>("EURUSD");
  const [loadingAccounts, setLoadingAccounts] = useState(false);
  const [loadingCandles, setLoadingCandles] = useState(false);
  const [busyAction, setBusyAction] = useState<{ close: string; cancel: string } | null>(null);
  const [orderSheetOpen, setOrderSheetOpen] = useState(false);
  const [tradeBusy, setTradeBusy] = useState(false);
  const [error, setError] = useState<string>("");
  const [tokenInput, setTokenInput] = useState<string>("");
  const [demoMode, setDemoMode] = useState<boolean>(true); // Start in demo mode for testing

  const currentAccount = (accounts || []).find((a) => a.login === selectedAcc);

  // Load account list
  const refreshAccounts = useCallback(async () => {
    setLoadingAccounts(true);
    setError("");
    if (demoMode) {
      await new Promise((r) => setTimeout(r, 300));
      setAccounts(MOCK_ACCOUNTS);
      if (MOCK_ACCOUNTS.length > 0) {
        const keep = MOCK_ACCOUNTS.some((a) => a.login === selectedAcc);
        const pick = keep ? selectedAcc : MOCK_ACCOUNTS[0].login;
        setSelectedAcc(pick);
        const acc = MOCK_ACCOUNTS.find((a) => a.login === pick) || MOCK_ACCOUNTS[0];
        setSymbol((prev) => (acc.symbols.includes(prev) ? prev : acc.symbols[0] || prev));
      }
      setLoadingAccounts(false);
      return;
    }
    if (!token) {
      // Live mode without a token cannot call the API; the token gate handles it.
      setAccounts([]);
      setSelectedAcc("");
      setPositions([]);
      setPending([]);
      setCandles([]);
      setLoadingAccounts(false);
      return;
    }
    try {
      const res = await api.accounts();
      const list = res.accounts || [];
      setAccounts(list);
      if (list.length > 0) {
        const keep = list.some((a) => a.login === selectedAcc);
        const pick = keep ? selectedAcc : list[0].login;
        setSelectedAcc(pick);
        const acc = list.find((a) => a.login === pick) || list[0];
        setSymbol((prev) => (acc.symbols.includes(prev) ? prev : acc.symbols[0] || prev));
      } else {
        setSelectedAcc("");
        setPositions([]);
        setPending([]);
        setCandles([]);
      }
    } catch (e) {
      setError((e as Error).message);
      // Never leave demo data on screen while in live mode.
      setAccounts([]);
      setSelectedAcc("");
      setPositions([]);
      setPending([]);
      setCandles([]);
    } finally {
      setLoadingAccounts(false);
    }
  }, [selectedAcc, demoMode, token]);

  useEffect(() => {
    refreshAccounts();
  }, [refreshAccounts, demoMode]);

  // Load positions for current account
  const refreshPositions = useCallback(async () => {
    if (!selectedAcc) return;
    if (demoMode) {
      setPositions(MOCK_POSITIONS[selectedAcc] || []);
      setPending(MOCK_PENDING[selectedAcc] || []);
      return;
    }
    try {
      const res = await api.positions(selectedAcc);
      setPositions(res.positions || []);
      setPending(res.pending || []);
    } catch {
      /* ignore transient poll fails */
    }
  }, [selectedAcc, demoMode]);

  // Poll positions every 3s
  useEffect(() => {
    if (!selectedAcc) return;
    refreshPositions();
    const unsub = poll(refreshPositions, 3000);
    return unsub;
  }, [selectedAcc, refreshPositions]);

  // Closed trades are fetched when the History tab is visible.
  const refreshHistory = useCallback(async () => {
    if (!selectedAcc) return;
    if (demoMode) {
      setHistory(MOCK_HISTORY[selectedAcc] || []);
      return;
    }
    setLoadingHistory(true);
    try {
      const res = await api.history(selectedAcc);
      setHistory(res.trades || []);
    } catch {
      /* transient — keep the last list */
    } finally {
      setLoadingHistory(false);
    }
  }, [selectedAcc, demoMode]);

  useEffect(() => {
    if (!selectedAcc || tab !== "history") return;
    refreshHistory();
    return poll(refreshHistory, 5000);
  }, [selectedAcc, tab, refreshHistory]);

  // Load candles from the fast market feed (Dukascopy). Polling keeps both the
  // series and the bid/ask strip live without touching the MT5 bridge.
  const refreshCandles = useCallback(async (showSpinner = false) => {
    if (!symbol) return;
    if (showSpinner) setLoadingCandles(true);
    if (demoMode) {
      const bars = MOCK_CANDLES[symbol] || [];
      setCandles(bars);
      // Synthesise a quote so the demo view also shows both sides.
      if (bars.length) {
        const last = bars[bars.length - 1].close;
        const point = last >= 100 ? 0.01 : 0.00001;
        setBid(last);
        setAsk(Number((last + point * 8).toFixed(5)));
      } else {
        setBid(null);
        setAsk(null);
      }
      setLoadingCandles(false);
      return;
    }
    try {
      const res = await api.marketCandles(symbol, timeframe, selectedAcc);
      setCandles(res.candles || []);
      setBid(typeof res.bid === "number" ? res.bid : null);
      setAsk(typeof res.ask === "number" ? res.ask : null);
    } catch {
      // Keep the last good series on a transient feed hiccup.
    } finally {
      setLoadingCandles(false);
    }
  }, [selectedAcc, symbol, timeframe, demoMode]);

  // Switching instrument/timeframe must not leave the previous symbol's bars
  // on screen while the new feed request is in flight.
  useEffect(() => {
    setCandles([]);
  }, [symbol, timeframe]);

  useEffect(() => {
    setLoadingCandles(true);
    if (demoMode) {
      refreshCandles(false);
      return;
    }
    refreshCandles(false);
    return poll(() => refreshCandles(false), 3000);
  }, [refreshCandles, demoMode]);

  // Save auth token
  const saveToken = (val: string) => {
    setToken(val);
    if (val) localStorage.setItem("guardrails.token", val);
    else localStorage.removeItem("guardrails.token");
    if (val && !demoMode) refreshAccounts();
  };

  // Trade close/cancel handlers
  const handleClose = async (ticket: string) => {
    if (!selectedAcc) return;
    setBusyAction((b) => ({ close: ticket, cancel: b?.cancel || "" }));
    try {
      await api.close(selectedAcc, ticket);
      await refreshPositions();
    } catch (e) {
      alert((e as Error).message);
    } finally {
      setBusyAction((b) => (b ? { close: "", cancel: b.cancel } : null));
    }
  };

  const handleCancel = async (ticket: string) => {
    if (!selectedAcc) return;
    setBusyAction((b) => ({ close: b?.close || "", cancel: ticket }));
    try {
      await api.cancel(selectedAcc, ticket);
      await refreshPositions();
    } catch (e) {
      alert((e as Error).message);
    } finally {
      setBusyAction((b) => (b ? { close: b.close, cancel: "" } : null));
    }
  };

  const handlePlaceOrder = async (
    side: TradeSide,
    kind: OrderKind,
    price: number | null,
    sl: number | null,
    tp: number | null,
    riskPct: number | null,
    lotSize: number | null,
  ) => {
    if (!selectedAcc) throw new Error("No account selected");
    if (demoMode) {
      await new Promise((r) => setTimeout(r, 350));
      return {
        ok: true,
        message: `Demo ${side} ${kind} queued for ${symbol} (not sent to a live account)`,
      } as TradeResult;
    }
    setTradeBusy(true);
    try {
      const res = await api.trade({
        account: selectedAcc,
        symbol,
        side,
        kind,
        risk_pct: riskPct,
        lot_size: lotSize,
        sl,
        tp,
        price: kind === "limit" ? price : null,
      });
      await refreshPositions();
      return res;
    } finally {
      setTradeBusy(false);
    }
  };

  // Submit an order drawn directly on the chart. The backend derives TP from the
  // fixed 3.25 R:R rule, so any client-side TP line is treated as a preview only.
  const handleChartOrder = async (order: {
    type: string | null;
    entry: number;
    sl: number | null;
    tp: number | null;
  }) => {
    if (!order.type) return;
    if (!currentAccount) return;
    if (currentAccount.read_only || currentAccount.blocked) {
      alert("Trading is disabled on this account.");
      return;
    }
    const side: TradeSide = order.type.startsWith("SELL") ? "SELL" : "BUY";
    const kind: OrderKind = order.type.endsWith("_MARKET") ? "market" : "limit";
    if (order.sl === null) {
      alert("Set a stop loss before submitting.");
      return;
    }
    if (kind === "limit") {
      if (side === "BUY" && order.sl >= order.entry) {
        alert("A BUY entry needs its stop loss below the entry price.");
        return;
      }
      if (side === "SELL" && order.sl <= order.entry) {
        alert("A SELL entry needs its stop loss above the entry price.");
        return;
      }
    }
    if (demoMode) {
      alert(
        `${order.type.replace("_", " ")} simulated\nEntry: ${order.entry.toFixed(5)}\nSL: ${order.sl.toFixed(5)}` +
          (order.tp !== null ? `\nTP: ${order.tp.toFixed(5)} (auto 3.25R)` : ""),
      );
      return;
    }
    try {
      const res = await handlePlaceOrder(
        side,
        kind,
        kind === "limit" ? order.entry : null,
        order.sl,
        null,
        null,
        null,
      );
      alert(res.message || "Order sent");
    } catch (e) {
      alert((e as Error).message);
    }
  };

  // Latest mid price from candle data or dummy
  const latestClose = candles.length ? candles[candles.length - 1].close : 1.085;

  return (
    <div className="app-container">
      <header className="app-header">
        <div className="brand">
          <div className="brand-dot" />
          <span className="brand-name">GuardRails</span>
          {demoMode && <span className="demo-badge">DEMO</span>}
        </div>
        <div className="header-right">
          <button
            className={`token-btn ${demoMode ? "demo-active" : ""}`}
            onClick={() => {
              // Drop the old mode's selection; refreshAccounts picks the first
              // account of the new mode instead of requesting a stale login.
              setSelectedAcc("");
              setDemoMode((v) => !v);
            }}
            title={demoMode ? "Demo Mode ON (click for live)" : "Live Mode (click for demo)"}
            style={{ marginRight: 8 }}
          >
            <Database size={16} />
          </button>
          <button
            className="token-btn"
            onClick={() => {
              const t = prompt("Enter Mobile API Token", token);
              if (t !== null) saveToken(t.trim());
            }}
            title="API Token"
          >
            <KeyRound size={16} />
          </button>
        </div>
      </header>

      {error && (
        <div className="banner-error">
          <ShieldAlert size={14} />
          <span>{error}</span>
        </div>
      )}

      {!token && !demoMode && (
        <div className="token-gate">
          <div className="token-gate-card">
            <KeyRound size={32} />
            <h2>GuardRails Mobile</h2>
            <p>Enter your Mobile API token to connect.</p>
            <input
              type="password"
              className="token-input"
              placeholder="MOBILE_API_TOKEN"
              value={tokenInput}
              onChange={(e) => setTokenInput(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter") saveToken(tokenInput);
              }}
            />
            <button className="btn-primary" onClick={() => saveToken(tokenInput)}>
              Connect
            </button>
          </div>
        </div>
      )}

      {(demoMode || token) && accounts.length > 0 ? (
        <>
          <AccountSwitcher
            accounts={accounts}
            selected={selectedAcc}
            onSelect={(login) => {
              setSelectedAcc(login);
              const acc = accounts.find((a) => a.login === login);
              if (acc && acc.symbols.length > 0 && !acc.symbols.includes(symbol)) {
                setSymbol(acc.symbols[0]);
              }
            }}
            onRefresh={refreshAccounts}
            refreshing={loadingAccounts}
          />

          <main className="tab-body">
            {tab === "chart" && (
              <div className="tab-pane">
                <div className="symbol-bar">
                  <label className="symbol-label">Symbol</label>
                  <select
                    className="symbol-select"
                    value={symbol}
                    onChange={(e) => setSymbol(e.target.value)}
                  >
                    {(currentAccount?.symbols || ["EURUSD"]).map((s) => (
                      <option key={s} value={s}>
                        {s}
                      </option>
                    ))}
                  </select>
                  <button
                    className="btn-trade-trigger"
                    onClick={() => setOrderSheetOpen(true)}
                    disabled={currentAccount?.read_only || currentAccount?.blocked}
                  >
                    <PlusCircle size={16} /> Trade
                  </button>
                </div>

                <Suspense
                  fallback={
                    <div className="chart-card" style={{ alignItems: "center", justifyContent: "center", minHeight: 340 }}>
                      <span className="chart-loading">Loading chart…</span>
                    </div>
                  }
                >
                  <CandleChart
                    candles={candles}
                    timeframe={timeframe}
                    symbol={symbol}
                    positions={positions}
                    bid={bid}
                    ask={ask}
                    onTimeframe={setTimeframe}
                    loading={loadingCandles}
                    onOrderSubmit={handleChartOrder}
                  />
                </Suspense>
              </div>
            )}

            {tab === "positions" && (
              <div className="tab-pane">
                <AccountStats account={currentAccount} />
                <PositionsList
                  positions={positions}
                  pending={pending}
                  blocked={currentAccount?.blocked || false}
                  readOnly={currentAccount?.read_only || false}
                  busy={busyAction}
                  onClose={handleClose}
                  onCancel={handleCancel}
                />
              </div>
            )}

            {tab === "history" && (
              <div className="tab-pane">
                <AccountStats account={currentAccount} />
                <HistoryList trades={history} loading={loadingHistory} />
              </div>
            )}
          </main>

          {orderSheetOpen && currentAccount && (
            <OrderSheet
              account={currentAccount}
              symbol={symbol}
              bid={bid ?? latestClose}
              ask={ask ?? latestClose}
              busy={tradeBusy}
              onPlace={handlePlaceOrder}
              onCloseSheet={() => setOrderSheetOpen(false)}
            />
          )}

          <nav className="tab-nav">
            <button
              className={`nav-tab ${tab === "chart" ? "active" : ""}`}
              onClick={() => setTab("chart")}
            >
              <BarChart3 size={20} />
              <span>Chart</span>
            </button>
            <button
              className={`nav-tab ${tab === "positions" ? "active" : ""}`}
              onClick={() => setTab("positions")}
            >
              <ListOrdered size={20} />
              <span>Positions</span>
              {positions.length > 0 && (
                <span className="badge">{positions.length}</span>
              )}
            </button>
            <button
              className={`nav-tab ${tab === "history" ? "active" : ""}`}
              onClick={() => setTab("history")}
            >
              <History size={20} />
              <span>History</span>
            </button>
          </nav>
        </>
      ) : (
        !demoMode && token && (
          <div className="empty-panel">
            {loadingAccounts ? (
              <p>Loading accounts…</p>
            ) : (
              <>
                <p>No accounts configured or unauthorized.</p>
                <p className="empty-hint">
                  Add an account from Discord with <code>/addaccount</code>, then use
                  the setup link it DMs you.
                </p>
                <div className="empty-actions">
                  <button className="btn-primary" onClick={refreshAccounts}>
                    Refresh
                  </button>
                  <button
                    className="btn-ghost"
                    onClick={() => {
                      const t = prompt("Set API Token", token);
                      if (t !== null) saveToken(t.trim());
                    }}
                  >
                    Set API Token
                  </button>
                  <button className="btn-ghost" onClick={() => setDemoMode(true)}>
                    Open Demo
                  </button>
                </div>
              </>
            )}
          </div>
        )
      )}
    </div>
  );
}