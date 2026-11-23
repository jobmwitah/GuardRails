import { useCallback, useEffect, useRef, useState, lazy, Suspense } from "react";
import {
  ArrowsClockwise,
  CaretDown,
  ChartLineUp,
  Check,
  ClockCounterClockwise,
  Key,
  ListBullets,
  Plus,
  ShieldCheck,
  ShieldWarning,
  Wallet,
} from "@phosphor-icons/react";
import { api, poll } from "./api";
import type {
  Account,
  CandleLike,
  OrderKind,
  PendingOrder,
  Position,
  Timeframe,
  TradeHistoryItem,
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

function initialTab(): Tab {
  const requested = new URLSearchParams(window.location.search).get("tab");
  return requested === "positions" || requested === "history" ? requested : "chart";
}

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
  const [tab, setTab] = useState<Tab>(initialTab);
  const [symbol, setSymbol] = useState<string>("EURUSD");
  const [loadingAccounts, setLoadingAccounts] = useState(false);
  const [loadingCandles, setLoadingCandles] = useState(false);
  const [busyAction, setBusyAction] = useState<{ close: string; cancel: string } | null>(null);
  const [orderSheetOpen, setOrderSheetOpen] = useState(false);
  const [tradeBusy, setTradeBusy] = useState(false);
  const [error, setError] = useState<string>("");
  const [tokenInput, setTokenInput] = useState<string>("");
  const [accountsOpen, setAccountsOpen] = useState(false);
  const [symbolMenuOpen, setSymbolMenuOpen] = useState(false);
  const [refreshing, setRefreshing] = useState(false);

  const currentAccount = (accounts || []).find((a) => a.login === selectedAcc);

  // Async replies must never land on a different account or instrument than the
  // one they were requested for (e.g. an in-flight positions poll during a
  // switch could otherwise show the previous account's trades).
  const accountRef = useRef(selectedAcc);
  accountRef.current = selectedAcc;
  const symbolRef = useRef(symbol);
  symbolRef.current = symbol;
  const timeframeRef = useRef(timeframe);
  timeframeRef.current = timeframe;

  // Switching accounts clears everything scoped to the old account before the
  // first poll of the new one returns.
  useEffect(() => {
    setPositions([]);
    setPending([]);
    setHistory([]);
    setCandles([]);
    setBid(null);
    setAsk(null);
  }, [selectedAcc]);

  // Load account list
  const refreshAccounts = useCallback(async () => {
    setLoadingAccounts(true);
    setError("");
    if (!token) {
      // The token gate handles sign-in; never show stale account data.
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
      // Never leave stale data on screen while signed out.
      setAccounts([]);
      setSelectedAcc("");
      setPositions([]);
      setPending([]);
      setCandles([]);
    } finally {
      setLoadingAccounts(false);
    }
  }, [selectedAcc, token]);

  useEffect(() => {
    refreshAccounts();
  }, [refreshAccounts]);

  // With the manual refresh controls gone, keep retrying quietly while the
  // account list is still empty (e.g. just provisioned or briefly offline).
  useEffect(() => {
    if (!token || accounts.length > 0) return;
    return poll(refreshAccounts, 15000);
  }, [token, accounts.length, refreshAccounts]);

  // Escape closes the account or instrument popover, matching a backdrop tap.
  useEffect(() => {
    if (!accountsOpen && !symbolMenuOpen) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        setAccountsOpen(false);
        setSymbolMenuOpen(false);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [accountsOpen, symbolMenuOpen]);

  // Load positions for current account
  const refreshPositions = useCallback(async () => {
    const acc = selectedAcc;
    if (!acc) return;
    try {
      const res = await api.positions(acc);
      if (accountRef.current !== acc) return;
      setPositions(res.positions || []);
      setPending(res.pending || []);
    } catch {
      /* ignore transient poll fails */
    }
  }, [selectedAcc]);

  // Poll positions every 3s
  useEffect(() => {
    if (!selectedAcc) return;
    refreshPositions();
    const unsub = poll(refreshPositions, 3000);
    return unsub;
  }, [selectedAcc, refreshPositions]);

  // Closed trades are fetched when the History tab is visible.
  const refreshHistory = useCallback(async () => {
    const acc = selectedAcc;
    if (!acc) return;
    setLoadingHistory(true);
    try {
      const res = await api.history(acc);
      if (accountRef.current !== acc) return;
      setHistory(res.trades || []);
    } catch {
      /* transient — keep the last list */
    } finally {
      setLoadingHistory(false);
    }
  }, [selectedAcc]);

  useEffect(() => {
    if (!selectedAcc || tab !== "history") return;
    refreshHistory();
    return poll(refreshHistory, 5000);
  }, [selectedAcc, tab, refreshHistory]);

  // Load candles from the fast market feed (Dukascopy). Polling keeps both the
  // series and the bid/ask strip live without touching the MT5 bridge.
  const refreshCandles = useCallback(async (showSpinner = false) => {
    const acc = selectedAcc;
    const sym = symbol;
    const tf = timeframe;
    if (!sym) return;
    if (showSpinner) setLoadingCandles(true);
    try {
      const res = await api.marketCandles(sym, tf, acc);
      if (
        symbolRef.current !== sym ||
        timeframeRef.current !== tf ||
        accountRef.current !== acc
      ) {
        return;
      }
      setCandles(res.candles || []);
      setBid(typeof res.bid === "number" ? res.bid : null);
      setAsk(typeof res.ask === "number" ? res.ask : null);
    } catch {
      // Keep the last good series on a transient feed hiccup.
    } finally {
      setLoadingCandles(false);
    }
  }, [selectedAcc, symbol, timeframe]);

  // Switching instrument/timeframe must not leave the previous symbol's bars
  // on screen while the new feed request is in flight.
  useEffect(() => {
    setCandles([]);
  }, [symbol, timeframe]);

  useEffect(() => {
    setLoadingCandles(true);
    refreshCandles(false);
    return poll(() => refreshCandles(false), 3000);
  }, [refreshCandles]);

  // Save auth token
  const saveToken = (val: string) => {
    setToken(val);
    if (val) localStorage.setItem("guardrails.token", val);
    else localStorage.removeItem("guardrails.token");
    if (val) refreshAccounts();
  };

  // Top-bar refresh: re-read the account list (balance/equity) and pull the
  // visible tab's data immediately instead of waiting for its next poll.
  const refreshAll = useCallback(async () => {
    if (refreshing) return;
    setRefreshing(true);
    try {
      await refreshAccounts();
      if (!token) return;
      if (tab === "chart") await refreshCandles(true);
      else if (tab === "positions") await refreshPositions();
      else if (tab === "history") await refreshHistory();
    } finally {
      setRefreshing(false);
    }
  }, [refreshing, refreshAccounts, refreshCandles, refreshPositions, refreshHistory, tab, token]);

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
  ) => {
    if (!selectedAcc) throw new Error("No account selected");
    setTradeBusy(true);
    try {
      // Risk is fixed server-side by account phase. The client only sends the
      // stop loss; TP and position size are derived by the backend/EA.
      const res = await api.trade({
        account: selectedAcc,
        symbol,
        side,
        kind,
        sl,
        tp: null,
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
    try {
      const res = await handlePlaceOrder(
        side,
        kind,
        kind === "limit" ? order.entry : null,
        order.sl,
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
          <span className="brand-mark">
            <ShieldCheck size={18} weight="duotone" />
          </span>
          <span className="brand-name">GuardRails</span>
        </div>
        <div className="header-right">
          <button
            className="token-btn"
            onClick={() => {
              const t = prompt("Enter Mobile API Token", token);
              if (t !== null) saveToken(t.trim());
            }}
            title="API Token"
          >
            <Key size={17} />
          </button>
          {token && (
            <button
              className="token-btn"
              onClick={refreshAll}
              disabled={refreshing}
              title="Refresh"
              aria-label="Refresh"
            >
              <ArrowsClockwise size={17} className={refreshing ? "spin" : ""} />
            </button>
          )}
          {token && (
            <div className="account-menu">
              <button
                className={`token-btn ${accountsOpen ? "active" : ""}`}
                onClick={() => {
                  setSymbolMenuOpen(false);
                  setAccountsOpen((v) => !v);
                }}
                title="Accounts"
                aria-label="Accounts"
                aria-expanded={accountsOpen}
              >
                <Wallet size={18} weight={accountsOpen ? "fill" : "regular"} />
              </button>
              {accountsOpen && (
                <>
                  <div className="acc-backdrop" onClick={() => setAccountsOpen(false)} />
                  <AccountSwitcher
                    accounts={accounts}
                    selected={selectedAcc}
                    onSelect={(login) => {
                      if (login !== selectedAcc) {
                        // Drop the old account's rows before the first poll of
                        // the new one lands, so Close/Cancel can never target
                        // a trade that is not on the selected account.
                        setPositions([]);
                        setPending([]);
                        setHistory([]);
                        setCandles([]);
                        setBid(null);
                        setAsk(null);
                      }
                      setSelectedAcc(login);
                      const acc = accounts.find((a) => a.login === login);
                      if (acc && acc.symbols.length > 0 && !acc.symbols.includes(symbol)) {
                        setSymbol(acc.symbols[0]);
                      }
                      setAccountsOpen(false);
                    }}
                  />
                </>
              )}
            </div>
          )}
        </div>
      </header>

      {error && (
        <div className="banner-error">
          <ShieldWarning size={15} weight="duotone" />
          <span>{error}</span>
        </div>
      )}

      {!token && (
        <div className="token-gate">
          <div className="token-gate-card">
            <div className="token-gate-mark">
              <ShieldCheck size={30} weight="duotone" />
            </div>
            <h2>GuardRails</h2>
            <p>Enter your Mobile API token to open the trading terminal.</p>
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
          <main className="tab-body">
            {tab === "chart" && (
              <div className="tab-pane">
                <div className="symbol-bar">
                  <div className="symbol-field">
                    <span className="symbol-label">Instrument</span>
                    <div className="symbol-picker">
                      <button
                        type="button"
                        className="symbol-button"
                        onClick={() => {
                          setAccountsOpen(false);
                          setSymbolMenuOpen((v) => !v);
                        }}
                        aria-haspopup="listbox"
                        aria-expanded={symbolMenuOpen}
                      >
                        <span>{symbol}</span>
                        <CaretDown
                          size={14}
                          weight="bold"
                          className={`symbol-caret ${symbolMenuOpen ? "open" : ""}`}
                        />
                      </button>
                      {symbolMenuOpen && (
                        <>
                          <div
                            className="symbol-backdrop"
                            onClick={() => setSymbolMenuOpen(false)}
                          />
                          <div className="symbol-menu" role="listbox" aria-label="Instrument">
                            {(currentAccount?.symbols || ["EURUSD"]).map((s) => (
                              <button
                                key={s}
                                type="button"
                                role="option"
                                aria-selected={s === symbol}
                                className={`symbol-item ${s === symbol ? "selected" : ""}`}
                                onClick={() => {
                                  setSymbol(s);
                                  setSymbolMenuOpen(false);
                                }}
                              >
                                <span>{s}</span>
                                {s === symbol && <Check size={15} weight="bold" />}
                              </button>
                            ))}
                          </div>
                        </>
                      )}
                    </div>
                  </div>
                  <button
                    className="btn-trade-trigger"
                    onClick={() => setOrderSheetOpen(true)}
                    disabled={currentAccount?.read_only || currentAccount?.blocked}
                  >
                    <Plus size={15} weight="bold" /> Trade
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
              <span className="nav-icon">
                <ChartLineUp size={20} weight={tab === "chart" ? "fill" : "regular"} />
              </span>
              <span>Chart</span>
            </button>
            <button
              className={`nav-tab ${tab === "positions" ? "active" : ""}`}
              onClick={() => setTab("positions")}
            >
              <span className="nav-icon">
                <ListBullets size={20} weight={tab === "positions" ? "fill" : "regular"} />
                {positions.length > 0 && <span className="badge">{positions.length}</span>}
              </span>
              <span>Positions</span>
            </button>
            <button
              className={`nav-tab ${tab === "history" ? "active" : ""}`}
              onClick={() => setTab("history")}
            >
              <span className="nav-icon">
                <ClockCounterClockwise size={20} weight={tab === "history" ? "fill" : "regular"} />
              </span>
              <span>History</span>
            </button>
          </nav>
        </>
      ) : (
        token && (
          <div className="empty-panel">
            {loadingAccounts ? (
              <p>Loading accounts…</p>
            ) : (
              <>
                <div className="empty-orb">
                  <Wallet size={26} weight="duotone" />
                </div>
                <p>No accounts configured or unauthorized.</p>
                <p className="empty-hint">
                  Add an MT5 account from Discord with <code>/addaccount</code>, or
                  connect a cTrader demo with <code>/addctrader</code>, then refresh.
                </p>
                <div className="empty-actions">
                  <button
                    className="btn-ghost"
                    onClick={() => {
                      const t = prompt("Set API Token", token);
                      if (t !== null) saveToken(t.trim());
                    }}
                  >
                    Set API Token
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