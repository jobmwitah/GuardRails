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
      if (!selectedAcc && MOCK_ACCOUNTS.length > 0) {
        setSelectedAcc(MOCK_ACCOUNTS[0].login);
        if (MOCK_ACCOUNTS[0].symbols.length > 0) {
          setSymbol(MOCK_ACCOUNTS[0].symbols[0]);
        }
      }
      setLoadingAccounts(false);
      return;
    }
    try {
      const res = await api.accounts();
      setAccounts(res.accounts || []);
      if (!selectedAcc && (res.accounts || []).length > 0) {
        setSelectedAcc(res.accounts[0].login);
        if (res.accounts[0].symbols.length > 0) {
          setSymbol(res.accounts[0].symbols[0]);
        }
      }
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setLoadingAccounts(false);
    }
  }, [selectedAcc, demoMode]);

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

  const handlePlaceOrder = async (side: TradeSide, kind: OrderKind, sl: number | null, tp: number | null, riskPct: number | null) => {
    if (!selectedAcc) throw new Error("No account selected");
    setTradeBusy(true);
    try {
      const res = await api.trade({
        account: selectedAcc,
        symbol,
        side,
        kind,
        risk_pct: riskPct,
        lot_size: null,
        sl,
        tp,
        price: kind === "limit" ? sl : null,
      });
      await refreshPositions();
      return res;
    } finally {
      setTradeBusy(false);
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

      {!token && (
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

      {token && accounts.length > 0 ? (
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
                  onOrderSubmit={(order) => {
                    console.log("Order submitted:", order);
                    alert(`Order placed: ${order.type} @ ${order.entry.toFixed(5)}\nSL: ${order.sl?.toFixed(5)}\nTP: ${order.tp?.toFixed(5)}`);
                  }}
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
            <p>No accounts configured or unauthorized.</p>
            <button
              className="btn-primary"
              onClick={() => {
                const t = prompt("Set API Token", token);
                if (t !== null) saveToken(t.trim());
              }}
            >
              Set API Token
            </button>
          </div>
        )
      )}
    </div>
  );
}