import { useCallback, useEffect, useState } from "react";
import {
  TrendingUp,
  BarChart3,
  ListOrdered,
  PlusCircle,
  KeyRound,
  ShieldAlert,
  Database,
} from "lucide-react";
import { api, poll } from "./api";
import { MOCK_ACCOUNTS, MOCK_POSITIONS, MOCK_PENDING, MOCK_CANDLES } from "./mockData";
import type {
  Account,
  CandleLike,
  OrderKind,
  PendingOrder,
  Position,
  Timeframe,
  TradeResult,
  TradeSide,
} from "./types";
import AccountSwitcher from "./components/AccountSwitcher";
import CandleChart from "./components/CandleChart";
import PositionsList from "./components/PositionsList";
import OrderSheet from "./components/OrderSheet";

type Tab = "chart" | "positions" | "trade";

export default function App() {
  const [token, setToken] = useState<string>(
    () => localStorage.getItem("guardrails.token") || "",
  );
  const [accounts, setAccounts] = useState<Account[]>([]);
  const [selectedAcc, setSelectedAcc] = useState<string>("");
  const [positions, setPositions] = useState<Position[]>([]);
  const [pending, setPending] = useState<PendingOrder[]>([]);
  const [candles, setCandles] = useState<CandleLike[]>([]);
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

  // Load candles
  const refreshCandles = useCallback(async () => {
    if (!selectedAcc || !symbol) return;
    setLoadingCandles(true);
    if (demoMode) {
      await new Promise((r) => setTimeout(r, 200));
      setCandles(MOCK_CANDLES[symbol] || []);
      setLoadingCandles(false);
      return;
    }
    try {
      const res = await api.candles(selectedAcc, symbol, timeframe);
      setCandles(res.candles || []);
    } catch (e) {
      setCandles([]);
    } finally {
      setLoadingCandles(false);
    }
  }, [selectedAcc, symbol, timeframe, demoMode]);

  useEffect(() => {
    if (selectedAcc) {
      refreshCandles();
    }
  }, [refreshCandles, selectedAcc]);

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
        `${order.type.replace("_", " ")} simulated\nEntry: ${order.entry.toFixed(5)}\nSL: ${order.sl.toFixed(5)}`,
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
            onClick={() => setDemoMode(!demoMode)}
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

                <CandleChart
                  candles={candles}
                  timeframe={timeframe}
                  onTimeframe={setTimeframe}
                  loading={loadingCandles}
                  onOrderSubmit={handleChartOrder}
                />
              </div>
            )}

            {tab === "positions" && (
              <div className="tab-pane">
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

            {tab === "trade" && (
              <div className="tab-pane">
                <div className="trade-quick-pane">
                  <h3>Quick Order</h3>
                  <p className="trade-quick-desc">
                    Place an order directly onto <strong>{selectedAcc}</strong>.
                  </p>
                  <button
                    className="btn-primary"
                    onClick={() => setOrderSheetOpen(true)}
                    disabled={currentAccount?.read_only || currentAccount?.blocked}
                  >
                    Open Order Sheet
                  </button>
                </div>
              </div>
            )}
          </main>

          {orderSheetOpen && currentAccount && (
            <OrderSheet
              account={currentAccount}
              symbol={symbol}
              bid={latestClose * 0.9999}
              ask={latestClose * 1.0001}
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
              className={`nav-tab ${tab === "trade" ? "active" : ""}`}
              onClick={() => setTab("trade")}
            >
              <TrendingUp size={20} />
              <span>Trade</span>
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