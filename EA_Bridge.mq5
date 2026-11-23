//+------------------------------------------------------------------+
//| EA_Bridge.mq5                                                     |
//| Reports account/position state to your backend and executes      |
//| commands it queues (close, block, open trade).                   |
//|                                                                    |
//| Enforces: SL/TP required on every bot trade, risk-% based lot     |
//| sizing (risk % is sent by backend per-account, capped at 80% of   |
//| free margin), automatic market/limit/stop order type selection,   |
//| correct filling mode read from the symbol, auto-close of manually |
//| placed market positions (leaving external-bot positions alone), |
//| adoption of manual pending orders at the account's fixed risk %,  |
//| and event alerts (pending triggered, SL/TP hit, trade closed)     |
//| pushed to the backend immediately.                                |
//|                                                                    |
//| SETUP:                                                            |
//|  1. Tools > Options > Expert Advisors > enable "Allow WebRequest  |
//|     for listed URL" and add your backend URL                     |
//|  2. Set BackendURL and AccountToken inputs below                  |
//|  3. Attach to any chart, enable "Allow live trading"              |
//|  4. TEST ON A DEMO ACCOUNT FIRST.                                  |
//+------------------------------------------------------------------+
#property strict

input string BackendURL           = "http://127.0.0.1:8000";
input string AccountToken         = "changeme";
input string BridgeDir            = "";       // file-bridge folder under MQL5\Files (e.g. "guardrails"); set by the provisioning agent
input long   AccountLoginOverride = 0;      // 0 = use the terminal's account login; set to the backend account key when they differ
input int    PollSeconds          = 5;
input long   MagicNumber          = 990011;
input long   ExternalMagicMin     = 100000;  // external bot magic range to leave alone
input long   ExternalMagicMax     = 119999;
input double MarginCapPct         = 80.0;   // max % of equity that may be committed as margin across all open positions (keeps free margin >= (100-this)% of equity)
input double PriceTolerancePoints = 10;     // within this many points of market = market order
input string SymbolSuffix         = "0";    // broker suffix for this account (e.g. "b", or "0" for none) — symbols themselves are set from Discord
input int    FastPollMs           = 1000;   // minimum gap between tick-driven command checks (faster than PollSeconds)
input int    CloseAggregationMs   = 1500;   // wait this long after the last partial-close deal before sending one alert
input int    HistoryReportSeconds = 60;     // resend closed-deal history (including manual trades) this often

string g_symbolRoots[];   // bare roots from the backend, e.g. EURUSD
string g_symbols[];       // roots with this EA's SymbolSuffix applied
int    g_symbolCount = 0;
string g_suffix = "";

// Account limits pushed by the backend on every /commands poll. They are
// used when adopting a manual pending order so it gets the same sizing and
// 3.25R TP as a Discord-issued trade.
double g_riskPct = 0.0;
int    g_maxPositions = 2;
double g_rrr = 3.25;
ulong  g_startedAt = 0;

ulong lastFastPollMs = 0;

// Closed trades are replayed from MT5 deal history on startup. This covers a
// terminal/EA restart, or a close that was buffered while the chart symbol was
// quiet (OnTick does not fire for other symbols). The backend drops tickets it
// has already recorded, so a replay can never duplicate history or alerts.
bool  g_historyReconciled = false;
ulong g_historyScanAt = 0;
int   g_historyScanAttempts = 0;
ulong g_lastHistoryReportMs = 0;

// Defined below; declared here because the trade-transaction handler uses
// them as soon as a manual pending order appears.
bool IsPendingOrderType(long orderType);
bool IsSupportedPendingOrderType(long orderType);
string OrderTypeName(ENUM_ORDER_TYPE orderType);
bool IsSymbolEnabled(string symbol);
int OpenBotPositionCount();
bool DeletePendingOrder(ulong ticket);
double CalculateLotSize(string symbol, string dir, double entry, double sl, double riskPct);
double CalculateLotSize(string symbol, string dir, double entry, double sl, double riskPct,
                        double &riskPerLotOut);
bool PlaceAdoptedPending(ENUM_ORDER_TYPE orderType, string symbol, double entry,
                         double sl, double tp, double lots);
bool AdoptManualPendingOrder(ulong ticket);
void AdoptManualPendingOrders();
void PollCommandsFile();
void ReconcileClosedHistory();
void ReportHistory();

// --- Position-level dedup for open/trigger alerts (partial fills can
//     generate multiple deals for what is really one event) ---
string recentKeys[];
ulong  recentKeyTimes[];
int    recentKeyCount = 0;

bool RecentlyFired(string key, int cooldownMs)
{
   ulong now = GetTickCount();
   for(int i = 0; i < recentKeyCount; i++)
   {
      if(recentKeys[i] == key)
      {
         if(now - recentKeyTimes[i] < (ulong)cooldownMs) return true;
         recentKeyTimes[i] = now;
         return false;
      }
   }
   ArrayResize(recentKeys, recentKeyCount + 1);
   ArrayResize(recentKeyTimes, recentKeyCount + 1);
   recentKeys[recentKeyCount] = key;
   recentKeyTimes[recentKeyCount] = now;
   recentKeyCount++;
   if(recentKeyCount > 300)
   {
      for(int i = 0; i < recentKeyCount - 100; i++)
      {
         recentKeys[i] = recentKeys[i + 100];
         recentKeyTimes[i] = recentKeyTimes[i + 100];
      }
      recentKeyCount -= 100;
      ArrayResize(recentKeys, recentKeyCount);
      ArrayResize(recentKeyTimes, recentKeyCount);
   }
   return false;
}

// --- External-bot exemptions. GuardRails must not
//     auto-close these positions/orders or count them in its own reports. ---
bool IsExternalBotMagic(long magic)
{
   return (magic >= ExternalMagicMin && magic <= ExternalMagicMax);
}

// MT5 order types 2..7 are pending orders (limit, stop, stop-limit).
bool IsPendingOrderType(long orderType)
{
   return (orderType >= 2 && orderType <= 7);
}

// GuardRails can recreate the four standard pending types exactly. A
// stop-limit order is still removed, but rejected rather than converted.
bool IsSupportedPendingOrderType(long orderType)
{
   return (orderType == ORDER_TYPE_BUY_LIMIT ||
           orderType == ORDER_TYPE_SELL_LIMIT ||
           orderType == ORDER_TYPE_BUY_STOP ||
           orderType == ORDER_TYPE_SELL_STOP);
}

string OrderTypeName(ENUM_ORDER_TYPE orderType)
{
   if(orderType == ORDER_TYPE_BUY_LIMIT)  return "BUY_LIMIT";
   if(orderType == ORDER_TYPE_SELL_LIMIT) return "SELL_LIMIT";
   if(orderType == ORDER_TYPE_BUY_STOP)   return "BUY_STOP";
   if(orderType == ORDER_TYPE_SELL_STOP)  return "SELL_STOP";
   return "PENDING";
}

bool IsSymbolEnabled(string symbol)
{
   for(int i = 0; i < g_symbolCount; i++)
      if(g_symbols[i] == symbol) return true;
   return false;
}

int OpenBotPositionCount()
{
   int count = 0;
   for(int i = 0; i < PositionsTotal(); i++)
   {
      ulong ticket = PositionGetTicket(i);
      if(ticket <= 0) continue;
      if((long)PositionGetInteger(POSITION_MAGIC) == MagicNumber)
         count++;
   }
   return count;
}

// --- Close-deal aggregation, keyed by position ID (not deal ticket) — combines
//     partial-fill closing deals into a single alert with the correct total P/L ---
ulong  pendingClosePosId[];
double pendingCloseProfit[];
double pendingCloseVolume[];
string pendingCloseSymbol[];
string pendingCloseReason[];
ulong  pendingCloseLastUpdate[];
ulong  pendingCloseTime[];
int    pendingCloseCount = 0;

int FindOrCreatePendingClose(ulong posId)
{
   for(int i = 0; i < pendingCloseCount; i++)
      if(pendingClosePosId[i] == posId) return i;

   int idx = pendingCloseCount;
   ArrayResize(pendingClosePosId, idx + 1);
   ArrayResize(pendingCloseProfit, idx + 1);
   ArrayResize(pendingCloseVolume, idx + 1);
   ArrayResize(pendingCloseSymbol, idx + 1);
   ArrayResize(pendingCloseReason, idx + 1);
   ArrayResize(pendingCloseLastUpdate, idx + 1);
   ArrayResize(pendingCloseTime, idx + 1);
   pendingClosePosId[idx] = posId;
   pendingCloseProfit[idx] = 0;
   pendingCloseVolume[idx] = 0;
   pendingCloseSymbol[idx] = "";
   pendingCloseReason[idx] = "";
   pendingCloseTime[idx] = 0;
   pendingCloseCount++;
   return idx;
}

void BufferCloseDeal(ulong posId, string symbol, string reasonStr, double volume, double profit, datetime closeTime)
{
   int idx = FindOrCreatePendingClose(posId);
   pendingCloseSymbol[idx] = symbol;
   pendingCloseProfit[idx] += profit;
   pendingCloseVolume[idx] += volume;
   if((long)closeTime > (long)pendingCloseTime[idx])
      pendingCloseTime[idx] = (ulong)closeTime;
   if(reasonStr == "SL" || reasonStr == "TP")
      pendingCloseReason[idx] = reasonStr;   // a specific reason always wins over a generic one
   else if(pendingCloseReason[idx] == "")
      pendingCloseReason[idx] = reasonStr;
   pendingCloseLastUpdate[idx] = GetTickCount();
}

// Broker server time can differ from UTC (FundingPips is UTC+2). Deal
// timestamps are in server time, so normalise them before the backend stores
// them as Unix seconds.
long ServerGmtOffsetSeconds()
{
   // TimeTradeServer() keeps advancing with the local clock even when the
   // symbol hasn't ticked (TimeCurrent() is frozen over a quiet weekend).
   long offset = (long)TimeTradeServer() - (long)TimeGMT();
   if(offset < -12 * 3600 || offset > 14 * 3600) return 0;
   return offset;
}

datetime DealTimeUtc(datetime serverTime)
{
   return (datetime)((long)serverTime - ServerGmtOffsetSeconds());
}

void FlushPendingCloses()
{
   for(int i = pendingCloseCount - 1; i >= 0; i--)
   {
      ulong sinceUpdate = GetTickCount() - pendingCloseLastUpdate[i];

      // Primary signal: the position is gone from the terminal, meaning MT5 has
      // fully closed it and no further partial-close deals can arrive for it.
      // A short settle delay guards against flushing in the same instant a
      // same-batch deal is still being written. Falls back to the plain
      // inactivity timeout so we never buffer forever if the position lookup
      // is ever wrong (e.g. some hedging edge case).
      bool positionGone   = !PositionSelectByTicket(pendingClosePosId[i]);
      bool readyByPosition = positionGone && sinceUpdate >= 250;
      bool readyByTimeout  = sinceUpdate >= (ulong)CloseAggregationMs;
      if(!readyByPosition && !readyByTimeout) continue;

      datetime closeUtc = DealTimeUtc((datetime)pendingCloseTime[i]);
      SendEvent("TRADE_CLOSED " + pendingCloseSymbol[i] + " " + IntegerToString((long)pendingClosePosId[i]) + " " +
                pendingCloseReason[i] + " " + DoubleToString(pendingCloseVolume[i], 2) + " " +
                DoubleToString(pendingCloseProfit[i], 2) + " " +
                IntegerToString((long)closeUtc));

      int last = pendingCloseCount - 1;
      pendingClosePosId[i] = pendingClosePosId[last];
      pendingCloseProfit[i] = pendingCloseProfit[last];
      pendingCloseVolume[i] = pendingCloseVolume[last];
      pendingCloseSymbol[i] = pendingCloseSymbol[last];
      pendingCloseReason[i] = pendingCloseReason[last];
      pendingCloseLastUpdate[i] = pendingCloseLastUpdate[last];
      pendingCloseTime[i] = pendingCloseTime[last];
      pendingCloseCount--;
      ArrayResize(pendingClosePosId, pendingCloseCount);
      ArrayResize(pendingCloseProfit, pendingCloseCount);
      ArrayResize(pendingCloseVolume, pendingCloseCount);
      ArrayResize(pendingCloseSymbol, pendingCloseCount);
      ArrayResize(pendingCloseReason, pendingCloseCount);
      ArrayResize(pendingCloseLastUpdate, pendingCloseCount);
      ArrayResize(pendingCloseTime, pendingCloseCount);
   }
}

bool tradingBlocked = false;

//+------------------------------------------------------------------+
//| Symbol list pushed by the backend (SYMBOLS command)              |
//+------------------------------------------------------------------+
string QuoteKey(string root)
{
   string key = "";
   for(int i = 0; i < StringLen(root); i++)
   {
      ushort ch = StringGetCharacter(root, i);
      if(ch >= 'A' && ch <= 'Z')
         ch = (ushort)(ch + 32);
      if((ch >= 'a' && ch <= 'z') || (ch >= '0' && ch <= '9'))
         key += ShortToString(ch);
      else
         key += "_";
   }
   return key;
}

void SetSymbolList(string csv)
{
   string parts[];
   int n = StringSplit(csv, ',', parts);
   if(n <= 0) return;

   string roots[];
   for(int i = 0; i < n && ArraySize(roots) < 20; i++)
   {
      string root = parts[i];
      StringTrimLeft(root);
      StringTrimRight(root);
      // Preserve case: some brokers use case-sensitive suffixes such as
      // FTMO's US100.cash, and SymbolSelect() rejects US100.CASH.
      if(root == "") continue;

      bool duplicate = false;
      for(int j = 0; j < ArraySize(roots); j++)
         if(StringCompare(roots[j], root, false) == 0) { duplicate = true; break; }
      if(duplicate) continue;

      int k = ArraySize(roots);
      ArrayResize(roots, k + 1);
      roots[k] = root;
   }
   if(ArraySize(roots) == 0) return;   // never accept an empty list

   string full[];
   ArrayResize(full, ArraySize(roots));
   for(int i = 0; i < ArraySize(roots); i++)
      full[i] = roots[i] + g_suffix;

   bool same = (ArraySize(full) == g_symbolCount);
   if(same)
      for(int i = 0; i < g_symbolCount; i++)
         if(full[i] != g_symbols[i]) { same = false; break; }
   if(same) return;

   ArrayResize(g_symbolRoots, ArraySize(roots));
   ArrayResize(g_symbols, ArraySize(full));
   for(int i = 0; i < ArraySize(roots); i++)
   {
      g_symbolRoots[i] = roots[i];
      g_symbols[i] = full[i];
      if(!SymbolSelect(g_symbols[i], true))
         Print("Symbol not available in Market Watch: ", g_symbols[i]);
   }
   g_symbolCount = ArraySize(full);

   string shown = "";
   for(int i = 0; i < g_symbolCount; i++)
   {
      if(shown != "") shown += ",";
      shown += g_symbolRoots[i];
   }
   Print("Reporting symbols updated: ", shown);
}

int OnInit()
{
   g_startedAt = GetTickCount();
   EventSetTimer(PollSeconds);
   if(StringLen(BridgeDir) > 0)
      FolderCreate(BridgeDir);

   string sfx = (SymbolSuffix == "0" || SymbolSuffix == "") ? "" : SymbolSuffix;
   g_suffix = sfx;
   // Fallback until the backend pushes the account's enabled list. The
   // backend sends SYMBOLS on every /commands poll, so this is usually
   // replaced within a second of the first poll.
   SetSymbolList("EURUSD,GBPUSD");

   // Give the terminal a few seconds to finish syncing account history, then
   // replay any recent close the event path might have missed.
   g_historyReconciled = false;
   g_historyScanAttempts = 0;
   g_historyScanAt = GetTickCount() + 5000;

   Print("EA_Bridge started. Backend: ", BackendURL, " Magic: ", MagicNumber,
         " Reporting ", g_symbolCount, " symbol(s), suffix '", SymbolSuffix, "'");
   return(INIT_SUCCEEDED);
}

void OnDeinit(const int reason)
{
   EventKillTimer();
}

void OnTimer()
{
   // A close can happen on a symbol the chart isn't subscribed to (the chart is
   // EURUSD H1, the trade might be BTCUSD). With no ticks, OnTick never runs,
   // so buffered closes must also be flushed from the timer.
   FlushPendingCloses();
   ReconcileClosedHistory();
   ReportHistory();

   ReportState();
   PollCommands();              // sync RISK/SYMBOLS before adopting anything
   AdoptManualPendingOrders();
}

void OnTick()
{
   FlushPendingCloses();

   ulong nowMs = GetTickCount();
   if(nowMs - lastFastPollMs >= (ulong)FastPollMs)
   {
      lastFastPollMs = nowMs;
      PollCommands();               // fast path — reacts within FastPollMs during active market hours
      AdoptManualPendingOrders();   // catch phone-placed pendings within ~1s
   }
}

// NOTE: the "R:" prefix is still written into each order's comment (see
// PlaceOrder/PlaceMarketOrder) purely as a human-readable label visible in the
// MT5 terminal — it is deliberately NOT parsed back out anywhere.

//+------------------------------------------------------------------+
//| Auto-close/delete anything not opened by this bot; emit events    |
//| for pending-order triggers and trade closures (SL/TP/manual)      |
//+------------------------------------------------------------------+
ulong processedDeals[];
int processedDealCount = 0;

bool AlreadyProcessedDeal(ulong ticket)
{
   for(int i = 0; i < processedDealCount; i++)
      if(processedDeals[i] == ticket) return true;

   ArrayResize(processedDeals, processedDealCount + 1);
   processedDeals[processedDealCount] = ticket;
   processedDealCount++;

   // keep the list from growing forever
   if(processedDealCount > 500)
   {
      for(int i = 0; i < processedDealCount - 200; i++)
         processedDeals[i] = processedDeals[i + 200];
      processedDealCount -= 200;
      ArrayResize(processedDeals, processedDealCount);
   }
   return false;
}

bool PositionBelongsToUs(ulong posId)
{
   if(!HistorySelectByPosition(posId)) return false;
   int total = HistoryDealsTotal();
   for(int i = 0; i < total; i++)
   {
      ulong dTicket = HistoryDealGetTicket(i);
      if(dTicket == 0) continue;
      if((long)HistoryDealGetInteger(dTicket, DEAL_ENTRY) == DEAL_ENTRY_IN &&
         (long)HistoryDealGetInteger(dTicket, DEAL_MAGIC) == MagicNumber)
         return true;
   }
   return false;
}

// Replays recently closed bot positions from MT5 deal history. This is the
// safety net for a close the live event path missed: EA/terminal restarted
// before the buffer flushed, the chart symbol was quiet so OnTick never ran,
// or the event never reached the backend. It re-sends the exact same
// TRADE_CLOSED message, and the backend drops tickets it has already
// recorded, so a replay can never duplicate history or a Discord alert.
void ReconcileClosedHistory()
{
   if(g_historyReconciled) return;
   if(GetTickCount() < g_historyScanAt) return;

   datetime to   = TimeTradeServer() + 3600;             // +1h guards clock skew
   datetime from = to - (datetime)(7 * 86400);
   if(!HistorySelect(from, to))
   {
      // History may still be downloading right after init; retry a few times.
      g_historyScanAttempts++;
      if(g_historyScanAttempts >= 6) g_historyReconciled = true;
      else g_historyScanAt = GetTickCount() + 5000;
      return;
   }

   // First pass: distinct positions that have a closing deal in the window.
   ulong posIds[];
   int posCount = 0;
   int total = HistoryDealsTotal();
   for(int i = 0; i < total; i++)
   {
      ulong dTicket = HistoryDealGetTicket(i);
      if(dTicket == 0) continue;
      long entry = (long)HistoryDealGetInteger(dTicket, DEAL_ENTRY);
      if(entry != DEAL_ENTRY_OUT && entry != DEAL_ENTRY_OUT_BY) continue;
      ulong posId = (ulong)HistoryDealGetInteger(dTicket, DEAL_POSITION_ID);
      bool seen = false;
      for(int j = 0; j < posCount; j++)
         if(posIds[j] == posId) { seen = true; break; }
      if(seen) continue;
      ArrayResize(posIds, posCount + 1);
      posIds[posCount++] = posId;
   }

   int replayed = 0;
   for(int p = 0; p < posCount; p++)
   {
      ulong posId = posIds[p];

      // Already buffered from the live OnTradeTransaction path - let the
      // normal aggregation flush it instead of sending a second copy.
      bool buffered = false;
      for(int i = 0; i < pendingCloseCount; i++)
         if(pendingClosePosId[i] == posId) { buffered = true; break; }
      if(buffered) continue;

      if(!HistorySelectByPosition(posId)) continue;

      bool ours = false;
      double closeVolume = 0;
      double closeProfit = 0;
      datetime lastClose = 0;
      string symbol = "";
      string reasonStr = "";

      int deals = HistoryDealsTotal();
      for(int i = 0; i < deals; i++)
      {
         ulong dTicket = HistoryDealGetTicket(i);
         if(dTicket == 0) continue;

         long entry = (long)HistoryDealGetInteger(dTicket, DEAL_ENTRY);
         long magic = (long)HistoryDealGetInteger(dTicket, DEAL_MAGIC);
         if(entry == DEAL_ENTRY_IN && magic == MagicNumber) ours = true;
         if(entry != DEAL_ENTRY_OUT && entry != DEAL_ENTRY_OUT_BY) continue;

         if(symbol == "") symbol = HistoryDealGetString(dTicket, DEAL_SYMBOL);
         closeVolume += HistoryDealGetDouble(dTicket, DEAL_VOLUME);
         closeProfit += HistoryDealGetDouble(dTicket, DEAL_PROFIT);

         long dealTime = (long)HistoryDealGetInteger(dTicket, DEAL_TIME);
         if(dealTime > (long)lastClose) lastClose = (datetime)dealTime;

         long rawReason = (long)HistoryDealGetInteger(dTicket, DEAL_REASON);
         if(rawReason == DEAL_REASON_SL) reasonStr = "SL";
         else if(rawReason == DEAL_REASON_TP) reasonStr = "TP";
         else if(rawReason == DEAL_REASON_EXPERT && reasonStr != "SL" && reasonStr != "TP")
            reasonStr = "EA";
         else if(reasonStr == "")
            reasonStr = "MANUAL";
      }

      if(!ours || lastClose == 0) continue;
      if(reasonStr == "") reasonStr = "MANUAL";

      // A close that just happened belongs to the live path, which waits for
      // partial fills to settle before sending. Don't race it.
      if((long)TimeTradeServer() - (long)lastClose < 5) continue;

      datetime closeUtc = DealTimeUtc(lastClose);
      SendEvent("TRADE_CLOSED " + symbol + " " + IntegerToString((long)posId) + " " +
                reasonStr + " " + DoubleToString(closeVolume, 2) + " " +
                DoubleToString(closeProfit, 2) + " " +
                IntegerToString((long)closeUtc));
      replayed++;
   }

   g_historyReconciled = true;
   Print("History reconciliation: ", posCount, " closed position(s) in 7d, ",
         replayed, " replayed");
}

void OnTradeTransaction(const MqlTradeTransaction &trans,
                         const MqlTradeRequest &request,
                         const MqlTradeResult &result)
{
   if(trans.type == TRADE_TRANSACTION_DEAL_ADD)
   {
      ulong dealTicket = trans.deal;
      if(AlreadyProcessedDeal(dealTicket)) return;   // guards against duplicate firing
      if(!HistoryDealSelect(dealTicket)) return;

      long dealMagic = (long)HistoryDealGetInteger(dealTicket, DEAL_MAGIC);
      long entryType = HistoryDealGetInteger(dealTicket, DEAL_ENTRY);
      string symbol  = HistoryDealGetString(dealTicket, DEAL_SYMBOL);
      ulong posId    = HistoryDealGetInteger(dealTicket, DEAL_POSITION_ID);

      if(entryType == DEAL_ENTRY_OUT || entryType == DEAL_ENTRY_OUT_BY)
      {
         // Don't trust this deal's own magic for closes — MT5 commonly stamps
         // the closing deal from a manual terminal close with magic 0 even when
         // closing a position this bot opened, which used to make that close
         // fall through unhandled entirely: no TRADE_CLOSED event, no loss
         // counted, no accounting at all. Check who opened the POSITION instead.
         if(PositionBelongsToUs(posId))
         {
            double profit = HistoryDealGetDouble(dealTicket, DEAL_PROFIT);
            double volume = HistoryDealGetDouble(dealTicket, DEAL_VOLUME);
            ENUM_DEAL_REASON reason = (ENUM_DEAL_REASON)HistoryDealGetInteger(dealTicket, DEAL_REASON);
            string reasonStr = "MANUAL";
            if(reason == DEAL_REASON_SL) reasonStr = "SL";
            else if(reason == DEAL_REASON_TP) reasonStr = "TP";
            else if(reason == DEAL_REASON_EXPERT) reasonStr = "EA";

            // Buffer instead of sending immediately — partial-fill closes generate multiple
            // deals for one real close, so we combine them into a single alert with the
            // correct total profit once no more deals arrive for this position for a bit.
            datetime dealTime = (datetime)HistoryDealGetInteger(dealTicket, DEAL_TIME);
            BufferCloseDeal(posId, symbol, reasonStr, volume, profit, dealTime);
         }
      }
      else if(dealMagic == MagicNumber && entryType == DEAL_ENTRY_IN)
      {
         bool wasPending = false;
         if(HistoryOrderSelect(trans.order))
         {
            long orderType = HistoryOrderGetInteger(trans.order, ORDER_TYPE);
            if(orderType == ORDER_TYPE_BUY_LIMIT || orderType == ORDER_TYPE_SELL_LIMIT ||
               orderType == ORDER_TYPE_BUY_STOP  || orderType == ORDER_TYPE_SELL_STOP)
            {
               wasPending = true;
               // Partial fills can trigger this more than once for the same position — dedup by posId.
               if(!RecentlyFired("PENDING_TRIG:" + IntegerToString((long)posId), 3000))
                  SendEvent("PENDING_TRIGGERED " + symbol + " " + IntegerToString((long)posId));
            }
         }
         if(!wasPending)
         {
            if(!RecentlyFired("OPEN:" + IntegerToString((long)posId), 3000))
            {
               long dealType = HistoryDealGetInteger(dealTicket, DEAL_TYPE);
               string dir = (dealType == DEAL_TYPE_BUY) ? "BUY" : "SELL";
               double price = HistoryDealGetDouble(dealTicket, DEAL_PRICE);
               double volume = HistoryDealGetDouble(dealTicket, DEAL_VOLUME);
               SendEvent("POSITION_OPENED " + symbol + " " + IntegerToString((long)posId) + " " +
                         dir + " " + DoubleToString(volume, 2) + " " + DoubleToString(price, 5));
            }
         }
      }
      else if(dealMagic != MagicNumber && !IsExternalBotMagic(dealMagic) && entryType == DEAL_ENTRY_IN)
      {
         // Not ours — this is a manual trade. Close it immediately.
         Print("Manual position detected (ticket ", posId, ") — auto-closing.");
         ClosePositionByTicket(posId);
      }
   }
   else if(trans.type == TRADE_TRANSACTION_ORDER_ADD)
   {
      // Intentionally no immediate adoption here. MT5 has not always finished
      // registering the order inside this transaction, and deleting it now
      // returns 10013. OnTick/OnTimer adopt it about a second later.
   }
}

//+------------------------------------------------------------------+
//| Account key used by the backend                                  |
//|                                                                  |
//| Some setups register the account in the backend under a different |
//| number than the MT5 terminal login (for example a Discord-side    |
//| account key). AccountLoginOverride keeps reports and command      |
//| polling on that key; 0 keeps the normal terminal login.           |
//+------------------------------------------------------------------+
long BackendAccountLogin()
{
   long login = AccountLoginOverride;
   if(login <= 0) login = (long)AccountInfoInteger(ACCOUNT_LOGIN);
   return login;
}

//+------------------------------------------------------------------+
//| Parse a timeframe name into an MT5 ENUM_TIMEFRAMES value          |
//+------------------------------------------------------------------+
ENUM_TIMEFRAMES ParseTimeframe(string name)
{
   if(name == "M1")  return PERIOD_M1;
   if(name == "M5")  return PERIOD_M5;
   if(name == "M15") return PERIOD_M15;
   if(name == "M30") return PERIOD_M30;
   if(name == "H1")  return PERIOD_H1;
   if(name == "H4")  return PERIOD_H4;
   if(name == "D1")  return PERIOD_D1;
   if(name == "W1")  return PERIOD_W1;
   return PERIOD_CURRENT;
}

//+------------------------------------------------------------------+
//| File-bridge transport                                            |
//|                                                                   |
//| Some brokers/VPS setups cannot whitelist a WebRequest URL in      |
//| Tools > Options (the list is encrypted per data folder), which    |
//| makes every WebRequest fail with 4014. The Linux file-bridge       |
//| relay runs on the same host, so it can relay the exact same HTTP   |
//| traffic: the EA writes request bodies into MQL5\Files and reads    |
//| command lines back. No whitelist is involved.                     |
//+------------------------------------------------------------------+
bool FileBridgeEnabled()
{
   return StringLen(BridgeDir) > 0;
}

string BridgePath(string name)
{
   return BridgeDir + "\\" + name;
}

void BridgeWrite(string name, string content)
{
   string tmp = name + ".tmp";
   int h = FileOpen(BridgePath(tmp), FILE_WRITE | FILE_TXT | FILE_ANSI);
   if(h == INVALID_HANDLE)
   {
      Print("File bridge write failed for ", name, ", error ", GetLastError());
      return;
   }
   FileWriteString(h, content);
   FileClose(h);
   FileDelete(BridgePath(name));
   if(!FileMove(BridgePath(tmp), 0, BridgePath(name), FILE_REWRITE))
      Print("File bridge move failed for ", name, ", error ", GetLastError());
}

void BridgeQueueEvent(string content)
{
   static int seq = 0;
   seq++;
   BridgeWrite("event_" + IntegerToString((int)GetTickCount()) + "_" +
               IntegerToString(seq) + ".body", content);
}

bool ReadCommandFile(string path)
{
   if(!FileIsExist(path)) return false;
   int h = FileOpen(path, FILE_READ | FILE_TXT | FILE_ANSI);
   if(h == INVALID_HANDLE) return false;
   string response = "";
   while(!FileIsEnding(h))
   {
      string line = FileReadString(h);
      response += line + "\n";
   }
   FileClose(h);
   FileDelete(path);
   if(StringLen(response) == 0) return true;

   string lines[];
   int n = StringSplit(response, '\n', lines);
   for(int i = 0; i < n; i++)
   {
      string line = lines[i];
      StringTrimLeft(line);
      StringTrimRight(line);
      if(line == "") continue;
      ExecuteCommand(line);
   }
   return true;
}

void PollCommandsFile()
{
   // Legacy single-file queue. Older relays used to overwrite commands.txt
   // every poll, which could drop a queued OPEN before this EA read it. Keep
   // reading it during upgrades, then switch to the loss-free unique-file queue.
   ReadCommandFile(BridgePath("commands.txt"));

   string filter = BridgeDir + "\\cmd_*.txt";
   string found = "";
   long search = FileFindFirst(filter, found);
   if(search == INVALID_HANDLE) return;

   string files[];
   int count = 0;
   do
   {
      if(found != "" && found != "." && found != "..")
      {
         ArrayResize(files, count + 1);
         files[count] = found;
         count++;
      }
   }
   while(FileFindNext(search, found));
   FileFindClose(search);

   for(int i = 0; i < count; i++)
   {
      string path = files[i];
      // Some builds return the path relative to MQL5\Files, others just the
      // file name. Normalise both.
      if(StringFind(path, "\\") < 0)
         path = BridgePath(path);
      ReadCommandFile(path);
   }
}

//+------------------------------------------------------------------+
//| Fetch candles for a symbol/timeframe and POST them to /candles    |
//+------------------------------------------------------------------+
void SendCandles(string root, string tfName, int count)
{
   string symbol = root + g_suffix;
   if(!SymbolSelect(symbol, true)) return;
   ENUM_TIMEFRAMES tf = ParseTimeframe(tfName);
   if(tf == PERIOD_CURRENT) return;
   if(count < 1 || count > 500) count = 300;
   MqlRates rates[];
   int got = CopyRates(symbol, tf, 0, count, rates);
   if(got <= 0) return;
   string url = BackendURL + "/candles";
   string body = "token=" + AccountToken;
   body += "&account=" + IntegerToString(BackendAccountLogin());
   body += "&symbol=" + root;
   body += "&timeframe=" + tfName;
   int digits = (int)SymbolInfoInteger(symbol, SYMBOL_DIGITS);
   if(digits <= 0) digits = 5;
   string bars = "";
   for(int i = 0; i < got; i++)
   {
      if(bars != "") bars += ";";
      bars += IntegerToString((int)rates[i].time) + "," +
              DoubleToString(rates[i].open, digits) + "," +
              DoubleToString(rates[i].high, digits) + "," +
              DoubleToString(rates[i].low, digits) + "," +
              DoubleToString(rates[i].close, digits) + "," +
              IntegerToString((int)rates[i].tick_volume);
   }
   body += "&bars=" + bars;
   if(FileBridgeEnabled())
   {
      BridgeWrite("candles_" + root + "_" + tfName + ".body", body);
      Print("Queued ", got, " candles for ", root, " ", tfName);
      return;
   }
   char post[]; char result[]; string resultHeaders;
   StringToCharArray(body, post, 0, StringLen(body));
   ResetLastError();
   int res = WebRequest("POST", url, "Content-Type: application/x-www-form-urlencoded\r\n",
                        5000, post, result, resultHeaders);
   if(res == -1)
      Print("SendCandles failed, error ", GetLastError(), " — check Allow WebRequest URL list");
   else
      Print("Sent ", got, " candles for ", root, " ", tfName);
}

//+------------------------------------------------------------------+
//| Send account + position snapshot to backend                      |
//+------------------------------------------------------------------+
void ReportState()
{
   string url = BackendURL + "/report";
   string body = "token=" + AccountToken;
   body += "&account=" + IntegerToString(BackendAccountLogin());
   body += "&balance=" + DoubleToString(AccountInfoDouble(ACCOUNT_BALANCE), 2);
   body += "&equity="  + DoubleToString(AccountInfoDouble(ACCOUNT_EQUITY), 2);
   body += "&margin="  + DoubleToString(AccountInfoDouble(ACCOUNT_MARGIN), 2);
   body += "&freemargin=" + DoubleToString(AccountInfoDouble(ACCOUNT_MARGIN_FREE), 2);
   body += "&blocked=" + (tradingBlocked ? "1" : "0");

   // Quotes for every symbol enabled on this account. The backend sends the
   // list; keys use the same lower-case / non-alphanumeric -> "_" mapping as
   // symbols.quote_key() so status and market-closed checks can find them.
   for(int i = 0; i < g_symbolCount; i++)
   {
      string key = QuoteKey(g_symbolRoots[i]);
      int digits = (int)SymbolInfoInteger(g_symbols[i], SYMBOL_DIGITS);
      if(digits <= 0) digits = 5;
      body += "&" + key + "_ask=" + DoubleToString(SymbolInfoDouble(g_symbols[i], SYMBOL_ASK), digits);
      body += "&" + key + "_bid=" + DoubleToString(SymbolInfoDouble(g_symbols[i], SYMBOL_BID), digits);
   }

   string positions = "";
   int total = PositionsTotal();
   for(int i = 0; i < total; i++)
   {
      ulong ticket = PositionGetTicket(i);
      if(ticket <= 0) continue;
      // External-bot positions are not GuardRails' business: don't report them,
      // don't count them toward the max-open-position limit, and never
      // include them in close/status commands.
      if(IsExternalBotMagic((long)PositionGetInteger(POSITION_MAGIC))) continue;
      string type = (PositionGetInteger(POSITION_TYPE) == POSITION_TYPE_BUY) ? "BUY" : "SELL";
      double entryP = PositionGetDouble(POSITION_PRICE_OPEN);
      double curP   = (PositionGetInteger(POSITION_TYPE) == POSITION_TYPE_BUY)
                      ? SymbolInfoDouble(PositionGetString(POSITION_SYMBOL), SYMBOL_BID)
                      : SymbolInfoDouble(PositionGetString(POSITION_SYMBOL), SYMBOL_ASK);
      int pdig = (int)SymbolInfoInteger(PositionGetString(POSITION_SYMBOL), SYMBOL_DIGITS);
      if(pdig <= 0) pdig = 5;
      if(positions != "") positions += "|";
      positions += PositionGetString(POSITION_SYMBOL) + "," +
                   IntegerToString((long)ticket) + "," +
                   type + "," +
                   DoubleToString(PositionGetDouble(POSITION_VOLUME), 2) + "," +
                   DoubleToString(PositionGetDouble(POSITION_PROFIT), 2) + "," +
                   DoubleToString(PositionGetDouble(POSITION_SL), pdig) + "," +
                   DoubleToString(PositionGetDouble(POSITION_TP), pdig) + "," +
                   DoubleToString(entryP, pdig) + "," +
                   DoubleToString(curP, pdig);
   }
   body += "&positions=" + positions;

   string pendings = "";
   int totalOrders = OrdersTotal();
   for(int i = 0; i < totalOrders; i++)
   {
      ulong ticket = OrderGetTicket(i);
      if(ticket <= 0) continue;
      if((long)OrderGetInteger(ORDER_MAGIC) != MagicNumber) continue;

      long type = OrderGetInteger(ORDER_TYPE);
      string typeStr = "OTHER";
      if(type == ORDER_TYPE_BUY_LIMIT)  typeStr = "BUY_LIMIT";
      else if(type == ORDER_TYPE_SELL_LIMIT) typeStr = "SELL_LIMIT";
      else if(type == ORDER_TYPE_BUY_STOP)   typeStr = "BUY_STOP";
      else if(type == ORDER_TYPE_SELL_STOP)  typeStr = "SELL_STOP";

      if(pendings != "") pendings += "|";
      pendings += OrderGetString(ORDER_SYMBOL) + "," +
                  IntegerToString((long)ticket) + "," +
                  typeStr + "," +
                  DoubleToString(OrderGetDouble(ORDER_PRICE_OPEN), 5) + "," +
                  DoubleToString(OrderGetDouble(ORDER_SL), 5) + "," +
                  DoubleToString(OrderGetDouble(ORDER_TP), 5);
   }
   body += "&pending=" + pendings;

   if(FileBridgeEnabled())
   {
      BridgeWrite("report.body", body);
      return;
   }

   char post[]; char result[]; string resultHeaders;
   StringToCharArray(body, post, 0, StringLen(body));
   ResetLastError();
   int res = WebRequest("POST", url, "Content-Type: application/x-www-form-urlencoded\r\n",
                         5000, post, result, resultHeaders);
   if(res == -1)
      Print("ReportState failed, error ", GetLastError(), " — check Allow WebRequest URL list");
}

//+------------------------------------------------------------------+
//| Percent-encode a form value. Symbols can contain characters such  |
//| as '+' or '&', which would otherwise corrupt the deals field.     |
//+------------------------------------------------------------------+
string UrlEncodeValue(string text)
{
   string result = "";
   for(int i = 0; i < StringLen(text); i++)
   {
      ushort ch = StringGetCharacter(text, i);
      if((ch >= 'A' && ch <= 'Z') || (ch >= 'a' && ch <= 'z') ||
         (ch >= '0' && ch <= '9') || ch == '-' || ch == '_' || ch == '.' || ch == '~')
         result += ShortToString(ch);
      else
         result += StringFormat("%%%02X", (int)ch);
   }
   return result;
}

//+------------------------------------------------------------------+
//| Send closed deals (manual and bot) for display-only history.      |
//| The backend stores them outside the risk-managed trades table, so |
//| they can never affect daily loss limits or blocking.              |
//+------------------------------------------------------------------+
void ReportHistory()
{
   int interval = (HistoryReportSeconds > 0) ? HistoryReportSeconds : 60;
   ulong now = GetTickCount();
   if(g_lastHistoryReportMs != 0 &&
      now - g_lastHistoryReportMs < (ulong)interval * 1000)
      return;
   g_lastHistoryReportMs = now;

   datetime to = TimeTradeServer() + 3600;             // +1h guards clock skew
   datetime from = to - (datetime)(90 * 86400);
   if(!HistorySelect(from, to))
   {
      Print("History report: HistorySelect failed, error ", GetLastError());
      return;
   }

   // Remember which positions this EA opened. A manual close is stamped with
   // magic 0, so the closing deal alone cannot tell us whether it was ours.
   long botPosIds[];
   int botPosCount = 0;
   int total = HistoryDealsTotal();
   for(int i = 0; i < total; i++)
   {
      ulong dealTicket = HistoryDealGetTicket(i);
      if(dealTicket == 0) continue;
      if((long)HistoryDealGetInteger(dealTicket, DEAL_ENTRY) != DEAL_ENTRY_IN) continue;
      if((long)HistoryDealGetInteger(dealTicket, DEAL_MAGIC) != MagicNumber) continue;
      long posId = (long)HistoryDealGetInteger(dealTicket, DEAL_POSITION_ID);
      if(posId == 0) continue;
      bool known = false;
      for(int j = 0; j < botPosCount; j++)
         if(botPosIds[j] == posId) { known = true; break; }
      if(!known)
      {
         ArrayResize(botPosIds, botPosCount + 1);
         botPosIds[botPosCount++] = posId;
      }
   }

   // Collect closing deals newest-first, capped so the bridge body stays small.
   ulong dealIds[];
   long posIds[];
   long closeTimes[];
   string symbols[];
   string sides[];
   double volumes[];
   double profits[];
   int count = 0;
   for(int i = total - 1; i >= 0 && count < 500; i--)
   {
      ulong dealTicket = HistoryDealGetTicket(i);
      if(dealTicket == 0) continue;
      long entry = (long)HistoryDealGetInteger(dealTicket, DEAL_ENTRY);
      if(entry != DEAL_ENTRY_OUT && entry != DEAL_ENTRY_OUT_BY) continue;
      long posId = (long)HistoryDealGetInteger(dealTicket, DEAL_POSITION_ID);
      if(posId == 0) continue;
      string symbol = HistoryDealGetString(dealTicket, DEAL_SYMBOL);
      if(symbol == "") continue;

      long dealType = (long)HistoryDealGetInteger(dealTicket, DEAL_TYPE);
      datetime closeUtc = DealTimeUtc((datetime)HistoryDealGetInteger(dealTicket, DEAL_TIME));
      if((long)closeUtc <= 0) continue;

      ArrayResize(dealIds, count + 1);
      ArrayResize(posIds, count + 1);
      ArrayResize(closeTimes, count + 1);
      ArrayResize(symbols, count + 1);
      ArrayResize(sides, count + 1);
      ArrayResize(volumes, count + 1);
      ArrayResize(profits, count + 1);
      dealIds[count] = dealTicket;
      posIds[count] = posId;
      closeTimes[count] = (long)closeUtc;
      symbols[count] = symbol;
      sides[count] = (dealType == DEAL_TYPE_SELL) ? "BUY" : "SELL";
      volumes[count] = HistoryDealGetDouble(dealTicket, DEAL_VOLUME);
      profits[count] = HistoryDealGetDouble(dealTicket, DEAL_PROFIT)
                     + HistoryDealGetDouble(dealTicket, DEAL_SWAP)
                     + HistoryDealGetDouble(dealTicket, DEAL_COMMISSION);
      count++;
   }

   string deals = "";
   for(int i = 0; i < count; i++)
   {
      bool ours = ((long)HistoryDealGetInteger(dealIds[i], DEAL_MAGIC) == MagicNumber);
      if(!ours)
      {
         for(int j = 0; j < botPosCount; j++)
            if(botPosIds[j] == posIds[i]) { ours = true; break; }
      }
      string source = ours ? "broker_bot" : "broker";
      if(deals != "") deals += "|";
      deals += IntegerToString((long)dealIds[i]) + "," +
               IntegerToString(posIds[i]) + "," +
               IntegerToString(closeTimes[i]) + "," +
               symbols[i] + "," +
               sides[i] + "," +
               DoubleToString(volumes[i], 2) + "," +
               DoubleToString(profits[i], 2) + "," +
               source;
   }

   string body = "token=" + AccountToken +
                 "&account=" + IntegerToString(BackendAccountLogin()) +
                 "&deals=" + UrlEncodeValue(deals);
   if(FileBridgeEnabled())
   {
      BridgeWrite("history.body", body);
      return;
   }

   string url = BackendURL + "/mt5/history";
   char post[]; char result[]; string resultHeaders;
   StringToCharArray(body, post, 0, StringLen(body));
   ResetLastError();
   int res = WebRequest("POST", url, "Content-Type: application/x-www-form-urlencoded\r\n",
                        5000, post, result, resultHeaders);
   if(res == -1)
      Print("ReportHistory failed, error ", GetLastError());
}

//+------------------------------------------------------------------+
//| Fire-and-forget event notification to backend                    |
//+------------------------------------------------------------------+
void SendEvent(string message)
{
   string url = BackendURL + "/event";
   string body = "token=" + AccountToken +
                 "&account=" + IntegerToString(BackendAccountLogin()) +
                 "&message=" + UrlEncodeSpaces(message);
   if(FileBridgeEnabled())
   {
      BridgeQueueEvent(body);
      return;
   }
   char post[]; char result[]; string resultHeaders;
   StringToCharArray(body, post, 0, StringLen(body));
   ResetLastError();
   int res = WebRequest("POST", url, "Content-Type: application/x-www-form-urlencoded\r\n",
                         5000, post, result, resultHeaders);
   if(res == -1)
      Print("SendEvent failed, error ", GetLastError());
}

string UrlEncodeSpaces(string text)
{
   string result = "";
   for(int i = 0; i < StringLen(text); i++)
   {
      ushort ch = StringGetCharacter(text, i);
      if(ch == ' ') result += "%20";
      else result += ShortToString(ch);
   }
   return result;
}

//+------------------------------------------------------------------+
//| Poll backend for queued commands and execute them                 |
//+------------------------------------------------------------------+
void PollCommands()
{
   if(FileBridgeEnabled())
   {
      PollCommandsFile();
      return;
   }
   string url = BackendURL + "/commands?token=" + AccountToken +
                "&account=" + IntegerToString(BackendAccountLogin());
   char post[]; char result[]; string resultHeaders;
   ResetLastError();
   int res = WebRequest("GET", url, "", 5000, post, result, resultHeaders);
   if(res == -1)
   {
      Print("PollCommands failed, error ", GetLastError());
      return;
   }
   string response = CharArrayToString(result);
   if(StringLen(response) == 0) return;

   string lines[];
   int n = StringSplit(response, '\n', lines);
   for(int i = 0; i < n; i++)
   {
      string line = lines[i];
      StringTrimLeft(line);
      StringTrimRight(line);
      if(line == "") continue;
      ExecuteCommand(line);
   }
}

void ExecuteCommand(string line)
{
   string parts[];
   int n = StringSplit(line, ' ', parts);
   if(n == 0) return;
   string cmd = parts[0];

   if(cmd == "CLOSE" && n >= 2)
   {
      ClosePositionByTicket((ulong)StringToInteger(parts[1]));
   }
   else if(cmd == "CLOSE_ALL")
   {
      CloseAllBotPositions();
   }
   else if(cmd == "BLOCK")
   {
      tradingBlocked = true;
      CloseAllBotPositions();
      Print("Trading BLOCKED by backend rule.");
   }
   else if(cmd == "UNBLOCK")
   {
      tradingBlocked = false;
      Print("Trading UNBLOCKED.");
   }
   else if(cmd == "DELETE_PENDING" && n >= 2)
   {
      DeletePendingOrder((ulong)StringToInteger(parts[1]));
   }
   else if(cmd == "SYMBOLS" && n >= 2)
   {
      // SYMBOLS EURUSD,GBPUSD,US100.cash — bare broker roots from Discord;
      // the spelling is preserved because suffixes can be case-sensitive.
      SetSymbolList(parts[1]);
   }
   else if(cmd == "RISK" && n >= 2)
   {
      // RISK 1.0 — account's fixed risk % (0 means not configured).
      g_riskPct = StringToDouble(parts[1]);
   }
   else if(cmd == "MAXPOS" && n >= 2)
   {
      // MAXPOS 2 — hardcoded max open positions for this account.
      g_maxPositions = (int)StringToInteger(parts[1]);
   }
   else if(cmd == "RRR" && n >= 2)
   {
      // RRR 3.25 — take-profit multiple used for adopted pending orders.
      double rrr = StringToDouble(parts[1]);
      if(rrr > 0) g_rrr = rrr;
   }
   else if(cmd == "OPEN_MARKET" && n >= 6)
   {
      // OPEN_MARKET BUY|SELL SYMBOL SL RISK_PCT RRR
      // SYMBOL is the bare root from Discord — append our own configured suffix
      string dir    = parts[1];
      string symbol = parts[2] + g_suffix;
      double sl     = StringToDouble(parts[3]);
      double risk   = StringToDouble(parts[4]);
      double rrr    = StringToDouble(parts[5]);
      PlaceMarketOrder(dir, symbol, sl, risk, rrr);
   }
   else if(cmd == "OPEN" && n >= 7)
   {
      // OPEN BUY|SELL SYMBOL ENTRY SL TP RISK_PCT
      // SYMBOL is the bare root from Discord — append our own configured suffix
      string dir    = parts[1];
      string symbol = parts[2] + g_suffix;
      double entry  = StringToDouble(parts[3]);
      double sl     = StringToDouble(parts[4]);
      double tp     = StringToDouble(parts[5]);
      double risk   = StringToDouble(parts[6]);
      PlaceOrder(dir, symbol, entry, sl, tp, risk);
   }
   else if(cmd == "CANDLES" && n >= 4)
   {
      // CANDLES SYMBOL TIMEFRAME COUNT — fetch candles and POST them back.
      // SYMBOL is the bare root; we append our own configured suffix.
      SendCandles(parts[1], parts[2], (int)StringToInteger(parts[3]));
   }
}

void ClosePositionByTicket(ulong ticket)
{
   if(!PositionSelectByTicket(ticket)) return;
   string symbol = PositionGetString(POSITION_SYMBOL);
   MqlTradeRequest request = {};
   MqlTradeResult  result  = {};
   request.action       = TRADE_ACTION_DEAL;
   request.position      = ticket;
   request.symbol        = symbol;
   request.volume        = PositionGetDouble(POSITION_VOLUME);
   request.type          = (PositionGetInteger(POSITION_TYPE) == POSITION_TYPE_BUY) ? ORDER_TYPE_SELL : ORDER_TYPE_BUY;
   request.price         = (request.type == ORDER_TYPE_SELL) ?
                            SymbolInfoDouble(symbol, SYMBOL_BID) :
                            SymbolInfoDouble(symbol, SYMBOL_ASK);
   request.deviation     = 20;
   request.magic         = MagicNumber;
   request.type_filling  = GetFillingMode(symbol);
   if(!OrderSend(request, result))
      Print("Close failed for ticket ", ticket, ": ", result.retcode, " ", result.comment);
}

void CloseAllBotPositions()
{
   for(int i = PositionsTotal() - 1; i >= 0; i--)
   {
      ulong ticket = PositionGetTicket(i);
      if(ticket <= 0) continue;
      if((long)PositionGetInteger(POSITION_MAGIC) == MagicNumber)
         ClosePositionByTicket(ticket);
   }
}

bool DeletePendingOrder(ulong ticket)
{
   if(!OrderSelect(ticket))
   {
      Print("Pending order not found: ", ticket);
      return false;
   }
   MqlTradeRequest req = {};
   MqlTradeResult  res = {};
   req.action = TRADE_ACTION_REMOVE;
   req.order  = ticket;
   if(!OrderSend(req, res))
   {
      Print("Delete pending failed for ticket ", ticket, ": ", res.retcode, " ", res.comment);
      return false;
   }
   if(res.retcode != TRADE_RETCODE_DONE && res.retcode != TRADE_RETCODE_PLACED)
   {
      Print("Delete pending rejected for ticket ", ticket, ": ", res.retcode, " ", res.comment);
      return false;
   }
   Print("Pending order deleted: ", ticket);
   return true;
}

ENUM_ORDER_TYPE_FILLING GetFillingMode(string symbol)
{
   long filling = SymbolInfoInteger(symbol, SYMBOL_FILLING_MODE);
   if((filling & SYMBOL_FILLING_FOK) != 0) return ORDER_FILLING_FOK;
   if((filling & SYMBOL_FILLING_IOC) != 0) return ORDER_FILLING_IOC;
   return ORDER_FILLING_RETURN;
}

double CalculateLotSize(string symbol, string dir, double entry, double sl, double riskPct)
{
   double ignoredRiskPerLot = 0.0;
   return CalculateLotSize(symbol, dir, entry, sl, riskPct, ignoredRiskPerLot);
}

double CalculateLotSize(string symbol, string dir, double entry, double sl, double riskPct,
                        double &riskPerLotOut)
{
   riskPerLotOut = 0.0;
   double balance = AccountInfoDouble(ACCOUNT_BALANCE);
   double riskAmount = balance * riskPct / 100.0;

   double tickValue = SymbolInfoDouble(symbol, SYMBOL_TRADE_TICK_VALUE);
   double tickSize  = SymbolInfoDouble(symbol, SYMBOL_TRADE_TICK_SIZE);
   if(tickSize <= 0) tickSize = SymbolInfoDouble(symbol, SYMBOL_POINT);

   double slDistance = MathAbs(entry - sl);
   if(slDistance <= 0) return 0;

   // Risk per lot = the money lost if 1 lot moves from entry to SL. Ask the
   // terminal's own profit calculator instead of trusting the symbol's tick
   // value: some CFD brokers report a tick value that is 100x too small for
   // XAUUSD (0.01 instead of 1.00), which makes the risk-based lot size 100x
   // too large and leaves the 80% margin cap to decide the size. That is why
   // gold used to come out around 0.6 lots on a $10k account. OrderCalcProfit
   // applies contract size, quote-currency conversion and the symbol's CFD
   // calculation mode correctly.
   double riskPerLot = 0.0;
   double probeProfit = 0.0;
   ENUM_ORDER_TYPE calcType = (dir == "BUY") ? ORDER_TYPE_BUY : ORDER_TYPE_SELL;
   if(OrderCalcProfit(calcType, symbol, 1.0, entry, sl, probeProfit) && probeProfit != 0.0)
      riskPerLot = MathAbs(probeProfit);

   // Fallback for symbols where OrderCalcProfit is unavailable.
   if(riskPerLot <= 0 && tickValue > 0 && tickSize > 0)
      riskPerLot = slDistance * tickValue / tickSize;

   if(riskPerLot <= 0)
   {
      Print("Cannot calculate risk per lot for ", symbol, " — no tick value or profit data.");
      return 0;
   }
   riskPerLotOut = riskPerLot;

   double idealLots = riskAmount / riskPerLot;

   double lotStep = SymbolInfoDouble(symbol, SYMBOL_VOLUME_STEP);
   double minLot   = SymbolInfoDouble(symbol, SYMBOL_VOLUME_MIN);
   double maxLot   = SymbolInfoDouble(symbol, SYMBOL_VOLUME_MAX);
   if(lotStep <= 0) lotStep = 0.01;

   // Round to the nearer broker step instead of always flooring. A coarse
   // step (NAS100 uses 0.1) can otherwise leave a trade risking far less
   // than requested — e.g. 0.2 lots at a 179-point SL risks 0.35%, not 0.5%.
   // Never let rounding itself push the risk more than 5% over target.
   double flooredLots = MathFloor(idealLots / lotStep) * lotStep;
   double ceiledLots  = flooredLots + lotStep;
   double flooredRisk = flooredLots * riskPerLot;
   double ceiledRisk  = ceiledLots * riskPerLot;
   double lots = flooredLots;
   if(ceiledRisk <= riskAmount * 1.05 &&
      (riskAmount - flooredRisk) > (ceiledRisk - riskAmount))
      lots = ceiledLots;

   double riskBasedLots = lots;   // what the requested risk % actually calls for — kept for comparison below

   ENUM_ORDER_TYPE orderType = (dir == "BUY") ? ORDER_TYPE_BUY : ORDER_TYPE_SELL;
   double marginPerLot = 0;
   double maxLotsByMargin = -1;   // -1 = margin check unavailable, cap not applied
   if(OrderCalcMargin(orderType, symbol, 1.0, entry, marginPerLot) && marginPerLot > 0)
   {
      // Margin Used = Lots × Contract Size × Price ÷ Leverage — OrderCalcMargin
      // above computes exactly that per-lot, broker-correct (handles contract
      // size, leverage, and any quote/account currency conversion for us).
      //
      // BUG FIXED: this used to compute the cap as 80% of CURRENTLY FREE margin,
      // i.e. "how much more may I commit on top of whatever's already used".
      // That doesn't keep total usage under 80% of the account — it compounds
      // wrong the moment more than one position is open. Example with a real
      // 80% target: position 1 uses 50% of equity, leaving 50% free; capping
      // the NEXT trade to 80% of that 50%-free lets it add another 40%,
      // bringing total usage to 90% — already past the 80% ceiling, with free
      // margin at 10%, not the intended >=20%.
      //
      // Fixed version: cap TOTAL margin used (existing positions + this trade)
      // to MarginCapPct of equity, so free margin never drops below
      // (100 - MarginCapPct)% of equity, regardless of how many positions are
      // already open.
      double equity = AccountInfoDouble(ACCOUNT_EQUITY);
      double marginUsed = AccountInfoDouble(ACCOUNT_MARGIN);      // already committed, all positions
      double maxTotalMarginAllowed = equity * (MarginCapPct / 100.0);
      double marginBudget = maxTotalMarginAllowed - marginUsed;   // how much MORE this trade may add
      maxLotsByMargin = (marginBudget > 0) ? (marginBudget / marginPerLot) : 0;
      if(lots > maxLotsByMargin)
         lots = MathFloor(maxLotsByMargin / lotStep) * lotStep;
   }

   bool cappedByMargin = (maxLotsByMargin >= 0 && lots < riskBasedLots);

   // BUG FIXED: this used to run unconditionally, so if the margin cap above had
   // just reduced lots to (say) 0.006 and the broker's minimum is 0.01, this line
   // silently bumped it straight back up to 0.01 — blowing past the margin cap
   // that was just computed AND making the real risk taken bear no relation to
   // either the requested risk % or "however much margin allows". That's almost
   // certainly why the cut looked arbitrary instead of "capped near the max":
   // it wasn't margin's number at all, it was the unrelated broker minimum.
   // Now: only apply the min-lot floor when it WASN'T the margin cap that pushed
   // lots down (i.e. the pure risk-based calc itself rounded to under one lot
   // step — a separate, legitimate case). If margin is what did it, reject
   // instead of silently overriding the cap.
   if(cappedByMargin && lots < minLot)
   {
      SendEvent("ORDER_REJECTED " + symbol + " margin_cap_below_min_lot requested_lots=" +
                DoubleToString(riskBasedLots, 2) + " margin_allows_lots=" + DoubleToString(maxLotsByMargin, 2) +
                " broker_min_lot=" + DoubleToString(minLot, 2));
      return 0;
   }

   if(lots < minLot) lots = minLot;
   if(lots > maxLot) lots = maxLot;

   // Now that the min-lot bug above is fixed, a margin-capped fill really is
   // "as close to the requested size as the cap allows" — this alert just makes
   // that visible instead of leaving it to be inferred after the fact.
   if(cappedByMargin)
   {
      double actualRiskPct = riskPct * (lots / riskBasedLots);
      SendEvent("LOT_CAPPED " + symbol + " requested_lots=" + DoubleToString(riskBasedLots, 2) +
                " actual_lots=" + DoubleToString(lots, 2) +
                " requested_risk_pct=" + DoubleToString(riskPct, 2) +
                " actual_risk_pct=" + DoubleToString(actualRiskPct, 2) +
                " margin_allows_lots=" + DoubleToString(maxLotsByMargin, 2));
   }

   return lots;
}

void PlaceMarketOrder(string dir, string symbol, double sl, double riskPct, double rrr)
{
   if(tradingBlocked)
   {
      Print("Trading blocked — ignoring OPEN_MARKET for ", symbol);
      SendEvent("ORDER_REJECTED " + symbol + " trading_blocked");
      return;
   }
   if(sl <= 0)
   {
      Print("Rejected market order for ", symbol, " — SL is required.");
      SendEvent("ORDER_REJECTED " + symbol + " missing_sl");
      return;
   }
   if(!SymbolSelect(symbol, true))
   {
      Print("Symbol not available: ", symbol);
      SendEvent("ORDER_REJECTED " + symbol + " symbol_unavailable");
      return;
   }

   // Fetched right now, at the moment of execution — not a price the backend guessed earlier.
   double ask = SymbolInfoDouble(symbol, SYMBOL_ASK);
   double bid = SymbolInfoDouble(symbol, SYMBOL_BID);
   if(ask <= 0 || bid <= 0)
   {
      Print("No quotes for ", symbol, " — market likely closed.");
      SendEvent("ORDER_REJECTED " + symbol + " market_closed_or_no_quotes");
      return;
   }

   double entry = (dir == "BUY") ? ask : bid;
   double tp = (dir == "BUY") ? entry + rrr * MathAbs(entry - sl) : entry - rrr * MathAbs(entry - sl);

   double lots = CalculateLotSize(symbol, dir, entry, sl, riskPct);
   if(lots <= 0)
   {
      Print("Calculated lot size is 0 for ", symbol, " — check risk%/SL distance/margin.");
      SendEvent("ORDER_REJECTED " + symbol + " zero_lot_size");
      return;
   }

   MqlTradeRequest request = {};
   MqlTradeResult  result  = {};
   request.action       = TRADE_ACTION_DEAL;
   request.symbol       = symbol;
   request.volume       = lots;
   request.type         = (dir == "BUY") ? ORDER_TYPE_BUY : ORDER_TYPE_SELL;
   request.price        = entry;
   request.sl            = sl;
   request.tp            = tp;
   request.magic        = MagicNumber;
   request.deviation    = 20;
   request.type_filling = GetFillingMode(symbol);
   request.comment      = "R:" + DoubleToString(MathAbs(entry - sl), (int)SymbolInfoInteger(symbol, SYMBOL_DIGITS));

   if(!OrderSend(request, result))
   {
      Print("OrderSend failed for ", symbol, ": ", result.retcode, " ", result.comment);
      SendEvent("ORDER_REJECTED " + symbol + " send_failed_" + IntegerToString((int)result.retcode));
   }
   else
      Print("Market order placed: ", dir, " ", symbol, " lots=", lots, " entry=", entry, " tp=", tp);
}

void PlaceOrder(string dir, string symbol, double entry, double sl, double tp, double riskPct)
{
   if(tradingBlocked)
   {
      Print("Trading blocked — ignoring OPEN for ", symbol);
      SendEvent("ORDER_REJECTED " + symbol + " trading_blocked");
      return;
   }
   if(sl <= 0 || tp <= 0)
   {
      Print("Rejected order for ", symbol, " — SL and TP are required.");
      SendEvent("ORDER_REJECTED " + symbol + " missing_sl_or_tp");
      return;
   }
   if(!SymbolSelect(symbol, true))
   {
      Print("Symbol not available: ", symbol);
      SendEvent("ORDER_REJECTED " + symbol + " symbol_unavailable");
      return;
   }

   double ask = SymbolInfoDouble(symbol, SYMBOL_ASK);
   double bid = SymbolInfoDouble(symbol, SYMBOL_BID);
   if(ask <= 0 || bid <= 0)
   {
      Print("No quotes for ", symbol, " — market likely closed.");
      SendEvent("ORDER_REJECTED " + symbol + " market_closed_or_no_quotes");
      return;
   }

   double lots = CalculateLotSize(symbol, dir, entry, sl, riskPct);
   if(lots <= 0)
   {
      Print("Calculated lot size is 0 for ", symbol, " — check risk%/SL distance/margin.");
      SendEvent("ORDER_REJECTED " + symbol + " zero_lot_size");
      return;
   }

   double point = SymbolInfoDouble(symbol, SYMBOL_POINT);
   double tolerance = PriceTolerancePoints * point;
   double slDistance = MathAbs(entry - sl);

   MqlTradeRequest request = {};
   MqlTradeResult  result  = {};
   request.symbol       = symbol;
   request.volume       = lots;
   request.sl           = sl;
   request.tp           = tp;
   request.magic        = MagicNumber;
   request.deviation    = 20;
   request.comment      = "R:" + DoubleToString(slDistance, (int)SymbolInfoInteger(symbol, SYMBOL_DIGITS));
   // type_filling is set per-branch below, NOT here — FOK/IOC (from GetFillingMode)
   // are for immediate execution only. Most brokers reject TRADE_ACTION_PENDING
   // requests carrying FOK/IOC with "Unsupported filling mode", so pending orders
   // (stop and limit alike) must always use ORDER_FILLING_RETURN regardless of
   // what the symbol supports for market fills.

   if(dir == "BUY")
   {
      if(MathAbs(entry - ask) <= tolerance)
      {
         request.action       = TRADE_ACTION_DEAL;
         request.type         = ORDER_TYPE_BUY;
         request.price        = ask;
         request.type_filling = GetFillingMode(symbol);
      }
      else if(entry > ask)
      {
         request.action       = TRADE_ACTION_PENDING;
         request.type         = ORDER_TYPE_BUY_STOP;
         request.price        = entry;
         request.type_filling = ORDER_FILLING_RETURN;
      }
      else
      {
         request.action       = TRADE_ACTION_PENDING;
         request.type         = ORDER_TYPE_BUY_LIMIT;
         request.price        = entry;
         request.type_filling = ORDER_FILLING_RETURN;
      }
   }
   else // SELL
   {
      if(MathAbs(entry - bid) <= tolerance)
      {
         request.action       = TRADE_ACTION_DEAL;
         request.type         = ORDER_TYPE_SELL;
         request.price        = bid;
         request.type_filling = GetFillingMode(symbol);
      }
      else if(entry < bid)
      {
         request.action       = TRADE_ACTION_PENDING;
         request.type         = ORDER_TYPE_SELL_STOP;
         request.price        = entry;
         request.type_filling = ORDER_FILLING_RETURN;
      }
      else
      {
         request.action       = TRADE_ACTION_PENDING;
         request.type         = ORDER_TYPE_SELL_LIMIT;
         request.price        = entry;
         request.type_filling = ORDER_FILLING_RETURN;
      }
   }

   if(!OrderSend(request, result))
   {
      Print("OrderSend failed for ", symbol, ": ", result.retcode, " ", result.comment);
      SendEvent("ORDER_REJECTED " + symbol + " send_failed_" + IntegerToString((int)result.retcode));
   }
   else
   {
      Print("Order placed: ", dir, " ", symbol, " lots=", lots, " type=", EnumToString(request.type));
      // Pending orders do not generate a deal, so there is no POSITION_OPENED
      // transaction to alert on. Tell the backend the moment MT5 accepts it.
      if(request.action == TRADE_ACTION_PENDING)
      {
         int placedDigits = (int)SymbolInfoInteger(symbol, SYMBOL_DIGITS);
         if(placedDigits <= 0) placedDigits = 5;
         SendEvent("PENDING_PLACED type=" + OrderTypeName(request.type) +
                   " symbol=" + symbol +
                   " ticket=" + IntegerToString((long)result.order) +
                   " entry=" + DoubleToString(entry, placedDigits) +
                   " sl=" + DoubleToString(sl, placedDigits) +
                   " tp=" + DoubleToString(tp, placedDigits) +
                   " lots=" + DoubleToString(lots, 2) +
                   " risk=" + DoubleToString(riskPct, 2));
      }
   }
}

//+------------------------------------------------------------------+
//| Manual pending-order adoption                                    |
//|                                                                  |
//| A pending order placed by hand (phone/tablet/desktop) arrives    |
//| with a magic outside this EA's range. We keep the symbol, side,  |
//| entry and SL the user chose, delete the original order, and      |
//| re-place the same order type with the account's fixed risk %,    |
//| correct lot size, and TP at the configured RRR.                  |
//+------------------------------------------------------------------+
bool PlaceAdoptedPending(ENUM_ORDER_TYPE orderType, string symbol, double entry,
                         double sl, double tp, double lots)
{
   if(!SymbolSelect(symbol, true))
   {
      Print("Adopted order symbol not available: ", symbol);
      return false;
   }

   int digits = (int)SymbolInfoInteger(symbol, SYMBOL_DIGITS);
   if(digits <= 0) digits = 5;

   MqlTradeRequest request = {};
   MqlTradeResult  result  = {};
   request.action       = TRADE_ACTION_PENDING;
   request.symbol       = symbol;
   request.volume       = lots;
   request.type         = orderType;
   request.price        = entry;
   request.sl           = sl;
   request.tp           = tp;
   request.magic        = MagicNumber;
   request.type_filling = ORDER_FILLING_RETURN;
   request.comment      = "R:" + DoubleToString(MathAbs(entry - sl), digits);

   if(!OrderSend(request, result))
   {
      Print("Adopted order send failed for ", symbol, ": ", result.retcode, " ", result.comment);
      return false;
   }
   if(result.retcode != TRADE_RETCODE_DONE && result.retcode != TRADE_RETCODE_PLACED)
   {
      Print("Adopted order rejected for ", symbol, ": ", result.retcode, " ", result.comment);
      return false;
   }
   return true;
}

bool AdoptManualPendingOrder(ulong ticket)
{
   if(!OrderSelect(ticket)) return false;

   long rawType = (long)OrderGetInteger(ORDER_TYPE);
   if(!IsPendingOrderType(rawType)) return false;

   long magic = (long)OrderGetInteger(ORDER_MAGIC);
   if(magic == MagicNumber || IsExternalBotMagic(magic)) return false;

   // MT5 raises ORDER_ADD before the server has finished registering the
   // pending order. A delete in that window returns 10013 (Invalid request)
   // and used to post a false "remove_failed" rejection, then adopt 10s later.
   // Let the order settle and process it on the next scan (OnTick runs ~1s).
   datetime setupTime = (datetime)OrderGetInteger(ORDER_TIME_SETUP);
   datetime serverNow = TimeCurrent();
   if(setupTime > 0 && serverNow >= setupTime && serverNow - setupTime < 1)
      return false;

   // At EA start the backend hasn't sent RISK/SYMBOLS yet. Leave existing
   // pendings alone for a short grace period instead of deleting them before
   // the first /commands poll arrives.
   if(g_riskPct <= 0 && GetTickCount() - g_startedAt < 30000) return false;

   // The same ticket can be seen by both OnTradeTransaction and the periodic
   // scan. Give a failed attempt time to retry, but never process it twice.
   if(RecentlyFired("ADOPT:" + IntegerToString((long)ticket), 1000)) return false;

   string symbol = OrderGetString(ORDER_SYMBOL);
   double entry  = OrderGetDouble(ORDER_PRICE_OPEN);
   double sl     = OrderGetDouble(ORDER_SL);
   int digits    = (int)SymbolInfoInteger(symbol, SYMBOL_DIGITS);
   if(digits <= 0) digits = 5;

   bool isBuy = (rawType == ORDER_TYPE_BUY_LIMIT || rawType == ORDER_TYPE_BUY_STOP);
   string reason = "";

   if(!IsSupportedPendingOrderType(rawType))
      reason = "unsupported_order_type";
   else if(g_riskPct <= 0)
      reason = "risk_not_configured";
   else if(!IsSymbolEnabled(symbol))
      reason = "symbol_not_enabled";
   else if(tradingBlocked)
      reason = "trading_blocked";
   else if(g_maxPositions > 0 && OpenBotPositionCount() >= g_maxPositions)
      reason = "position_limit";
   else if(sl <= 0)
      reason = "missing_sl";
   else if(isBuy && sl >= entry)
      reason = "invalid_sl";
   else if(!isBuy && sl <= entry)
      reason = "invalid_sl";

   if(reason != "")
   {
      bool removed = DeletePendingOrder(ticket);
      SendEvent("MANUAL_ORDER_REJECTED " + OrderTypeName((ENUM_ORDER_TYPE)rawType) +
                " " + symbol + " " + reason +
                (removed ? " removed" : " remove_failed"));
      Print("Manual pending order rejected: ", symbol, " ", reason,
            removed ? " (removed)" : " (remove failed)");
      return removed;
   }

   double distance = MathAbs(entry - sl);
   double tp = isBuy ? entry + g_rrr * distance : entry - g_rrr * distance;
   double riskPerLot = 0.0;
   double lots = CalculateLotSize(symbol, isBuy ? "BUY" : "SELL", entry, sl, g_riskPct, riskPerLot);
   if(lots <= 0)
   {
      bool removed = DeletePendingOrder(ticket);
      SendEvent("MANUAL_ORDER_REJECTED " + OrderTypeName((ENUM_ORDER_TYPE)rawType) +
                " " + symbol + " zero_lot_size" +
                (removed ? " removed" : " remove_failed"));
      Print("Manual pending order rejected: ", symbol, " zero_lot_size",
            removed ? " (removed)" : " (remove failed)");
      return removed;
   }

   string typeName = OrderTypeName((ENUM_ORDER_TYPE)rawType);

   // Delete first, then replace. A failed replacement means no trade rather
   // than two overlapping orders (or an unsized manual order).
   if(!DeletePendingOrder(ticket))
   {
      // 10013 right after placement is transient; the periodic scan retries.
      // Only alert when the order has been sitting un-adoptable for 10s.
      datetime failSetup = (datetime)OrderGetInteger(ORDER_TIME_SETUP);
      datetime failNow = TimeCurrent();
      if(failSetup > 0 && failNow >= failSetup && failNow - failSetup >= 10 &&
         !RecentlyFired("ADOPT_FAIL:" + IntegerToString((long)ticket), 60000))
         SendEvent("MANUAL_ORDER_REJECTED " + typeName + " " + symbol + " remove_failed");
      else
         Print("Manual pending adoption deferred for ", symbol, " ticket ", ticket,
               ": delete not ready, will retry.");
      return false;
   }

   if(!PlaceAdoptedPending((ENUM_ORDER_TYPE)rawType, symbol, entry, sl, tp, lots))
   {
      SendEvent("MANUAL_ORDER_REJECTED " + typeName + " " + symbol + " replace_failed");
      return false;
   }

   double balance = AccountInfoDouble(ACCOUNT_BALANCE);
   double actualRiskPct = 0.0;
   if(balance > 0 && riskPerLot > 0)
      actualRiskPct = lots * riskPerLot / balance * 100.0;

   SendEvent("MANUAL_ORDER_ADOPTED " + typeName + " " + symbol +
             " entry=" + DoubleToString(entry, digits) +
             " sl=" + DoubleToString(sl, digits) +
             " tp=" + DoubleToString(tp, digits) +
             " lots=" + DoubleToString(lots, 2) +
             " risk=" + DoubleToString(g_riskPct, 2) + "%" +
             " actual_risk=" + DoubleToString(actualRiskPct, 2) + "%");
   Print("Manual pending order adopted: ", typeName, " ", symbol,
         " lots=", lots, " entry=", entry, " sl=", sl, " tp=", tp);
   return true;
}

void AdoptManualPendingOrders()
{
   ulong tickets[];
   int count = 0;

   for(int i = OrdersTotal() - 1; i >= 0; i--)
   {
      ulong ticket = OrderGetTicket(i);
      if(ticket == 0) continue;

      long magic = (long)OrderGetInteger(ORDER_MAGIC);
      if(magic == MagicNumber || IsExternalBotMagic(magic)) continue;

      long orderType = (long)OrderGetInteger(ORDER_TYPE);
      if(!IsPendingOrderType(orderType)) continue;

      ArrayResize(tickets, count + 1);
      tickets[count] = ticket;
      count++;
   }

   for(int i = 0; i < count; i++)
      AdoptManualPendingOrder(tickets[i]);
}
