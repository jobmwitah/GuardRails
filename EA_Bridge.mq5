//+------------------------------------------------------------------+
//| EA_Bridge.mq5                                                     |
//| Reports account/position state to your backend and executes      |
//| commands it queues (close, block, open trade).                   |
//|                                                                    |
//| Enforces: SL/TP required on every bot trade, risk-% based lot     |
//| sizing (risk % is sent by backend per-account, capped at 80% of   |
//| free margin), automatic market/limit/stop order type selection,   |
//| correct filling mode read from the symbol, auto-close/delete of   |
//| any manually placed position/order, automatic breakeven at 2R,    |
//| and event alerts (pending triggered, SL/TP hit, breakeven, trade  |
//| closed) pushed to the backend immediately.                        |
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
input int    PollSeconds          = 5;
input long   MagicNumber          = 990011;
input double MarginCapPct         = 80.0;   // max % of free margin one trade may use
input double PriceTolerancePoints = 10;     // within this many points of market = market order
input double BreakevenRMultiple   = 2.0;    // move SL to entry once profit reaches this many R
input string SymbolSuffix         = "0";    // broker suffix for this account — MUST match what you set in /addaccount (e.g. "b", or "0" for none)
input int    FastPollMs           = 1000;   // minimum gap between tick-driven command checks (faster than PollSeconds)
input int    CloseAggregationMs   = 1500;   // wait this long after the last partial-close deal before sending one alert

string g_eurusdSymbol = "";
string g_gbpusdSymbol = "";

ulong lastFastPollMs = 0;

// --- Position-level dedup for open/trigger/breakeven alerts (partial fills can
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

// --- Close-deal aggregation, keyed by position ID (not deal ticket) — combines
//     partial-fill closing deals into a single alert with the correct total P/L ---
ulong  pendingClosePosId[];
double pendingCloseProfit[];
double pendingCloseVolume[];
string pendingCloseSymbol[];
string pendingCloseReason[];
ulong  pendingCloseLastUpdate[];
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
   pendingClosePosId[idx] = posId;
   pendingCloseProfit[idx] = 0;
   pendingCloseVolume[idx] = 0;
   pendingCloseSymbol[idx] = "";
   pendingCloseReason[idx] = "";
   pendingCloseCount++;
   return idx;
}

void BufferCloseDeal(ulong posId, string symbol, string reasonStr, double volume, double profit)
{
   int idx = FindOrCreatePendingClose(posId);
   pendingCloseSymbol[idx] = symbol;
   pendingCloseProfit[idx] += profit;
   pendingCloseVolume[idx] += volume;
   if(reasonStr == "SL" || reasonStr == "TP")
      pendingCloseReason[idx] = reasonStr;   // a specific reason always wins over a generic one
   else if(pendingCloseReason[idx] == "")
      pendingCloseReason[idx] = reasonStr;
   pendingCloseLastUpdate[idx] = GetTickCount();
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

      SendEvent("TRADE_CLOSED " + pendingCloseSymbol[i] + " " + IntegerToString((int)pendingClosePosId[i]) + " " +
                pendingCloseReason[i] + " " + DoubleToString(pendingCloseVolume[i], 2) + " " +
                DoubleToString(pendingCloseProfit[i], 2));

      int last = pendingCloseCount - 1;
      pendingClosePosId[i] = pendingClosePosId[last];
      pendingCloseProfit[i] = pendingCloseProfit[last];
      pendingCloseVolume[i] = pendingCloseVolume[last];
      pendingCloseSymbol[i] = pendingCloseSymbol[last];
      pendingCloseReason[i] = pendingCloseReason[last];
      pendingCloseLastUpdate[i] = pendingCloseLastUpdate[last];
      pendingCloseCount--;
      ArrayResize(pendingClosePosId, pendingCloseCount);
      ArrayResize(pendingCloseProfit, pendingCloseCount);
      ArrayResize(pendingCloseVolume, pendingCloseCount);
      ArrayResize(pendingCloseSymbol, pendingCloseCount);
      ArrayResize(pendingCloseReason, pendingCloseCount);
      ArrayResize(pendingCloseLastUpdate, pendingCloseCount);
   }
}

bool tradingBlocked = false;

int OnInit()
{
   EventSetTimer(PollSeconds);

   string sfx = (SymbolSuffix == "0" || SymbolSuffix == "") ? "" : SymbolSuffix;
   g_eurusdSymbol = "EURUSD" + sfx;
   g_gbpusdSymbol = "GBPUSD" + sfx;
   SymbolSelect(g_eurusdSymbol, true);
   SymbolSelect(g_gbpusdSymbol, true);

   Print("EA_Bridge started. Backend: ", BackendURL, " Magic: ", MagicNumber,
         " Reporting: ", g_eurusdSymbol, " / ", g_gbpusdSymbol);
   return(INIT_SUCCEEDED);
}

void OnDeinit(const int reason)
{
   EventKillTimer();
}

void OnTimer()
{
   ReportState();
   PollCommands();
}

void OnTick()
{
   CheckBreakeven();
   FlushPendingCloses();

   ulong nowMs = GetTickCount();
   if(nowMs - lastFastPollMs >= (ulong)FastPollMs)
   {
      lastFastPollMs = nowMs;
      PollCommands();   // fast path — reacts within FastPollMs during active market hours
   }
}

//+------------------------------------------------------------------+
//| Move SL to entry once profit reaches BreakevenRMultiple * R       |
//+------------------------------------------------------------------+
void CheckBreakeven()
{
   for(int i = 0; i < PositionsTotal(); i++)
   {
      ulong ticket = PositionGetTicket(i);
      if(ticket <= 0) continue;
      if((long)PositionGetInteger(POSITION_MAGIC) != MagicNumber) continue;

      double riskDist = ExtractRiskFromComment(PositionGetString(POSITION_COMMENT));
      if(riskDist <= 0) continue;

      string symbol = PositionGetString(POSITION_SYMBOL);
      double entry  = PositionGetDouble(POSITION_PRICE_OPEN);
      double sl     = PositionGetDouble(POSITION_SL);
      long   type   = PositionGetInteger(POSITION_TYPE);
      double currentPrice = (type == POSITION_TYPE_BUY) ?
                             SymbolInfoDouble(symbol, SYMBOL_BID) :
                             SymbolInfoDouble(symbol, SYMBOL_ASK);

      double profitDist = (type == POSITION_TYPE_BUY) ? (currentPrice - entry) : (entry - currentPrice);
      bool alreadyBE = (type == POSITION_TYPE_BUY) ? (sl >= entry - _Point) : (sl <= entry + _Point);

      if(profitDist >= BreakevenRMultiple * riskDist && !alreadyBE)
      {
         MqlTradeRequest req = {};
         MqlTradeResult  res = {};
         req.action   = TRADE_ACTION_SLTP;
         req.position = ticket;
         req.symbol   = symbol;
         req.sl       = entry;
         req.tp       = PositionGetDouble(POSITION_TP);
         if(OrderSend(req, res))
         {
            Print("Breakeven triggered for ticket ", ticket);
            SendEvent("BREAKEVEN " + symbol + " " + IntegerToString((int)ticket));
         }
      }
   }
}

double ExtractRiskFromComment(string comment)
{
   int pos = StringFind(comment, "R:");
   if(pos < 0) return 0;
   return StringToDouble(StringSubstr(comment, pos + 2));
}

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

      if(dealMagic == MagicNumber)
      {
         ulong posId = HistoryDealGetInteger(dealTicket, DEAL_POSITION_ID);

         if(entryType == DEAL_ENTRY_IN)
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
                  if(!RecentlyFired("PENDING_TRIG:" + IntegerToString((int)posId), 3000))
                     SendEvent("PENDING_TRIGGERED " + symbol + " " + IntegerToString((int)posId));
               }
            }
            if(!wasPending)
            {
               if(!RecentlyFired("OPEN:" + IntegerToString((int)posId), 3000))
               {
                  long dealType = HistoryDealGetInteger(dealTicket, DEAL_TYPE);
                  string dir = (dealType == DEAL_TYPE_BUY) ? "BUY" : "SELL";
                  double price = HistoryDealGetDouble(dealTicket, DEAL_PRICE);
                  double volume = HistoryDealGetDouble(dealTicket, DEAL_VOLUME);
                  SendEvent("POSITION_OPENED " + symbol + " " + IntegerToString((int)posId) + " " +
                            dir + " " + DoubleToString(volume, 2) + " " + DoubleToString(price, 5));
               }
            }
         }
         else if(entryType == DEAL_ENTRY_OUT || entryType == DEAL_ENTRY_OUT_BY)
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
            BufferCloseDeal(posId, symbol, reasonStr, volume, profit);
         }
      }
      else if(entryType == DEAL_ENTRY_IN)
      {
         // Not ours — this is a manual trade. Close it immediately.
         ulong posTicket = HistoryDealGetInteger(dealTicket, DEAL_POSITION_ID);
         Print("Manual position detected (ticket ", posTicket, ") — auto-closing.");
         ClosePositionByTicket(posTicket);
      }
   }
   else if(trans.type == TRADE_TRANSACTION_ORDER_ADD)
   {
      ulong orderTicket = trans.order;
      if(OrderSelect(orderTicket))
      {
         long orderMagic = OrderGetInteger(ORDER_MAGIC);
         if(orderMagic != MagicNumber)
         {
            MqlTradeRequest req = {};
            MqlTradeResult  res = {};
            req.action = TRADE_ACTION_REMOVE;
            req.order  = orderTicket;
            if(OrderSend(req, res))
               Print("Manual pending order detected (ticket ", orderTicket, ") — removed.");
         }
      }
   }
}

//+------------------------------------------------------------------+
//| Send account + position snapshot to backend                      |
//+------------------------------------------------------------------+
void ReportState()
{
   string url = BackendURL + "/report";
   string body = "token=" + AccountToken;
   body += "&account=" + IntegerToString((int)AccountInfoInteger(ACCOUNT_LOGIN));
   body += "&balance=" + DoubleToString(AccountInfoDouble(ACCOUNT_BALANCE), 2);
   body += "&equity="  + DoubleToString(AccountInfoDouble(ACCOUNT_EQUITY), 2);
   body += "&margin="  + DoubleToString(AccountInfoDouble(ACCOUNT_MARGIN), 2);
   body += "&freemargin=" + DoubleToString(AccountInfoDouble(ACCOUNT_MARGIN_FREE), 2);
   body += "&blocked=" + (tradingBlocked ? "1" : "0");

   body += "&eurusd_ask=" + DoubleToString(SymbolInfoDouble(g_eurusdSymbol, SYMBOL_ASK), 5);
   body += "&eurusd_bid=" + DoubleToString(SymbolInfoDouble(g_eurusdSymbol, SYMBOL_BID), 5);
   body += "&gbpusd_ask=" + DoubleToString(SymbolInfoDouble(g_gbpusdSymbol, SYMBOL_ASK), 5);
   body += "&gbpusd_bid=" + DoubleToString(SymbolInfoDouble(g_gbpusdSymbol, SYMBOL_BID), 5);

   string positions = "";
   int total = PositionsTotal();
   for(int i = 0; i < total; i++)
   {
      ulong ticket = PositionGetTicket(i);
      if(ticket <= 0) continue;
      string type = (PositionGetInteger(POSITION_TYPE) == POSITION_TYPE_BUY) ? "BUY" : "SELL";
      if(positions != "") positions += "|";
      positions += PositionGetString(POSITION_SYMBOL) + "," +
                   IntegerToString((int)ticket) + "," +
                   type + "," +
                   DoubleToString(PositionGetDouble(POSITION_VOLUME), 2) + "," +
                   DoubleToString(PositionGetDouble(POSITION_PROFIT), 2) + "," +
                   DoubleToString(PositionGetDouble(POSITION_SL), 5) + "," +
                   DoubleToString(PositionGetDouble(POSITION_TP), 5);
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
                  IntegerToString((int)ticket) + "," +
                  typeStr + "," +
                  DoubleToString(OrderGetDouble(ORDER_PRICE_OPEN), 5) + "," +
                  DoubleToString(OrderGetDouble(ORDER_SL), 5) + "," +
                  DoubleToString(OrderGetDouble(ORDER_TP), 5);
   }
   body += "&pending=" + pendings;

   char post[]; char result[]; string resultHeaders;
   StringToCharArray(body, post, 0, StringLen(body));
   ResetLastError();
   int res = WebRequest("POST", url, "Content-Type: application/x-www-form-urlencoded\r\n",
                         5000, post, result, resultHeaders);
   if(res == -1)
      Print("ReportState failed, error ", GetLastError(), " — check Allow WebRequest URL list");
}

//+------------------------------------------------------------------+
//| Fire-and-forget event notification to backend                    |
//+------------------------------------------------------------------+
void SendEvent(string message)
{
   string url = BackendURL + "/event";
   string body = "token=" + AccountToken +
                 "&account=" + IntegerToString((int)AccountInfoInteger(ACCOUNT_LOGIN)) +
                 "&message=" + UrlEncodeSpaces(message);
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
   string url = BackendURL + "/commands?token=" + AccountToken +
                "&account=" + IntegerToString((int)AccountInfoInteger(ACCOUNT_LOGIN));
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
   else if(cmd == "OPEN_MARKET" && n >= 6)
   {
      // OPEN_MARKET BUY|SELL SYMBOL SL RISK_PCT RRR
      string dir    = parts[1];
      string symbol = parts[2];
      double sl     = StringToDouble(parts[3]);
      double risk   = StringToDouble(parts[4]);
      double rrr    = StringToDouble(parts[5]);
      PlaceMarketOrder(dir, symbol, sl, risk, rrr);
   }
   else if(cmd == "OPEN" && n >= 7)
   {
      // OPEN BUY|SELL SYMBOL ENTRY SL TP RISK_PCT
      string dir    = parts[1];
      string symbol = parts[2];
      double entry  = StringToDouble(parts[3]);
      double sl     = StringToDouble(parts[4]);
      double tp     = StringToDouble(parts[5]);
      double risk   = StringToDouble(parts[6]);
      PlaceOrder(dir, symbol, entry, sl, tp, risk);
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

void DeletePendingOrder(ulong ticket)
{
   if(!OrderSelect(ticket)) { Print("Pending order not found: ", ticket); return; }
   MqlTradeRequest req = {};
   MqlTradeResult  res = {};
   req.action = TRADE_ACTION_REMOVE;
   req.order  = ticket;
   if(!OrderSend(req, res))
      Print("Delete pending failed for ticket ", ticket, ": ", res.retcode, " ", res.comment);
   else
      Print("Pending order deleted: ", ticket);
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
   double balance = AccountInfoDouble(ACCOUNT_BALANCE);
   double riskAmount = balance * riskPct / 100.0;

   double point = SymbolInfoDouble(symbol, SYMBOL_POINT);
   double tickValue = SymbolInfoDouble(symbol, SYMBOL_TRADE_TICK_VALUE);
   double tickSize  = SymbolInfoDouble(symbol, SYMBOL_TRADE_TICK_SIZE);
   if(tickSize <= 0) tickSize = point;

   double slDistance = MathAbs(entry - sl);
   if(slDistance <= 0) return 0;

   double valuePerPoint = (tickValue / tickSize) * point;
   double slPoints = slDistance / point;
   if(slPoints <= 0 || valuePerPoint <= 0) return 0;

   double lots = riskAmount / (slPoints * valuePerPoint);

   double lotStep = SymbolInfoDouble(symbol, SYMBOL_VOLUME_STEP);
   double minLot   = SymbolInfoDouble(symbol, SYMBOL_VOLUME_MIN);
   double maxLot   = SymbolInfoDouble(symbol, SYMBOL_VOLUME_MAX);
   if(lotStep <= 0) lotStep = 0.01;
   lots = MathFloor(lots / lotStep) * lotStep;

   ENUM_ORDER_TYPE orderType = (dir == "BUY") ? ORDER_TYPE_BUY : ORDER_TYPE_SELL;
   double marginPerLot = 0;
   if(OrderCalcMargin(orderType, symbol, 1.0, entry, marginPerLot) && marginPerLot > 0)
   {
      double freeMargin = AccountInfoDouble(ACCOUNT_MARGIN_FREE);
      double maxMarginAllowed = freeMargin * (MarginCapPct / 100.0);
      double maxLotsByMargin = maxMarginAllowed / marginPerLot;
      if(lots > maxLotsByMargin)
         lots = MathFloor(maxLotsByMargin / lotStep) * lotStep;
   }

   if(lots < minLot) lots = minLot;
   if(lots > maxLot) lots = maxLot;
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
   request.comment      = "R:" + DoubleToString(MathAbs(entry - sl), _Digits);

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
   request.type_filling = GetFillingMode(symbol);
   request.comment      = "R:" + DoubleToString(slDistance, _Digits);

   if(dir == "BUY")
   {
      if(MathAbs(entry - ask) <= tolerance)
      {
         request.action = TRADE_ACTION_DEAL;
         request.type   = ORDER_TYPE_BUY;
         request.price  = ask;
      }
      else if(entry > ask)
      {
         request.action = TRADE_ACTION_PENDING;
         request.type   = ORDER_TYPE_BUY_STOP;
         request.price  = entry;
      }
      else
      {
         request.action = TRADE_ACTION_PENDING;
         request.type   = ORDER_TYPE_BUY_LIMIT;
         request.price  = entry;
      }
   }
   else // SELL
   {
      if(MathAbs(entry - bid) <= tolerance)
      {
         request.action = TRADE_ACTION_DEAL;
         request.type   = ORDER_TYPE_SELL;
         request.price  = bid;
      }
      else if(entry < bid)
      {
         request.action = TRADE_ACTION_PENDING;
         request.type   = ORDER_TYPE_SELL_STOP;
         request.price  = entry;
      }
      else
      {
         request.action = TRADE_ACTION_PENDING;
         request.type   = ORDER_TYPE_SELL_LIMIT;
         request.price  = entry;
      }
   }

   if(!OrderSend(request, result))
   {
      Print("OrderSend failed for ", symbol, ": ", result.retcode, " ", result.comment);
      SendEvent("ORDER_REJECTED " + symbol + " send_failed_" + IntegerToString((int)result.retcode));
   }
   else
      Print("Order placed: ", dir, " ", symbol, " lots=", lots, " type=", EnumToString(request.type));
}
