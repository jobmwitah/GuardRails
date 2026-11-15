//+------------------------------------------------------------------+
//|                                                      MotherEA.mq5 |
//|  MQL5 port of the cTrader MetaEq portfolio cBot.                  |
//|  One EA, one CTrade per strategy, a magic-number bank per         |
//|  strategy, and a global risk layer:                               |
//|    S1  = Intraday Volatility Breakout                             |
//|    S2A = Multi-Symbol Crypto Edge (pair set 1)                    |
//|    S2B = Multi-Symbol Crypto Edge (pair set 2)                    |
//|    S3  = Multi-Symbol Trend Pullback A (long only)                |
//|    S3B = Multi-Symbol Trend Pullback B (long only)                |
//|    S4  = Opening Range Breakout (ORB)                             |
//|    S5  = Breakout A (Asian range, long only)                      |
//|    S6  = Breakout B (gold opening range)                          |
//|    S7  = Gotobi (Tokyo fix)                                       |
//|    S8  = NAS100 Monday                                            |
//|    S9  = Index Fade                                               |
//|  All trades use the comment set in GlobalComment.                 |
//+------------------------------------------------------------------+
#property copyright "MotherEA"
#property version   "2.00"
#property strict

#include <Trade\Trade.mqh>

//+------------------------------------------------------------------+
//| Enumerations                                                      |
//+------------------------------------------------------------------+
enum ENUM_REGIME_MODE
  {
   REGIME_LOW_VOLATILITY  = 0,  // Low Volatility Regime
   REGIME_HIGH_VOLATILITY = 1   // High Volatility Regime
  };

enum ENUM_TRADE_DIRECTION
  {
   TRADE_BOTH       = 0,  // Both Directions
   TRADE_LONG_ONLY  = 1,  // Long Only
   TRADE_SHORT_ONLY = 2   // Short Only
  };

//+------------------------------------------------------------------+
//| GLOBAL INPUTS                                                     |
//+------------------------------------------------------------------+
//+------------------------------------------------------------------+
//| MetaEq extension enumerations                                     |
//+------------------------------------------------------------------+
enum ENUM_GOTOBI_LEGS
  {
   GOTOBI_LONG_ONLY  = 0,  // Long only
   GOTOBI_SHORT_ONLY = 1,  // Short only
   GOTOBI_BOTH        = 2  // Long and short
  };

enum ENUM_GOTOBI_CALENDAR
  {
   GOTOBI_CAL_5_30       = 0,  // 5,10,15,20,25,30
   GOTOBI_CAL_5_25_EOM   = 1,  // 5,10,15,20,25,EOM
   GOTOBI_CAL_30_EOM     = 2   // 30,EOM
  };

enum ENUM_GOTOBI_CONTROL
  {
   GOTOBI_EVENT_DAYS     = 0,  // Event days
   GOTOBI_NON_EVENT_DAYS = 1,  // Non-event days
   GOTOBI_ALL_BANK_DAYS  = 2   // All bank days
  };

enum ENUM_GOTOBI_RISK
  {
   GOTOBI_FIXED_LOTS   = 0,  // Fixed lots
   GOTOBI_PERCENT_RISK = 1,  // Percent risk
   GOTOBI_FIXED_USD    = 2   // Fixed USD risk
  };

enum ENUM_BOA_CLOCK
  {
   BOA_CLOCK_UTC    = 0,  // UTC
   BOA_CLOCK_BROKER = 1,  // Broker clock
   BOA_CLOCK_CET    = 2   // CET/CEST
  };


input group "=== GLOBAL ==="
input int    GlobalBaseMagic        = 100000;     // Base magic number (S1 = +1000.., S2A = +2000.., S2B = +4000.., S3 = +3000..)
input string GlobalComment          = "MotherEA"; // Comment written on every trade
input bool   GlobalPreventOverlap   = true;       // Block the strategies from both going long on the same symbol
input bool   GlobalUseFixedBalance  = false;      // Size S2A/S2B risk on a fixed balance (non-compounding, for testing)
input double GlobalFixedBalance     = 100000.0;   // Fixed balance used when the option above is on
input int    GlobalRandomSeed       = 0;          // RNG seed for the randomizers (0 = time-based, fixed value reproduces tests)

input group "=== GLOBAL RISK ==="
input double GlobalMaxDailyDDPercent = 0.0;       // Max daily drawdown % of day-start equity (0 = off)
input double GlobalMaxTotalDDPercent = 0.0;       // Max total drawdown % from peak equity (0 = off)
input int    GlobalMaxOpenPositions  = 0;         // Max open positions across all strategies (0 = no limit)

//+------------------------------------------------------------------+
//| S1 INPUTS: Intraday Volatility Breakout                           |
//+------------------------------------------------------------------+
input group "=== S1: Intraday Volatility Breakout ==="
input bool                 S1_Enabled              = true;            // S1: enabled
input string               S1_Symbols              = "CURRENT";       // S1: symbols (pipe separated, CURRENT = chart symbol)
input string               S1_Timeframes           = "M15";           // S1: confirmation timeframe per symbol (pipe separated)
input int                  S1_ATR_Period           = 5;               // S1: ATR period (Daily)
input double               S1_ATR_Multiplier       = 0.25;            // S1: ATR multiplier for breakout levels
input double               S1_Stop_ATR_Multiplier  = 0.25;            // S1: ATR multiplier for stop loss
input bool                 S1_Use_Candle_Close     = true;            // S1: require candle close confirmation
input int                  S1_ATR_Short_Period     = 1;               // S1: short-term ATR period (Daily)
input int                  S1_ATR_Long_Period      = 5;               // S1: long-term ATR period (Daily)
input double               S1_ATR_Regime_Factor    = 1.0;             // S1: regime factor
input ENUM_REGIME_MODE     S1_Regime_Filter_Mode   = REGIME_LOW_VOLATILITY; // S1: regime filter mode
input ENUM_TRADE_DIRECTION S1_Trade_Direction      = TRADE_BOTH;      // S1: allowed trade direction
input bool                 S1_Reverse_Mode         = false;           // S1: reverse mode (fade the breakout)
input double               S1_Fixed_Risk_Amount    = 500.0;           // S1: fixed risk amount (account currency)
input string               S1_Entry_Time           = "16:30";         // S1: entry window start (HH:MM, server time)
input string               S1_Max_Entry_Time       = "18:05";         // S1: latest entry time (HH:MM, server time)
input string               S1_Exit_Time            = "22:55";         // S1: close all positions time (HH:MM, server time)
input bool                 S1_EnableRandomization  = false;           // S1: randomizer master switch (OFF = exact original behavior)
input double               S1_RandomDelayMaxSec    = 0.0;             // S1: max random entry delay, seconds (0 = immediately)
input double               S1_RandomVolumeJitterPct = 0.0;            // S1: volume jitter +/- percent (0 = off)
input double               S1_RandomSlJitterPoints = 0.0;             // S1: SL distance jitter +/- points (0 = off)

//+------------------------------------------------------------------+

//=== S2A INPUTS: Multi-Symbol Crypto Edge ================================
input group "=== S2A: Multi-Symbol Crypto Edge ==="
input bool   S2A_Enabled                = true;                  // S2A: enabled
input string S2A_Symbols                = "BTCUSD|XAUUSD|USDJPY";    // S2A: symbols (pipe separated, ALL or CURRENT)
input string S2A_Timeframes             = "CURRENT";                  // S2A: timeframe per symbol (pipe separated, CURRENT = chart timeframe)
input int    S2A_EMALength              = 200;                         // S2A: EMA length
input int    S2A_BreakoutBars           = 3;                           // S2A: breakout bars
input int    S2A_StopLossBars           = 5;                           // S2A: stop loss lookback period
input double S2A_RiskPerTrade           = 0.01;                        // S2A: risk per trade as a fraction of equity (0.01 = 1%)
input bool   S2A_AllowMultiplePositions = true;                        // S2A: enable pyramiding (multiple positions per symbol)
input int    S2A_MaxPositions           = 5;                           // S2A: maximum positions per symbol
input bool   S2A_EnableRandomization    = false;                       // S2A: randomizer master switch (OFF = exact original behavior)
input double S2A_RandomDelayMaxSec      = 0.0;                         // S2A: max random entry delay, seconds (0 = immediately)
input double S2A_RandomVolumeJitterPct  = 0.0;                         // S2A: volume jitter +/- percent (0 = off)
input double S2A_RandomSlJitterPoints   = 0.0;                         // S2A: SL level jitter +/- points (0 = off)

//=== S2B INPUTS: Multi-Symbol Crypto Edge ================================
input group "=== S2B: Multi-Symbol Crypto Edge ==="
input bool   S2B_Enabled                = false;                  // S2B: enabled
input string S2B_Symbols                = "BTCUSD|XAUUSD|USDJPY";    // S2B: symbols (pipe separated, ALL or CURRENT)
input string S2B_Timeframes             = "CURRENT";                  // S2B: timeframe per symbol (pipe separated, CURRENT = chart timeframe)
input int    S2B_EMALength              = 200;                         // S2B: EMA length
input int    S2B_BreakoutBars           = 3;                           // S2B: breakout bars
input int    S2B_StopLossBars           = 5;                           // S2B: stop loss lookback period
input double S2B_RiskPerTrade           = 0.01;                        // S2B: risk per trade as a fraction of equity (0.01 = 1%)
input bool   S2B_AllowMultiplePositions = true;                        // S2B: enable pyramiding (multiple positions per symbol)
input int    S2B_MaxPositions           = 5;                           // S2B: maximum positions per symbol
input bool   S2B_EnableRandomization    = false;                       // S2B: randomizer master switch (OFF = exact original behavior)
input double S2B_RandomDelayMaxSec      = 0.0;                         // S2B: max random entry delay, seconds (0 = immediately)
input double S2B_RandomVolumeJitterPct  = 0.0;                         // S2B: volume jitter +/- percent (0 = off)
input double S2B_RandomSlJitterPoints   = 0.0;                         // S2B: SL level jitter +/- points (0 = off)



//=== S3 INPUTS: Multi-Symbol Trend Pullback (long only) ================
input group "=== S3: Multi-Symbol Trend Pullback ==="
input bool            S3_Enabled               = false;   // S3: enabled
input string          S3_Symbols               = "EURUSD,GBPUSD,USDJPY,XAUUSD"; // S3: symbols (comma or pipe separated, CURRENT = chart symbol)
input ENUM_TIMEFRAMES S3_EntryTF               = PERIOD_H1;// S3: entry timeframe
input string          S3_EntryTime             = "00:00";  // S3: earliest entry time (HH:MM, server time)
input int             S3_EntryEMA_Period       = 20;      // S3: entry EMA period
input int             S3_DailyFastEMA          = 50;      // S3: daily fast EMA period
input int             S3_DailySlowEMA          = 200;     // S3: daily slow EMA period
input int             S3_ATR_Period            = 14;      // S3: ATR period
input double          S3_ATR_SL_Multiplier     = 2.0;     // S3: ATR stop loss multiplier
input double          S3_ATR_Trail_Multiplier  = 2.5;     // S3: ATR trailing stop multiplier
input double          S3_RiskPercent           = 1.0;     // S3: risk per trade (% of balance)
input bool            S3_EnableRandomization   = false;   // S3: randomizer master switch (OFF = exact original behavior)
input double          S3_RandomDelayMaxSec     = 0.0;     // S3: max random delay, seconds (0 = immediately)
input double          S3_RandomVolumeJitterPct = 0.0;     // S3: volume jitter +/- percent (0 = off)
input double          S3_RandomSlJitterPoints  = 0.0;     // S3: SL distance jitter +/- points (0 = off)


//+------------------------------------------------------------------+
//| MetaEq extension inputs                                           |
//+------------------------------------------------------------------+
input group "=== GLOBAL CLOCK ==="
input double GlobalBrokerUtcOffsetHours = 2.0;      // Server clock minus UTC in winter (hours, e.g. 2 = GMT+2 winter)
input bool   GlobalBrokerOffsetFollowsUsDst = true; // Server follows US DST: +3 in summer, +2 in winter (Pepperstone-style)

input group "=== PORTFOLIO GUARD ==="
input bool   PortfolioGuardEnabled        = false;  // MetaEq portfolio guard: block new entries
input int    MaxCombinedPositions         = 3;      // Max combined open positions (0 = off)
input double MaxPortfolioDailyLossUsd     = 0.0;    // Max portfolio daily loss in USD (0 = off)
input bool   FlattenOnPortfolioGuardTrip  = false;  // Flatten all portfolio positions when the daily loss trips

input group "=== DIRECTION CAP ==="
input bool   DirectionCapEnabled          = false;  // Enable per-symbol direction cap
input string DirCapSymbol1                = "NAS100"; // Symbol 1
input int    DirCapMax1                   = 2;      // Symbol 1 max buys / max sells (0 = no limit)
input string DirCapSymbol2                = "USDJPY"; // Symbol 2
input int    DirCapMax2                   = 2;      // Symbol 2 max buys / max sells (0 = no limit)
input string DirCapSymbol3                = "XAUUSD"; // Symbol 3
input int    DirCapMax3                   = 2;      // Symbol 3 max buys / max sells (0 = no limit)
input bool   DirCapCloseExcess            = true;   // Close newest excess fills (stop-order overfill)

input group "=== S1 EXTRA: IVB ==="
input double S1_HoursShift                = 0.0;    // S1: strategy clock shift in hours
input double S1_MaxProfitAmount           = 0.0;    // S1: close at this net profit (0 = off)
input bool   S1_ProfitLockEnabled         = false;  // S1: profit lock
input double S1_ProfitLockTrigger         = 75.0;   // S1: profit lock trigger (account currency)
input double S1_ProfitLockGiveback        = 50.0;   // S1: profit lock give-back (% of peak)

input group "=== S2A EXTRA: CryptoEdge A ==="
input bool   S2A_ProfitLockEnabled        = false;  // S2A: profit lock
input double S2A_ProfitLockTrigger        = 100.0;  // S2A: profit lock trigger (account currency)
input double S2A_ProfitLockGiveback       = 50.0;   // S2A: profit lock give-back (% of peak)

input group "=== S2B EXTRA: CryptoEdge B ==="
input bool   S2B_ProfitLockEnabled        = false;  // S2B: profit lock
input double S2B_ProfitLockTrigger        = 100.0;  // S2B: profit lock trigger (account currency)
input double S2B_ProfitLockGiveback       = 50.0;   // S2B: profit lock give-back (% of peak)

input group "=== S3 EXTRA: Trend Pullback A ==="
input bool   S3_ProfitLockEnabled         = false;  // S3: profit lock
input double S3_ProfitLockTrigger         = 100.0;  // S3: profit lock trigger (account currency)
input double S3_ProfitLockGiveback        = 50.0;   // S3: profit lock give-back (% of peak)

input group "=== S3B: Trend Pullback B ==="
input bool            S3B_Enabled               = false;   // S3B: enabled
input string          S3B_Symbols               = "EURUSD|GBPUSD|USDJPY|XAUUSD"; // S3B: symbols
input ENUM_TIMEFRAMES S3B_EntryTF               = PERIOD_H4; // S3B: entry timeframe
input int             S3B_EntryEMA_Period       = 20;      // S3B: entry EMA period
input int             S3B_DailyFastEMA          = 50;      // S3B: daily fast EMA
input int             S3B_DailySlowEMA          = 200;     // S3B: daily slow EMA
input int             S3B_ATR_Period            = 14;      // S3B: ATR period
input double          S3B_ATR_SL_Multiplier     = 2.0;     // S3B: ATR stop loss multiplier
input double          S3B_ATR_Trail_Multiplier  = 2.5;     // S3B: ATR trailing stop multiplier
input double          S3B_RiskPercent           = 1.0;     // S3B: risk per trade (% of balance)
input bool            S3B_EnableRandomization   = false;   // S3B: randomizer master switch
input double          S3B_RandomDelayMaxSec     = 0.0;     // S3B: max random delay, seconds
input double          S3B_RandomVolumeJitterPct = 0.0;     // S3B: volume jitter +/- percent
input double          S3B_RandomSlJitterPoints  = 0.0;     // S3B: SL distance jitter +/- points
input bool            S3B_ProfitLockEnabled     = false;   // S3B: profit lock
input double          S3B_ProfitLockTrigger     = 100.0;   // S3B: profit lock trigger
input double          S3B_ProfitLockGiveback    = 50.0;    // S3B: profit lock give-back (%)

input group "=== S4: ORB ==="
input bool   S4_Enabled                   = true;    // S4: enabled
input string S4_Symbol                    = "CURRENT"; // S4: symbol (CURRENT = chart)
input int    S4_RangeMinutes              = 120;     // S4: range minutes
input int    S4_SessionStartHour          = 4;       // S4: session start hour (broker clock)
input int    S4_SessionStartMinute        = 0;       // S4: session start minute
input int    S4_SessionEndHour            = 16;      // S4: session end hour (broker clock)
input int    S4_SessionEndMinute          = 0;       // S4: session end minute
input int    S4_BrokerUtcOffsetHours      = 3;       // S4: broker UTC offset (hours)
input bool   S4_TradeLongs                = true;    // S4: trade longs
input bool   S4_TradeShorts               = true;    // S4: trade shorts
input double S4_BufferPoints              = 10.0;    // S4: breakout buffer in points
input double S4_RrTarget                  = 2.0;     // S4: take profit R multiple (0 = off)
input int    S4_TrendDays                 = 20;      // S4: trend filter days (0 = off)
input double S4_TrendThresholdPct         = 1.0;     // S4: trend threshold (% change)
input double S4_FixedRisk                 = 100.0;   // S4: fixed risk amount
input double S4_MaxLots                   = 10.0;    // S4: max lots
input int    S4_MaxConsecLosses           = 3;       // S4: max consecutive losing days (0 = off)
input string S4_BreakerSeed               = "";      // S4: breaker seed yyyy-MM-dd|R,R,...
input bool   S4_ProfitLockEnabled         = false;   // S4: profit lock
input double S4_ProfitLockTrigger         = 50.0;    // S4: profit lock trigger
input double S4_ProfitLockGiveback        = 50.0;    // S4: profit lock give-back (%)

input group "=== S5: Breakout A (long only) ==="
input bool   S5_Enabled                   = true;    // S5: enabled
input string S5_Symbol                    = "USDJPY"; // S5: symbol
input int    S5_RangeStartHour            = 3;       // S5: range start hour
input int    S5_RangeEndHour              = 6;       // S5: range end hour
input int    S5_ExitHour                  = 17;      // S5: exit hour
input double S5_RangeTrimPips             = 1.2;     // S5: range trim (pips)
input double S5_Lots                      = 0.1;     // S5: lots
input ENUM_BOA_CLOCK S5_SessionClock      = BOA_CLOCK_BROKER; // S5: session clock
input bool   S5_UseEma                    = true;    // S5: EMA trend gate
input int    S5_EmaPeriod                 = 75;      // S5: EMA period
input ENUM_TIMEFRAMES S5_EmaTF            = PERIOD_H1; // S5: EMA timeframe
input bool   S5_ProfitLockEnabled         = false;   // S5: profit lock
input double S5_ProfitLockTrigger         = 100.0;   // S5: profit lock trigger
input double S5_ProfitLockGiveback        = 50.0;    // S5: profit lock give-back (%)

input group "=== S6: Breakout B (gold) ==="
input bool   S6_Enabled                   = false;   // S6: enabled
input string S6_Symbol                    = "XAUUSD"; // S6: symbol
input int    S6_RangeStartHour            = 3;       // S6: range start hour (UTC)
input int    S6_RangeEndHour              = 8;       // S6: range end hour (UTC)
input int    S6_ExitHour                  = 16;      // S6: exit hour (UTC)
input int    S6_ExitMinute                = 0;       // S6: exit minute (UTC)
input double S6_RangeTrim                 = 0.10;    // S6: range trim (price)
input double S6_StopPct                   = 1.25;    // S6: stop (% of entry price)
input double S6_Lots                      = 0.02;    // S6: lots
input double S6_RelRangeMin               = 0.70;    // S6: relative range min (0 = off)
input int    S6_RelRangeLookback          = 20;      // S6: relative range lookback (sessions)
input double S6_MaxSpreadPct              = 0.0;     // S6: max spread (% of price, 0 = off)
input bool   S6_IgnorePositionCap         = false;   // S6: ignore the portfolio position cap
input string S6_HistoryStart              = "";      // S6: history start yyyy-MM-dd (blank = auto)
input double S6_MaxProfitPct              = 0.0;     // S6: max profit (% of price, 0 = off)
input bool   S6_UseAtrTrail               = false;   // S6: use ATR profit trail
input ENUM_TIMEFRAMES S6_AtrTF            = PERIOD_M15; // S6: ATR timeframe
input int    S6_AtrPeriod                 = 14;      // S6: ATR period
input double S6_AtrMult                   = 3.0;     // S6: ATR trail multiplier
input double S6_AtrStartPct               = 0.50;    // S6: start trailing after profit (%)
input double S6_LockPct                   = 0.10;    // S6: locked profit once active (% of entry)
input bool   S6_OneEntryPerDay            = true;    // S6: one entry per day
input int    S6_MaxEntriesPerDay          = 2;       // S6: max entries per day (1-2)
input bool   S6_ProfitLockEnabled         = false;   // S6: profit lock
input double S6_ProfitLockTrigger         = 100.0;   // S6: profit lock trigger
input double S6_ProfitLockGiveback        = 50.0;    // S6: profit lock give-back (%)

input group "=== S7: Gotobi ==="
input bool   S7_Enabled                   = true;    // S7: enabled
input string S7_Symbol                    = "";      // S7: symbol (blank = chart)
input ENUM_GOTOBI_LEGS S7_Legs            = GOTOBI_BOTH; // S7: legs
input ENUM_GOTOBI_CALENDAR S7_Calendar    = GOTOBI_CAL_5_25_EOM; // S7: calendar
input ENUM_GOTOBI_CONTROL S7_Control      = GOTOBI_EVENT_DAYS; // S7: control
input bool   S7_SkipJstMondays            = true;    // S7: skip JST Mondays
input bool   S7_RequireRiseForShort       = false;   // S7: require pre-fix rise for short
input bool   S7_RequireJpySymbol          = true;    // S7: require JPY symbol
input string S7_LabelPrefix               = "GTO_";  // S7: label prefix
input bool   S7_LogEvents                 = true;    // S7: log events
input int    S7_LongHourJst               = 3;       // S7: long hour JST
input int    S7_LongMinuteJst             = 0;       // S7: long minute JST
input int    S7_FixHourJst                = 9;       // S7: fix hour JST
input int    S7_FixMinuteJst              = 55;      // S7: fix minute JST
input int    S7_ShortExitHourJst          = 10;      // S7: short exit hour JST
input int    S7_ShortExitMinuteJst        = 20;      // S7: short exit minute JST
input int    S7_EntryGraceMinutes         = 2;       // S7: entry grace minutes
input ENUM_GOTOBI_RISK S7_RiskMode        = GOTOBI_FIXED_USD; // S7: risk mode
input double S7_Lots                      = 0.01;    // S7: lots (fixed-lots mode)
input double S7_RiskPercent               = 0.5;     // S7: risk percent
input double S7_FixedRiskUsd              = 100.0;   // S7: fixed risk USD
input int    S7_AtrPeriod                 = 14;      // S7: ATR period
input double S7_AtrMult                   = 2.5;     // S7: ATR multiplier
input double S7_MaxSpreadPips             = 3.0;     // S7: max spread pips (FX)
input double S7_MaxSpreadBps              = 3.0;     // S7: max spread bps (non-FX)
input bool   S7_ProfitLockEnabled         = false;   // S7: profit lock
input double S7_ProfitLockTrigger         = 100.0;   // S7: profit lock trigger
input double S7_ProfitLockGiveback        = 50.0;    // S7: profit lock give-back (%)

input group "=== S8: NAS100 Monday ==="
input bool   S8_Enabled                   = false;   // S8: enabled
input string S8_Symbol                    = "NAS100"; // S8: symbol
input int    S8_BrokerUtcOffsetHours      = 0;       // S8: broker UTC offset (hours)
input double S8_FixedRisk                 = 100.0;   // S8: fixed risk (account currency)
input double S8_FixedLots                 = 0.0;     // S8: fixed lots (0 = use fixed risk)
input double S8_MaxLots                   = 10.0;    // S8: max lots
input bool   S8_TradeLongs                = true;    // S8: trade longs
input bool   S8_ProfitLockEnabled         = false;   // S8: profit lock
input double S8_ProfitLockTrigger         = 100.0;   // S8: profit lock trigger
input double S8_ProfitLockGiveback        = 50.0;    // S8: profit lock give-back (%)

input group "=== S9: Index Fade ==="
input bool   S9_Enabled                   = false;   // S9: enabled
input string S9_Symbols                   = "NAS100;SPA35;XAUUSD"; // S9: tracked symbols
input double S9_ClosePosThreshold         = 0.25;    // S9: close-position threshold (0-1)
input double S9_RiskPct                   = 1.0;     // S9: risk per trade (% of balance)
input double S9_StopPct                   = 6.0;     // S9: stop (% of price)
input int    S9_MaxHoldBars               = 1;       // S9: max hold (daily bars)
input bool   S9_AllowLong                 = true;    // S9: allow long
input bool   S9_RiskFromCurrentBalance    = false;   // S9: risk from current balance
input bool   S9_ProfitLockEnabled         = false;   // S9: profit lock
input double S9_ProfitLockTrigger         = 100.0;   // S9: profit lock trigger
input double S9_ProfitLockGiveback        = 50.0;    // S9: profit lock give-back (%)


//+------------------------------------------------------------------+
//| Constants                                                         |
//+------------------------------------------------------------------+
#define S1_OFFSET 1000
#define S2A_OFFSET 2000
#define S2B_OFFSET 4000
#define S3_OFFSET 3000
#define S2_DEFAULT_ALL "BTCUSD|XAUUSD|USDJPY"
#define S3_DEFAULT_ALL "EURUSD|GBPUSD|USDJPY|XAUUSD"
//+------------------------------------------------------------------+
//| MetaEq extension constants                                        |
//+------------------------------------------------------------------+
#define S3B_OFFSET 5000
#define S4_OFFSET  6000
#define S5_OFFSET  7000
#define S6_OFFSET  8000
#define S7_OFFSET  9000
#define S8_OFFSET  10000
#define S9_OFFSET  11000



//+------------------------------------------------------------------+
//| Data structures                                                   |
//+------------------------------------------------------------------+
struct S1SymbolData
  {
   string            symbol;
   ENUM_TIMEFRAMES   confTf;
   int               magic;
   int               atrHandle;
   int               atrLongHandle;
   int               atrShortHandle;
   double            atrValue;
   double            entryOpenPrice;
   double            upperLevel;
   double            lowerLevel;
   double            stopDistance;
   bool              setupReady;
   bool              levelsCalculated;
   bool              tradeTaken;
   datetime          lastConfirmBar;
   int               lastDay;
   int               pendingDir;     // 0 = none, +1 = deferred long, -1 = deferred short
   datetime          pendingDue;
  };

struct S2SymbolData
  {
   string            symbol;
   ENUM_TIMEFRAMES   tf;
   int               magic;
   int               emaHandle;
   datetime          lastClosedCandle;
   int               pendingDir;     // 0 = none, 1 = deferred long
   datetime          pendingDue;
   double            pendingPrice;   // confirmed close stored at signal time
  };

//+------------------------------------------------------------------+
//| S3 DATA STRUCTURE                                                 |
//+------------------------------------------------------------------+
struct S3SymbolData
  {
   string            symbol;
   int               magic;
   int               entryEmaHandle;
   int               dailyFastHandle;
   int               dailySlowHandle;
   int               atrHandle;
   datetime          lastBarTime;
   int               pendingDir;      // 0 = none, 1 = deferred long
   datetime          pendingDue;
   double            pendingPrice;    // confirmed close stored at signal time
  };
//+------------------------------------------------------------------+
//| MetaEq extension data structures                                  |
//+------------------------------------------------------------------+
struct PLRule
  {
   int               offset;
   bool              enabled;
   double            trigger;
   double            giveback;
   string            name;
  };

struct IFData
  {
   string            symbol;
   int               magic;
   datetime          lastBar;
   datetime          pendingBar;
   int               lastSignalDay;
  };




//+------------------------------------------------------------------+
//| Globals                                                           |
//+------------------------------------------------------------------+
// One CTrade per strategy, never shared.
CTrade        S1_Trade;
CTrade        S2A_Trade;
CTrade        S2B_Trade;
CTrade        S3_Trade;

S1SymbolData  S1_Data[];
S2SymbolData  S2A_Data[];
S2SymbolData  S2B_Data[];
S3SymbolData  S3_Data[];

// S1 parsed time-of-day settings
int           g_s1EntryHour,    g_s1EntryMin;
int           g_s1MaxEntryHour, g_s1MaxEntryMin;
int           g_s1ExitHour,     g_s1ExitMin;
int           g_s3EntryHour,    g_s3EntryMin;

// Global risk state
bool          g_TradingDisabled = false;
bool          g_dailyBreached   = false;
bool          g_totalBreached   = false;
int           g_riskDay         = 0;
double        g_dayStartEquity  = 0.0;
double        g_peakEquity      = 0.0;
//+------------------------------------------------------------------+
//| MetaEq extension globals                                          |
//+------------------------------------------------------------------+
CTrade        S3B_Trade;
CTrade        S4_Trade;
CTrade        S5_Trade;
CTrade        S6_Trade;
CTrade        S7_Trade;
CTrade        S8_Trade;
CTrade        S9_Trade;

S3SymbolData  S3B_Data[];

// S3B parsed time is not used (no entry time window).

// ---- S4 ORB ----
string        g_s4Symbol = "";
int           g_s4Magic = 0;
int           g_s4Day = 0;
bool          g_s4Armed = false;
bool          g_s4Placed = false;
bool          g_s4Finished = false;
double        g_s4RangeHigh = 0.0;
double        g_s4RangeLow = 0.0;
double        g_s4LongSl = 0.0;
double        g_s4LongTp = 0.0;
double        g_s4ShortSl = 0.0;
double        g_s4ShortTp = 0.0;
double        g_s4PlannedRiskLong = 0.0;
double        g_s4PlannedRiskShort = 0.0;
double        g_s4TodayR = 0.0;
double        g_s4DayR[];
bool          g_s4WindowHadData = false;
int           g_s4AtrHandle = INVALID_HANDLE;

// ---- S5 Breakout A ----
string        g_s5Symbol = "";
int           g_s5Magic = 0;
int           g_s5Day = 0;
bool          g_s5Armed = false;
bool          g_s5Locked = false;
bool          g_s5Placed = false;
bool          g_s5Finished = false;
double        g_s5Hi = 0.0;
double        g_s5Lo = 0.0;

// ---- S6 Breakout B ----
string        g_s6Symbol = "";
int           g_s6Magic = 0;
int           g_s6Day = 0;
bool          g_s6Locked = false;
bool          g_s6Placed = false;
bool          g_s6Finished = false;
bool          g_s6SpreadBlocked = false;
double        g_s6Hi = 0.0;
double        g_s6Lo = 0.0;
double        g_s6RangePct = 0.0;
int           g_s6Entries = 0;
double        g_s6RangeHistory[];
datetime      g_s6AtrBarTime = 0;
double        g_s6BestLong = 0.0;
double        g_s6BestShort = 0.0;
bool          g_s6HasLong = false;
bool          g_s6HasShort = false;
int           g_s6AtrHandle = INVALID_HANDLE;

// ---- S7 Gotobi ----
string        g_s7Symbol = "";
int           g_s7Magic = 0;
string        g_s7LongLabel = "";
string        g_s7ShortLabel = "";
int           g_s7AtrHandle = INVALID_HANDLE;
datetime      g_s7LastStopGuard = 0;
int           g_s7LastLongSkipDay = 0;
int           g_s7LastShortSkipDay = 0;
int           g_s7LastLongEntryDay = 0;
int           g_s7LastShortEntryDay = 0;
int           g_s7LastObservedEventDay = 0;
int           g_s7EntryLongCount = 0;
int           g_s7EntryShortCount = 0;
int           g_s7SkipLongCount = 0;
int           g_s7SkipShortCount = 0;
int           g_s7EntryFailCount = 0;
int           g_s7ExitFailCount = 0;

// ---- S8 Monday ----
string        g_s8Symbol = "";
int           g_s8Magic = 0;
int           g_s8CurDay = 0;
int           g_s8CurDow = -1;
bool          g_s8HaveMonday = false;
double        g_s8MondayOpen = 0.0;
int           g_s8LastEntryDay = 0;

// ---- S9 Index Fade ----
IFData        g_ifData[];

// ---- Portfolio guard / direction cap ----
int           g_pfDay = 0;
double        g_pfRealized = 0.0;
datetime      g_pfLastCalc = 0;
bool          g_pfFlattenedToday = false;
ulong         g_dirCapGrandfathered[];
datetime      g_dirCapLastLog = 0;

// ---- Profit lock ----
PLRule        g_plRules[];
ulong         g_plTickets[];
double        g_plPeaks[];

// ---- ORB R ledger risk map ----
ulong         g_orbRiskTicket[];
double        g_orbRiskMoney[];
int           g_orbRiskDay[];

// ---- MetaEq Manager control-file gates (runtime, not inputs) ----
struct MEQControlState
  {
   bool ready;
   bool s1;
   bool s2a;
   bool s2b;
   bool s3;
   bool s3b;
   bool s4;
   bool s5;
   bool s6;
   bool s7;
   bool s8;
   bool s9;
  };

MEQControlState g_meqCtl;
uint            g_meqCtlLastRead = 0;
bool            g_meqCtlWarned   = false;
uint            g_meqStatusLastWrite = 0;


//+------------------------------------------------------------------+
//| COMMON UTILITIES                                                  |
//+------------------------------------------------------------------+
// Splits "A|B|C" into trimmed, non-empty items.
int ParseList(const string text, string &items[])
  {
   ArrayResize(items, 0);

   string parts[];
   int n = StringSplit(text, '|', parts);

   for(int i = 0; i < n; i++)
     {
      string p = parts[i];
      StringTrimLeft(p);
      StringTrimRight(p);
      if(StringLen(p) == 0)
         continue;

      int size = ArraySize(items);
      ArrayResize(items, size + 1);
      items[size] = p;
     }

   return ArraySize(items);
  }

// Parses a symbol list. ALL expands to allList, CURRENT to the chart symbol.
int ParseSymbols(const string text, string &symbols[], const string allList)
  {
   string t = text;
   StringTrimLeft(t);
   StringTrimRight(t);

   string upper = t;
   StringToUpper(upper);
   if(upper == "ALL")
      t = allList;

   int n = ParseList(t, symbols);

   for(int i = 0; i < n; i++)
     {
      string u = symbols[i];
      StringToUpper(u);
      if(u == "CURRENT")
         symbols[i] = _Symbol;
     }

   return n;
  }

ENUM_TIMEFRAMES StringToTimeframe(const string text)
  {
   string u = text;
   StringToUpper(u);

   if(u == "M1")  return PERIOD_M1;
   if(u == "M2")  return PERIOD_M2;
   if(u == "M3")  return PERIOD_M3;
   if(u == "M4")  return PERIOD_M4;
   if(u == "M5")  return PERIOD_M5;
   if(u == "M6")  return PERIOD_M6;
   if(u == "M10") return PERIOD_M10;
   if(u == "M12") return PERIOD_M12;
   if(u == "M15") return PERIOD_M15;
   if(u == "M20") return PERIOD_M20;
   if(u == "M30") return PERIOD_M30;
   if(u == "H1")  return PERIOD_H1;
   if(u == "H2")  return PERIOD_H2;
   if(u == "H3")  return PERIOD_H3;
   if(u == "H4")  return PERIOD_H4;
   if(u == "H6")  return PERIOD_H6;
   if(u == "H8")  return PERIOD_H8;
   if(u == "H12") return PERIOD_H12;
   if(u == "D1")  return PERIOD_D1;
   if(u == "W1")  return PERIOD_W1;
   if(u == "MN1") return PERIOD_MN1;

   if(u != "CURRENT")
      PrintFormat("Unknown timeframe '%s', using the chart timeframe.", text);

   return (ENUM_TIMEFRAMES)Period();
  }

// One timeframe per symbol. If the list is shorter than the symbols, the last item is reused.
void ParseTimeframes(const string text, ENUM_TIMEFRAMES &tfs[], const int count)
  {
   string items[];
   int n = ParseList(text, items);

   ArrayResize(tfs, count);

   for(int i = 0; i < count; i++)
     {
      if(n == 0)
        {
         tfs[i] = (ENUM_TIMEFRAMES)Period();
         continue;
        }

      int idx = (i < n) ? i : n - 1;
      tfs[i] = StringToTimeframe(items[idx]);
     }
  }

bool ParseTime(const string timeStr, int &hour, int &minute)
  {
   string parts[];
   if(StringSplit(timeStr, ':', parts) != 2)
      return false;

   hour   = (int)StringToInteger(parts[0]);
   minute = (int)StringToInteger(parts[1]);

   return (hour >= 0 && hour <= 23 && minute >= 0 && minute <= 59);
  }

double SymRand(const double maxAbs)
  {
   return (2.0 * (double)MathRand() / 32767.0 - 1.0) * maxAbs;
  }

// Applies +/- percent jitter, then snaps to the volume step and limits.
double JitterVolume(const string sym, double lots, const double pct)
  {
   double lotStep = SymbolInfoDouble(sym, SYMBOL_VOLUME_STEP);
   double minLot  = SymbolInfoDouble(sym, SYMBOL_VOLUME_MIN);
   double maxLot  = SymbolInfoDouble(sym, SYMBOL_VOLUME_MAX);

   if(lotStep <= 0.0)
      return lots;

   lots = lots * (1.0 + SymRand(pct) / 100.0);
   lots = MathFloor(lots / lotStep) * lotStep;
   lots = MathMax(minLot, MathMin(maxLot, lots));

   return lots;
  }

// True when a new closed candle has appeared on the given symbol and timeframe.
bool IsNewBar(const string sym, const ENUM_TIMEFRAMES tf, datetime &lastBar)
  {
   datetime t = iTime(sym, tf, 1);
   if(t == 0)
      return false;

   if(t == lastBar)
      return false;

   lastBar = t;
   return true;
  }

// Balance that S2 risk is measured on.
double RiskBase()
  {
   if(GlobalUseFixedBalance)
      return GlobalFixedBalance;

   return AccountInfoDouble(ACCOUNT_EQUITY);
  }

bool IsOurMagic(const long magic)
  {
   return (magic >= GlobalBaseMagic + 1000 && magic < GlobalBaseMagic + 20000);
  }

int GetPositionCount(const string sym, const long magic)
  {
   int count = 0;

   for(int i = PositionsTotal() - 1; i >= 0; i--)
     {
      ulong ticket = PositionGetTicket(i);
      if(ticket == 0)
         continue;

      if(PositionGetString(POSITION_SYMBOL) == sym && PositionGetInteger(POSITION_MAGIC) == magic)
         count++;
     }

   return count;
  }

bool HasPosition(const string sym, const long magic, const bool checkLong)
  {
   long wanted = checkLong ? (long)POSITION_TYPE_BUY : (long)POSITION_TYPE_SELL;

   for(int i = PositionsTotal() - 1; i >= 0; i--)
     {
      ulong ticket = PositionGetTicket(i);
      if(ticket == 0)
         continue;

      if(PositionGetString(POSITION_SYMBOL) != sym)
         continue;
      if(PositionGetInteger(POSITION_MAGIC) != magic)
         continue;
      if(PositionGetInteger(POSITION_TYPE) == wanted)
         return true;
     }

   return false;
  }

int CountOurPositions()
  {
   int count = 0;

   for(int i = PositionsTotal() - 1; i >= 0; i--)
     {
      ulong ticket = PositionGetTicket(i);
      if(ticket == 0)
         continue;

      if(IsOurMagic(PositionGetInteger(POSITION_MAGIC)))
         count++;
     }

   return count;
  }

// True when ANOTHER strategy of this EA already holds a position in this direction on this symbol.
bool OtherStrategyHasPosition(const string sym, const bool isLong, const int ownOffset)
  {
   long wanted = isLong ? (long)POSITION_TYPE_BUY : (long)POSITION_TYPE_SELL;

   for(int i = PositionsTotal() - 1; i >= 0; i--)
     {
      ulong ticket = PositionGetTicket(i);
      if(ticket == 0)
         continue;

      if(PositionGetString(POSITION_SYMBOL) != sym)
         continue;
      if(PositionGetInteger(POSITION_TYPE) != wanted)
         continue;

      long magic = PositionGetInteger(POSITION_MAGIC);
      if(!IsOurMagic(magic))
         continue;

      int strategyOffset = (int)((magic - GlobalBaseMagic) / 1000) * 1000;
      if(strategyOffset != ownOffset)
         return true;
     }

   return false;
  }

// Global position limit and trading-disabled flag.
bool CanOpenNewPosition()
  {
   if(g_TradingDisabled)
      return false;

   if(GlobalMaxOpenPositions > 0 && CountOurPositions() >= GlobalMaxOpenPositions)
      return false;

   return true;
  }


//+------------------------------------------------------------------+
//| MetaEq extension utilities                                        |
//+------------------------------------------------------------------+
int Ymd(const datetime t)
  {
   MqlDateTime dt;
   TimeToStruct(t, dt);
   return dt.year * 10000 + dt.mon * 100 + dt.day;
  }

int YmdOf(const int year, const int month, const int day)
  {
   return year * 10000 + month * 100 + day;
  }

int WeekdayOfYmd(const int ymd)
  {
   MqlDateTime dt;
   dt.year = ymd / 10000;
   dt.mon  = (ymd / 100) % 100;
   dt.day  = ymd % 100;
   dt.hour = 12;
   dt.min  = 0;
   dt.sec  = 0;
   datetime t = StructToTime(dt);
   MqlDateTime out;
   TimeToStruct(t, out);
   return out.day_of_week;
  }

int ParseYmd(const string text)
  {
   string t = text;
   StringTrimLeft(t);
   StringTrimRight(t);
   StringReplace(t, ".", "-");
   string parts[];
   if(StringSplit(t, '-', parts) != 3)
      return 0;
   int y = (int)StringToInteger(parts[0]);
   int m = (int)StringToInteger(parts[1]);
   int d = (int)StringToInteger(parts[2]);
   if(y < 1900 || m < 1 || m > 12 || d < 1 || d > 31)
      return 0;
   return YmdOf(y, m, d);
  }

datetime DayStart(const datetime t)
  {
   return (datetime)((long)t - ((long)t % 86400));
  }

datetime FirstSunday(const int year, const int month)
  {
   MqlDateTime dt;
   dt.year = year;
   dt.mon  = month;
   dt.day  = 1;
   dt.hour = 0;
   dt.min  = 0;
   dt.sec  = 0;
   datetime first = StructToTime(dt);
   MqlDateTime out;
   TimeToStruct(first, out);
   int offset = (0 - out.day_of_week + 7) % 7;
   return (datetime)((long)first + (long)offset * 86400);
  }

datetime LastSunday(const int year, const int month)
  {
   MqlDateTime dt;
   dt.year = year;
   dt.mon  = month;
   dt.day  = 1;
   dt.hour = 0;
   dt.min  = 0;
   dt.sec  = 0;
   datetime first = StructToTime(dt);
   MqlDateTime out;
   TimeToStruct(first, out);
   int days = 31;
   if(month == 2)
      days = ((year % 4 == 0 && year % 100 != 0) || year % 400 == 0) ? 29 : 28;
   else
      if(month == 4 || month == 6 || month == 9 || month == 11)
         days = 30;
   datetime last = (datetime)((long)first + (long)(days - 1) * 86400);
   TimeToStruct(last, out);
   return (datetime)((long)last - (long)out.day_of_week * 86400);
  }

bool IsEuDst(const datetime utc)
  {
   MqlDateTime dt;
   TimeToStruct(utc, dt);
   datetime start = (datetime)((long)LastSunday(dt.year, 3) + 3600);
   datetime end   = (datetime)((long)LastSunday(dt.year, 10) + 3600);
   return (utc >= start && utc < end);
  }

datetime ServerToUtc(const datetime server)
  {
   double base = GlobalBrokerUtcOffsetHours;
   datetime guess = (datetime)((long)server - (long)(base * 3600.0));
   // Pepperstone-style US-DST server: GMT+2 in winter, GMT+3 during US DST.
   // A fixed GMT+3 server is covered by setting GlobalBrokerOffsetFollowsUsDst = false.
   double extra = (GlobalBrokerOffsetFollowsUsDst && IsUsDst(guess)) ? 1.0 : 0.0;
   return (datetime)((long)server - (long)((base + extra) * 3600.0));
  }

datetime UtcToServer(const datetime utc)
  {
   double base = GlobalBrokerUtcOffsetHours;
   double extra = (GlobalBrokerOffsetFollowsUsDst && IsUsDst(utc)) ? 1.0 : 0.0;
   return (datetime)((long)utc + (long)((base + extra) * 3600.0));
  }

datetime S5_SecondSunday(const int year, const int month)
  {
   datetime first = FirstSunday(year, month);
   return (datetime)((long)first + 7 * 86400);
  }

bool IsUsDst(const datetime utc)
  {
   MqlDateTime dt;
   TimeToStruct(utc, dt);
   // Second Sunday of March 07:00 UTC -> first Sunday of November 06:00 UTC.
   datetime start = (datetime)((long)S5_SecondSunday(dt.year, 3) + 7 * 3600);
   datetime end   = (datetime)((long)FirstSunday(dt.year, 11) + 6 * 3600);
   return (utc >= start && utc < end);
  }

datetime StrategyNowS1()
  {
   return (datetime)((long)TimeCurrent() + (long)(S1_HoursShift * 3600.0));
  }

double PipSizeFor(const string sym)
  {
   int digits = (int)SymbolInfoInteger(sym, SYMBOL_DIGITS);
   double point = SymbolInfoDouble(sym, SYMBOL_POINT);
   if(point <= 0.0)
      return 0.0;
   if(digits == 3 || digits == 5)
      return point * 10.0;
   return point;
  }

//+------------------------------------------------------------------+
//| Broker-side tradability checks                                    |
//| MT5 answers "invalid price" (10015) when a pending stop is on the  |
//| wrong side of the market or between ticks, and "market closed"     |
//| (10018) when an order is sent outside the symbol's session. Every  |
//| entry path runs these checks first; the entry engines can then     |
//| defer a signal instead of firing a doomed order at the rollover.   |
//+------------------------------------------------------------------+
bool TradingPermitted()
  {
   if(!TerminalInfoInteger(TERMINAL_TRADE_ALLOWED))
      return false;
   if(!MQLInfoInteger(MQL_TRADE_ALLOWED))
      return false;
   if(!AccountInfoInteger(ACCOUNT_TRADE_ALLOWED))
      return false;
   if(!AccountInfoInteger(ACCOUNT_TRADE_EXPERT))
      return false;
   return true;
  }

// True when the symbol's trade session covers serverTime. Symbols with no
// session table (most FX/metals feeds) are treated as open; the order-send
// retcode handling still covers a closed market.
bool SymbolSessionOpen(const string sym, const datetime serverTime)
  {
   MqlDateTime dt;
   TimeToStruct(serverTime, dt);
   ENUM_DAY_OF_WEEK dow = (ENUM_DAY_OF_WEEK)dt.day_of_week;
   long sec = (long)dt.hour * 3600 + (long)dt.min * 60 + (long)dt.sec;
   datetime from = 0, to = 0;
   bool haveSchedule = false;

   for(uint i = 0; i < 16; i++)
     {
      if(!SymbolInfoSessionTrade(sym, dow, i, from, to))
         break;
      haveSchedule = true;

      MqlDateTime fd, td;
      TimeToStruct(from, fd);
      TimeToStruct(to, td);
      long f = (long)fd.hour * 3600 + (long)fd.min * 60 + (long)fd.sec;
      long t = (long)td.hour * 3600 + (long)td.min * 60 + (long)td.sec;
      if(t <= 0)
         t = 86400;

      if(f <= t)
        {
         if(sec >= f && sec < t)
            return true;
        }
      else
        {
         // Session crosses midnight (e.g. 23:00-01:00).
         if(sec >= f || sec < t)
            return true;
        }
     }

   return (!haveSchedule);
  }

bool SymbolQuoteReady(const string sym, double &ask, double &bid, string &why)
  {
   ask = 0.0;
   bid = 0.0;
   MqlTick tick;
   if(!SymbolInfoTick(sym, tick))
     {
      why = "no tick";
      return false;
     }
   ask = tick.ask;
   bid = tick.bid;
   if(ask <= 0.0 || bid <= 0.0 || ask < bid)
     {
      why = "bad quote";
      return false;
     }
   if(!MQLInfoInteger(MQL_TESTER) && tick.time > 0)
     {
      long age = (long)TimeCurrent() - (long)tick.time;
      if(age > 180)
        {
         why = StringFormat("stale quote (%d s old)", (int)age);
         return false;
        }
     }
   return true;
  }

bool SymbolCanTrade(const string sym, const bool isLong, string &why)
  {
   why = "";
   if(!TradingPermitted())
     {
      why = "trading not allowed";
      return false;
     }

   long mode = SymbolInfoInteger(sym, SYMBOL_TRADE_MODE);
   if(mode == SYMBOL_TRADE_MODE_DISABLED)
     {
      why = "symbol trade disabled";
      return false;
     }
   if(mode == SYMBOL_TRADE_MODE_CLOSEONLY)
     {
      why = "symbol close-only";
      return false;
     }
   if(isLong && mode == SYMBOL_TRADE_MODE_SHORTONLY)
     {
      why = "symbol short-only";
      return false;
     }
   if(!isLong && mode == SYMBOL_TRADE_MODE_LONGONLY)
     {
      why = "symbol long-only";
      return false;
     }

   if(!SymbolSessionOpen(sym, TimeCurrent()))
     {
      why = "market closed (session)";
      return false;
     }

   double ask, bid;
   return SymbolQuoteReady(sym, ask, bid, why);
  }

// Broker minimum distance for a stop/pending price. Some servers report a
// stops level of zero but still reject a stop one point from the market, so
// always add a two-point cushion and honour the freeze level too.
double StopLevelBuffer(const string sym)
  {
   double point = SymbolInfoDouble(sym, SYMBOL_POINT);
   if(point <= 0.0)
      return 0.0;
   long stops  = SymbolInfoInteger(sym, SYMBOL_TRADE_STOPS_LEVEL);
   long freeze = SymbolInfoInteger(sym, SYMBOL_TRADE_FREEZE_LEVEL);
   long level  = (stops > freeze) ? stops : freeze;
   return (double)(level + 2) * point;
  }

double AlignPriceToTick(const string sym, const double price)
  {
   double tick = SymbolInfoDouble(sym, SYMBOL_TRADE_TICK_SIZE);
   int digits = (int)SymbolInfoInteger(sym, SYMBOL_DIGITS);
   if(tick <= 0.0)
      tick = SymbolInfoDouble(sym, SYMBOL_POINT);
   if(tick <= 0.0)
      return NormalizeDouble(price, digits);
   return NormalizeDouble(MathRound(price / tick) * tick, digits);
  }

bool PendingStopLevelsOk(const string sym, const bool isLong,
                         const double price, const double sl, const double tp,
                         string &why)
  {
   why = "";
   double ask, bid;
   if(!SymbolQuoteReady(sym, ask, bid, why))
      return false;

   double buffer = StopLevelBuffer(sym);
   if(buffer <= 0.0)
      buffer = SymbolInfoDouble(sym, SYMBOL_POINT);

   if(isLong)
     {
      if(price < ask + buffer)
        {
         why = StringFormat("buy stop %.5f is not %.5f above ask %.5f", price, buffer, ask);
         return false;
        }
      if(sl > 0.0 && sl > price - buffer)
        {
         why = StringFormat("buy stop SL %.5f is inside the %.5f buffer", sl, buffer);
         return false;
        }
      if(tp > 0.0 && tp < price + buffer)
        {
         why = StringFormat("buy stop TP %.5f is inside the %.5f buffer", tp, buffer);
         return false;
        }
     }
   else
     {
      if(price > bid - buffer)
        {
         why = StringFormat("sell stop %.5f is not %.5f below bid %.5f", price, buffer, bid);
         return false;
        }
      if(sl > 0.0 && sl < price + buffer)
        {
         why = StringFormat("sell stop SL %.5f is inside the %.5f buffer", sl, buffer);
         return false;
        }
      if(tp > 0.0 && tp > price - buffer)
        {
         why = StringFormat("sell stop TP %.5f is inside the %.5f buffer", tp, buffer);
         return false;
        }
     }
   return true;
  }

bool MarketOrderLevelsOk(const string sym, const bool isLong,
                         const double sl, const double tp, string &why)
  {
   why = "";
   double ask, bid;
   if(!SymbolQuoteReady(sym, ask, bid, why))
      return false;

   double buffer = StopLevelBuffer(sym);
   if(buffer <= 0.0)
      buffer = SymbolInfoDouble(sym, SYMBOL_POINT);
   double entry = isLong ? ask : bid;

   if(sl > 0.0)
     {
      if(isLong && sl > entry - buffer)
        {
         why = StringFormat("SL %.5f is inside the %.5f buffer below ask %.5f", sl, buffer, ask);
         return false;
        }
      if(!isLong && sl < entry + buffer)
        {
         why = StringFormat("SL %.5f is inside the %.5f buffer above bid %.5f", sl, buffer, bid);
         return false;
        }
     }
   if(tp > 0.0)
     {
      if(isLong && tp < entry + buffer)
        {
         why = StringFormat("TP %.5f is inside the %.5f buffer above ask %.5f", tp, buffer, ask);
         return false;
        }
      if(!isLong && tp > entry - buffer)
        {
         why = StringFormat("TP %.5f is inside the %.5f buffer below bid %.5f", tp, buffer, bid);
         return false;
        }
     }
   return true;
  }

// Broker-side check for a stop modification on an OPEN position. A long is
// closed at the bid and a short at the ask, so those are the prices the
// server compares the new stop against.
bool PositionStopOk(const string sym, const bool isLong, const double sl, string &why)
  {
   why = "";
   double ask, bid;
   if(!SymbolQuoteReady(sym, ask, bid, why))
      return false;

   double buffer = StopLevelBuffer(sym);
   if(buffer <= 0.0)
      buffer = SymbolInfoDouble(sym, SYMBOL_POINT);

   if(isLong && sl > bid - buffer)
     {
      why = StringFormat("SL %.5f is inside the %.5f buffer below bid %.5f", sl, buffer, bid);
      return false;
     }
   if(!isLong && sl < ask + buffer)
     {
      why = StringFormat("SL %.5f is inside the %.5f buffer above ask %.5f", sl, buffer, ask);
      return false;
     }
   return true;
  }

bool RetcodeRetryable(const uint rc)
  {
   if(rc == TRADE_RETCODE_REQUOTE ||
      rc == TRADE_RETCODE_PRICE_CHANGED ||
      rc == TRADE_RETCODE_PRICE_OFF ||
      rc == TRADE_RETCODE_TIMEOUT ||
      rc == TRADE_RETCODE_CONNECTION ||
      rc == TRADE_RETCODE_TOO_MANY_REQUESTS ||
      rc == TRADE_RETCODE_MARKET_CLOSED ||
      rc == TRADE_RETCODE_INVALID_PRICE)
      return true;
   return false;
  }

double NormalizeLots(const string sym, double lots)
  {
   double step = SymbolInfoDouble(sym, SYMBOL_VOLUME_STEP);
   double minLot = SymbolInfoDouble(sym, SYMBOL_VOLUME_MIN);
   double maxLot = SymbolInfoDouble(sym, SYMBOL_VOLUME_MAX);
   if(step <= 0.0)
      return 0.0;
   lots = MathFloor(lots / step) * step;
   lots = MathMax(minLot, MathMin(maxLot, lots));
   return NormalizeDouble(lots, 8);
  }

double LotsForRisk(const string sym, const double riskMoney, const double stopDistance, const bool clampMin)
  {
   double tickValue = SymbolInfoDouble(sym, SYMBOL_TRADE_TICK_VALUE);
   double tickSize  = SymbolInfoDouble(sym, SYMBOL_TRADE_TICK_SIZE);
   double step      = SymbolInfoDouble(sym, SYMBOL_VOLUME_STEP);
   double minLot    = SymbolInfoDouble(sym, SYMBOL_VOLUME_MIN);
   double maxLot    = SymbolInfoDouble(sym, SYMBOL_VOLUME_MAX);
   if(riskMoney <= 0.0 || stopDistance <= 0.0 || tickValue <= 0.0 || tickSize <= 0.0 || step <= 0.0)
      return 0.0;
   double perLot = stopDistance / tickSize * tickValue;
   if(perLot <= 0.0)
      return 0.0;
   double lots = MathFloor(riskMoney / perLot / step) * step;
   if(clampMin)
      lots = MathMax(minLot, lots);
   if(lots > maxLot)
      lots = MathFloor(maxLot / step) * step;
   if(lots < minLot)
      return 0.0;
   return NormalizeDouble(lots, 8);
  }

bool IsCurrencyCode(const string text)
  {
   string u = text;
   StringToUpper(u);
   if(u == "USD" || u == "EUR" || u == "GBP" || u == "JPY" || u == "CHF" ||
      u == "AUD" || u == "NZD" || u == "CAD" || u == "SGD" || u == "HKD" ||
      u == "NOK" || u == "SEK" || u == "DKK" || u == "PLN" || u == "CZK" ||
      u == "HUF" || u == "TRY" || u == "ZAR" || u == "MXN" || u == "CNH" || u == "CNY")
      return true;
   return false;
  }

bool IsFxPairName(const string sym)
  {
   string desc = SymbolInfoString(sym, SYMBOL_DESCRIPTION);
   string du = desc;
   StringToUpper(du);
   if(StringFind(du, "INDEX") >= 0 || StringFind(du, "FUTURES") >= 0)
      return false;
   string base = SymbolInfoString(sym, SYMBOL_CURRENCY_BASE);
   string quote = SymbolInfoString(sym, SYMBOL_CURRENCY_PROFIT);
   if(IsCurrencyCode(base) && IsCurrencyCode(quote))
      return true;
   string u = sym;
   StringToUpper(u);
   if(StringLen(u) >= 6)
     {
      string b = StringSubstr(u, 0, 3);
      string q = StringSubstr(u, StringLen(u) - 3, 3);
      return (IsCurrencyCode(b) && IsCurrencyCode(q));
     }
   return false;
  }

bool IsJpyExposure(const string sym)
  {
   string u = sym;
   StringToUpper(u);
   if(StringFind(u, "JPY") >= 0)
      return true;
   string base = SymbolInfoString(sym, SYMBOL_CURRENCY_BASE);
   string quote = SymbolInfoString(sym, SYMBOL_CURRENCY_PROFIT);
   string b = base, q = quote;
   StringToUpper(b);
   StringToUpper(q);
   if(StringFind(b, "JPY") >= 0 || StringFind(q, "JPY") >= 0)
      return true;
   string desc = SymbolInfoString(sym, SYMBOL_DESCRIPTION);
   StringToUpper(desc);
   if(StringFind(desc, "JAPAN") >= 0 || StringFind(desc, "NIKKEI") >= 0 ||
      StringFind(desc, "JPX") >= 0 || StringFind(desc, "TOPIX") >= 0 ||
      StringFind(desc, "NKY") >= 0 || StringFind(desc, "JPN225") >= 0 ||
      StringFind(desc, "JP225") >= 0)
      return true;
   return false;
  }

int StrategyOffset(const long magic)
  {
   if(magic < GlobalBaseMagic)
      return -1;
   return (int)((magic - GlobalBaseMagic) / 1000) * 1000;
  }

int CountPositionsByOffset(const int offset)
  {
   int n = 0;
   for(int i = PositionsTotal() - 1; i >= 0; i--)
     {
      ulong ticket = PositionGetTicket(i);
      if(ticket == 0)
         continue;
      if(StrategyOffset(PositionGetInteger(POSITION_MAGIC)) == offset)
         n++;
     }
   return n;
  }

int CountPendingByMagic(const string sym, const int magic)
  {
   int n = 0;
   for(int i = OrdersTotal() - 1; i >= 0; i--)
     {
      ulong ticket = OrderGetTicket(i);
      if(ticket == 0)
         continue;
      if(OrderGetString(ORDER_SYMBOL) != sym)
         continue;
      if(OrderGetInteger(ORDER_MAGIC) != magic)
         continue;
      n++;
     }
   return n;
  }

bool HasPendingSide(const string sym, const int magic, const bool isLong)
  {
   for(int i = OrdersTotal() - 1; i >= 0; i--)
     {
      ulong ticket = OrderGetTicket(i);
      if(ticket == 0)
         continue;
      if(OrderGetString(ORDER_SYMBOL) != sym)
         continue;
      if(OrderGetInteger(ORDER_MAGIC) != magic)
         continue;
      long type = OrderGetInteger(ORDER_TYPE);
      bool buySide  = (type == ORDER_TYPE_BUY_LIMIT  || type == ORDER_TYPE_BUY_STOP  || type == ORDER_TYPE_BUY_STOP_LIMIT);
      bool sellSide = (type == ORDER_TYPE_SELL_LIMIT || type == ORDER_TYPE_SELL_STOP || type == ORDER_TYPE_SELL_STOP_LIMIT);
      if(isLong ? buySide : sellSide)
         return true;
     }
   return false;
  }

bool HasPositionSince(const string sym, const int magic, const bool isLong, const datetime sinceServer)
  {
   long wanted = isLong ? (long)POSITION_TYPE_BUY : (long)POSITION_TYPE_SELL;
   for(int i = PositionsTotal() - 1; i >= 0; i--)
     {
      ulong ticket = PositionGetTicket(i);
      if(ticket == 0)
         continue;
      if(PositionGetString(POSITION_SYMBOL) != sym)
         continue;
      if(PositionGetInteger(POSITION_MAGIC) != magic)
         continue;
      if(PositionGetInteger(POSITION_TYPE) != wanted)
         continue;
      if((datetime)PositionGetInteger(POSITION_TIME) >= sinceServer)
         return true;
     }
   return false;
  }

bool CancelPending(const string sym, const int magic, CTrade &trade)
  {
   bool any = false;
   for(int i = OrdersTotal() - 1; i >= 0; i--)
     {
      ulong ticket = OrderGetTicket(i);
      if(ticket == 0)
         continue;
      if(OrderGetString(ORDER_SYMBOL) != sym)
         continue;
      if(OrderGetInteger(ORDER_MAGIC) != magic)
         continue;
      trade.SetTypeFillingBySymbol(sym);
      if(trade.OrderDelete(ticket))
         any = true;
     }
   return any;
  }

bool CancelAllOurPending()
  {
   bool any = false;
   for(int i = OrdersTotal() - 1; i >= 0; i--)
     {
      ulong ticket = OrderGetTicket(i);
      if(ticket == 0)
         continue;
      long magic = OrderGetInteger(ORDER_MAGIC);
      if(!IsOurMagic(magic))
         continue;
      string sym = OrderGetString(ORDER_SYMBOL);
      int offset = StrategyOffset(magic);
      bool ok = false;
      if(offset == S3B_OFFSET)
        {
         S3B_Trade.SetTypeFillingBySymbol(sym);
         ok = S3B_Trade.OrderDelete(ticket);
        }
      else
         if(offset == S4_OFFSET)
           {
            S4_Trade.SetTypeFillingBySymbol(sym);
            ok = S4_Trade.OrderDelete(ticket);
           }
         else
            if(offset == S5_OFFSET)
              {
               S5_Trade.SetTypeFillingBySymbol(sym);
               ok = S5_Trade.OrderDelete(ticket);
              }
            else
               if(offset == S6_OFFSET)
                 {
                  S6_Trade.SetTypeFillingBySymbol(sym);
                  ok = S6_Trade.OrderDelete(ticket);
                 }
      if(ok)
         any = true;
     }
   return any;
  }

//+------------------------------------------------------------------+
//| MetaEq Manager control file                                      |
//|                                                                  |
//| The backend writes MQL5\Files\MetaEq\control.ini. Only new        |
//| entries are gated; position management keeps running. Missing,   |
//| partial, or corrupt files block every entry (fail-closed).       |
//+------------------------------------------------------------------+
void MEQ_ClearControl(MEQControlState &state)
  {
   state.ready = false;
   state.s1    = false;
   state.s2a   = false;
   state.s2b   = false;
   state.s3    = false;
   state.s3b   = false;
   state.s4    = false;
   state.s5    = false;
   state.s6    = false;
   state.s7    = false;
   state.s8    = false;
   state.s9    = false;
  }

bool MEQ_ValueIsOn(const string value)
  {
   string v = value;
   StringTrimLeft(v);
   StringTrimRight(v);
   StringToLower(v);
   return (v == "1" || v == "true" || v == "yes" || v == "on");
  }

bool MEQ_ReadControl(MEQControlState &state)
  {
   MEQ_ClearControl(state);
   int handle = FileOpen("MetaEq\\control.ini",
                         FILE_READ | FILE_TXT | FILE_ANSI |
                         FILE_SHARE_READ | FILE_SHARE_WRITE);
   if(handle == INVALID_HANDLE)
      return false;

   int version = -1;
   while(!FileIsEnding(handle))
     {
      string raw = FileReadString(handle);
      StringReplace(raw, "\r", "");
      string line = raw;
      StringTrimLeft(line);
      StringTrimRight(line);
      if(line == "" || StringGetCharacter(line, 0) == ';' || StringGetCharacter(line, 0) == '#')
         continue;
      int eq = StringFind(line, "=");
      if(eq <= 0)
         continue;
      string key = StringSubstr(line, 0, eq);
      string value = StringSubstr(line, eq + 1);
      StringTrimLeft(key);
      StringTrimRight(key);
      if(key == "version")
         version = (int)StringToInteger(value);
      else
         if(key == "S1")       state.s1  = MEQ_ValueIsOn(value);
         else if(key == "S2A") state.s2a = MEQ_ValueIsOn(value);
         else if(key == "S2B") state.s2b = MEQ_ValueIsOn(value);
         else if(key == "S3")  state.s3  = MEQ_ValueIsOn(value);
         else if(key == "S3B") state.s3b = MEQ_ValueIsOn(value);
         else if(key == "S4")  state.s4  = MEQ_ValueIsOn(value);
         else if(key == "S5")  state.s5  = MEQ_ValueIsOn(value);
         else if(key == "S6")  state.s6  = MEQ_ValueIsOn(value);
         else if(key == "S7")  state.s7  = MEQ_ValueIsOn(value);
         else if(key == "S8")  state.s8  = MEQ_ValueIsOn(value);
         else if(key == "S9")  state.s9  = MEQ_ValueIsOn(value);
     }
   FileClose(handle);

   if(version != 1)
     {
      MEQ_ClearControl(state);
      return false;
     }
   state.ready = true;
   return true;
  }

void MEQ_CancelPendingForOffset(const int offset)
  {
   int cancelled = 0;
   CTrade trade;
   for(int i = OrdersTotal() - 1; i >= 0; i--)
     {
      ulong ticket = OrderGetTicket(i);
      if(ticket == 0)
         continue;
      long magic = OrderGetInteger(ORDER_MAGIC);
      if(!IsOurMagic(magic) || StrategyOffset(magic) != offset)
         continue;
      string sym = OrderGetString(ORDER_SYMBOL);
      trade.SetTypeFillingBySymbol(sym);
      if(trade.OrderDelete(ticket))
         cancelled++;
     }
   if(cancelled > 0)
      PrintFormat("MetaEq Manager: cancelled %d pending order(s) for offset %d",
                  cancelled, offset);
  }

// Publish the gates this EA actually ingested. EA_Bridge reads this small
// file and forwards it in /report, so Discord can distinguish "the backend
// saved a toggle" from "the running EA applied it".
void MEQ_WriteStatus()
  {
   int handle = FileOpen("MetaEq\\status.ini",
                         FILE_WRITE | FILE_TXT | FILE_ANSI |
                         FILE_SHARE_READ | FILE_SHARE_WRITE);
   if(handle == INVALID_HANDLE)
      return;

   string states = StringFormat("S1=%d,S2A=%d,S2B=%d,S3=%d,S3B=%d,S4=%d,"
                                "S5=%d,S6=%d,S7=%d,S8=%d,S9=%d",
                                g_meqCtl.s1, g_meqCtl.s2a, g_meqCtl.s2b,
                                g_meqCtl.s3, g_meqCtl.s3b, g_meqCtl.s4,
                                g_meqCtl.s5, g_meqCtl.s6, g_meqCtl.s7,
                                g_meqCtl.s8, g_meqCtl.s9);
   FileWriteString(handle, "version=1\r\n");
   FileWriteString(handle, "updated=" + IntegerToString((long)TimeLocal()) + "\r\n");
   FileWriteString(handle, StringFormat("ready=%d\r\n", g_meqCtl.ready ? 1 : 0));
   FileWriteString(handle, "states=" + states + "\r\n");
   FileClose(handle);
  }

void MEQ_RefreshControl()
  {
   uint now = GetTickCount();
   if(now - g_meqCtlLastRead < 900)
      return;
   g_meqCtlLastRead = now;

   MEQControlState next;
   MEQ_ClearControl(next);
   bool ok = MEQ_ReadControl(next);
   if(!ok && !g_meqCtlWarned)
     {
      g_meqCtlWarned = true;
      Print("MetaEq Manager: control.ini missing or unreadable - all new entries are BLOCKED (fail-closed)");
     }
   if(ok)
      g_meqCtlWarned = false;

   bool changed = (next.ready != g_meqCtl.ready ||
                   next.s1 != g_meqCtl.s1 || next.s2a != g_meqCtl.s2a ||
                   next.s2b != g_meqCtl.s2b || next.s3 != g_meqCtl.s3 ||
                   next.s3b != g_meqCtl.s3b || next.s4 != g_meqCtl.s4 ||
                   next.s5 != g_meqCtl.s5 || next.s6 != g_meqCtl.s6 ||
                   next.s7 != g_meqCtl.s7 || next.s8 != g_meqCtl.s8 ||
                   next.s9 != g_meqCtl.s9);
   if(changed)
      PrintFormat("MetaEq Manager: %s | S1=%d S2A=%d S2B=%d S3=%d S3B=%d S4=%d S5=%d S6=%d S7=%d S8=%d S9=%d",
                  next.ready ? "control loaded" : "FAIL-CLOSED",
                  next.s1, next.s2a, next.s2b, next.s3, next.s3b, next.s4,
                  next.s5, next.s6, next.s7, next.s8, next.s9);

   // Turning a strategy off cancels its still-working pending orders. Open
   // positions are deliberately left alone and keep being managed.
   if(g_meqCtl.ready && g_meqCtl.s3b && (!next.ready || !next.s3b))
      MEQ_CancelPendingForOffset(S3B_OFFSET);
   if(g_meqCtl.ready && g_meqCtl.s4 && (!next.ready || !next.s4))
      MEQ_CancelPendingForOffset(S4_OFFSET);
   if(g_meqCtl.ready && g_meqCtl.s5 && (!next.ready || !next.s5))
      MEQ_CancelPendingForOffset(S5_OFFSET);
   if(g_meqCtl.ready && g_meqCtl.s6 && (!next.ready || !next.s6))
      MEQ_CancelPendingForOffset(S6_OFFSET);

   g_meqCtl = next;
   if(changed || g_meqStatusLastWrite == 0 || now - g_meqStatusLastWrite > 5000)
     {
      MEQ_WriteStatus();
      g_meqStatusLastWrite = now;
     }
  }

bool CloseTicket(const ulong ticket)
  {
   if(!PositionSelectByTicket(ticket))
      return false;
   string sym = PositionGetString(POSITION_SYMBOL);
   int offset = StrategyOffset(PositionGetInteger(POSITION_MAGIC));
   bool ok = false;
   if(offset == S1_OFFSET)
     {
      S1_Trade.SetTypeFillingBySymbol(sym);
      ok = S1_Trade.PositionClose(ticket);
     }
   else
      if(offset == S2A_OFFSET)
        {
         S2A_Trade.SetTypeFillingBySymbol(sym);
         ok = S2A_Trade.PositionClose(ticket);
        }
      else
         if(offset == S2B_OFFSET)
           {
            S2B_Trade.SetTypeFillingBySymbol(sym);
            ok = S2B_Trade.PositionClose(ticket);
           }
         else
            if(offset == S3_OFFSET)
              {
               S3_Trade.SetTypeFillingBySymbol(sym);
               ok = S3_Trade.PositionClose(ticket);
              }
            else
               if(offset == S3B_OFFSET)
                 {
                  S3B_Trade.SetTypeFillingBySymbol(sym);
                  ok = S3B_Trade.PositionClose(ticket);
                 }
               else
                  if(offset == S4_OFFSET)
                    {
                     S4_Trade.SetTypeFillingBySymbol(sym);
                     ok = S4_Trade.PositionClose(ticket);
                    }
                  else
                     if(offset == S5_OFFSET)
                       {
                        S5_Trade.SetTypeFillingBySymbol(sym);
                        ok = S5_Trade.PositionClose(ticket);
                       }
                     else
                        if(offset == S6_OFFSET)
                          {
                           S6_Trade.SetTypeFillingBySymbol(sym);
                           ok = S6_Trade.PositionClose(ticket);
                          }
                        else
                           if(offset == S7_OFFSET)
                             {
                              S7_Trade.SetTypeFillingBySymbol(sym);
                              ok = S7_Trade.PositionClose(ticket);
                             }
                           else
                              if(offset == S8_OFFSET)
                                {
                                 S8_Trade.SetTypeFillingBySymbol(sym);
                                 ok = S8_Trade.PositionClose(ticket);
                                }
                              else
                                 if(offset == S9_OFFSET)
                                   {
                                    S9_Trade.SetTypeFillingBySymbol(sym);
                                    ok = S9_Trade.PositionClose(ticket);
                                   }
   return ok;
  }

void CloseAllOurPositions()
  {
   for(int i = PositionsTotal() - 1; i >= 0; i--)
     {
      ulong ticket = PositionGetTicket(i);
      if(ticket == 0)
         continue;
      if(!IsOurMagic(PositionGetInteger(POSITION_MAGIC)))
         continue;
      CloseTicket(ticket);
     }
  }

void ClosePositionsByOffset(const int offset)
  {
   for(int i = PositionsTotal() - 1; i >= 0; i--)
     {
      ulong ticket = PositionGetTicket(i);
      if(ticket == 0)
         continue;
      if(StrategyOffset(PositionGetInteger(POSITION_MAGIC)) != offset)
         continue;
      CloseTicket(ticket);
     }
  }

void ClosePositionsByMagic(const string sym, const int magic)
  {
   for(int i = PositionsTotal() - 1; i >= 0; i--)
     {
      ulong ticket = PositionGetTicket(i);
      if(ticket == 0)
         continue;
      if(PositionGetString(POSITION_SYMBOL) != sym)
         continue;
      if(PositionGetInteger(POSITION_MAGIC) != magic)
         continue;
      CloseTicket(ticket);
     }
  }

//+------------------------------------------------------------------+
//| Portfolio guard                                                   |
//+------------------------------------------------------------------+
double PortfolioDailyRealized()
  {
   datetime now = TimeCurrent();
   int today = Ymd(now);
   if(today != g_pfDay)
     {
      g_pfDay = today;
      g_pfRealized = 0.0;
      g_pfFlattenedToday = false;
      g_pfLastCalc = 0;
     }
   if(g_pfLastCalc != 0 && (now - g_pfLastCalc) < 1)
      return g_pfRealized;

   g_pfLastCalc = now;
   datetime from = DayStart(now);
   double sum = 0.0;
   if(HistorySelect(from, (datetime)((long)now + 1)))
     {
      int total = HistoryDealsTotal();
      for(int i = 0; i < total; i++)
        {
         ulong ticket = HistoryDealGetTicket(i);
         if(ticket == 0)
            continue;
         long magic = HistoryDealGetInteger(ticket, DEAL_MAGIC);
         if(!IsOurMagic(magic))
            continue;
         long entry = HistoryDealGetInteger(ticket, DEAL_ENTRY);
         if(entry != DEAL_ENTRY_OUT && entry != DEAL_ENTRY_OUT_BY)
            continue;
         sum += HistoryDealGetDouble(ticket, DEAL_PROFIT);
         sum += HistoryDealGetDouble(ticket, DEAL_SWAP);
         sum += HistoryDealGetDouble(ticket, DEAL_COMMISSION);
        }
     }
   g_pfRealized = sum;
   return sum;
  }

bool PortfolioGuardBlocked()
  {
   if(!PortfolioGuardEnabled)
      return false;
   if(MaxPortfolioDailyLossUsd > 0.0 && PortfolioDailyRealized() <= -MathAbs(MaxPortfolioDailyLossUsd))
      return true;
   if(MaxCombinedPositions > 0 && CountOurPositions() >= MaxCombinedPositions)
      return true;
   return false;
  }

bool PortfolioGuardBlockedNoCap()
  {
   if(!PortfolioGuardEnabled)
      return false;
   if(MaxPortfolioDailyLossUsd > 0.0 && PortfolioDailyRealized() <= -MathAbs(MaxPortfolioDailyLossUsd))
      return true;
   return false;
  }

void FlattenPortfolio(const string reason)
  {
   int before = CountOurPositions();
   CloseAllOurPositions();
   int pending = 0;
   for(int i = OrdersTotal() - 1; i >= 0; i--)
     {
      ulong ticket = OrderGetTicket(i);
      if(ticket == 0)
         continue;
      if(IsOurMagic(OrderGetInteger(ORDER_MAGIC)))
         pending++;
     }
   CancelAllOurPending();
   PrintFormat("PORTFOLIO FLATTEN (%s): closed %d position(s), cancelled %d pending order(s).",
               reason, before, pending);
  }

void PortfolioGuardCheck()
  {
   if(!PortfolioGuardEnabled)
      return;
   double realized = PortfolioDailyRealized();
   if(MaxPortfolioDailyLossUsd > 0.0 && realized <= -MathAbs(MaxPortfolioDailyLossUsd) && !g_pfFlattenedToday)
     {
      g_pfFlattenedToday = true;
      PrintFormat("PORTFOLIO GUARD: daily loss %.2f hit limit %.2f. New entries blocked.%s",
                  realized, -MathAbs(MaxPortfolioDailyLossUsd),
                  FlattenOnPortfolioGuardTrip ? " Flattening all portfolio positions." : "");
      if(FlattenOnPortfolioGuardTrip)
         FlattenPortfolio("portfolio daily loss limit");
     }
  }

//+------------------------------------------------------------------+
//| Direction cap                                                     |
//+------------------------------------------------------------------+
int DirCapLimitFor(const string sym)
  {
   if(!DirectionCapEnabled || StringLen(sym) == 0)
      return 0;
   string s = sym;
   string a = DirCapSymbol1; StringTrimLeft(a); StringTrimRight(a);
   string b = DirCapSymbol2; StringTrimLeft(b); StringTrimRight(b);
   string c = DirCapSymbol3; StringTrimLeft(c); StringTrimRight(c);
   if(StringLen(a) > 0 && s == a) return DirCapMax1;
   if(StringLen(b) > 0 && s == b) return DirCapMax2;
   if(StringLen(c) > 0 && s == c) return DirCapMax3;
   return 0;
  }

int DirCapCount(const string sym, const bool isLong)
  {
   long wanted = isLong ? (long)POSITION_TYPE_BUY : (long)POSITION_TYPE_SELL;
   int n = 0;
   for(int i = PositionsTotal() - 1; i >= 0; i--)
     {
      ulong ticket = PositionGetTicket(i);
      if(ticket == 0)
         continue;
      if(PositionGetString(POSITION_SYMBOL) != sym)
         continue;
      if(PositionGetInteger(POSITION_TYPE) != wanted)
         continue;
      if(!IsOurMagic(PositionGetInteger(POSITION_MAGIC)))
         continue;
      n++;
     }
   return n;
  }

bool DirectionCapBlocked(const string sym, const bool isLong, const string who)
  {
   int limit = DirCapLimitFor(sym);
   if(limit <= 0)
      return false;
   int open = DirCapCount(sym, isLong);
   if(open < limit)
      return false;
   datetime now = TimeCurrent();
   if(g_dirCapLastLog == 0 || (now - g_dirCapLastLog) >= 60)
     {
      g_dirCapLastLog = now;
      PrintFormat("DIRECTION CAP | %s | %s %s blocked: %d open, max %d",
                  who, isLong ? "BUY" : "SELL", sym, open, limit);
     }
   return true;
  }

bool IsGrandfathered(const ulong ticket)
  {
   for(int i = 0; i < ArraySize(g_dirCapGrandfathered); i++)
      if(g_dirCapGrandfathered[i] == ticket)
         return true;
   return false;
  }

void DirectionCapStart()
  {
   ArrayResize(g_dirCapGrandfathered, 0);
   if(!DirectionCapEnabled)
      return;
   for(int i = PositionsTotal() - 1; i >= 0; i--)
     {
      ulong ticket = PositionGetTicket(i);
      if(ticket == 0)
         continue;
      if(!IsOurMagic(PositionGetInteger(POSITION_MAGIC)))
         continue;
      int n = ArraySize(g_dirCapGrandfathered);
      ArrayResize(g_dirCapGrandfathered, n + 1);
      g_dirCapGrandfathered[n] = ticket;
     }
   PrintFormat("DIRECTION CAP | ON | %s=%d | %s=%d | %s=%d | close excess %s | %d existing position(s) exempt",
               DirCapSymbol1, DirCapMax1, DirCapSymbol2, DirCapMax2, DirCapSymbol3, DirCapMax3,
               DirCapCloseExcess ? "ON" : "off", ArraySize(g_dirCapGrandfathered));
  }

void DirectionCapSweep()
  {
   if(!DirectionCapEnabled || !DirCapCloseExcess)
      return;
   string names[3];
   names[0] = DirCapSymbol1;
   names[1] = DirCapSymbol2;
   names[2] = DirCapSymbol3;
   for(int k = 0; k < 3; k++)
     {
      string name = names[k];
      StringTrimLeft(name);
      StringTrimRight(name);
      if(StringLen(name) == 0)
         continue;
      int limit = DirCapLimitFor(name);
      if(limit <= 0)
         continue;
      for(int side = 0; side < 2; side++)
        {
         bool isLong = (side == 0);
         long wanted = isLong ? (long)POSITION_TYPE_BUY : (long)POSITION_TYPE_SELL;
         ulong tickets[];
         datetime times[];
         ArrayResize(tickets, 0);
         ArrayResize(times, 0);
         for(int i = PositionsTotal() - 1; i >= 0; i--)
           {
            ulong ticket = PositionGetTicket(i);
            if(ticket == 0)
               continue;
            if(PositionGetString(POSITION_SYMBOL) != name)
               continue;
            if(PositionGetInteger(POSITION_TYPE) != wanted)
               continue;
            if(!IsOurMagic(PositionGetInteger(POSITION_MAGIC)))
               continue;
            int n = ArraySize(tickets);
            ArrayResize(tickets, n + 1);
            ArrayResize(times, n + 1);
            tickets[n] = ticket;
            times[n] = (datetime)PositionGetInteger(POSITION_TIME);
           }
         int count = ArraySize(tickets);
         int excess = count - limit;
         if(excess <= 0)
            continue;
         // newest first
         for(int a = 0; a < count - 1; a++)
            for(int b = a + 1; b < count; b++)
               if(times[b] > times[a])
                 {
                  datetime tt = times[a]; times[a] = times[b]; times[b] = tt;
                  ulong tk = tickets[a]; tickets[a] = tickets[b]; tickets[b] = tk;
                 }
         for(int a = 0; a < count && excess > 0; a++)
           {
            if(IsGrandfathered(tickets[a]))
               continue;
            PrintFormat("DIRECTION CAP | closing excess %s %s: %d open, max %d",
                        isLong ? "BUY" : "SELL", name, count, limit);
            if(CloseTicket(tickets[a]))
               excess--;
           }
        }
     }
  }

//+------------------------------------------------------------------+
//| Profit lock                                                       |
//+------------------------------------------------------------------+
void AddPLRule(const int offset, const bool enabled, const double trigger,
               const double giveback, const string name)
  {
   int n = ArraySize(g_plRules);
   ArrayResize(g_plRules, n + 1);
   g_plRules[n].offset   = offset;
   g_plRules[n].enabled  = enabled;
   g_plRules[n].trigger  = trigger;
   g_plRules[n].giveback = giveback;
   g_plRules[n].name     = name;
  }

void ProfitLockStart()
  {
   ArrayResize(g_plRules, 0);
   ArrayResize(g_plTickets, 0);
   ArrayResize(g_plPeaks, 0);
   AddPLRule(S1_OFFSET,  S1_ProfitLockEnabled,  S1_ProfitLockTrigger,  S1_ProfitLockGiveback,  "IVB");
   AddPLRule(S2A_OFFSET, S2A_ProfitLockEnabled, S2A_ProfitLockTrigger, S2A_ProfitLockGiveback, "CryptoEdge A");
   AddPLRule(S2B_OFFSET, S2B_ProfitLockEnabled, S2B_ProfitLockTrigger, S2B_ProfitLockGiveback, "CryptoEdge B");
   AddPLRule(S3_OFFSET,  S3_ProfitLockEnabled,  S3_ProfitLockTrigger,  S3_ProfitLockGiveback,  "Trend Pullback A");
   AddPLRule(S3B_OFFSET, S3B_ProfitLockEnabled, S3B_ProfitLockTrigger, S3B_ProfitLockGiveback, "Trend Pullback B");
   AddPLRule(S4_OFFSET,  S4_ProfitLockEnabled,  S4_ProfitLockTrigger,  S4_ProfitLockGiveback,  "ORB");
   AddPLRule(S5_OFFSET,  S5_ProfitLockEnabled,  S5_ProfitLockTrigger,  S5_ProfitLockGiveback,  "Breakout A");
   AddPLRule(S6_OFFSET,  S6_ProfitLockEnabled,  S6_ProfitLockTrigger,  S6_ProfitLockGiveback,  "Breakout B");
   AddPLRule(S7_OFFSET,  S7_ProfitLockEnabled,  S7_ProfitLockTrigger,  S7_ProfitLockGiveback,  "Gotobi");
   AddPLRule(S8_OFFSET,  S8_ProfitLockEnabled,  S8_ProfitLockTrigger,  S8_ProfitLockGiveback,  "NAS100 Monday");
   AddPLRule(S9_OFFSET,  S9_ProfitLockEnabled,  S9_ProfitLockTrigger,  S9_ProfitLockGiveback,  "Index Fade");
   for(int i = 0; i < ArraySize(g_plRules); i++)
      if(g_plRules[i].enabled)
         PrintFormat("%s: profit lock ON | trigger %.2f | give-back %.1f%%",
                     g_plRules[i].name, g_plRules[i].trigger, g_plRules[i].giveback);
  }

int PLFindTicket(const ulong ticket)
  {
   for(int i = 0; i < ArraySize(g_plTickets); i++)
      if(g_plTickets[i] == ticket)
         return i;
   return -1;
  }

void ProfitLockProcess()
  {
   bool any = false;
   for(int r = 0; r < ArraySize(g_plRules); r++)
      if(g_plRules[r].enabled)
        {
         any = true;
         break;
        }
   if(!any)
      return;

   ulong toClose[];
   ArrayResize(toClose, 0);

   for(int r = 0; r < ArraySize(g_plRules); r++)
     {
      if(!g_plRules[r].enabled)
         continue;
      for(int i = PositionsTotal() - 1; i >= 0; i--)
        {
         ulong ticket = PositionGetTicket(i);
         if(ticket == 0)
            continue;
         if(StrategyOffset(PositionGetInteger(POSITION_MAGIC)) != g_plRules[r].offset)
            continue;
         double profit = PositionGetDouble(POSITION_PROFIT) + PositionGetDouble(POSITION_SWAP);
         int idx = PLFindTicket(ticket);
         if(idx < 0)
           {
            if(profit < g_plRules[r].trigger)
               continue;
            idx = ArraySize(g_plTickets);
            ArrayResize(g_plTickets, idx + 1);
            ArrayResize(g_plPeaks, idx + 1);
            g_plTickets[idx] = ticket;
            g_plPeaks[idx] = profit;
            PrintFormat("%s PROFIT LOCK armed | #%I64u | profit %.2f reached trigger %.2f | floor %.2f",
                        g_plRules[r].name, ticket, profit, g_plRules[r].trigger,
                        profit * (1.0 - g_plRules[r].giveback / 100.0));
           }
         else
            if(profit > g_plPeaks[idx])
               g_plPeaks[idx] = profit;

         double floor = g_plPeaks[idx] * (1.0 - g_plRules[r].giveback / 100.0);
         if(profit <= floor)
           {
            PrintFormat("%s PROFIT LOCK exit | #%I64u | profit %.2f <= floor %.2f (peak %.2f)",
                        g_plRules[r].name, ticket, profit, floor, g_plPeaks[idx]);
            int n = ArraySize(toClose);
            ArrayResize(toClose, n + 1);
            toClose[n] = ticket;
           }
        }
     }

   for(int i = 0; i < ArraySize(toClose); i++)
      if(!CloseTicket(toClose[i]))
         PrintFormat("PROFIT LOCK: close failed for #%I64u", toClose[i]);

   // Drop peaks of positions that no longer exist.
   if(ArraySize(g_plTickets) > 0)
     {
      ulong keepT[];
      double keepP[];
      ArrayResize(keepT, 0);
      ArrayResize(keepP, 0);
      for(int i = 0; i < ArraySize(g_plTickets); i++)
        {
         if(!PositionSelectByTicket(g_plTickets[i]))
            continue;
         int n = ArraySize(keepT);
         ArrayResize(keepT, n + 1);
         ArrayResize(keepP, n + 1);
         keepT[n] = g_plTickets[i];
         keepP[n] = g_plPeaks[i];
        }
      ArrayResize(g_plTickets, ArraySize(keepT));
      ArrayResize(g_plPeaks, ArraySize(keepP));
      for(int i = 0; i < ArraySize(keepT); i++)
        {
         g_plTickets[i] = keepT[i];
         g_plPeaks[i] = keepP[i];
        }
     }
  }

//+------------------------------------------------------------------+
//| History helpers                                                   |
//+------------------------------------------------------------------+
bool HistoryHasEntry(const int magic, const string sym, const bool isLong, const datetime sinceServer)
  {
   if(!HistorySelect(sinceServer, (datetime)((long)TimeCurrent() + 1)))
      return false;
   long wanted = isLong ? (long)DEAL_TYPE_BUY : (long)DEAL_TYPE_SELL;
   int total = HistoryDealsTotal();
   for(int i = 0; i < total; i++)
     {
      ulong ticket = HistoryDealGetTicket(i);
      if(ticket == 0)
         continue;
      if(HistoryDealGetInteger(ticket, DEAL_MAGIC) != magic)
         continue;
      if(HistoryDealGetString(ticket, DEAL_SYMBOL) != sym)
         continue;
      if(HistoryDealGetInteger(ticket, DEAL_ENTRY) != DEAL_ENTRY_IN)
         continue;
      if(HistoryDealGetInteger(ticket, DEAL_TYPE) != wanted)
         continue;
      return true;
     }
   return false;
  }

//+------------------------------------------------------------------+
//| ORB risk map                                                      |
//+------------------------------------------------------------------+
int OrbRiskFind(const ulong ticket)
  {
   for(int i = 0; i < ArraySize(g_orbRiskTicket); i++)
      if(g_orbRiskTicket[i] == ticket)
         return i;
   return -1;
  }

void OrbRiskStore(const ulong ticket, const double riskMoney, const int entryDay)
  {
   int idx = OrbRiskFind(ticket);
   if(idx < 0)
     {
      idx = ArraySize(g_orbRiskTicket);
      ArrayResize(g_orbRiskTicket, idx + 1);
      ArrayResize(g_orbRiskMoney, idx + 1);
      ArrayResize(g_orbRiskDay, idx + 1);
     }
   g_orbRiskTicket[idx] = ticket;
   g_orbRiskMoney[idx] = riskMoney;
   g_orbRiskDay[idx] = entryDay;
  }

double OrbRiskMoney(const ulong ticket)
  {
   int idx = OrbRiskFind(ticket);
   return (idx >= 0) ? g_orbRiskMoney[idx] : 0.0;
  }

int OrbRiskDay(const ulong ticket)
  {
   int idx = OrbRiskFind(ticket);
   return (idx >= 0) ? g_orbRiskDay[idx] : 0;
  }

void OrbRiskRemove(const ulong ticket)
  {
   int idx = OrbRiskFind(ticket);
   if(idx < 0)
      return;
   int last = ArraySize(g_orbRiskTicket) - 1;
   g_orbRiskTicket[idx] = g_orbRiskTicket[last];
   g_orbRiskMoney[idx]  = g_orbRiskMoney[last];
   g_orbRiskDay[idx]    = g_orbRiskDay[last];
   ArrayResize(g_orbRiskTicket, last);
   ArrayResize(g_orbRiskMoney, last);
   ArrayResize(g_orbRiskDay, last);
  }

void ClosePositionsWithTrade(CTrade &trade, const string sym, const long magic)
  {
   for(int i = PositionsTotal() - 1; i >= 0; i--)
     {
      ulong ticket = PositionGetTicket(i);
      if(ticket == 0)
         continue;

      if(PositionGetString(POSITION_SYMBOL) != sym)
         continue;
      if(PositionGetInteger(POSITION_MAGIC) != magic)
         continue;

      trade.SetTypeFillingBySymbol(sym);

      if(trade.PositionClose(ticket))
         PrintFormat("Position closed: #%I64u (%s)", ticket, sym);
     }
  }

// Closes every position that belongs to any strategy of this EA.
void CloseAllPositions()
  {
   for(int i = PositionsTotal() - 1; i >= 0; i--)
     {
      ulong ticket = PositionGetTicket(i);
      if(ticket == 0)
         continue;
      if(!IsOurMagic(PositionGetInteger(POSITION_MAGIC)))
         continue;
      CloseTicket(ticket);
     }
  }

//+------------------------------------------------------------------+
//| GLOBAL RISK LAYER                                                 |
//+------------------------------------------------------------------+
// Returns false while trading is disabled by a drawdown limit.
bool CheckGlobalRisk()
  {
   double equity = AccountInfoDouble(ACCOUNT_EQUITY);

   MqlDateTime dt;
   TimeToStruct(TimeCurrent(), dt);
   int today = dt.year * 10000 + dt.mon * 100 + dt.day;

   if(today != g_riskDay)
     {
      g_riskDay        = today;
      g_dayStartEquity = equity;

      if(g_dailyBreached)
        {
         g_dailyBreached = false;
         Print("Global risk: new day, the daily drawdown limit is reset.");
        }
     }

   if(equity > g_peakEquity)
      g_peakEquity = equity;

   if(!g_dailyBreached && GlobalMaxDailyDDPercent > 0.0 && g_dayStartEquity > 0.0)
     {
      double dd = (g_dayStartEquity - equity) / g_dayStartEquity * 100.0;
      if(dd >= GlobalMaxDailyDDPercent)
        {
         g_dailyBreached = true;
         PrintFormat("Global risk: daily drawdown %.2f%% reached the %.2f%% limit. Closing all and pausing until the next day.",
                     dd, GlobalMaxDailyDDPercent);
        }
     }

   if(!g_totalBreached && GlobalMaxTotalDDPercent > 0.0 && g_peakEquity > 0.0)
     {
      double dd = (g_peakEquity - equity) / g_peakEquity * 100.0;
      if(dd >= GlobalMaxTotalDDPercent)
        {
         g_totalBreached = true;
         PrintFormat("Global risk: total drawdown %.2f%% reached the %.2f%% limit. Closing all and stopping trading.",
                     dd, GlobalMaxTotalDDPercent);
        }
     }

   g_TradingDisabled = (g_dailyBreached || g_totalBreached);
   return !g_TradingDisabled;
  }

//+------------------------------------------------------------------+
//| S1: INTRADAY VOLATILITY BREAKOUT                                  |
//+------------------------------------------------------------------+
bool InitStrategy1()
  {
   S1_Trade.SetDeviationInPoints(10);
   S2A_Trade.SetDeviationInPoints(10);
   S2B_Trade.SetDeviationInPoints(10);

   // Filling mode is resolved per symbol via SetTypeFillingBySymbol() before every
   // operation. Hard-coding ORDER_FILLING_FOK made every order fail on symbols
   // whose broker does not offer that fill mode.

   if(!ParseTime(S1_Entry_Time, g_s1EntryHour, g_s1EntryMin))
     {
      Print("S1 error: invalid S1_Entry_Time format. Use HH:MM");
      return false;
     }

   if(!ParseTime(S1_Max_Entry_Time, g_s1MaxEntryHour, g_s1MaxEntryMin))
     {
      Print("S1 error: invalid S1_Max_Entry_Time format. Use HH:MM");
      return false;
     }

   if(!ParseTime(S1_Exit_Time, g_s1ExitHour, g_s1ExitMin))
     {
      Print("S1 error: invalid S1_Exit_Time format. Use HH:MM");
      return false;
     }

   int entryMins    = g_s1EntryHour * 60 + g_s1EntryMin;
   int maxEntryMins = g_s1MaxEntryHour * 60 + g_s1MaxEntryMin;
   int exitMins     = g_s1ExitHour * 60 + g_s1ExitMin;

   if(entryMins >= maxEntryMins)
     {
      Print("S1 error: S1_Entry_Time must be earlier than S1_Max_Entry_Time");
      return false;
     }

   if(maxEntryMins >= exitMins)
     {
      Print("S1 error: S1_Max_Entry_Time must be earlier than S1_Exit_Time");
      return false;
     }

   string symbols[];
   int n = ParseSymbols(S1_Symbols, symbols, S2_DEFAULT_ALL);
   if(n == 0)
     {
      Print("S1 error: no symbols configured.");
      return false;
     }

   ENUM_TIMEFRAMES tfs[];
   ParseTimeframes(S1_Timeframes, tfs, n);

   ArrayResize(S1_Data, n);

   for(int i = 0; i < n; i++)
     {
      SymbolSelect(symbols[i], true);

      S1_Data[i].symbol           = symbols[i];
      S1_Data[i].confTf           = tfs[i];
      S1_Data[i].magic            = GlobalBaseMagic + S1_OFFSET + i;
      S1_Data[i].atrValue         = 0.0;
      S1_Data[i].entryOpenPrice   = 0.0;
      S1_Data[i].upperLevel       = 0.0;
      S1_Data[i].lowerLevel       = 0.0;
      S1_Data[i].stopDistance     = 0.0;
      S1_Data[i].setupReady       = false;
      S1_Data[i].levelsCalculated = false;
      S1_Data[i].tradeTaken       = false;
      S1_Data[i].lastConfirmBar   = 0;
      S1_Data[i].lastDay          = -1;
      S1_Data[i].pendingDir       = 0;
      S1_Data[i].pendingDue       = 0;

      S1_Data[i].atrHandle = iATR(symbols[i], PERIOD_D1, S1_ATR_Period);
      if(S1_Data[i].atrHandle == INVALID_HANDLE)
        {
         PrintFormat("S1 error: failed to create the ATR handle for %s", symbols[i]);
         return false;
        }

      S1_Data[i].atrLongHandle = iATR(symbols[i], PERIOD_D1, S1_ATR_Long_Period);
      if(S1_Data[i].atrLongHandle == INVALID_HANDLE)
        {
         PrintFormat("S1 error: failed to create the long-term ATR handle for %s", symbols[i]);
         return false;
        }

      S1_Data[i].atrShortHandle = iATR(symbols[i], PERIOD_D1, S1_ATR_Short_Period);
      if(S1_Data[i].atrShortHandle == INVALID_HANDLE)
        {
         PrintFormat("S1 error: failed to create the short-term ATR handle for %s", symbols[i]);
         return false;
        }

      PrintFormat("S1 ready: %s | confirmation %s | magic %d", symbols[i], EnumToString(tfs[i]), S1_Data[i].magic);
     }

   PrintFormat("S1 settings: ATR %d x %.2f | stop x %.2f | regime short(%d) vs long(%d) factor %.2f | hours %s - %s, exit %s | direction %s | reverse %s | risk %.2f %s",
               S1_ATR_Period, S1_ATR_Multiplier, S1_Stop_ATR_Multiplier,
               S1_ATR_Short_Period, S1_ATR_Long_Period, S1_ATR_Regime_Factor,
               S1_Entry_Time, S1_Max_Entry_Time, S1_Exit_Time,
               S1_DirectionToString(), S1_Reverse_Mode ? "On" : "Off",
               S1_Fixed_Risk_Amount, AccountInfoString(ACCOUNT_CURRENCY));

   return true;
  }

void DeinitStrategy1()
  {
   for(int i = 0; i < ArraySize(S1_Data); i++)
     {
      if(S1_Data[i].atrHandle != INVALID_HANDLE)
         IndicatorRelease(S1_Data[i].atrHandle);
      if(S1_Data[i].atrLongHandle != INVALID_HANDLE)
         IndicatorRelease(S1_Data[i].atrLongHandle);
      if(S1_Data[i].atrShortHandle != INVALID_HANDLE)
         IndicatorRelease(S1_Data[i].atrShortHandle);
     }
  }

string S1_DirectionToString()
  {
   switch(S1_Trade_Direction)
     {
      case TRADE_BOTH:
         return "Both";
      case TRADE_LONG_ONLY:
         return "Long Only";
      case TRADE_SHORT_ONLY:
         return "Short Only";
      default:
         return "Unknown";
     }
  }

// ---------- S1 time helpers (server time + S1_HoursShift) ----------
datetime S1_StrategyTime()
  {
   return (datetime)((long)TimeCurrent() + (long)(S1_HoursShift * 3600.0));
  }

bool S1_IsAtOrPastTime(const int hour, const int minute)
  {
   MqlDateTime dt;
   TimeToStruct(S1_StrategyTime(), dt);
   return (dt.hour * 60 + dt.min >= hour * 60 + minute);
  }

bool S1_IsInEntryWindow()
  {
   MqlDateTime dt;
   TimeToStruct(S1_StrategyTime(), dt);
   int now = dt.hour * 60 + dt.min;

   return (now >= g_s1EntryHour * 60 + g_s1EntryMin &&
           now <  g_s1MaxEntryHour * 60 + g_s1MaxEntryMin);
  }

bool S1_IsNewDay(S1SymbolData &d)
  {
   MqlDateTime dt;
   TimeToStruct(S1_StrategyTime(), dt);

   if(dt.day != d.lastDay)
     {
      d.lastDay = dt.day;
      return true;
     }

   return false;
  }

void S1_ResetDailyState(S1SymbolData &d)
  {
   d.setupReady       = false;
   d.levelsCalculated = false;
   d.tradeTaken       = false;
   d.entryOpenPrice   = 0.0;
   d.pendingDir       = 0;
   d.pendingDue       = 0;
  }

// ---------- S1 position helpers ----------
bool S1_HasAnyPosition(const S1SymbolData &d)
  {
   return (GetPositionCount(d.symbol, d.magic) > 0);
  }

// Stale position from a previous day, or exit time reached.
bool S1_ShouldClosePosition(const S1SymbolData &d)
  {
   datetime posTime = 0;
   bool     found   = false;

   for(int i = PositionsTotal() - 1; i >= 0; i--)
     {
      ulong ticket = PositionGetTicket(i);
      if(ticket == 0)
         continue;

      if(PositionGetString(POSITION_SYMBOL) == d.symbol && PositionGetInteger(POSITION_MAGIC) == d.magic)
        {
         posTime = (datetime)PositionGetInteger(POSITION_TIME);
         found   = true;
         break;
        }
     }

   if(!found)
      return false;

   MqlDateTime posDt, nowDt;
   TimeToStruct((datetime)((long)posTime + (long)(S1_HoursShift * 3600.0)), posDt);
   TimeToStruct(S1_StrategyTime(), nowDt);

   if(posDt.day != nowDt.day || posDt.mon != nowDt.mon || posDt.year != nowDt.year)
     {
      PrintFormat("S1 %s: stale position from a previous day detected", d.symbol);
      return true;
     }

   int nowMins  = nowDt.hour * 60 + nowDt.min;
   int exitMins = g_s1ExitHour * 60 + g_s1ExitMin;

   if(nowMins >= exitMins)
     {
      PrintFormat("S1 %s: exit time reached", d.symbol);
      return true;
     }

   return false;
  }

// ---------- S1 levels, regime, signal ----------
bool S1_CalculateLevels(S1SymbolData &d)
  {
   double buf[];
   ArraySetAsSeries(buf, true);

   if(CopyBuffer(d.atrHandle, 0, 1, 1, buf) <= 0)
     {
      PrintFormat("S1 %s: failed to read the ATR buffer", d.symbol);
      return false;
     }

   d.atrValue = buf[0];

   MqlDateTime dt;
   TimeToStruct(S1_StrategyTime(), dt);
   dt.hour = g_s1EntryHour;
   dt.min  = g_s1EntryMin;
   dt.sec  = 0;

   int shift = iBarShift(d.symbol, PERIOD_M1, StructToTime(dt));
   if(shift < 0)
     {
      PrintFormat("S1 %s: could not locate the bar at entry time", d.symbol);
      return false;
     }

   d.entryOpenPrice = iOpen(d.symbol, PERIOD_M1, shift);
   if(d.entryOpenPrice <= 0.0)
     {
      PrintFormat("S1 %s: invalid open price at entry time", d.symbol);
      return false;
     }

   d.upperLevel   = d.entryOpenPrice + S1_ATR_Multiplier * d.atrValue;
   d.lowerLevel   = d.entryOpenPrice - S1_ATR_Multiplier * d.atrValue;
   d.stopDistance = S1_Stop_ATR_Multiplier * d.atrValue;

   return true;
  }

bool S1_PassesRegimeFilter(const S1SymbolData &d)
  {
   double longBuf[], shortBuf[];
   ArraySetAsSeries(longBuf, true);
   ArraySetAsSeries(shortBuf, true);

   if(CopyBuffer(d.atrLongHandle, 0, 1, 1, longBuf) <= 0)
      return false;

   if(CopyBuffer(d.atrShortHandle, 0, 1, 1, shortBuf) <= 0)
      return false;

   double shortAtr  = shortBuf[0];
   double threshold = longBuf[0] * S1_ATR_Regime_Factor;

   if(S1_Regime_Filter_Mode == REGIME_LOW_VOLATILITY)
      return (shortAtr < threshold);

   return (shortAtr > threshold);
  }

double S1_GetSignalPrice(const S1SymbolData &d)
  {
   if(S1_Use_Candle_Close)
      return iClose(d.symbol, d.confTf, 1);

   // Forex symbols have no "last" price, so fall back to Bid.
   double last = SymbolInfoDouble(d.symbol, SYMBOL_LAST);
   if(last <= 0.0)
      last = SymbolInfoDouble(d.symbol, SYMBOL_BID);

   return last;
  }

// ---------- S1 sizing and execution ----------
double S1_CalculateLotSize(const string sym, const double slDistance)
  {
   double tickVal  = SymbolInfoDouble(sym, SYMBOL_TRADE_TICK_VALUE);
   double tickSize = SymbolInfoDouble(sym, SYMBOL_TRADE_TICK_SIZE);
   double minLot   = SymbolInfoDouble(sym, SYMBOL_VOLUME_MIN);
   double maxLot   = SymbolInfoDouble(sym, SYMBOL_VOLUME_MAX);
   double lotStep  = SymbolInfoDouble(sym, SYMBOL_VOLUME_STEP);

   if(slDistance <= 0.0 || tickVal <= 0.0 || tickSize <= 0.0 || lotStep <= 0.0)
      return 0.0;

   double lots = (S1_Fixed_Risk_Amount / slDistance) * (tickSize / tickVal);

   lots = MathFloor(lots / lotStep) * lotStep;
   lots = MathMax(lots, minLot);
   lots = MathMin(lots, maxLot);

   return lots;
  }

void S1_Execute(S1SymbolData &d, const bool isLong, const string tag)
  {
   if(!g_meqCtl.s1)
      return;
   if(!CanOpenNewPosition())
      return;

   if(GlobalPreventOverlap && OtherStrategyHasPosition(d.symbol, isLong, S1_OFFSET))
     {
      PrintFormat("S1 %s: %s skipped, another strategy already holds this direction", d.symbol, tag);
      return;
     }

   double slDist = d.stopDistance;
   if(S1_EnableRandomization && S1_RandomSlJitterPoints != 0.0)
     {
      double point = SymbolInfoDouble(d.symbol, SYMBOL_POINT);
      slDist = d.stopDistance + SymRand(S1_RandomSlJitterPoints) * point;
      if(slDist <= 0.0)
         slDist = d.stopDistance;
     }

   int    digits = (int)SymbolInfoInteger(d.symbol, SYMBOL_DIGITS);
   double price  = SymbolInfoDouble(d.symbol, isLong ? SYMBOL_ASK : SYMBOL_BID);
   double sl     = NormalizeDouble(isLong ? price - slDist : price + slDist, digits);

   double lots = S1_CalculateLotSize(d.symbol, slDist);
   if(lots <= 0.0)
     {
      PrintFormat("S1 %s: could not size the trade, skipped", d.symbol);
      return;
     }

   if(S1_EnableRandomization && S1_RandomVolumeJitterPct != 0.0)
      lots = JitterVolume(d.symbol, lots, S1_RandomVolumeJitterPct);

   S1_Trade.SetExpertMagicNumber(d.magic);
   S1_Trade.SetTypeFillingBySymbol(d.symbol);

   string why = "";
   if(!SymbolCanTrade(d.symbol, isLong, why))
     {
      static datetime lastSkipLog = 0;
      if(TimeCurrent() - lastSkipLog >= 60)
        {
         lastSkipLog = TimeCurrent();
         PrintFormat("S1 %s: %s entry skipped (%s)", d.symbol, isLong ? "long" : "short", why);
        }
      return;
     }
   if(!MarketOrderLevelsOk(d.symbol, isLong, sl, 0.0, why))
     {
      static datetime lastLevelLog = 0;
      if(TimeCurrent() - lastLevelLog >= 60)
        {
         lastLevelLog = TimeCurrent();
         PrintFormat("S1 %s: %s entry skipped (%s)", d.symbol, isLong ? "long" : "short", why);
        }
      return;
     }

   // Price 0 = current ask/bid at send time; the quoted price is only used
   // for the stop and the log line.
   bool ok = isLong
             ? S1_Trade.Buy(lots, d.symbol, 0.0, sl, 0.0, GlobalComment)
             : S1_Trade.Sell(lots, d.symbol, 0.0, sl, 0.0, GlobalComment);

   if(ok)
     {
      PrintFormat("S1 %s: %s opened @ %s | Lots: %.2f | SL: %s | %s",
                  d.symbol, isLong ? "Long" : "Short", DoubleToString(price, digits), lots,
                  DoubleToString(sl, digits), tag);
      d.tradeTaken = true;
     }
   else
     {
      PrintFormat("S1 %s: %s order failed. Error: %d | retcode %d (%s)",
             d.symbol, isLong ? "Long" : "Short", GetLastError(),
             S1_Trade.ResultRetcode(), S1_Trade.ResultRetcodeDescription());
     }
  }

// Opens now, or schedules the entry when the random delay is on.
void S1_Open(S1SymbolData &d, const bool isLong, const string tag)
  {
   if(S1_EnableRandomization && S1_RandomDelayMaxSec > 0.0)
     {
      int delay = (int)((double)MathRand() / 32767.0 * S1_RandomDelayMaxSec);
      d.pendingDir = isLong ? 1 : -1;
      d.pendingDue = TimeCurrent() + delay;
      PrintFormat("S1 %s: randomizer deferred the %s signal %ds", d.symbol, isLong ? "long" : "short", delay);
      return;
     }

   S1_Execute(d, isLong, tag);
  }

void S1_EvaluateSignals(S1SymbolData &d)
  {
   double price = S1_GetSignalPrice(d);
   if(price <= 0.0)
      return;

   bool upperHit = (price >= d.upperLevel);
   bool lowerHit = (price <= d.lowerLevel);

   if(S1_Reverse_Mode)
     {
      // Fade: upper hit = short, lower hit = long.
      if(upperHit && (S1_Trade_Direction == TRADE_BOTH || S1_Trade_Direction == TRADE_SHORT_ONLY))
         S1_Open(d, false, "Fade Short");

      if(lowerHit && (S1_Trade_Direction == TRADE_BOTH || S1_Trade_Direction == TRADE_LONG_ONLY))
         S1_Open(d, true, "Fade Long");
     }
   else
     {
      if(upperHit && (S1_Trade_Direction == TRADE_BOTH || S1_Trade_Direction == TRADE_LONG_ONLY))
         S1_Open(d, true, "Breakout Long");

      if(lowerHit && (S1_Trade_Direction == TRADE_BOTH || S1_Trade_Direction == TRADE_SHORT_ONLY))
         S1_Open(d, false, "Breakout Short");
     }
  }

void S1_Process(S1SymbolData &d)
  {
   // Exit handling for an open position.
   if(S1_HasAnyPosition(d))
     {
      if(S1_MaxProfitAmount > 0.0)
        {
         for(int i = PositionsTotal() - 1; i >= 0; i--)
           {
            ulong ticket = PositionGetTicket(i);
            if(ticket == 0)
               continue;
            if(PositionGetString(POSITION_SYMBOL) != d.symbol)
               continue;
            if(PositionGetInteger(POSITION_MAGIC) != d.magic)
               continue;
            double net = PositionGetDouble(POSITION_PROFIT) + PositionGetDouble(POSITION_SWAP);
            if(net >= S1_MaxProfitAmount)
              {
               PrintFormat("S1 %s: profit cap hit on #%I64u: %.2f >= %.2f. Closing.",
                           d.symbol, ticket, net, S1_MaxProfitAmount);
               ClosePositionsWithTrade(S1_Trade, d.symbol, d.magic);
               return;
              }
           }
        }
      if(S1_ShouldClosePosition(d))
        {
         ClosePositionsWithTrade(S1_Trade, d.symbol, d.magic);
         return;
        }
     }

   // Deferred randomizer entry: fires once its delay has elapsed, independent of the candle gate.
   if(d.pendingDir != 0 && TimeCurrent() >= d.pendingDue)
     {
      int dir = d.pendingDir;
      d.pendingDir = 0;
      d.pendingDue = 0;

      bool inWindow = S1_IsInEntryWindow();
      if(inWindow && !S1_HasAnyPosition(d))
         S1_Execute(d, dir > 0, dir > 0 ? "Deferred Long" : "Deferred Short");
      else
         PrintFormat("S1 %s: randomizer deferred entry dropped (window closed or position open)", d.symbol);
     }

   if(S1_Use_Candle_Close)
     {
      datetime barTime = iTime(d.symbol, d.confTf, 0);
      if(barTime == d.lastConfirmBar)
         return;
      d.lastConfirmBar = barTime;
     }

   if(S1_IsNewDay(d))
     {
      S1_ResetDailyState(d);
      PrintFormat("S1 %s: new trading day detected", d.symbol);
     }

   if(!d.levelsCalculated && S1_IsAtOrPastTime(g_s1EntryHour, g_s1EntryMin))
     {
      if(S1_CalculateLevels(d))
        {
         d.levelsCalculated = true;
         d.setupReady       = true;
         PrintFormat("S1 %s: levels calculated | Open: %s | Upper: %s | Lower: %s | Stop: %s%s",
                     d.symbol, DoubleToString(d.entryOpenPrice, _Digits), DoubleToString(d.upperLevel, _Digits),
                     DoubleToString(d.lowerLevel, _Digits), DoubleToString(d.stopDistance, _Digits),
                     S1_Reverse_Mode ? " | reverse mode active" : "");
        }
     }

   if(!d.setupReady || d.tradeTaken || !d.levelsCalculated)
      return;

   if(!S1_IsInEntryWindow())
      return;

   if(S1_HasAnyPosition(d))
      return;

   // A deferred entry is already waiting: do not stack another signal.
   if(d.pendingDir != 0)
      return;

   if(!S1_PassesRegimeFilter(d))
      return;

   S1_EvaluateSignals(d);
  }

void OnTickStrategy1()
  {
   for(int i = 0; i < ArraySize(S1_Data); i++)
      S1_Process(S1_Data[i]);
  }

//+------------------------------------------------------------------+
//| S2A: MULTI-SYMBOL CRYPTO EDGE                                      |
//+------------------------------------------------------------------+
bool InitStrategy2A()
  {
   string symbols[];
   int n = ParseSymbols(S2A_Symbols, symbols, S2_DEFAULT_ALL);
   if(n == 0)
     {
      Print("S2A error: no symbols configured.");
      return false;
     }

   ENUM_TIMEFRAMES tfs[];
   ParseTimeframes(S2A_Timeframes, tfs, n);

   ArrayResize(S2A_Data, n);

   for(int i = 0; i < n; i++)
     {
      SymbolSelect(symbols[i], true);

      S2A_Data[i].symbol           = symbols[i];
      S2A_Data[i].tf               = tfs[i];
      S2A_Data[i].magic            = GlobalBaseMagic + S2A_OFFSET + i;
      S2A_Data[i].lastClosedCandle = 0;
      S2A_Data[i].pendingDir       = 0;
      S2A_Data[i].pendingDue       = 0;
      S2A_Data[i].pendingPrice     = 0.0;

      ResetLastError();
      S2A_Data[i].emaHandle = iMA(symbols[i], tfs[i], S2A_EMALength, 0, MODE_EMA, PRICE_CLOSE);

      if(S2A_Data[i].emaHandle == INVALID_HANDLE)
        {
         int err = GetLastError();
         string reason = (err == 4302) ? "the symbol needs to be added to the Market Watch" : StringFormat("error code %d", err);
         PrintFormat("S2A error: failed to create the EMA handle for %s / %s (%s)", symbols[i], EnumToString(tfs[i]), reason);
         return false;
        }

      PrintFormat("S2A ready: %s | %s | magic %d", symbols[i], EnumToString(tfs[i]), S2A_Data[i].magic);
     }

   return true;
  }

void DeinitStrategy2A()
  {
   for(int i = 0; i < ArraySize(S2A_Data); i++)
     {
      if(S2A_Data[i].emaHandle != INVALID_HANDLE)
         IndicatorRelease(S2A_Data[i].emaHandle);
     }
  }

// EMA value at a shift of the symbol's timeframe. Returns false when it is not available yet.
bool S2A_GetEMA(const S2SymbolData &d, const int shift, double &value)
  {
   double buf[];
   ArraySetAsSeries(buf, true);

   if(CopyBuffer(d.emaHandle, 0, 0, 10, buf) != 10)
      return false;

   value = buf[shift];
   return true;
  }

// Lowest low of the last confirmed candles.
double S2A_CalculateStopLoss(const S2SymbolData &d)
  {
   MqlRates rates[];
   ArraySetAsSeries(rates, true);

   if(CopyRates(d.symbol, d.tf, 0, S2A_StopLossBars + 1, rates) < S2A_StopLossBars + 1)
      return 0.0;

   double lowestLow = rates[1].low;
   for(int i = 1; i <= S2A_StopLossBars; i++)
      lowestLow = MathMin(lowestLow, rates[i].low);

   return lowestLow;
  }

double S2A_CalculatePositionSize(const string sym, const double entryPrice, const double stopLoss)
  {
   if(stopLoss >= entryPrice)
     {
      PrintFormat("S2A %s: stop loss must be below the entry price", sym);
      return 0.0;
     }

   double riskAmount = RiskBase() * S2A_RiskPerTrade;
   double tickValue  = SymbolInfoDouble(sym, SYMBOL_TRADE_TICK_VALUE);
   double tickSize   = SymbolInfoDouble(sym, SYMBOL_TRADE_TICK_SIZE);

   if(tickValue == 0.0 || tickSize == 0.0)
     {
      PrintFormat("S2A %s: invalid tick value or size", sym);
      return 0.0;
     }

   double distance = entryPrice - stopLoss;
   double ticks    = distance / tickSize;
   double size     = riskAmount / (ticks * tickValue);

   double minLot  = SymbolInfoDouble(sym, SYMBOL_VOLUME_MIN);
   double maxLot  = SymbolInfoDouble(sym, SYMBOL_VOLUME_MAX);
   double stepLot = SymbolInfoDouble(sym, SYMBOL_VOLUME_STEP);

   if(stepLot <= 0.0)
      return 0.0;

   size = MathFloor(size / stepLot) * stepLot;
   size = MathMax(minLot, MathMin(maxLot, size));

   return size;
  }

void S2A_ExecuteEntry(S2SymbolData &d, const double refPrice)
  {
   if(!g_meqCtl.s2a)
      return;
   if(!CanOpenNewPosition())
      return;

   if(GlobalPreventOverlap && OtherStrategyHasPosition(d.symbol, true, S2A_OFFSET))
     {
      PrintFormat("S2A %s: entry skipped, another strategy already holds a long", d.symbol);
      return;
     }

   double stopLoss = S2A_CalculateStopLoss(d);
   if(stopLoss <= 0.0)
     {
      PrintFormat("S2A %s: invalid stop loss calculation", d.symbol);
      return;
     }

   if(S2A_EnableRandomization && S2A_RandomSlJitterPoints != 0.0)
     {
      double pt = SymbolInfoDouble(d.symbol, SYMBOL_POINT);
      if(pt > 0.0)
        {
         double jsl = stopLoss + SymRand(S2A_RandomSlJitterPoints) * pt;
         if(jsl > 0.0 && jsl < refPrice)
            stopLoss = jsl;
        }
     }

   double size = S2A_CalculatePositionSize(d.symbol, refPrice, stopLoss);
   if(size <= 0.0)
     {
      PrintFormat("S2A %s: invalid position size calculation", d.symbol);
      return;
     }

   if(S2A_EnableRandomization && S2A_RandomVolumeJitterPct != 0.0)
      size = JitterVolume(d.symbol, size, S2A_RandomVolumeJitterPct);

   int digits = (int)SymbolInfoInteger(d.symbol, SYMBOL_DIGITS);
   stopLoss = NormalizeDouble(stopLoss, digits);

   PrintFormat("S2A %s: placing trade | Lots=%.2f | Ref=%s | Stop=%s",
               d.symbol, size, DoubleToString(refPrice, digits), DoubleToString(stopLoss, digits));

   S2A_Trade.SetExpertMagicNumber(d.magic);
   S2A_Trade.SetTypeFillingBySymbol(d.symbol);

   string why = "";
   if(!SymbolCanTrade(d.symbol, true, why))
     {
      PrintFormat("S2A %s: entry skipped (%s)", d.symbol, why);
      return;
     }
   if(!MarketOrderLevelsOk(d.symbol, true, stopLoss, 0.0, why))
     {
      PrintFormat("S2A %s: entry skipped (%s)", d.symbol, why);
      return;
     }

   // Price 0 = current market price. The confirmed close is only used for the sizing above.
   if(!S2A_Trade.Buy(size, d.symbol, 0.0, stopLoss, 0.0, GlobalComment))
      PrintFormat("S2A %s: trade failed. Error: %d | retcode %d (%s)",
                 d.symbol, GetLastError(), S2A_Trade.ResultRetcode(), S2A_Trade.ResultRetcodeDescription());
  }

bool S2A_HasRoom(const S2SymbolData &d)
  {
   int count = GetPositionCount(d.symbol, d.magic);

   if(S2A_AllowMultiplePositions)
      return (count < S2A_MaxPositions);

   return (count == 0);
  }

void S2A_CheckForEntry(S2SymbolData &d)
  {
   MqlRates rates[];
   ArraySetAsSeries(rates, true);

   int needed = S2A_BreakoutBars + 3;
   if(CopyRates(d.symbol, d.tf, 0, needed, rates) < needed)
      return;

   // Highest high of the candles before the last closed candle.
   double highestHigh = rates[2].high;
   for(int i = 3; i <= S2A_BreakoutBars + 1; i++)
      highestHigh = MathMax(highestHigh, rates[i].high);

   double confirmedClose = rates[1].close;

   double confirmedEMA = 0.0;
   if(!S2A_GetEMA(d, 1, confirmedEMA))
     {
      PrintFormat("S2A %s: EMA not available yet, entry check skipped", d.symbol);
      return;
     }

   bool entryCondition = (confirmedClose > highestHigh) && (confirmedClose > confirmedEMA);
   if(!entryCondition)
      return;

   if(S2A_EnableRandomization && S2A_RandomDelayMaxSec > 0.0)
     {
      int delay = (int)((double)MathRand() / 32767.0 * S2A_RandomDelayMaxSec);
      d.pendingDir   = 1;
      d.pendingDue   = TimeCurrent() + delay;
      d.pendingPrice = confirmedClose;
      PrintFormat("S2A %s: randomizer deferred the entry %ds", d.symbol, delay);
      return;
     }

   S2A_ExecuteEntry(d, confirmedClose);
  }

// Trails the stop up to the lowest low of the last confirmed candles. Never moves it down.
void S2A_ManagePositions(const S2SymbolData &d)
  {
   double newStopLoss = S2A_CalculateStopLoss(d);
   if(newStopLoss <= 0.0)
      return;

   int digits = (int)SymbolInfoInteger(d.symbol, SYMBOL_DIGITS);
   newStopLoss = NormalizeDouble(newStopLoss, digits);

   for(int i = PositionsTotal() - 1; i >= 0; i--)
     {
      ulong ticket = PositionGetTicket(i);
      if(ticket == 0)
         continue;

      if(PositionGetString(POSITION_SYMBOL) != d.symbol)
         continue;
      if(PositionGetInteger(POSITION_MAGIC) != d.magic)
         continue;

      double currentStopLoss = PositionGetDouble(POSITION_SL);
      if(newStopLoss > currentStopLoss)
        {
         string why = "";
         if(!PositionStopOk(d.symbol, true, newStopLoss, why))
            continue;
         S2A_Trade.SetTypeFillingBySymbol(d.symbol);
         S2A_Trade.PositionModify(ticket, newStopLoss, 0.0);
        }
     }
  }

void S2A_Process(S2SymbolData &d)
  {
   // Deferred randomizer entry: re-checks room when it fires.
   if(d.pendingDir != 0 && TimeCurrent() >= d.pendingDue)
     {
      double refPrice = d.pendingPrice;
      d.pendingDir   = 0;
      d.pendingDue   = 0;

      if(S2A_HasRoom(d))
         S2A_ExecuteEntry(d, refPrice);
      else
         PrintFormat("S2A %s: randomizer deferred entry dropped (no room)", d.symbol);
     }

   // Everything else runs once per closed candle.
   if(!IsNewBar(d.symbol, d.tf, d.lastClosedCandle))
      return;

   if(S2A_HasRoom(d))
      S2A_CheckForEntry(d);

   if(GetPositionCount(d.symbol, d.magic) > 0)
      S2A_ManagePositions(d);
  }

void OnTickStrategy2A()
  {
   for(int i = 0; i < ArraySize(S2A_Data); i++)
      S2A_Process(S2A_Data[i]);
  }

//+------------------------------------------------------------------+
//+------------------------------------------------------------------+


//| S2B: MULTI-SYMBOL CRYPTO EDGE                                      |
//+------------------------------------------------------------------+
bool InitStrategy2B()
  {
   string symbols[];
   int n = ParseSymbols(S2B_Symbols, symbols, S2_DEFAULT_ALL);
   if(n == 0)
     {
      Print("S2B error: no symbols configured.");
      return false;
     }

   ENUM_TIMEFRAMES tfs[];
   ParseTimeframes(S2B_Timeframes, tfs, n);

   ArrayResize(S2B_Data, n);

   for(int i = 0; i < n; i++)
     {
      SymbolSelect(symbols[i], true);

      S2B_Data[i].symbol           = symbols[i];
      S2B_Data[i].tf               = tfs[i];
      S2B_Data[i].magic            = GlobalBaseMagic + S2B_OFFSET + i;
      S2B_Data[i].lastClosedCandle = 0;
      S2B_Data[i].pendingDir       = 0;
      S2B_Data[i].pendingDue       = 0;
      S2B_Data[i].pendingPrice     = 0.0;

      ResetLastError();
      S2B_Data[i].emaHandle = iMA(symbols[i], tfs[i], S2B_EMALength, 0, MODE_EMA, PRICE_CLOSE);

      if(S2B_Data[i].emaHandle == INVALID_HANDLE)
        {
         int err = GetLastError();
         string reason = (err == 4302) ? "the symbol needs to be added to the Market Watch" : StringFormat("error code %d", err);
         PrintFormat("S2B error: failed to create the EMA handle for %s / %s (%s)", symbols[i], EnumToString(tfs[i]), reason);
         return false;
        }

      PrintFormat("S2B ready: %s | %s | magic %d", symbols[i], EnumToString(tfs[i]), S2B_Data[i].magic);
     }

   return true;
  }

void DeinitStrategy2B()
  {
   for(int i = 0; i < ArraySize(S2B_Data); i++)
     {
      if(S2B_Data[i].emaHandle != INVALID_HANDLE)
         IndicatorRelease(S2B_Data[i].emaHandle);
     }
  }

// EMA value at a shift of the symbol's timeframe. Returns false when it is not available yet.
bool S2B_GetEMA(const S2SymbolData &d, const int shift, double &value)
  {
   double buf[];
   ArraySetAsSeries(buf, true);

   if(CopyBuffer(d.emaHandle, 0, 0, 10, buf) != 10)
      return false;

   value = buf[shift];
   return true;
  }

// Lowest low of the last confirmed candles.
double S2B_CalculateStopLoss(const S2SymbolData &d)
  {
   MqlRates rates[];
   ArraySetAsSeries(rates, true);

   if(CopyRates(d.symbol, d.tf, 0, S2B_StopLossBars + 1, rates) < S2B_StopLossBars + 1)
      return 0.0;

   double lowestLow = rates[1].low;
   for(int i = 1; i <= S2B_StopLossBars; i++)
      lowestLow = MathMin(lowestLow, rates[i].low);

   return lowestLow;
  }

double S2B_CalculatePositionSize(const string sym, const double entryPrice, const double stopLoss)
  {
   if(stopLoss >= entryPrice)
     {
      PrintFormat("S2B %s: stop loss must be below the entry price", sym);
      return 0.0;
     }

   double riskAmount = RiskBase() * S2B_RiskPerTrade;
   double tickValue  = SymbolInfoDouble(sym, SYMBOL_TRADE_TICK_VALUE);
   double tickSize   = SymbolInfoDouble(sym, SYMBOL_TRADE_TICK_SIZE);

   if(tickValue == 0.0 || tickSize == 0.0)
     {
      PrintFormat("S2B %s: invalid tick value or size", sym);
      return 0.0;
     }

   double distance = entryPrice - stopLoss;
   double ticks    = distance / tickSize;
   double size     = riskAmount / (ticks * tickValue);

   double minLot  = SymbolInfoDouble(sym, SYMBOL_VOLUME_MIN);
   double maxLot  = SymbolInfoDouble(sym, SYMBOL_VOLUME_MAX);
   double stepLot = SymbolInfoDouble(sym, SYMBOL_VOLUME_STEP);

   if(stepLot <= 0.0)
      return 0.0;

   size = MathFloor(size / stepLot) * stepLot;
   size = MathMax(minLot, MathMin(maxLot, size));

   return size;
  }

void S2B_ExecuteEntry(S2SymbolData &d, const double refPrice)
  {
   if(!g_meqCtl.s2b)
      return;
   if(!CanOpenNewPosition())
      return;

   if(GlobalPreventOverlap && OtherStrategyHasPosition(d.symbol, true, S2B_OFFSET))
     {
      PrintFormat("S2B %s: entry skipped, another strategy already holds a long", d.symbol);
      return;
     }

   double stopLoss = S2B_CalculateStopLoss(d);
   if(stopLoss <= 0.0)
     {
      PrintFormat("S2B %s: invalid stop loss calculation", d.symbol);
      return;
     }

   if(S2B_EnableRandomization && S2B_RandomSlJitterPoints != 0.0)
     {
      double pt = SymbolInfoDouble(d.symbol, SYMBOL_POINT);
      if(pt > 0.0)
        {
         double jsl = stopLoss + SymRand(S2B_RandomSlJitterPoints) * pt;
         if(jsl > 0.0 && jsl < refPrice)
            stopLoss = jsl;
        }
     }

   double size = S2B_CalculatePositionSize(d.symbol, refPrice, stopLoss);
   if(size <= 0.0)
     {
      PrintFormat("S2B %s: invalid position size calculation", d.symbol);
      return;
     }

   if(S2B_EnableRandomization && S2B_RandomVolumeJitterPct != 0.0)
      size = JitterVolume(d.symbol, size, S2B_RandomVolumeJitterPct);

   int digits = (int)SymbolInfoInteger(d.symbol, SYMBOL_DIGITS);
   stopLoss = NormalizeDouble(stopLoss, digits);

   PrintFormat("S2B %s: placing trade | Lots=%.2f | Ref=%s | Stop=%s",
               d.symbol, size, DoubleToString(refPrice, digits), DoubleToString(stopLoss, digits));

   S2B_Trade.SetExpertMagicNumber(d.magic);
   S2B_Trade.SetTypeFillingBySymbol(d.symbol);

   string why = "";
   if(!SymbolCanTrade(d.symbol, true, why))
     {
      PrintFormat("S2B %s: entry skipped (%s)", d.symbol, why);
      return;
     }
   if(!MarketOrderLevelsOk(d.symbol, true, stopLoss, 0.0, why))
     {
      PrintFormat("S2B %s: entry skipped (%s)", d.symbol, why);
      return;
     }

   // Price 0 = current market price. The confirmed close is only used for the sizing above.
   if(!S2B_Trade.Buy(size, d.symbol, 0.0, stopLoss, 0.0, GlobalComment))
      PrintFormat("S2B %s: trade failed. Error: %d | retcode %d (%s)",
                 d.symbol, GetLastError(), S2B_Trade.ResultRetcode(), S2B_Trade.ResultRetcodeDescription());
  }

bool S2B_HasRoom(const S2SymbolData &d)
  {
   int count = GetPositionCount(d.symbol, d.magic);

   if(S2B_AllowMultiplePositions)
      return (count < S2B_MaxPositions);

   return (count == 0);
  }

void S2B_CheckForEntry(S2SymbolData &d)
  {
   MqlRates rates[];
   ArraySetAsSeries(rates, true);

   int needed = S2B_BreakoutBars + 3;
   if(CopyRates(d.symbol, d.tf, 0, needed, rates) < needed)
      return;

   // Highest high of the candles before the last closed candle.
   double highestHigh = rates[2].high;
   for(int i = 3; i <= S2B_BreakoutBars + 1; i++)
      highestHigh = MathMax(highestHigh, rates[i].high);

   double confirmedClose = rates[1].close;

   double confirmedEMA = 0.0;
   if(!S2B_GetEMA(d, 1, confirmedEMA))
     {
      PrintFormat("S2B %s: EMA not available yet, entry check skipped", d.symbol);
      return;
     }

   bool entryCondition = (confirmedClose > highestHigh) && (confirmedClose > confirmedEMA);
   if(!entryCondition)
      return;

   if(S2B_EnableRandomization && S2B_RandomDelayMaxSec > 0.0)
     {
      int delay = (int)((double)MathRand() / 32767.0 * S2B_RandomDelayMaxSec);
      d.pendingDir   = 1;
      d.pendingDue   = TimeCurrent() + delay;
      d.pendingPrice = confirmedClose;
      PrintFormat("S2B %s: randomizer deferred the entry %ds", d.symbol, delay);
      return;
     }

   S2B_ExecuteEntry(d, confirmedClose);
  }

// Trails the stop up to the lowest low of the last confirmed candles. Never moves it down.
void S2B_ManagePositions(const S2SymbolData &d)
  {
   double newStopLoss = S2B_CalculateStopLoss(d);
   if(newStopLoss <= 0.0)
      return;

   int digits = (int)SymbolInfoInteger(d.symbol, SYMBOL_DIGITS);
   newStopLoss = NormalizeDouble(newStopLoss, digits);

   for(int i = PositionsTotal() - 1; i >= 0; i--)
     {
      ulong ticket = PositionGetTicket(i);
      if(ticket == 0)
         continue;

      if(PositionGetString(POSITION_SYMBOL) != d.symbol)
         continue;
      if(PositionGetInteger(POSITION_MAGIC) != d.magic)
         continue;

      double currentStopLoss = PositionGetDouble(POSITION_SL);
      if(newStopLoss > currentStopLoss)
        {
         string why = "";
         if(!PositionStopOk(d.symbol, true, newStopLoss, why))
            continue;
         S2B_Trade.SetTypeFillingBySymbol(d.symbol);
         S2B_Trade.PositionModify(ticket, newStopLoss, 0.0);
        }
     }
  }

void S2B_Process(S2SymbolData &d)
  {
   // Deferred randomizer entry: re-checks room when it fires.
   if(d.pendingDir != 0 && TimeCurrent() >= d.pendingDue)
     {
      double refPrice = d.pendingPrice;
      d.pendingDir   = 0;
      d.pendingDue   = 0;

      if(S2B_HasRoom(d))
         S2B_ExecuteEntry(d, refPrice);
      else
         PrintFormat("S2B %s: randomizer deferred entry dropped (no room)", d.symbol);
     }

   // Everything else runs once per closed candle.
   if(!IsNewBar(d.symbol, d.tf, d.lastClosedCandle))
      return;

   if(S2B_HasRoom(d))
      S2B_CheckForEntry(d);

   if(GetPositionCount(d.symbol, d.magic) > 0)
      S2B_ManagePositions(d);
  }

void OnTickStrategy2B()
  {
   for(int i = 0; i < ArraySize(S2B_Data); i++)
      S2B_Process(S2B_Data[i]);
  }

//+------------------------------------------------------------------+
//+------------------------------------------------------------------+

//| S3: MULTI-SYMBOL TREND PULLBACK (LONG ONLY)                      |
//| Daily EMA regime + EMA pullback entry + ATR stop + ATR trail.    |
//| Driven by OnTimer so one chart controls every symbol.            |

//+------------------------------------------------------------------+
//| S3B: MULTI-SYMBOL TREND PULLBACK B (LONG ONLY)                    |
//| Same rules as S3, fully independent symbols/timeframe/risk.        |
//+------------------------------------------------------------------+
bool InitStrategy3B()
  {
   string t = S3B_Symbols;
   StringReplace(t, ",", "|");
   string symbols[];
   int n = ParseSymbols(t, symbols, S3_DEFAULT_ALL);
   if(n == 0)
     {
      Print("S3B error: no symbols configured.");
      return false;
     }
   if(S3B_EntryEMA_Period < 1 || S3B_DailyFastEMA < 1 || S3B_DailySlowEMA < 1 || S3B_ATR_Period < 1)
     {
      Print("S3B error: indicator periods must be >= 1.");
      return false;
     }
   ArrayResize(S3B_Data, n);
   for(int i = 0; i < n; i++)
     {
      SymbolSelect(symbols[i], true);
      S3B_Data[i].symbol      = symbols[i];
      S3B_Data[i].magic       = GlobalBaseMagic + S3B_OFFSET + i;
      S3B_Data[i].lastBarTime = 0;
      S3B_Data[i].pendingDir  = 0;
      S3B_Data[i].pendingDue  = 0;
      S3B_Data[i].entryEmaHandle = iMA(symbols[i], S3B_EntryTF, S3B_EntryEMA_Period, 0, MODE_EMA, PRICE_CLOSE);
      S3B_Data[i].dailyFastHandle = iMA(symbols[i], PERIOD_D1, S3B_DailyFastEMA, 0, MODE_EMA, PRICE_CLOSE);
      S3B_Data[i].dailySlowHandle = iMA(symbols[i], PERIOD_D1, S3B_DailySlowEMA, 0, MODE_EMA, PRICE_CLOSE);
      S3B_Data[i].atrHandle = iATR(symbols[i], S3B_EntryTF, S3B_ATR_Period);
      if(S3B_Data[i].entryEmaHandle == INVALID_HANDLE ||
         S3B_Data[i].dailyFastHandle == INVALID_HANDLE ||
         S3B_Data[i].dailySlowHandle == INVALID_HANDLE ||
         S3B_Data[i].atrHandle == INVALID_HANDLE)
        {
         PrintFormat("S3B error: indicator handle creation failed for %s", symbols[i]);
         return false;
        }
      PrintFormat("S3B ready: %s | %s | magic %d", symbols[i], EnumToString(S3B_EntryTF), S3B_Data[i].magic);
     }
   PrintFormat("S3B settings: entry EMA %d on %s | daily EMA %d/%d | ATR %d | SL x%.2f | trail x%.2f | risk %.2f%%",
               S3B_EntryEMA_Period, EnumToString(S3B_EntryTF), S3B_DailyFastEMA, S3B_DailySlowEMA,
               S3B_ATR_Period, S3B_ATR_SL_Multiplier, S3B_ATR_Trail_Multiplier, S3B_RiskPercent);
   return true;
  }

void DeinitStrategy3B()
  {
   for(int i = 0; i < ArraySize(S3B_Data); i++)
     {
      if(S3B_Data[i].entryEmaHandle != INVALID_HANDLE)
         IndicatorRelease(S3B_Data[i].entryEmaHandle);
      if(S3B_Data[i].dailyFastHandle != INVALID_HANDLE)
         IndicatorRelease(S3B_Data[i].dailyFastHandle);
      if(S3B_Data[i].dailySlowHandle != INVALID_HANDLE)
         IndicatorRelease(S3B_Data[i].dailySlowHandle);
      if(S3B_Data[i].atrHandle != INVALID_HANDLE)
         IndicatorRelease(S3B_Data[i].atrHandle);
     }
  }

double S3B_Value(const int handle, const int shift)
  {
   double buffer[];
   ArraySetAsSeries(buffer, true);
   if(CopyBuffer(handle, 0, shift, 1, buffer) <= 0)
      return 0.0;
   return buffer[0];
  }

double S3B_Normalize(const string sym, const double price)
  {
   int digits = (int)SymbolInfoInteger(sym, SYMBOL_DIGITS);
   return NormalizeDouble(price, digits);
  }

bool S3B_HasOpenPosition(const string sym, const int magic)
  {
   return (GetPositionCount(sym, magic) > 0);
  }

bool S3B_DailyBullishRegime(S3SymbolData &d)
  {
   string sym = d.symbol;
   double dailyClose    = iClose(sym, PERIOD_D1, 1);
   double previousClose = iClose(sym, PERIOD_D1, 2);
   double fastEMA       = S3B_Value(d.dailyFastHandle, 1);
   double slowEMA       = S3B_Value(d.dailySlowHandle, 1);
   if(dailyClose <= 0 || previousClose <= 0 || fastEMA <= 0 || slowEMA <= 0)
      return false;
   return (fastEMA > slowEMA && dailyClose > fastEMA && dailyClose > previousClose);
  }

bool S3B_PullbackLongSignal(S3SymbolData &d)
  {
   string sym = d.symbol;
   double ema1 = S3B_Value(d.entryEmaHandle, 1);
   double ema2 = S3B_Value(d.entryEmaHandle, 2);
   if(ema1 <= 0 || ema2 <= 0)
      return false;
   double open1  = iOpen(sym, S3B_EntryTF, 1);
   double close1 = iClose(sym, S3B_EntryTF, 1);
   double high2  = iHigh(sym, S3B_EntryTF, 2);
   double low1   = iLow(sym, S3B_EntryTF, 1);
   double close2 = iClose(sym, S3B_EntryTF, 2);
   bool pulledBackIntoEMA = (low1 <= ema1 || close2 < ema2);
   bool bullishClose      = (close1 > open1);
   bool closeAboveEMA     = (close1 > ema1);
   bool momentumBreak     = (close1 > high2);
   return (pulledBackIntoEMA && bullishClose && closeAboveEMA && momentumBreak);
  }

double S3B_CalculateLotSize(const string sym, const double slDistance)
  {
   double riskMoney = AccountInfoDouble(ACCOUNT_BALANCE) * S3B_RiskPercent / 100.0;
   return LotsForRisk(sym, riskMoney, slDistance, true);
  }

void S3B_ExecuteEntry(S3SymbolData &d, const double refPrice)
  {
   string sym = d.symbol;
   if(!g_meqCtl.s3b)
      return;
   if(!CanOpenNewPosition())
      return;
   if(GlobalPreventOverlap && OtherStrategyHasPosition(sym, true, S3B_OFFSET))
     {
      PrintFormat("S3B %s: entry skipped, another strategy already holds this direction", sym);
      return;
     }
   double atr = S3B_Value(d.atrHandle, 1);
   if(atr <= 0)
      return;
   double slDistance = atr * S3B_ATR_SL_Multiplier;
   if(S3B_EnableRandomization && S3B_RandomSlJitterPoints != 0.0)
     {
      double pt = SymbolInfoDouble(sym, SYMBOL_POINT);
      if(pt > 0.0)
        {
         double jittered = slDistance + SymRand(S3B_RandomSlJitterPoints) * pt;
         if(jittered > 0.0)
            slDistance = jittered;
        }
     }
   double ask = SymbolInfoDouble(sym, SYMBOL_ASK);
   if(ask <= 0)
      return;
   double stopLoss = S3B_Normalize(sym, ask - slDistance);
   double lots = S3B_CalculateLotSize(sym, slDistance);
   if(lots <= 0)
      return;
   if(S3B_EnableRandomization && S3B_RandomVolumeJitterPct != 0.0)
      lots = JitterVolume(sym, lots, S3B_RandomVolumeJitterPct);
   S3B_Trade.SetExpertMagicNumber(d.magic);
   S3B_Trade.SetTypeFillingBySymbol(sym);

   string why = "";
   if(!SymbolCanTrade(sym, true, why))
     {
      PrintFormat("S3B %s: entry skipped (%s)", sym, why);
      return;
     }
   if(!MarketOrderLevelsOk(sym, true, stopLoss, 0.0, why))
     {
      PrintFormat("S3B %s: entry skipped (%s)", sym, why);
      return;
     }

   // Price 0 = current ask at send time.
   bool ok = S3B_Trade.Buy(lots, sym, 0.0, stopLoss, 0.0, GlobalComment);
   if(ok)
      PrintFormat("S3B %s: Long entry opened | Lots: %.2f | SL: %s", sym, lots,
                  DoubleToString(stopLoss, (int)SymbolInfoInteger(sym, SYMBOL_DIGITS)));
   else
      PrintFormat("S3B %s: entry failed. Error: %d | retcode %d (%s)",
                  sym, GetLastError(), S3B_Trade.ResultRetcode(), S3B_Trade.ResultRetcodeDescription());
  }

void S3B_CheckEntry(S3SymbolData &d)
  {
   string sym = d.symbol;
   if(S3B_HasOpenPosition(sym, d.magic))
      return;
   if(PortfolioGuardBlocked())
      return;
   if(DirectionCapBlocked(sym, true, "S3B"))
      return;
   if(!S3B_DailyBullishRegime(d))
      return;
   if(!S3B_PullbackLongSignal(d))
      return;
   double refPrice = iClose(sym, S3B_EntryTF, 1);
   if(refPrice <= 0)
      return;
   if(S3B_EnableRandomization && S3B_RandomDelayMaxSec > 0.0)
     {
      int delay = (int)((double)MathRand() / 32767.0 * S3B_RandomDelayMaxSec);
      d.pendingDir = 1;
      d.pendingDue = TimeCurrent() + delay;
      d.pendingPrice = refPrice;
      PrintFormat("S3B %s: randomizer deferred the entry %ds", sym, delay);
      return;
     }
   S3B_ExecuteEntry(d, refPrice);
  }

void S3B_ManageTrailingStop(S3SymbolData &d)
  {
   string sym = d.symbol;
   int magic = d.magic;
   double atr = S3B_Value(d.atrHandle, 1);
   if(atr <= 0)
      return;
   double bid = SymbolInfoDouble(sym, SYMBOL_BID);
   if(bid <= 0)
      return;
   double newSL = S3B_Normalize(sym, bid - atr * S3B_ATR_Trail_Multiplier);
   for(int i = PositionsTotal() - 1; i >= 0; i--)
     {
      ulong ticket = PositionGetTicket(i);
      if(ticket == 0)
         continue;
      if(!PositionSelectByTicket(ticket))
         continue;
      if(PositionGetString(POSITION_SYMBOL) != sym)
         continue;
      if(PositionGetInteger(POSITION_MAGIC) != magic)
         continue;
      if(PositionGetInteger(POSITION_TYPE) != POSITION_TYPE_BUY)
         continue;
      double currentSL = PositionGetDouble(POSITION_SL);
      if(newSL > currentSL)
        {
         string why = "";
         if(!PositionStopOk(sym, true, newSL, why))
            continue;
         S3B_Trade.SetExpertMagicNumber(magic);
         S3B_Trade.SetTypeFillingBySymbol(sym);
         if(S3B_Trade.PositionModify(ticket, newSL, 0.0))
            PrintFormat("S3B %s: trailing SL updated to %s", sym,
                        DoubleToString(newSL, (int)SymbolInfoInteger(sym, SYMBOL_DIGITS)));
        }
     }
  }

void S3B_Process(S3SymbolData &d)
  {
   if(d.pendingDir != 0 && TimeCurrent() >= d.pendingDue)
     {
      double refPrice = d.pendingPrice;
      d.pendingDir = 0;
      d.pendingDue = 0;
      if(!S3B_HasOpenPosition(d.symbol, d.magic))
         S3B_ExecuteEntry(d, refPrice);
     }
   S3B_ManageTrailingStop(d);
   datetime currentBar = iTime(d.symbol, S3B_EntryTF, 0);
   if(currentBar <= 0 || currentBar == d.lastBarTime)
      return;
   d.lastBarTime = currentBar;
   S3B_CheckEntry(d);
  }

void OnTickStrategy3B()
  {
   for(int i = 0; i < ArraySize(S3B_Data); i++)
      S3B_Process(S3B_Data[i]);
  }

//+------------------------------------------------------------------+
//| S4: ORB (opening range breakout)                                  |
//| Range from M1 bars; stop orders at the range edges; fixed risk.    |
//+------------------------------------------------------------------+
datetime YmdToTime(const int ymd)
  {
   MqlDateTime dt;
   dt.year = ymd / 10000;
   dt.mon  = (ymd / 100) % 100;
   dt.day  = ymd % 100;
   dt.hour = 0;
   dt.min  = 0;
   dt.sec  = 0;
   return StructToTime(dt);
  }

datetime S4_LocalNow()
  {
   return (datetime)((long)ServerToUtc(TimeCurrent()) + (long)S4_BrokerUtcOffsetHours * 3600);
  }

bool S4_BuildRange(const datetime localStart, const datetime localEnd,
                   double &hi, double &lo, int &bars, datetime &firstBar)
  {
   hi = 0.0;
   lo = 0.0;
   bars = 0;
   firstBar = 0;
   datetime utcStart = (datetime)((long)localStart - (long)S4_BrokerUtcOffsetHours * 3600);
   datetime utcEnd   = (datetime)((long)localEnd - (long)S4_BrokerUtcOffsetHours * 3600);
   datetime srvStart = UtcToServer(utcStart);
   datetime srvEnd   = UtcToServer(utcEnd);
   MqlRates rates[];
   int n = CopyRates(g_s4Symbol, PERIOD_M1, srvStart, srvEnd, rates);
   if(n <= 0)
      return false;
   double h = -1e308;
   double l = 1e308;
   datetime first = 0;
   int count = 0;
   for(int i = 0; i < n; i++)
     {
      if(rates[i].time < srvStart || rates[i].time >= srvEnd)
         continue;
      if(rates[i].high > h) h = rates[i].high;
      if(rates[i].low < l) l = rates[i].low;
      if(first == 0 || rates[i].time < first) first = rates[i].time;
      count++;
     }
   if(count <= 0 || h <= l)
      return false;
   hi = h;
   lo = l;
   bars = count;
   firstBar = first;
   return true;
  }

void S4_Trend(bool &allowLong, bool &allowShort)
  {
   allowLong = S4_TradeLongs;
   allowShort = S4_TradeShorts;
   if(S4_TrendDays <= 0)
      return;
   double cur = iClose(g_s4Symbol, PERIOD_D1, 1);
   double ref = iClose(g_s4Symbol, PERIOD_D1, 1 + S4_TrendDays);
   if(cur <= 0 || ref <= 0)
      return;
   double chg = (cur - ref) / ref * 100.0;
   if(chg > S4_TrendThresholdPct)
      allowShort = false;
   else
      if(chg < -S4_TrendThresholdPct)
         allowLong = false;
      else
        {
         allowLong = false;
         allowShort = false;
        }
  }

int S4_LosingStreak()
  {
   int streak = 0;
   for(int i = ArraySize(g_s4DayR) - 1; i >= 0; i--)
     {
      if(g_s4DayR[i] < 0.0)
         streak++;
      else
         break;
     }
   return streak;
  }

bool S4_BreakerAllows()
  {
   if(S4_MaxConsecLosses <= 0)
      return true;
   return (S4_LosingStreak() < S4_MaxConsecLosses);
  }

void S4_ApplySeed()
  {
   ArrayResize(g_s4DayR, 0);
   if(StringLen(S4_BreakerSeed) == 0)
      return;
   string parts[];
   if(StringSplit(S4_BreakerSeed, '|', parts) != 2)
     {
      Print("ORB SEED | bad format - ignored");
      return;
     }
   int through = ParseYmd(parts[0]);
   if(through == 0)
     {
      Print("ORB SEED | bad date - ignored");
      return;
     }
   datetime throughTime = YmdToTime(through);
   datetime today = DayStart(S4_LocalNow());
   int age = (int)((long)(today - throughTime) / 86400);
   if(age > 30 || age < 0)
     {
      PrintFormat("ORB SEED | covers through %s, %d days from today - ignored", parts[0], age);
      return;
     }
   string vals[];
   int n = StringSplit(parts[1], ',', vals);
   for(int i = 0; i < n; i++)
     {
      string s = vals[i];
      StringTrimLeft(s);
      StringTrimRight(s);
      if(StringLen(s) == 0)
         continue;
      double v = StringToDouble(s);
      int k = ArraySize(g_s4DayR);
      ArrayResize(g_s4DayR, k + 1);
      g_s4DayR[k] = v;
     }
   PrintFormat("ORB SEED | applied %d day results through %s (%d days old) | losing streak %d of %d",
               ArraySize(g_s4DayR), parts[0], age, S4_LosingStreak(), S4_MaxConsecLosses);
  }

double S4_RiskLots(const double priceRisk)
  {
   if(priceRisk <= 0.0 || S4_FixedRisk <= 0.0)
      return 0.0;
   double tickValue = SymbolInfoDouble(g_s4Symbol, SYMBOL_TRADE_TICK_VALUE);
   double tickSize  = SymbolInfoDouble(g_s4Symbol, SYMBOL_TRADE_TICK_SIZE);
   double step      = SymbolInfoDouble(g_s4Symbol, SYMBOL_VOLUME_STEP);
   double minLot    = SymbolInfoDouble(g_s4Symbol, SYMBOL_VOLUME_MIN);
   if(tickValue <= 0.0 || tickSize <= 0.0 || step <= 0.0)
      return 0.0;
   double perLot = priceRisk / tickSize * tickValue;
   if(perLot <= 0.0)
      return 0.0;
   double lots = MathFloor(S4_FixedRisk / perLot / step) * step;
   double maxLots = S4_MaxLots;
   if(maxLots <= 0.0)
      maxLots = SymbolInfoDouble(g_s4Symbol, SYMBOL_VOLUME_MAX);
   double capLots = MathMin(SymbolInfoDouble(g_s4Symbol, SYMBOL_VOLUME_MAX), maxLots);
   if(lots > capLots)
      lots = MathFloor(capLots / step) * step;
   if(lots < minLot)
      lots = minLot;
   return NormalizeDouble(lots, 8);
  }

void S4_PlaceSide(const bool isLong, const datetime local,
                  const double rangeHigh, const double rangeLow)
  {
   if(!g_meqCtl.s4)
      return;
   double tickSize = SymbolInfoDouble(g_s4Symbol, SYMBOL_TRADE_TICK_SIZE);
   if(tickSize <= 0.0)
      return;
   double buffer = S4_BufferPoints * tickSize;
   double entry = isLong ? rangeHigh + buffer : rangeLow - buffer;
   double sl    = isLong ? rangeLow : rangeHigh;

   // Put every price on the symbol's tick grid. A range high + buffer can
   // land between ticks, which some brokers reject as "invalid price" even
   // when the distance from the market is otherwise fine.
   entry = AlignPriceToTick(g_s4Symbol, entry);
   sl    = AlignPriceToTick(g_s4Symbol, sl);
   double risk = MathAbs(entry - sl);
   if(risk <= 0.0)
      return;

   if(DirectionCapBlocked(g_s4Symbol, isLong, "ORB"))
      return;

   double tp = 0.0;
   if(S4_RrTarget > 0.0)
      tp = AlignPriceToTick(g_s4Symbol, isLong ? entry + risk * S4_RrTarget
                                               : entry - risk * S4_RrTarget);

   // Per-side duplicate guard, mirroring MetaEq's EntryAlreadyHandled: one
   // entry per side per broker day, whether it is still pending, open, or was
   // already filled today. Keeps a restart/re-arm from stacking a second order.
   datetime sinceLocal  = DayStart(local);
   datetime sinceServer = UtcToServer((datetime)((long)sinceLocal - (long)S4_BrokerUtcOffsetHours * 3600));
   if(HasPendingSide(g_s4Symbol, g_s4Magic, isLong)
      || HasPositionSince(g_s4Symbol, g_s4Magic, isLong, sinceServer)
      || HistoryHasEntry(g_s4Magic, g_s4Symbol, isLong, sinceServer))
     {
      if(isLong)
        {
         g_s4LongSl = sl;
         g_s4LongTp = tp;
        }
      else
        {
         g_s4ShortSl = sl;
         g_s4ShortTp = tp;
        }
      PrintFormat("ORB DUPLICATE GUARD | %s | day %s | a %s entry already exists for this side - NOT placed again",
                  g_s4Symbol, TimeToString(local, TIME_DATE), isLong ? "LONG" : "SHORT");
      return;
     }

   double lots = S4_RiskLots(risk);
   if(lots <= 0.0)
     {
      PrintFormat("ORB %s: could not size the trade, side skipped", isLong ? "LONG" : "SHORT");
      return;
     }

   string why = "";
   if(!SymbolCanTrade(g_s4Symbol, isLong, why))
     {
      PrintFormat("ORB %s: %s - not placed", isLong ? "LONG" : "SHORT", why);
      return;
     }
   if(!PendingStopLevelsOk(g_s4Symbol, isLong, entry, sl, tp, why))
     {
      PrintFormat("ORB %s: %s - not placed", isLong ? "LONG" : "SHORT", why);
      return;
     }

   datetime sessionEndLocal = (datetime)((long)DayStart(local) + (long)S4_SessionEndHour * 3600 + (long)S4_SessionEndMinute * 60);
   datetime utcEnd = (datetime)((long)sessionEndLocal - (long)S4_BrokerUtcOffsetHours * 3600);
   datetime expiry = UtcToServer(utcEnd);
   if(expiry <= TimeCurrent() + 60)
     {
      PrintFormat("ORB %s: session end %s is already past - not placed",
                  isLong ? "LONG" : "SHORT", TimeToString(expiry, TIME_DATE | TIME_MINUTES));
      return;
     }

   S4_Trade.SetExpertMagicNumber(g_s4Magic);
   S4_Trade.SetTypeFillingBySymbol(g_s4Symbol);
   bool ok = false;
   if(isLong)
     {
      ok = S4_Trade.BuyStop(lots, entry, g_s4Symbol, sl, tp, ORDER_TIME_SPECIFIED, expiry, GlobalComment);
      if(!ok && S4_Trade.ResultRetcode() == TRADE_RETCODE_INVALID_EXPIRATION)
         ok = S4_Trade.BuyStop(lots, entry, g_s4Symbol, sl, tp, ORDER_TIME_GTC, 0, GlobalComment);
     }
   else
     {
      ok = S4_Trade.SellStop(lots, entry, g_s4Symbol, sl, tp, ORDER_TIME_SPECIFIED, expiry, GlobalComment);
      if(!ok && S4_Trade.ResultRetcode() == TRADE_RETCODE_INVALID_EXPIRATION)
         ok = S4_Trade.SellStop(lots, entry, g_s4Symbol, sl, tp, ORDER_TIME_GTC, 0, GlobalComment);
     }
   if(ok)
     {
      if(isLong)
        {
         g_s4LongSl = sl;
         g_s4LongTp = tp;
         g_s4PlannedRiskLong = risk;
        }
      else
        {
         g_s4ShortSl = sl;
         g_s4ShortTp = tp;
         g_s4PlannedRiskShort = risk;
        }
      PrintFormat("ORB ARM %s | day %s | entry %.5f sl %.5f tp %s lots %.2f | range %.5f-%.5f",
                  isLong ? "LONG" : "SHORT", TimeToString(local, TIME_DATE),
                  entry, sl, tp > 0.0 ? DoubleToString(tp, 5) : "-", lots, rangeLow, rangeHigh);
     }
   else
      PrintFormat("ORB %s order failed: %d | retcode %d (%s)",
                  isLong ? "LONG" : "SHORT", GetLastError(),
                  S4_Trade.ResultRetcode(), S4_Trade.ResultRetcodeDescription());
  }

void S4_Process()
  {
   if(!S4_Enabled)
      return;
   datetime local = S4_LocalNow();
   int day = Ymd(local);
   int startM = S4_SessionStartHour * 60 + S4_SessionStartMinute;
   int endM   = S4_SessionEndHour * 60 + S4_SessionEndMinute;

   if(day != g_s4Day)
     {
      if(g_s4Day != 0)
        {
         if(CountPositionsByOffset(S4_OFFSET) > 0)
            ClosePositionsByOffset(S4_OFFSET);
         CancelPending(g_s4Symbol, g_s4Magic, S4_Trade);
         if(g_s4WindowHadData)
           {
            int n = ArraySize(g_s4DayR);
            ArrayResize(g_s4DayR, n + 1);
            g_s4DayR[n] = g_s4TodayR;
           }
         PrintFormat("ORB DAY | %s | today R %.3f | streak %d | ledger %d",
                     TimeToString(local, TIME_DATE), g_s4TodayR, S4_LosingStreak(), ArraySize(g_s4DayR));
        }
      g_s4Day = day;
      g_s4Armed = false;
      g_s4Placed = false;
      g_s4Finished = false;
      g_s4TodayR = 0.0;
      g_s4WindowHadData = false;
      g_s4RangeHigh = 0.0;
      g_s4RangeLow = 0.0;
     }

   int m = 0;
   MqlDateTime dt;
   TimeToStruct(local, dt);
   m = dt.hour * 60 + dt.min;

   if(endM > startM && m >= endM)
     {
      if(!g_s4Finished)
        {
         g_s4Finished = true;
         ClosePositionsByOffset(S4_OFFSET);
         CancelPending(g_s4Symbol, g_s4Magic, S4_Trade);
         PrintFormat("ORB EXIT | %s %s | session end", TimeToString(local, TIME_DATE), TimeToString(local, TIME_MINUTES));
        }
      return;
     }
   if(m < startM)
      return;
   if(g_s4Armed || m < startM + S4_RangeMinutes)
      return;

   g_s4Armed = true;

   datetime localStart = (datetime)((long)DayStart(local) + (long)startM * 60);
   datetime localEnd   = (datetime)((long)localStart + (long)S4_RangeMinutes * 60);
   double hi = 0.0, lo = 0.0;
   int bars = 0;
   datetime firstBar = 0;
   if(!S4_BuildRange(localStart, localEnd, hi, lo, bars, firstBar))
     {
      PrintFormat("ORB SKIP | %s | no M1 bars in the range window", TimeToString(local, TIME_DATE));
      return;
     }
   g_s4WindowHadData = true;
   g_s4RangeHigh = hi;
   g_s4RangeLow = lo;

   datetime firstLocal = (datetime)((long)ServerToUtc(firstBar) + (long)S4_BrokerUtcOffsetHours * 3600);
   MqlDateTime fd;
   TimeToStruct(firstLocal, fd);
   int firstM = fd.hour * 60 + fd.min;
   int missed = firstM - startM;
   if(missed >= 30)
     {
      PrintFormat("ORB RANGE INCOMPLETE | %s | first bar %d min after the window opened (limit 30) | NO ORDER",
                  TimeToString(local, TIME_DATE), missed);
      return;
     }

   bool allowLong = false, allowShort = false;
   S4_Trend(allowLong, allowShort);
   bool breakerOk = S4_BreakerAllows();
   bool guardOk = !PortfolioGuardBlocked();
   if(!breakerOk)
      PrintFormat("ORB BREAKER | %s | %d losing days in a row | NO ORDERS",
                  TimeToString(local, TIME_DATE), S4_LosingStreak());
   if(!guardOk)
      PrintFormat("ORB GUARD | %s | portfolio guard blocks new entries | NO ORDERS",
                  TimeToString(local, TIME_DATE));

   CancelPending(g_s4Symbol, g_s4Magic, S4_Trade);
   bool placeLong  = breakerOk && guardOk && allowLong;
   bool placeShort = breakerOk && guardOk && allowShort;
   if(breakerOk && guardOk && !placeLong && !placeShort)
      PrintFormat("ORB TREND | %s | dead band - no side", TimeToString(local, TIME_DATE));
   if(placeLong)
      S4_PlaceSide(true, local, hi, lo);
   if(placeShort)
      S4_PlaceSide(false, local, hi, lo);
   PrintFormat("ORB RANGE done | %s | bars %d | high %.5f low %.5f | trend L%d S%d | breaker %s | guard %s",
               TimeToString(local, TIME_DATE), bars, hi, lo, placeLong ? 1 : 0, placeShort ? 1 : 0,
               breakerOk ? "ok" : "SKIP", guardOk ? "ok" : "BLOCKED");
  }

bool InitStrategy4()
  {
   g_s4Symbol = S4_Symbol;
   StringTrimLeft(g_s4Symbol);
   StringTrimRight(g_s4Symbol);
   string u = g_s4Symbol;
   StringToUpper(u);
   if(u == "CURRENT" || StringLen(g_s4Symbol) == 0)
      g_s4Symbol = _Symbol;
   SymbolSelect(g_s4Symbol, true);
   if(SymbolInfoDouble(g_s4Symbol, SYMBOL_POINT) <= 0.0)
     {
      PrintFormat("S4 error: symbol '%s' is not available", g_s4Symbol);
      return false;
     }
   g_s4Magic = GlobalBaseMagic + S4_OFFSET;
   g_s4Day = 0;
   g_s4Armed = false;
   g_s4Placed = false;
   g_s4Finished = false;
   g_s4TodayR = 0.0;
   g_s4WindowHadData = false;
   g_s4AtrHandle = INVALID_HANDLE;
   S4_ApplySeed();
   PrintFormat("S4 ORB ready: %s | range %dm %02d:%02d-%02d:%02d broker | buffer %.1f pts | tp %.2fR | trend %dd/%.2f%% | risk %.2f | max lots %.2f | breaker %d",
               g_s4Symbol, S4_RangeMinutes, S4_SessionStartHour, S4_SessionStartMinute,
               S4_SessionEndHour, S4_SessionEndMinute, S4_BufferPoints, S4_RrTarget,
               S4_TrendDays, S4_TrendThresholdPct, S4_FixedRisk, S4_MaxLots, S4_MaxConsecLosses);
   return true;
  }

void DeinitStrategy4()
  {
   if(g_s4AtrHandle != INVALID_HANDLE)
      IndicatorRelease(g_s4AtrHandle);
  }

void OnTickStrategy4()
  {
   S4_Process();
  }

//+------------------------------------------------------------------+
//| S5: BREAKOUT A (LONG ONLY, ASIAN RANGE)                           |
//| Range from M1 bars in the session window; one stop-buy at the      |
//| trimmed high; stop at the trimmed low; flat at the exit hour.      |
//+------------------------------------------------------------------+
int g_s5EmaHandle = INVALID_HANDLE;

datetime S5_MakeDate(const int year, const int month, const int day)
  {
   return YmdToTime(YmdOf(year, month, day));
  }

datetime S5_UsDstStart(const int year)
  {
   return (datetime)((long)S5_SecondSunday(year, 3) + 7 * 3600);
  }

datetime S5_UsDstEnd(const int year)
  {
   return (datetime)((long)FirstSunday(year, 11) + 6 * 3600);
  }

datetime S5_EuDstStart(const int year)
  {
   return (datetime)((long)LastSunday(year, 3) + 3600);
  }

datetime S5_EuDstEnd(const int year)
  {
   return (datetime)((long)LastSunday(year, 10) + 3600);
  }

datetime S5_WallClock(const datetime utc)
  {
   MqlDateTime dt;
   TimeToStruct(utc, dt);
   if(S5_SessionClock == BOA_CLOCK_CET)
     {
      bool eu = (utc >= S5_EuDstStart(dt.year) && utc < S5_EuDstEnd(dt.year));
      return (datetime)((long)utc + (long)(eu ? 2 : 1) * 3600);
     }
   if(S5_SessionClock == BOA_CLOCK_BROKER)
     {
      bool us = (utc >= S5_UsDstStart(dt.year) && utc < S5_UsDstEnd(dt.year));
      return (datetime)((long)utc + (long)(us ? 3 : 2) * 3600);
     }
   return utc;
  }

datetime S5_WallToUtc(const datetime wall)
  {
   if(S5_SessionClock == BOA_CLOCK_UTC)
      return wall;
   if(S5_SessionClock == BOA_CLOCK_CET)
     {
      datetime guess = (datetime)((long)wall - 3600);
      MqlDateTime dt;
      TimeToStruct(guess, dt);
      bool eu = (guess >= S5_EuDstStart(dt.year) && guess < S5_EuDstEnd(dt.year));
      return (datetime)((long)wall - (long)(eu ? 2 : 1) * 3600);
     }
   datetime guess = (datetime)((long)wall - 2 * 3600);
   MqlDateTime dt;
   TimeToStruct(guess, dt);
   bool us = (guess >= S5_UsDstStart(dt.year) && guess < S5_UsDstEnd(dt.year));
   return (datetime)((long)wall - (long)(us ? 3 : 2) * 3600);
  }

bool S5_BuildRange(const datetime wallMidnight, double &hi, double &lo, int &bars)
  {
   hi = 0.0;
   lo = 0.0;
   bars = 0;
   datetime wallStart = (datetime)((long)wallMidnight + (long)S5_RangeStartHour * 3600);
   datetime wallEnd   = (datetime)((long)wallMidnight + (long)S5_RangeEndHour * 3600);
   datetime srvStart  = UtcToServer(S5_WallToUtc(wallStart));
   datetime srvEnd    = UtcToServer(S5_WallToUtc(wallEnd));
   MqlRates rates[];
   int n = CopyRates(g_s5Symbol, PERIOD_M1, srvStart, srvEnd, rates);
   if(n <= 0)
      return false;
   double h = -1e308;
   double l = 1e308;
   int count = 0;
   for(int i = 0; i < n; i++)
     {
      if(rates[i].time < srvStart || rates[i].time >= srvEnd)
         continue;
      if(rates[i].high > h) h = rates[i].high;
      if(rates[i].low < l) l = rates[i].low;
      count++;
     }
   if(count <= 0 || h <= l)
      return false;
   hi = h;
   lo = l;
   bars = count;
   return true;
  }

double S5_Value(const int handle, const int shift)
  {
   if(handle == INVALID_HANDLE)
      return 0.0;
   double buf[];
   ArraySetAsSeries(buf, true);
   if(CopyBuffer(handle, 0, shift, 1, buf) <= 0)
      return 0.0;
   return buf[0];
  }

void S5_Flatten()
  {
   ClosePositionsByMagic(g_s5Symbol, g_s5Magic);
   CancelPending(g_s5Symbol, g_s5Magic, S5_Trade);
  }

void S5_Arm(const datetime wall)
  {
   if(!g_meqCtl.s5)
      return;
   if(g_s5Placed)
      return;

   datetime midnight = YmdToTime(Ymd(wall));
   double hi = 0.0, lo = 0.0;
   int bars = 0;
   if(!S5_BuildRange(midnight, hi, lo, bars))
     {
      PrintFormat("BOA SKIP | %s | no M1 bars inside the range window", TimeToString(wall, TIME_DATE));
      return;
     }

   double pip = PipSizeFor(g_s5Symbol);
   if(pip <= 0.0)
      return;

   double trim = S5_RangeTrimPips * pip;
   double level = hi - trim;
   double slPips = (hi - lo) / pip - 2.0 * S5_RangeTrimPips;
   if(slPips <= 0.0)
     {
      PrintFormat("BOA SKIP | %s | range %.1f pips is inside the %.1f-pip trim",
                  TimeToString(wall, TIME_DATE), (hi - lo) / pip, S5_RangeTrimPips);
      return;
     }

   if(S5_UseEma)
     {
      double ema = S5_Value(g_s5EmaHandle, 1);
      double bid = SymbolInfoDouble(g_s5Symbol, SYMBOL_BID);
      if(ema <= 0.0)
        {
         PrintFormat("BOA SKIP | %s | EMA not ready", TimeToString(wall, TIME_DATE));
         return;
        }
      if(bid <= ema)
        {
         PrintFormat("BOA GATE | %s | bid %.5f is not above EMA(%d) %.5f - stand aside",
                     TimeToString(wall, TIME_DATE), bid, S5_EmaPeriod, ema);
         return;
        }
     }

   double lots = NormalizeLots(g_s5Symbol, S5_Lots);
   double minLot = SymbolInfoDouble(g_s5Symbol, SYMBOL_VOLUME_MIN);
   if(lots < minLot || lots <= 0.0)
     {
      PrintFormat("BOA SKIP | %.2f lots is below the symbol minimum", S5_Lots);
      return;
     }

   g_s5Placed = true;

   if(DirectionCapBlocked(g_s5Symbol, true, "BOA"))
      return;

   datetime utcNow = S5_WallToUtc(wall);
   datetime since = UtcToServer(DayStart(utcNow));
   if(HistoryHasEntry(g_s5Magic, g_s5Symbol, true, since))
     {
      PrintFormat("BOA DUPLICATE GUARD | %s | a long entry already exists today - NOT placed again",
                  TimeToString(wall, TIME_DATE));
      return;
     }

   double sl = AlignPriceToTick(g_s5Symbol, lo + trim);
   level = AlignPriceToTick(g_s5Symbol, level);

   datetime exitWall = (datetime)((long)midnight + (long)S5_ExitHour * 3600);
   datetime expiry = UtcToServer(S5_WallToUtc(exitWall));
   if(expiry <= TimeCurrent() + 60)
     {
      PrintFormat("BOA SKIP | %s | exit %s is already past - not placed",
                  TimeToString(wall, TIME_DATE), TimeToString(expiry, TIME_DATE | TIME_MINUTES));
      return;
     }

   string why = "";
   if(!SymbolCanTrade(g_s5Symbol, true, why))
     {
      PrintFormat("BOA SKIP | %s | %s - not placed", TimeToString(wall, TIME_DATE), why);
      return;
     }
   if(!PendingStopLevelsOk(g_s5Symbol, true, level, sl, 0.0, why))
     {
      PrintFormat("BOA SKIP | %s | %s - not placed", TimeToString(wall, TIME_DATE), why);
      return;
     }

   S5_Trade.SetExpertMagicNumber(g_s5Magic);
   S5_Trade.SetTypeFillingBySymbol(g_s5Symbol);
   bool ok = S5_Trade.BuyStop(lots, level, g_s5Symbol, sl, 0.0,
                              ORDER_TIME_SPECIFIED, expiry, GlobalComment);
   if(!ok && S5_Trade.ResultRetcode() == TRADE_RETCODE_INVALID_EXPIRATION)
      ok = S5_Trade.BuyStop(lots, level, g_s5Symbol, sl, 0.0,
                            ORDER_TIME_GTC, 0, GlobalComment);
   if(ok)
      PrintFormat("BOA ARM | %s | range %.5f-%.5f (%.1f pips, %d bars) | entry %.5f | sl %.5f | lots %.2f",
                  TimeToString(wall, TIME_DATE), lo, hi, (hi - lo) / pip, bars, level, sl, lots);
   else
      PrintFormat("BOA order failed: %d | retcode %d (%s)",
                  GetLastError(), S5_Trade.ResultRetcode(), S5_Trade.ResultRetcodeDescription());
  }

void S5_Process()
  {
   if(!S5_Enabled)
      return;

   datetime utc = ServerToUtc(TimeCurrent());
   datetime wall = S5_WallClock(utc);
   int day = Ymd(wall);
   if(day != g_s5Day)
     {
      if(g_s5Day != 0)
        {
         if(CountPositionsByOffset(S5_OFFSET) > 0)
            S5_Flatten();
         else
            CancelPending(g_s5Symbol, g_s5Magic, S5_Trade);
        }
      g_s5Day = day;
      g_s5Hi = 0.0;
      g_s5Lo = 0.0;
      g_s5Locked = false;
      g_s5Placed = false;
      g_s5Finished = false;
     }

   MqlDateTime dt;
   TimeToStruct(wall, dt);
   int m = dt.hour * 60 + dt.min;
   int startM = S5_RangeStartHour * 60;
   int endM   = S5_RangeEndHour * 60;
   int exitM  = S5_ExitHour * 60;

   if(m >= exitM)
     {
      if(!g_s5Finished)
        {
         g_s5Finished = true;
         S5_Flatten();
         PrintFormat("BOA EXIT | %s %s | flattened positions and cancelled resting orders",
                     TimeToString(wall, TIME_DATE), TimeToString(wall, TIME_MINUTES));
        }
      return;
     }

   if(m < startM)
      return;

   if(m < endM)
      return;

   if(!g_s5Locked)
     {
      g_s5Locked = true;
      S5_Arm(wall);
     }
  }

bool InitStrategy5()
  {
   g_s5Symbol = S5_Symbol;
   StringTrimLeft(g_s5Symbol);
   StringTrimRight(g_s5Symbol);
   string u = g_s5Symbol;
   StringToUpper(u);
   if(u == "CURRENT" || StringLen(g_s5Symbol) == 0)
      g_s5Symbol = _Symbol;
   SymbolSelect(g_s5Symbol, true);
   if(SymbolInfoDouble(g_s5Symbol, SYMBOL_POINT) <= 0.0)
     {
      PrintFormat("S5 error: symbol '%s' is not available", g_s5Symbol);
      return false;
     }

   if(S5_RangeEndHour <= S5_RangeStartHour)
     {
      Print("S5 error: range end hour must be after the range start hour.");
      return false;
     }
   if(S5_ExitHour <= S5_RangeEndHour)
     {
      Print("S5 error: exit hour must be after the range end hour.");
      return false;
     }

   g_s5Magic = GlobalBaseMagic + S5_OFFSET;
   g_s5Day = 0;
   g_s5Hi = 0.0;
   g_s5Lo = 0.0;
   g_s5Locked = false;
   g_s5Placed = false;
   g_s5Finished = false;

   if(S5_UseEma)
     {
      g_s5EmaHandle = iMA(g_s5Symbol, S5_EmaTF, S5_EmaPeriod, 0, MODE_EMA, PRICE_CLOSE);
      if(g_s5EmaHandle == INVALID_HANDLE)
        {
         PrintFormat("S5 error: could not create the EMA handle for %s", g_s5Symbol);
         return false;
        }
     }

   S5_Trade.SetDeviationInPoints(10);
   PrintFormat("S5 BOA ready: %s LONG ONLY | range %02d:00-%02d:00 exit %02d:00 | trim %.1f pips | clock %s | lots %.2f | EMA %s (%d, %s)",
               g_s5Symbol, S5_RangeStartHour, S5_RangeEndHour, S5_ExitHour,
               S5_RangeTrimPips, EnumToString(S5_SessionClock), S5_Lots,
               S5_UseEma ? "on" : "off", S5_EmaPeriod, EnumToString(S5_EmaTF));
   return true;
  }

void DeinitStrategy5()
  {
   if(g_s5EmaHandle != INVALID_HANDLE)
      IndicatorRelease(g_s5EmaHandle);
  }

void OnTickStrategy5()
  {
   S5_Process();
  }

//+------------------------------------------------------------------+
//| S6: BREAKOUT B (GOLD, BOTH SIDES, UTC RANGE)                      |
//| 03:00-08:00 UTC range, trimmed edges, 1.25% stop, one entry a day. |
//| Relative-range filter, spread rail, optional max-profit and ATR    |
//| profit trail. Flat at the exit hour.                               |
//+------------------------------------------------------------------+
void S6_AppendRange(const double pct)
  {
   if(pct <= 0.0)
      return;
   int look = S6_RelRangeLookback;
   if(look < 2)
      look = 20;
   int n = ArraySize(g_s6RangeHistory);
   ArrayResize(g_s6RangeHistory, n + 1);
   g_s6RangeHistory[n] = pct;
   while(ArraySize(g_s6RangeHistory) > look)
     {
      int sz = ArraySize(g_s6RangeHistory);
      for(int i = 1; i < sz; i++)
         g_s6RangeHistory[i - 1] = g_s6RangeHistory[i];
      ArrayResize(g_s6RangeHistory, sz - 1);
     }
  }

double S6_HistoryMean()
  {
   int n = ArraySize(g_s6RangeHistory);
   if(n <= 0)
      return 0.0;
   double sum = 0.0;
   for(int i = 0; i < n; i++)
      sum += g_s6RangeHistory[i];
   return sum / n;
  }

bool S6_BuildRange(const datetime utcNow, double &hi, double &lo, int &bars)
  {
   hi = 0.0;
   lo = 0.0;
   bars = 0;
   datetime midnight = DayStart(utcNow);
   datetime utcStart = (datetime)((long)midnight + (long)S6_RangeStartHour * 3600);
   datetime utcEnd   = (datetime)((long)midnight + (long)S6_RangeEndHour * 3600);
   datetime srvStart = UtcToServer(utcStart);
   datetime srvEnd   = UtcToServer(utcEnd);
   MqlRates rates[];
   int n = CopyRates(g_s6Symbol, PERIOD_M1, srvStart, srvEnd, rates);
   if(n <= 0)
      return false;
   double h = -1e308;
   double l = 1e308;
   int count = 0;
   for(int i = 0; i < n; i++)
     {
      if(rates[i].time < srvStart || rates[i].time >= srvEnd)
         continue;
      if(rates[i].high > h) h = rates[i].high;
      if(rates[i].low < l) l = rates[i].low;
      count++;
     }
   if(count <= 0 || h <= l)
      return false;
   hi = h;
   lo = l;
   bars = count;
   return true;
  }

void S6_SeedHistory()
  {
   ArrayResize(g_s6RangeHistory, 0);
   if(S6_RelRangeMin <= 0.0 || S6_RelRangeLookback < 2)
      return;

   datetime nowUtc = ServerToUtc(TimeCurrent());
   datetime today = DayStart(nowUtc);
   int daysBack = (int)MathCeil(S6_RelRangeLookback * 7.0 / 5.0 + 10.0);
   datetime fromUtc = (datetime)((long)today - (long)daysBack * 86400);

   if(StringLen(S6_HistoryStart) > 0)
     {
      int ymd = ParseYmd(S6_HistoryStart);
      if(ymd > 0)
        {
         datetime hs = YmdToTime(ymd);
         if(hs < fromUtc)
            fromUtc = hs;
        }
      else
         PrintFormat("BOB | History Start Date '%s' is not yyyy-MM-dd - ignored", S6_HistoryStart);
     }

   datetime srvFrom = UtcToServer(fromUtc);
   datetime srvTo = UtcToServer(nowUtc);
   MqlRates rates[];
   int n = CopyRates(g_s6Symbol, PERIOD_M1, srvFrom, srvTo, rates);
   if(n <= 0)
     {
      Print("BOB | no minute history available - the relative-range filter warms up live.");
      return;
     }

   int startM = S6_RangeStartHour * 60;
   int endM = S6_RangeEndHour * 60;
   int curDay = 0;
   double curHi = 0.0;
   double curLo = 0.0;
   bool have = false;
   int firstYmd = 0;
   int lastYmd = 0;

   for(int i = 0; i < n; i++)
     {
      datetime utc = ServerToUtc(rates[i].time);
      if(utc >= nowUtc || utc >= today)
         continue;
      int d = Ymd(utc);
      if(d != curDay)
        {
         if(have && curHi > curLo)
           {
            double mid = (curHi + curLo) * 0.5;
            if(mid > 0.0)
              {
               double pct = 100.0 * (curHi - curLo) / mid;
               if(pct > 0.0)
                 {
                  S6_AppendRange(pct);
                  if(firstYmd == 0)
                     firstYmd = curDay;
                  lastYmd = curDay;
                 }
              }
           }
         curDay = d;
         have = false;
        }
      MqlDateTime md;
      TimeToStruct(utc, md);
      int m = md.hour * 60 + md.min;
      if(m < startM || m >= endM)
         continue;
      if(!have)
        {
         curHi = rates[i].high;
         curLo = rates[i].low;
         have = true;
        }
      else
        {
         if(rates[i].high > curHi) curHi = rates[i].high;
         if(rates[i].low < curLo) curLo = rates[i].low;
        }
     }
   if(have && curHi > curLo)
     {
      double mid = (curHi + curLo) * 0.5;
      if(mid > 0.0)
        {
         double pct = 100.0 * (curHi - curLo) / mid;
         if(pct > 0.0)
           {
            S6_AppendRange(pct);
            if(firstYmd == 0)
               firstYmd = curDay;
            lastYmd = curDay;
           }
        }
     }

   if(ArraySize(g_s6RangeHistory) > 0)
      PrintFormat("BOB | relative-range history: %d sessions, %s to %s, mean %.3f%%",
                  ArraySize(g_s6RangeHistory), TimeToString(YmdToTime(firstYmd), TIME_DATE),
                  TimeToString(YmdToTime(lastYmd), TIME_DATE), S6_HistoryMean());
  }

double S6_SpreadPct()
  {
   double ask = SymbolInfoDouble(g_s6Symbol, SYMBOL_ASK);
   double bid = SymbolInfoDouble(g_s6Symbol, SYMBOL_BID);
   if(ask <= 0.0 || bid <= 0.0)
      return 0.0;
   double mid = (ask + bid) * 0.5;
   if(mid <= 0.0)
      return 0.0;
   return 100.0 * (ask - bid) / mid;
  }

bool S6_SpreadOk()
  {
   if(S6_MaxSpreadPct <= 0.0)
      return true;
   return (S6_SpreadPct() <= S6_MaxSpreadPct);
  }

int S6_MaxEntries()
  {
   if(S6_OneEntryPerDay)
      return 1;
   int n = S6_MaxEntriesPerDay;
   if(n < 1) n = 1;
   if(n > 2) n = 2;
   return n;
  }

bool S6_PortfolioBlocked(string &reason)
  {
   reason = "";
   if(!PortfolioGuardEnabled)
      return false;
   double realized = PortfolioDailyRealized();
   if(MaxPortfolioDailyLossUsd > 0.0 && realized <= -MathAbs(MaxPortfolioDailyLossUsd))
     {
      reason = "daily loss limit";
      return true;
     }
   if(!S6_IgnorePositionCap && MaxCombinedPositions > 0 && CountOurPositions() >= MaxCombinedPositions)
     {
      reason = StringFormat("%d portfolio positions, cap %d", CountOurPositions(), MaxCombinedPositions);
      return true;
     }
   return false;
  }

void S6_PlaceOrders(const datetime wall)
  {
   if(!g_meqCtl.s6)
      return;
   double trim = S6_RangeTrim;
   double longLevel = AlignPriceToTick(g_s6Symbol, g_s6Hi - trim);
   double shortLevel = AlignPriceToTick(g_s6Symbol, g_s6Lo + trim);
   double lots = NormalizeLots(g_s6Symbol, S6_Lots);
   double minLot = SymbolInfoDouble(g_s6Symbol, SYMBOL_VOLUME_MIN);
   if(lots < minLot || lots <= 0.0)
      return;

   datetime exitUtc = (datetime)((long)DayStart(wall) + (long)S6_ExitHour * 3600 + (long)S6_ExitMinute * 60);
   datetime expiry = UtcToServer(exitUtc);
   datetime since = UtcToServer(DayStart(wall));
   bool expired = (expiry <= TimeCurrent() + 60);

   bool longHandled = HistoryHasEntry(g_s6Magic, g_s6Symbol, true, since);
   bool shortHandled = HistoryHasEntry(g_s6Magic, g_s6Symbol, false, since);
   bool longCapped = (!longHandled && DirectionCapBlocked(g_s6Symbol, true, "BOB"));
   bool shortCapped = (!shortHandled && DirectionCapBlocked(g_s6Symbol, false, "BOB"));

   double ask = 0.0, bid = 0.0;
   string why = "";
   if(!SymbolQuoteReady(g_s6Symbol, ask, bid, why))
     {
      PrintFormat("BOB | %s - no orders placed", why);
      return;
     }

   if(longHandled)
      PrintFormat("BOB DUPLICATE GUARD | %s | long entry already exists today - NOT placed again",
                  TimeToString(wall, TIME_DATE));
   else if(longCapped)
     {
      // direction cap already logged
     }
   else if(longLevel > ask)
     {
      double longStop = AlignPriceToTick(g_s6Symbol, longLevel * (1.0 - S6_StopPct / 100.0));
      if(expired)
         PrintFormat("BOB | %s | long stop not placed: the exit time has passed",
                     TimeToString(wall, TIME_DATE));
      else if(!SymbolCanTrade(g_s6Symbol, true, why))
         PrintFormat("BOB | %s | long stop not placed: %s", TimeToString(wall, TIME_DATE), why);
      else if(!PendingStopLevelsOk(g_s6Symbol, true, longLevel, longStop, 0.0, why))
         PrintFormat("BOB | %s | long stop not placed: %s", TimeToString(wall, TIME_DATE), why);
      else
        {
         S6_Trade.SetExpertMagicNumber(g_s6Magic);
         S6_Trade.SetTypeFillingBySymbol(g_s6Symbol);
         bool ok = S6_Trade.BuyStop(lots, longLevel, g_s6Symbol, longStop, 0.0,
                                    ORDER_TIME_SPECIFIED, expiry, GlobalComment);
         if(!ok && S6_Trade.ResultRetcode() == TRADE_RETCODE_INVALID_EXPIRATION)
            ok = S6_Trade.BuyStop(lots, longLevel, g_s6Symbol, longStop, 0.0,
                                  ORDER_TIME_GTC, 0, GlobalComment);
         if(!ok)
            PrintFormat("BOB | long stop order rejected: %d | retcode %d (%s)",
                        GetLastError(), S6_Trade.ResultRetcode(), S6_Trade.ResultRetcodeDescription());
        }
     }
   else
     {
      double sl = AlignPriceToTick(g_s6Symbol, ask * (1.0 - S6_StopPct / 100.0));
      if(!SymbolCanTrade(g_s6Symbol, true, why))
         PrintFormat("BOB | %s | long market entry not placed: %s", TimeToString(wall, TIME_DATE), why);
      else if(!MarketOrderLevelsOk(g_s6Symbol, true, sl, 0.0, why))
         PrintFormat("BOB | %s | long market entry not placed: %s", TimeToString(wall, TIME_DATE), why);
      else
        {
         S6_Trade.SetExpertMagicNumber(g_s6Magic);
         S6_Trade.SetTypeFillingBySymbol(g_s6Symbol);
         // Price 0 = current ask at send time.
         if(S6_Trade.Buy(lots, g_s6Symbol, 0.0, sl, 0.0, GlobalComment))
            PrintFormat("BOB MARKET | %s %s | long taken at the market, the range was already through",
                        TimeToString(wall, TIME_DATE), TimeToString(wall, TIME_MINUTES));
         else
            PrintFormat("BOB | market BUY rejected: %d | retcode %d (%s)",
                        GetLastError(), S6_Trade.ResultRetcode(), S6_Trade.ResultRetcodeDescription());
        }
     }

   if(shortHandled)
      PrintFormat("BOB DUPLICATE GUARD | %s | short entry already exists today - NOT placed again",
                  TimeToString(wall, TIME_DATE));
   else if(shortCapped)
     {
      // direction cap already logged
     }
   else if(shortLevel < bid)
     {
      double shortStop = AlignPriceToTick(g_s6Symbol, shortLevel * (1.0 + S6_StopPct / 100.0));
      if(expired)
         PrintFormat("BOB | %s | short stop not placed: the exit time has passed",
                     TimeToString(wall, TIME_DATE));
      else if(!SymbolCanTrade(g_s6Symbol, false, why))
         PrintFormat("BOB | %s | short stop not placed: %s", TimeToString(wall, TIME_DATE), why);
      else if(!PendingStopLevelsOk(g_s6Symbol, false, shortLevel, shortStop, 0.0, why))
         PrintFormat("BOB | %s | short stop not placed: %s", TimeToString(wall, TIME_DATE), why);
      else
        {
         S6_Trade.SetExpertMagicNumber(g_s6Magic);
         S6_Trade.SetTypeFillingBySymbol(g_s6Symbol);
         bool ok = S6_Trade.SellStop(lots, shortLevel, g_s6Symbol, shortStop, 0.0,
                                     ORDER_TIME_SPECIFIED, expiry, GlobalComment);
         if(!ok && S6_Trade.ResultRetcode() == TRADE_RETCODE_INVALID_EXPIRATION)
            ok = S6_Trade.SellStop(lots, shortLevel, g_s6Symbol, shortStop, 0.0,
                                   ORDER_TIME_GTC, 0, GlobalComment);
         if(!ok)
            PrintFormat("BOB | short stop order rejected: %d | retcode %d (%s)",
                        GetLastError(), S6_Trade.ResultRetcode(), S6_Trade.ResultRetcodeDescription());
        }
     }
   else
     {
      double sl = AlignPriceToTick(g_s6Symbol, bid * (1.0 + S6_StopPct / 100.0));
      if(!SymbolCanTrade(g_s6Symbol, false, why))
         PrintFormat("BOB | %s | short market entry not placed: %s", TimeToString(wall, TIME_DATE), why);
      else if(!MarketOrderLevelsOk(g_s6Symbol, false, sl, 0.0, why))
         PrintFormat("BOB | %s | short market entry not placed: %s", TimeToString(wall, TIME_DATE), why);
      else
        {
         S6_Trade.SetExpertMagicNumber(g_s6Magic);
         S6_Trade.SetTypeFillingBySymbol(g_s6Symbol);
         // Price 0 = current bid at send time.
         if(S6_Trade.Sell(lots, g_s6Symbol, 0.0, sl, 0.0, GlobalComment))
            PrintFormat("BOB MARKET | %s %s | short taken at the market, the range was already through",
                        TimeToString(wall, TIME_DATE), TimeToString(wall, TIME_MINUTES));
         else
            PrintFormat("BOB | market SELL rejected: %d | retcode %d (%s)",
                        GetLastError(), S6_Trade.ResultRetcode(), S6_Trade.ResultRetcodeDescription());
        }
     }
  }

void S6_Arm(const datetime wall)
  {
   if(g_s6Placed)
      return;

   double hi = 0.0, lo = 0.0;
   int bars = 0;
   if(!S6_BuildRange(wall, hi, lo, bars))
     {
      PrintFormat("BOB SKIP | %s | no M1 bars inside the range window", TimeToString(wall, TIME_DATE));
      return;
     }
   g_s6Hi = hi;
   g_s6Lo = lo;
   double mid = (hi + lo) * 0.5;
   double rangePct = (mid > 0.0) ? 100.0 * (hi - lo) / mid : 0.0;
   g_s6RangePct = rangePct;

   if(S6_RelRangeMin > 0.0 && ArraySize(g_s6RangeHistory) >= 10)
     {
      double avg = S6_HistoryMean();
      double ratio = (avg > 0.0) ? rangePct / avg : 0.0;
      if(ratio < S6_RelRangeMin)
        {
         PrintFormat("BOB SKIP | %s | range %.3f%% of price = %.3f x the %d-session average %.3f%% (min %.2f)",
                     TimeToString(wall, TIME_DATE), rangePct, ratio,
                     ArraySize(g_s6RangeHistory), avg, S6_RelRangeMin);
         return;
        }
     }

   if(!S6_SpreadOk())
     {
      PrintFormat("BOB SKIP | %s | spread %.4f%% over the %.4f%% cap",
                  TimeToString(wall, TIME_DATE), S6_SpreadPct(), S6_MaxSpreadPct);
      return;
     }

   string reason = "";
   if(S6_PortfolioBlocked(reason))
     {
      PrintFormat("BOB SKIP | %s | portfolio guard blocked new entries (%s)",
                  TimeToString(wall, TIME_DATE), reason);
      return;
     }

   if(S6_StopPct <= 0.0)
     {
      PrintFormat("BOB SKIP | %s | no stop distance set", TimeToString(wall, TIME_DATE));
      return;
     }

   double lots = NormalizeLots(g_s6Symbol, S6_Lots);
   double minLot = SymbolInfoDouble(g_s6Symbol, SYMBOL_VOLUME_MIN);
   if(lots < minLot || lots <= 0.0)
     {
      PrintFormat("BOB SKIP | %.2f lots is below the symbol minimum", S6_Lots);
      return;
     }

   g_s6Placed = true;
   S6_PlaceOrders(wall);

   int digits = (int)SymbolInfoInteger(g_s6Symbol, SYMBOL_DIGITS);
   PrintFormat("BOB ARM | %s %s | range %.2f-%.2f (%.3f%% of price, %d bars) | long %.2f sl %.2f | short %.2f sl %.2f | lots %.2f",
               TimeToString(wall, TIME_DATE), TimeToString(wall, TIME_MINUTES),
               lo, hi, rangePct, bars,
               hi - S6_RangeTrim, NormalizeDouble((hi - S6_RangeTrim) * (1.0 - S6_StopPct / 100.0), digits),
               lo + S6_RangeTrim, NormalizeDouble((lo + S6_RangeTrim) * (1.0 + S6_StopPct / 100.0), digits),
               lots);
  }

void S6_MaxProfit(const datetime wall)
  {
   double bid = SymbolInfoDouble(g_s6Symbol, SYMBOL_BID);
   double ask = SymbolInfoDouble(g_s6Symbol, SYMBOL_ASK);
   for(int i = PositionsTotal() - 1; i >= 0; i--)
     {
      ulong ticket = PositionGetTicket(i);
      if(ticket == 0)
         continue;
      if(PositionGetString(POSITION_SYMBOL) != g_s6Symbol)
         continue;
      if(PositionGetInteger(POSITION_MAGIC) != g_s6Magic)
         continue;
      double entry = PositionGetDouble(POSITION_PRICE_OPEN);
      long type = PositionGetInteger(POSITION_TYPE);
      if(type == POSITION_TYPE_BUY)
        {
         double target = entry * (1.0 + S6_MaxProfitPct / 100.0);
         if(bid >= target)
           {
            PrintFormat("BOB PROFIT | %s %s | long at max profit %.2f%% (%.2f -> %.2f)",
                        TimeToString(wall, TIME_DATE), TimeToString(wall, TIME_MINUTES),
                        S6_MaxProfitPct, entry, bid);
            CloseTicket(ticket);
           }
        }
      else
        {
         double target = entry * (1.0 - S6_MaxProfitPct / 100.0);
         if(ask <= target)
           {
            PrintFormat("BOB PROFIT | %s %s | short at max profit %.2f%% (%.2f -> %.2f)",
                        TimeToString(wall, TIME_DATE), TimeToString(wall, TIME_MINUTES),
                        S6_MaxProfitPct, entry, ask);
            CloseTicket(ticket);
           }
        }
     }
  }

void S6_Protect()
  {
   if(!S6_UseAtrTrail || g_s6Symbol == "")
      return;

   datetime barTime = iTime(g_s6Symbol, S6_AtrTF, 1);
   if(barTime <= 0 || barTime == g_s6AtrBarTime)
      return;
   g_s6AtrBarTime = barTime;

   int period = S6_AtrPeriod;
   if(period < 2)
      period = 14;
   MqlRates rates[];
   int n = CopyRates(g_s6Symbol, S6_AtrTF, 1, period + 1, rates);
   if(n < period + 1)
      return;
   double sum = 0.0;
   for(int i = 1; i <= period; i++)
     {
      double prevClose = rates[i - 1].close;
      double tr = rates[i].high - rates[i].low;
      tr = MathMax(tr, MathAbs(rates[i].high - prevClose));
      tr = MathMax(tr, MathAbs(rates[i].low - prevClose));
      sum += tr;
     }
   double atr = sum / period;
   if(atr <= 0.0)
      return;

   double bid = SymbolInfoDouble(g_s6Symbol, SYMBOL_BID);
   double ask = SymbolInfoDouble(g_s6Symbol, SYMBOL_ASK);

   bool anyLong = false;
   bool anyShort = false;

   for(int i = PositionsTotal() - 1; i >= 0; i--)
     {
      ulong ticket = PositionGetTicket(i);
      if(ticket == 0)
         continue;
      if(PositionGetString(POSITION_SYMBOL) != g_s6Symbol)
         continue;
      if(PositionGetInteger(POSITION_MAGIC) != g_s6Magic)
         continue;

      bool isLong = (PositionGetInteger(POSITION_TYPE) == POSITION_TYPE_BUY);
      double entry = PositionGetDouble(POSITION_PRICE_OPEN);
      double current = PositionGetDouble(POSITION_SL);
      double tp = PositionGetDouble(POSITION_TP);

      if(isLong)
        {
         anyLong = true;
         if(!g_s6HasLong)
           {
            g_s6BestLong = entry;
            g_s6HasLong = true;
           }
         if(bid > g_s6BestLong)
            g_s6BestLong = bid;
        }
      else
        {
         anyShort = true;
         if(!g_s6HasShort)
           {
            g_s6BestShort = entry;
            g_s6HasShort = true;
           }
         if(ask < g_s6BestShort)
            g_s6BestShort = ask;
        }

      double best = isLong ? g_s6BestLong : g_s6BestShort;
      double mfePct = isLong ? (best - entry) / entry * 100.0 : (entry - best) / entry * 100.0;
      if(mfePct < S6_AtrStartPct)
         continue;

      double candidate = isLong ? best - atr * S6_AtrMult : best + atr * S6_AtrMult;
      double floorPrice = isLong ? entry * (1.0 + S6_LockPct / 100.0)
                                 : entry * (1.0 - S6_LockPct / 100.0);
      candidate = isLong ? MathMax(candidate, floorPrice) : MathMin(candidate, floorPrice);

      bool improve = isLong ? (candidate > current || current <= 0.0)
                            : (candidate < current || current <= 0.0);
      string why = "";
      bool valid = PositionStopOk(g_s6Symbol, isLong, candidate, why);
      if(!improve || !valid)
         continue;

      S6_Trade.SetExpertMagicNumber(g_s6Magic);
      S6_Trade.SetTypeFillingBySymbol(g_s6Symbol);
      if(S6_Trade.PositionModify(ticket, NormalizeDouble(candidate, (int)SymbolInfoInteger(g_s6Symbol, SYMBOL_DIGITS)), tp))
         PrintFormat("BOB PROTECT | %s #%I64u | SL -> %s | best %.2f | atr %.2f",
                     isLong ? "LONG" : "SHORT", ticket,
                     DoubleToString(candidate, (int)SymbolInfoInteger(g_s6Symbol, SYMBOL_DIGITS)), best, atr);
     }

   if(!anyLong)
     {
      g_s6HasLong = false;
      g_s6BestLong = 0.0;
     }
   if(!anyShort)
     {
      g_s6HasShort = false;
      g_s6BestShort = 0.0;
     }
  }

void S6_Flatten()
  {
   ClosePositionsByMagic(g_s6Symbol, g_s6Magic);
   CancelPending(g_s6Symbol, g_s6Magic, S6_Trade);
  }

void S6_Process()
  {
   if(!S6_Enabled)
      return;

   datetime utc = ServerToUtc(TimeCurrent());
   int day = Ymd(utc);
   if(day != g_s6Day)
     {
      if(g_s6Day != 0)
        {
         if(CountPositionsByOffset(S6_OFFSET) > 0)
            S6_Flatten();
         else
            CancelPending(g_s6Symbol, g_s6Magic, S6_Trade);
         if(g_s6RangePct > 0.0)
            S6_AppendRange(g_s6RangePct);
        }
      g_s6Day = day;
      g_s6Hi = 0.0;
      g_s6Lo = 0.0;
      g_s6RangePct = 0.0;
      g_s6Locked = false;
      g_s6Placed = false;
      g_s6Finished = false;
      g_s6SpreadBlocked = false;
      g_s6Entries = 0;
      g_s6HasLong = false;
      g_s6HasShort = false;
     }

   MqlDateTime dt;
   TimeToStruct(utc, dt);
   int m = dt.hour * 60 + dt.min;
   int startM = S6_RangeStartHour * 60;
   int endM   = S6_RangeEndHour * 60;
   int exitM  = S6_ExitHour * 60 + S6_ExitMinute;

   if(m >= exitM)
     {
      if(!g_s6Finished)
        {
         g_s6Finished = true;
         S6_Flatten();
         PrintFormat("BOB EXIT | %s %s | flattened positions and cancelled resting orders",
                     TimeToString(utc, TIME_DATE), TimeToString(utc, TIME_MINUTES));
        }
      return;
     }

   if(m < startM)
      return;

   if(m < endM)
      return;

   if(!g_s6Locked)
     {
      g_s6Locked = true;
      S6_Arm(utc);
      return;
     }

   if(!g_s6Placed)
      return;

   int open = CountPositionsByOffset(S6_OFFSET);
   if(S6_MaxProfitPct > 0.0 && open > 0)
      S6_MaxProfit(utc);

   open = CountPositionsByOffset(S6_OFFSET);
   if(open > g_s6Entries)
     {
      g_s6Entries = open;
      int maxEntries = S6_MaxEntries();
      if(g_s6Entries >= maxEntries)
        {
         CancelPending(g_s6Symbol, g_s6Magic, S6_Trade);
         PrintFormat("BOB FILL | %s %s | entry %d of %d, no more entries today",
                     TimeToString(utc, TIME_DATE), TimeToString(utc, TIME_MINUTES), g_s6Entries, maxEntries);
        }
      else
         PrintFormat("BOB FILL | %s %s | entry %d of %d, the opposite order stays",
                     TimeToString(utc, TIME_DATE), TimeToString(utc, TIME_MINUTES), g_s6Entries, maxEntries);
      return;
     }

   if(S6_MaxSpreadPct > 0.0 && !S6_SpreadOk())
     {
      if(!g_s6SpreadBlocked)
        {
         CancelPending(g_s6Symbol, g_s6Magic, S6_Trade);
         g_s6SpreadBlocked = true;
         PrintFormat("BOB SPREAD | %s %s | spread %.4f%% over the %.4f%% cap, orders withdrawn",
                     TimeToString(utc, TIME_DATE), TimeToString(utc, TIME_MINUTES),
                     S6_SpreadPct(), S6_MaxSpreadPct);
        }
      return;
     }
   if(g_s6SpreadBlocked)
     {
      g_s6SpreadBlocked = false;
      if(S6_MaxEntries() > g_s6Entries)
        {
         S6_PlaceOrders(utc);
         PrintFormat("BOB SPREAD | %s %s | spread back to %.4f%%, orders re-armed",
                     TimeToString(utc, TIME_DATE), TimeToString(utc, TIME_MINUTES), S6_SpreadPct());
        }
     }

   if(S6_UseAtrTrail)
      S6_Protect();
  }

bool InitStrategy6()
  {
   g_s6Symbol = S6_Symbol;
   StringTrimLeft(g_s6Symbol);
   StringTrimRight(g_s6Symbol);
   string u = g_s6Symbol;
   StringToUpper(u);
   if(u == "CURRENT" || StringLen(g_s6Symbol) == 0)
      g_s6Symbol = _Symbol;
   SymbolSelect(g_s6Symbol, true);
   if(SymbolInfoDouble(g_s6Symbol, SYMBOL_POINT) <= 0.0)
     {
      PrintFormat("S6 error: symbol '%s' is not available", g_s6Symbol);
      return false;
     }

   if(S6_RangeEndHour <= S6_RangeStartHour)
     {
      Print("S6 error: range end hour must be after the range start hour.");
      return false;
     }
   if(S6_ExitHour < S6_RangeEndHour)
     {
      Print("S6 error: exit hour must not be before the range end hour.");
      return false;
     }
   if(S6_StopPct <= 0.0)
     {
      Print("S6 error: stop percentage must be positive.");
      return false;
     }
   if(S6_RelRangeLookback < 2)
      PrintFormat("S6 warning: relative-range lookback %d is too small, using 20.", S6_RelRangeLookback);

   g_s6Magic = GlobalBaseMagic + S6_OFFSET;
   g_s6Day = 0;
   g_s6Hi = 0.0;
   g_s6Lo = 0.0;
   g_s6RangePct = 0.0;
   g_s6Locked = false;
   g_s6Placed = false;
   g_s6Finished = false;
   g_s6SpreadBlocked = false;
   g_s6Entries = 0;
   g_s6HasLong = false;
   g_s6HasShort = false;
   g_s6AtrBarTime = 0;
   g_s6AtrHandle = INVALID_HANDLE;

   S6_Trade.SetDeviationInPoints(10);
   S6_SeedHistory();
   PrintFormat("S6 BOB ready: %s BOTH SIDES | range %02d:00-%02d:00 UTC exit %02d:%02d UTC | trim %.2f | stop %.2f%% | lots %.2f | rel >= %.2f over %d sessions | spread cap %.4f%% | max entries/day %d",
               g_s6Symbol, S6_RangeStartHour, S6_RangeEndHour, S6_ExitHour, S6_ExitMinute,
               S6_RangeTrim, S6_StopPct, S6_Lots, S6_RelRangeMin, S6_RelRangeLookback,
               S6_MaxSpreadPct, S6_MaxEntries());
   return true;
  }

void DeinitStrategy6()
  {
  }

void OnTickStrategy6()
  {
   S6_Process();
  }

//+------------------------------------------------------------------+
//| S7: GOTOBI (TOKYO FIX)                                            |
//| JST calendar, event dates rolled back to the previous JP business  |
//| day, JST Mondays skipped. Long 03:00 JST -> 09:55 JST; short       |
//| 09:55 -> 10:20 JST. ATR H1(14) x 2.5 stop. Fixed USD / percent /   |
//| fixed lots sizing. FX 3-pip spread cap, non-FX 3-bps cap.          |
//+------------------------------------------------------------------+
int JapanClosedYmd[] =
  {
   20000103, 20000110, 20000211, 20000320, 20000503, 20000504, 20000505, 20000720, 20000915, 20001009,
   20001103, 20001123, 20010101, 20010102, 20010103, 20010108, 20010212, 20010320, 20010430, 20010503,
   20010504, 20010720, 20010924, 20011008, 20011123, 20011224, 20011231, 20020101, 20020102, 20020103,
   20020114, 20020211, 20020321, 20020429, 20020503, 20020506, 20020916, 20020923, 20021014, 20021104,
   20021223, 20021231, 20030101, 20030102, 20030103, 20030113, 20030211, 20030321, 20030429, 20030505,
   20030721, 20030915, 20030923, 20031013, 20031103, 20031124, 20031223, 20031231, 20040101, 20040102,
   20040112, 20040211, 20040429, 20040503, 20040504, 20040505, 20040719, 20040920, 20040923, 20041011,
   20041103, 20041123, 20041223, 20041231, 20050103, 20050110, 20050211, 20050321, 20050429, 20050503,
   20050504, 20050505, 20050718, 20050919, 20050923, 20051010, 20051103, 20051123, 20051223, 20051231,
   20060102, 20060103, 20060109, 20060321, 20060503, 20060504, 20060505, 20060717, 20060918, 20061009,
   20061103, 20061123, 20070101, 20070102, 20070103, 20070108, 20070212, 20070321, 20070430, 20070503,
   20070504, 20070716, 20070917, 20070924, 20071008, 20071123, 20071224, 20071231, 20080101, 20080102,
   20080103, 20080114, 20080211, 20080320, 20080429, 20080505, 20080506, 20080721, 20080915, 20080923,
   20081013, 20081103, 20081124, 20081223, 20081231, 20090101, 20090102, 20090112, 20090211, 20090320,
   20090429, 20090504, 20090505, 20090506, 20090720, 20090921, 20090922, 20090923, 20091012, 20091103,
   20091123, 20091223, 20091231, 20100101, 20100111, 20100211, 20100322, 20100429, 20100503, 20100504,
   20100505, 20100719, 20100920, 20100923, 20101011, 20101103, 20101123, 20101223, 20101231, 20110103,
   20110110, 20110211, 20110321, 20110429, 20110503, 20110504, 20110505, 20110718, 20110919, 20110923,
   20111010, 20111103, 20111123, 20111223, 20120102, 20120103, 20120109, 20120320, 20120430, 20120503,
   20120504, 20120716, 20120917, 20121008, 20121123, 20121224, 20121231, 20130101, 20130102, 20130103,
   20130114, 20130211, 20130320, 20130429, 20130503, 20130506, 20130715, 20130916, 20130923, 20131014,
   20131104, 20131223, 20131231, 20140101, 20140102, 20140103, 20140113, 20140211, 20140321, 20140429,
   20140505, 20140506, 20140721, 20140915, 20140923, 20141013, 20141103, 20141124, 20141223, 20141231,
   20150101, 20150102, 20150112, 20150211, 20150429, 20150504, 20150505, 20150506, 20150720, 20150921,
   20150922, 20150923, 20151012, 20151103, 20151123, 20151223, 20151231, 20160101, 20160111, 20160211,
   20160321, 20160429, 20160503, 20160504, 20160505, 20160718, 20160811, 20160919, 20160922, 20161010,
   20161103, 20161123, 20161223, 20170101, 20170102, 20170103, 20170109, 20170320, 20170503, 20170504,
   20170505, 20170717, 20170811, 20170918, 20171009, 20171103, 20171123, 20180101, 20180102, 20180103,
   20180108, 20180212, 20180321, 20180430, 20180503, 20180504, 20180716, 20180917, 20180924, 20181008,
   20181123, 20181224, 20181231, 20190101, 20190102, 20190103, 20190114, 20190211, 20190321, 20190429,
   20190430, 20190501, 20190502, 20190503, 20190506, 20190715, 20190812, 20190916, 20190923, 20191014,
   20191022, 20191104, 20191231, 20200101, 20200102, 20200103, 20200113, 20200211, 20200224, 20200320,
   20200429, 20200504, 20200505, 20200506, 20200723, 20200724, 20200810, 20200921, 20200922, 20201001,
   20201103, 20201123, 20201231, 20210101, 20210111, 20210211, 20210223, 20210429, 20210503, 20210504,
   20210505, 20210722, 20210723, 20210809, 20210920, 20210923, 20211103, 20211123, 20211231, 20220103,
   20220110, 20220211, 20220223, 20220321, 20220429, 20220503, 20220504, 20220505, 20220718, 20220811,
   20220919, 20220923, 20221010, 20221103, 20221123, 20221231, 20230102, 20230103, 20230109, 20230223,
   20230321, 20230429, 20230503, 20230504, 20230505, 20230717, 20230811, 20230918, 20231009, 20231103,
   20231123, 20240101, 20240102, 20240103, 20240108, 20240212, 20240223, 20240320, 20240429, 20240503,
   20240506, 20240715, 20240812, 20240916, 20240923, 20241014, 20241104, 20241231, 20250101, 20250102,
   20250103, 20250113, 20250211, 20250224, 20250320, 20250429, 20250505, 20250506, 20250721, 20250811,
   20250915, 20250923, 20251013, 20251103, 20251124, 20251231, 20260101, 20260102, 20260112, 20260211,
   20260223, 20260320, 20260429, 20260504, 20260505, 20260506, 20260720, 20260811, 20260921, 20260923,
   20261012, 20261103, 20261123, 20261231, 20270101, 20270111, 20270211, 20270223, 20270322, 20270429,
   20270503, 20270504, 20270505, 20270719, 20270811, 20270920, 20270923, 20271011, 20271103, 20271123,
   20271231
  };

int g_s7LastEventLogDay = 0;
int g_s7LastLongMissedDay = 0;
int g_s7LastShortMissedDay = 0;
datetime g_s7LastLongAttempt = 0;
datetime g_s7LastShortAttempt = 0;
int g_s7RiskTrimmedPositions = 0;
int g_s7RiskTrimTransactions = 0;
int g_s7RiskEmergencyCloses = 0;

int S7_DaysInMonth(const int year, const int month)
  {
   if(month == 2)
      return ((year % 4 == 0 && year % 100 != 0) || year % 400 == 0) ? 29 : 28;
   if(month == 4 || month == 6 || month == 9 || month == 11)
      return 30;
   return 31;
  }

bool S7_IsJpClosedDate(const int ymd)
  {
   int left = 0;
   int right = ArraySize(JapanClosedYmd) - 1;
   while(left <= right)
     {
      int mid = (left + right) / 2;
      if(JapanClosedYmd[mid] == ymd)
         return true;
      if(JapanClosedYmd[mid] < ymd)
         left = mid + 1;
      else
         right = mid - 1;
     }
   return false;
  }

bool S7_IsJpBusinessDay(const datetime date)
  {
   MqlDateTime dt;
   TimeToStruct(date, dt);
   if(dt.year < 2000 || dt.year > 2027)
      return false;
   if(dt.day_of_week == 0 || dt.day_of_week == 6)
      return false;
   return !S7_IsJpClosedDate(Ymd(date));
  }

datetime S7_RollToPrevBusiness(datetime date)
  {
   for(int k = 0; k < 12; k++)
     {
      if(S7_IsJpBusinessDay(date))
         return date;
      date = (datetime)((long)date - 86400);
     }
   return 0;
  }

bool S7_IsGotobiEventDate(const datetime jst)
  {
   int today = Ymd(jst);
   MqlDateTime jd;
   TimeToStruct(jst, jd);

   for(int forward = 0; forward <= 1; forward++)
     {
      int y = jd.year;
      int m = jd.mon + forward;
      if(m > 12)
        {
         m -= 12;
         y++;
        }
      int dim = S7_DaysInMonth(y, m);
      int candidates[8];
      int count = 0;
      candidates[count++] = 5;
      candidates[count++] = 10;
      candidates[count++] = 15;
      candidates[count++] = 20;
      candidates[count++] = 25;
      if(S7_Calendar == GOTOBI_CAL_5_30 || S7_Calendar == GOTOBI_CAL_30_EOM)
         candidates[count++] = 30;
      if(S7_Calendar == GOTOBI_CAL_5_25_EOM || S7_Calendar == GOTOBI_CAL_30_EOM)
         candidates[count++] = dim;

      for(int c = 0; c < count; c++)
        {
         int dom = candidates[c];
         if(dom > dim)
            continue;
         datetime candidate = YmdToTime(YmdOf(y, m, dom));
         datetime rolled = S7_RollToPrevBusiness(candidate);
         if(rolled != 0 && Ymd(rolled) == today)
            return true;
        }
     }
   return false;
  }

bool S7_EventPassesFilter(const datetime jst)
  {
   if(!S7_IsJpBusinessDay(jst))
      return false;
   MqlDateTime dt;
   TimeToStruct(jst, dt);
   if(S7_SkipJstMondays && dt.day_of_week == 1)
      return false;

   bool gotobi = S7_IsGotobiEventDate(jst);
   if(S7_Control == GOTOBI_ALL_BANK_DAYS)
      return true;
   if(S7_Control == GOTOBI_NON_EVENT_DAYS)
      return !gotobi;
   return gotobi;
  }

double S7_Value(const int handle, const int shift)
  {
   if(handle == INVALID_HANDLE)
      return 0.0;
   double buf[];
   ArraySetAsSeries(buf, true);
   if(CopyBuffer(handle, 0, shift, 1, buf) <= 0)
      return 0.0;
   return buf[0];
  }

double S7_StopDistance()
  {
   double atr = S7_Value(g_s7AtrHandle, 1);
   if(atr <= 0.0)
      return 0.0;
   return atr * S7_AtrMult;
  }

bool S7_HasOwnPosition()
  {
   for(int i = PositionsTotal() - 1; i >= 0; i--)
     {
      ulong ticket = PositionGetTicket(i);
      if(ticket == 0)
         continue;
      if(PositionGetString(POSITION_SYMBOL) != g_s7Symbol)
         continue;
      if(PositionGetInteger(POSITION_MAGIC) != g_s7Magic)
         continue;
      return true;
     }
   return false;
  }

bool S7_HasPriorEntry(const int jstDay, const bool longLeg)
  {
   if(longLeg ? g_s7LastLongEntryDay == jstDay : g_s7LastShortEntryDay == jstDay)
      return true;

   datetime utcStart = (datetime)((long)YmdToTime(jstDay) - 9 * 3600);
   datetime srvStart = UtcToServer(utcStart);
   if(!HistorySelect(srvStart, (datetime)((long)TimeCurrent() + 1)))
      return false;

   long wanted = longLeg ? (long)DEAL_TYPE_BUY : (long)DEAL_TYPE_SELL;
   int total = HistoryDealsTotal();
   for(int i = 0; i < total; i++)
     {
      ulong ticket = HistoryDealGetTicket(i);
      if(ticket == 0)
         continue;
      if(HistoryDealGetInteger(ticket, DEAL_MAGIC) != g_s7Magic)
         continue;
      if(HistoryDealGetString(ticket, DEAL_SYMBOL) != g_s7Symbol)
         continue;
      if(HistoryDealGetInteger(ticket, DEAL_ENTRY) != DEAL_ENTRY_IN)
         continue;
      if(HistoryDealGetInteger(ticket, DEAL_TYPE) != wanted)
         continue;
      datetime dealTime = (datetime)HistoryDealGetInteger(ticket, DEAL_TIME);
      datetime dealJst = (datetime)((long)ServerToUtc(dealTime) + 9 * 3600);
      if(Ymd(dealJst) == jstDay)
         return true;
     }
   return false;
  }

void S7_CloseExpired(const datetime jst)
  {
   int today = Ymd(jst);
   MqlDateTime jd;
   TimeToStruct(jst, jd);
   int nowMinutes = jd.hour * 60 + jd.min;
   int longEnd = S7_FixHourJst * 60 + S7_FixMinuteJst;
   int shortEnd = S7_ShortExitHourJst * 60 + S7_ShortExitMinuteJst;

   for(int i = PositionsTotal() - 1; i >= 0; i--)
     {
      ulong ticket = PositionGetTicket(i);
      if(ticket == 0)
         continue;
      if(PositionGetString(POSITION_SYMBOL) != g_s7Symbol)
         continue;
      if(PositionGetInteger(POSITION_MAGIC) != g_s7Magic)
         continue;

      datetime posTime = (datetime)PositionGetInteger(POSITION_TIME);
      datetime posJst = (datetime)((long)ServerToUtc(posTime) + 9 * 3600);
      int positionDay = Ymd(posJst);
      bool isLong = (PositionGetInteger(POSITION_TYPE) == POSITION_TYPE_BUY);
      int due = isLong ? longEnd : shortEnd;

      if(positionDay < today || (positionDay == today && nowMinutes >= due))
        {
         double volume = PositionGetDouble(POSITION_VOLUME);
         if(CloseTicket(ticket))
           {
            if(S7_LogEvents)
               PrintFormat("GOTOBI EXIT | #%I64u | jst %s | long %d | volume %.2f",
                           ticket, TimeToString(jst, TIME_DATE | TIME_MINUTES),
                           isLong ? 1 : 0, volume);
           }
         else
           {
            g_s7ExitFailCount++;
            PrintFormat("GOTOBI EXIT_UNCONFIRMED | #%I64u | jst %s | volume %.2f",
                        ticket, TimeToString(jst, TIME_DATE | TIME_MINUTES), volume);
           }
        }
     }
  }

void S7_GuardStops(const datetime utc, const bool force)
  {
   if(!force && g_s7LastStopGuard != 0 && (utc - g_s7LastStopGuard) < 30)
      return;
   g_s7LastStopGuard = utc;

   for(int i = PositionsTotal() - 1; i >= 0; i--)
     {
      ulong ticket = PositionGetTicket(i);
      if(ticket == 0)
         continue;
      if(PositionGetString(POSITION_SYMBOL) != g_s7Symbol)
         continue;
      if(PositionGetInteger(POSITION_MAGIC) != g_s7Magic)
         continue;

      bool isLong = (PositionGetInteger(POSITION_TYPE) == POSITION_TYPE_BUY);
      double sl = PositionGetDouble(POSITION_SL);
      double entry = PositionGetDouble(POSITION_PRICE_OPEN);
      bool valid = (sl > 0.0) && (isLong ? sl < entry : sl > entry);
      if(valid)
         continue;

      PrintFormat("GOTOBI UNPROTECTED | #%I64u | sl %.5f | entry %.5f | action CLOSE",
                  ticket, sl, entry);
      if(!CloseTicket(ticket))
         PrintFormat("GOTOBI EMERGENCY_CLOSE_UNCONFIRMED | #%I64u", ticket);
     }
  }

bool S7_M1OpenAt(const datetime jstTime, double &openPrice)
  {
   openPrice = 0.0;
   datetime utc = (datetime)((long)jstTime - 9 * 3600);
   datetime srv = UtcToServer(utc);
   MqlRates rates[];
   int n = CopyRates(g_s7Symbol, PERIOD_M1, (datetime)((long)srv - 60), (datetime)((long)srv + 60), rates);
   if(n <= 0)
      return false;
   for(int i = 0; i < n; i++)
     {
      if(rates[i].time == srv)
        {
         openPrice = rates[i].open;
         return (openPrice > 0.0);
        }
     }
   return false;
  }

bool S7_RequireRise(const datetime jst)
  {
   datetime dayStart = DayStart(jst);
   datetime fromTime = (datetime)((long)dayStart + (long)S7_LongHourJst * 3600 + (long)S7_LongMinuteJst * 60);
   datetime toTime = (datetime)((long)dayStart + 9 * 3600);
   double fromPrice = 0.0;
   double toPrice = 0.0;
   if(!S7_M1OpenAt(fromTime, fromPrice) || !S7_M1OpenAt(toTime, toPrice))
     {
      Print("GOTOBI SKIP_SHORT | pre-fix M1 price missing");
      return false;
     }
   return (toPrice > fromPrice);
  }

double S7_RiskLots(const bool longLeg, const double entry, const double sl, const double distance)
  {
   double vmin = SymbolInfoDouble(g_s7Symbol, SYMBOL_VOLUME_MIN);
   double vmax = SymbolInfoDouble(g_s7Symbol, SYMBOL_VOLUME_MAX);
   double step = SymbolInfoDouble(g_s7Symbol, SYMBOL_VOLUME_STEP);
   if(vmin <= 0.0 || vmax < vmin || step <= 0.0)
      return 0.0;

   double desired = 0.0;
   double budget = 0.0;

   if(S7_RiskMode == GOTOBI_FIXED_LOTS)
      desired = S7_Lots;
   else
     {
      if(S7_RiskMode == GOTOBI_PERCENT_RISK)
         budget = AccountInfoDouble(ACCOUNT_EQUITY) * S7_RiskPercent / 100.0;
      else
        {
         budget = S7_FixedRiskUsd;
         if(AccountInfoDouble(ACCOUNT_BALANCE) < budget || AccountInfoDouble(ACCOUNT_EQUITY) <= 0.0)
           {
            PrintFormat("GOTOBI RISK_REJECT | budget %.2f exceeds available balance/equity", budget);
            return 0.0;
           }
        }
      if(budget <= 0.0)
         return 0.0;
      double tickValue = SymbolInfoDouble(g_s7Symbol, SYMBOL_TRADE_TICK_VALUE);
      double tickSize = SymbolInfoDouble(g_s7Symbol, SYMBOL_TRADE_TICK_SIZE);
      if(tickValue <= 0.0 || tickSize <= 0.0)
         return 0.0;
      double perLot = distance / tickSize * tickValue;
      if(perLot <= 0.0)
         return 0.0;
      desired = budget / perLot;
     }

   if(desired < vmin - 1e-9)
      return 0.0;

   desired = MathMin(desired, vmax);
   double volume = vmin + MathFloor((desired - vmin) / step + 1e-9) * step;
   volume = NormalizeDouble(volume, 8);
   if(volume < vmin - 1e-9 || volume > vmax + 1e-9)
      return 0.0;

   if(S7_RiskMode != GOTOBI_FIXED_LOTS)
     {
      double tickValue = SymbolInfoDouble(g_s7Symbol, SYMBOL_TRADE_TICK_VALUE);
      double tickSize = SymbolInfoDouble(g_s7Symbol, SYMBOL_TRADE_TICK_SIZE);
      if(tickValue <= 0.0 || tickSize <= 0.0)
         return 0.0;
      for(int j = 0; j < 1000; j++)
        {
         double risk = distance / tickSize * tickValue * volume;
         if(risk <= budget + 0.005)
            break;
         volume = NormalizeDouble(volume - step, 8);
         if(volume < vmin - 1e-9)
            return 0.0;
        }
      double finalRisk = distance / tickSize * tickValue * volume;
      if(finalRisk > budget + 0.005)
         return 0.0;
     }

   double margin = 0.0;
   if(!OrderCalcMargin(longLeg ? ORDER_TYPE_BUY : ORDER_TYPE_SELL, g_s7Symbol, volume, entry, margin))
      return 0.0;
   if(margin > AccountInfoDouble(ACCOUNT_MARGIN_FREE) + 0.001)
     {
      PrintFormat("GOTOBI MARGIN_SKIP | required %.2f | free %.2f",
                  margin, AccountInfoDouble(ACCOUNT_MARGIN_FREE));
      return 0.0;
     }
   return volume;
  }

bool S7_CheckActualFixedRisk()
  {
   if(S7_RiskMode != GOTOBI_FIXED_USD)
      return true;

   bool allWithin = true;
   int ownedPositions = 0;

   for(int i = PositionsTotal() - 1; i >= 0; i--)
     {
      ulong ticket = PositionGetTicket(i);
      if(ticket == 0)
         continue;
      if(PositionGetString(POSITION_SYMBOL) != g_s7Symbol)
         continue;
      if(PositionGetInteger(POSITION_MAGIC) != g_s7Magic)
         continue;

      ownedPositions++;
      bool compliant = false;
      bool trimmed = false;
      double lastRisk = 0.0;
      double tickValue = SymbolInfoDouble(g_s7Symbol, SYMBOL_TRADE_TICK_VALUE);
      double tickSize = SymbolInfoDouble(g_s7Symbol, SYMBOL_TRADE_TICK_SIZE);
      double vmin = SymbolInfoDouble(g_s7Symbol, SYMBOL_VOLUME_MIN);
      double step = SymbolInfoDouble(g_s7Symbol, SYMBOL_VOLUME_STEP);

      for(int attempt = 0; attempt < 8; attempt++)
        {
         if(!PositionSelectByTicket(ticket))
            break;
         double sl = PositionGetDouble(POSITION_SL);
         double entry = PositionGetDouble(POSITION_PRICE_OPEN);
         bool isLong = (PositionGetInteger(POSITION_TYPE) == POSITION_TYPE_BUY);
         double volume = PositionGetDouble(POSITION_VOLUME);
         bool validSl = (sl > 0.0) && (isLong ? sl < entry : sl > entry);
         double risk = 0.0;
         if(validSl && tickValue > 0.0 && tickSize > 0.0)
            risk = MathAbs(entry - sl) / tickSize * tickValue * volume;
         lastRisk = risk;

         if(validSl && risk <= S7_FixedRiskUsd + 0.005)
           {
            compliant = true;
            if(trimmed)
               g_s7RiskTrimmedPositions++;
            if(S7_LogEvents)
               PrintFormat("GOTOBI POSTFILL_RISK_OK | #%I64u | volume %.2f | gross risk %.2f | limit %.2f | trimmed %d",
                           ticket, volume, risk, S7_FixedRiskUsd, trimmed ? 1 : 0);
            break;
           }

         if(!validSl)
            break;
         if(vmin <= 0.0 || step <= 0.0 || volume <= vmin + 1e-8)
            break;

         double wanted = volume * (S7_FixedRiskUsd * 0.98 / risk);
         if(wanted < vmin - 1e-8)
            break;
         double target = vmin + MathFloor((wanted - vmin) / step + 1e-9) * step;
         target = NormalizeDouble(target, 8);
         if(target >= volume - 1e-8)
            target = NormalizeDouble(volume - step, 8);
         if(target < vmin - 1e-8 || target >= volume - 1e-8)
            break;

         double cut = NormalizeDouble(volume - target, 8);
         S7_Trade.SetTypeFillingBySymbol(g_s7Symbol);
         if(S7_Trade.PositionClosePartial(ticket, cut))
           {
            trimmed = true;
            g_s7RiskTrimTransactions++;
            PrintFormat("GOTOBI RISK_TRIM | #%I64u | before %.2f | cut %.2f | pre-trim SL risk %.2f | limit %.2f",
                        ticket, volume, cut, risk, S7_FixedRiskUsd);
            continue;
           }
         PrintFormat("GOTOBI RISK_TRIM_UNCONFIRMED | #%I64u | before %.2f | cut %.2f",
                     ticket, volume, cut);
         break;
        }

      if(compliant)
         continue;

      allWithin = false;
      if(PositionSelectByTicket(ticket))
        {
         PrintFormat("GOTOBI POSTFILL_RISK_FAIL | #%I64u | volume %.2f | gross risk %.2f | limit %.2f | action EMERGENCY_CLOSE",
                     ticket, PositionGetDouble(POSITION_VOLUME), lastRisk, S7_FixedRiskUsd);
         if(CloseTicket(ticket))
            g_s7RiskEmergencyCloses++;
         else
            PrintFormat("GOTOBI POSTFILL_RISK_CLOSE_UNCONFIRMED | #%I64u", ticket);
        }
     }

   if(ownedPositions == 0)
     {
      Print("GOTOBI POSTFILL_RISK_FAIL | no position after entry confirmation");
      return false;
     }
   return allWithin;
  }

void S7_LogSkip(const bool isLong, const int day, const string reason)
  {
   if(isLong)
     {
      if(g_s7LastLongSkipDay == day)
         return;
      g_s7LastLongSkipDay = day;
      g_s7SkipLongCount++;
     }
   else
     {
      if(g_s7LastShortSkipDay == day)
         return;
      g_s7LastShortSkipDay = day;
      g_s7SkipShortCount++;
     }
   if(S7_LogEvents)
      PrintFormat("GOTOBI SKIP | date %d | leg %s | reason %s", day, isLong ? "L" : "S", reason);
  }

void S7_TryOpenLeg(const bool longLeg, const datetime jst, const datetime utc)
  {
   if(!g_meqCtl.s7)
      return;
   if(S7_HasOwnPosition())
      return;

   int today = Ymd(jst);
   if(S7_HasPriorEntry(today, longLeg))
      return;

   if(PortfolioGuardBlocked())
     {
      S7_LogSkip(longLeg, today, "PORTFOLIO_GUARD");
      return;
     }
   if(DirectionCapBlocked(g_s7Symbol, longLeg, "GOTOBI"))
     {
      S7_LogSkip(longLeg, today, "DIRECTION_CAP");
      return;
     }
   if(!longLeg && S7_RequireRiseForShort && !S7_RequireRise(jst))
     {
      S7_LogSkip(longLeg, today, "NO_PRE_FIX_RISE_OR_M1_MISSING");
      return;
     }

   string why = "";
   if(!SymbolCanTrade(g_s7Symbol, longLeg, why))
     {
      S7_LogSkip(longLeg, today, why);
      return;
     }

   double ask = 0.0, bid = 0.0;
   if(!SymbolQuoteReady(g_s7Symbol, ask, bid, why))
     {
      S7_LogSkip(longLeg, today, why);
      return;
     }

   double pip = PipSizeFor(g_s7Symbol);
   if(pip <= 0.0)
     {
      S7_LogSkip(longLeg, today, "INVALID_PIP_SIZE");
      return;
     }

   bool fxPair = IsFxPairName(g_s7Symbol);
   double spreadPrice = ask - bid;
   double spreadPips = spreadPrice / pip;
   double mid = (ask + bid) * 0.5;
   double spreadBps = (mid > 0.0) ? spreadPrice / mid * 10000.0 : 1e308;
   double spreadValue = fxPair ? spreadPips : spreadBps;
   double spreadCap = fxPair ? S7_MaxSpreadPips : S7_MaxSpreadBps;
   if(spreadValue > spreadCap)
     {
      S7_LogSkip(longLeg, today, fxPair ? "SPREAD_ABOVE_CAP" : "SPREAD_BPS_ABOVE_CAP");
      return;
     }

   double distance = S7_StopDistance();
   if(distance <= 0.0)
     {
      S7_LogSkip(longLeg, today, "ATR_NOT_READY");
      return;
     }

   double point = SymbolInfoDouble(g_s7Symbol, SYMBOL_POINT);
   double minStop = ((double)SymbolInfoInteger(g_s7Symbol, SYMBOL_TRADE_STOPS_LEVEL) + 3.0) * point;
   distance = MathMax(distance, minStop + spreadPrice);

   double tickSize = SymbolInfoDouble(g_s7Symbol, SYMBOL_TRADE_TICK_SIZE);
   if(tickSize <= 0.0)
      tickSize = point;
   if(tickSize <= 0.0)
     {
      S7_LogSkip(longLeg, today, "INVALID_TICK_SIZE");
      return;
     }

   double entry = longLeg ? ask : bid;
   double rawStop = entry + (longLeg ? -distance : distance);
   double sl = longLeg ? MathFloor(rawStop / tickSize + 1e-10) * tickSize
                       : MathCeil(rawStop / tickSize - 1e-10) * tickSize;
   sl = NormalizeDouble(sl, (int)SymbolInfoInteger(g_s7Symbol, SYMBOL_DIGITS));
   if(sl <= 0.0 || (longLeg && sl >= entry) || (!longLeg && sl <= entry))
     {
      S7_LogSkip(longLeg, today, "INVALID_SL_SIDE");
      return;
     }

   double volume = S7_RiskLots(longLeg, entry, sl, distance);
   if(volume <= 0.0)
     {
      S7_LogSkip(longLeg, today, "RISK_SIZE_BELOW_MIN_OR_MARGIN_CALC");
      return;
     }

   if(!MarketOrderLevelsOk(g_s7Symbol, longLeg, sl, 0.0, why))
     {
      S7_LogSkip(longLeg, today, why);
      return;
     }

   datetime last = longLeg ? g_s7LastLongAttempt : g_s7LastShortAttempt;
   if(last != 0 && (utc - last) < 5)
      return;
   if(longLeg)
      g_s7LastLongAttempt = utc;
   else
      g_s7LastShortAttempt = utc;

   S7_Trade.SetExpertMagicNumber(g_s7Magic);
   S7_Trade.SetTypeFillingBySymbol(g_s7Symbol);
   // Price 0 = current ask/bid at send time.
   bool ok = longLeg ? S7_Trade.Buy(volume, g_s7Symbol, 0.0, sl, 0.0, GlobalComment)
                     : S7_Trade.Sell(volume, g_s7Symbol, 0.0, sl, 0.0, GlobalComment);

   if(ok)
     {
      S7_GuardStops(utc, true);
      bool withinRisk = S7_CheckActualFixedRisk();
      if(longLeg)
        {
         g_s7EntryLongCount++;
         g_s7LastLongEntryDay = today;
        }
      else
        {
         g_s7EntryShortCount++;
         g_s7LastShortEntryDay = today;
        }
      if(S7_LogEvents)
         PrintFormat("GOTOBI ENTER | %s | jst %s | utc %s | volume %.2f | entry %.5f | sl %.5f | spread %.2f %s",
                     longLeg ? "LONG" : "SHORT", TimeToString(jst, TIME_DATE | TIME_MINUTES),
                     TimeToString(utc, TIME_DATE | TIME_MINUTES), volume, entry, sl,
                     spreadValue, fxPair ? "pips" : "bps");
      if(!withinRisk)
         Print("GOTOBI RISK_ALERT | risk cap enforced by emergency exit request");
     }
   else
     {
      g_s7EntryFailCount++;
      PrintFormat("GOTOBI ENTRY_UNCONFIRMED | %s | jst %s | requested %.2f | retcode %d (%s)",
                  longLeg ? "LONG" : "SHORT", TimeToString(jst, TIME_DATE | TIME_MINUTES),
                  volume, S7_Trade.ResultRetcode(), S7_Trade.ResultRetcodeDescription());
      S7_GuardStops(utc, true);
     }
  }

void S7_AuditPriorEvent(const datetime jst)
  {
   int today = Ymd(jst);
   int previous = g_s7LastObservedEventDay;
   if(previous == 0 || previous == today)
      return;

   if(S7_Legs != GOTOBI_SHORT_ONLY && g_s7LastLongMissedDay != previous && !S7_HasPriorEntry(previous, true))
     {
      PrintFormat("GOTOBI WINDOW_MISSED_NO_TICKS | day %d | leg L | diagnosed_on %d", previous, today);
      g_s7LastLongMissedDay = previous;
     }
   if(S7_Legs != GOTOBI_LONG_ONLY && g_s7LastShortMissedDay != previous && !S7_HasPriorEntry(previous, false))
     {
      PrintFormat("GOTOBI WINDOW_MISSED_NO_TICKS | day %d | leg S | diagnosed_on %d", previous, today);
      g_s7LastShortMissedDay = previous;
     }
   g_s7LastObservedEventDay = 0;
  }

void S7_AuditMissed(const datetime jst)
  {
   int today = Ymd(jst);
   MqlDateTime jd;
   TimeToStruct(jst, jd);
   int m = jd.hour * 60 + jd.min;
   int longStart = S7_LongHourJst * 60 + S7_LongMinuteJst;
   int fixing = S7_FixHourJst * 60 + S7_FixMinuteJst;

   if(S7_Legs != GOTOBI_SHORT_ONLY && m > longStart + S7_EntryGraceMinutes && g_s7LastLongMissedDay != today)
     {
      g_s7LastLongMissedDay = today;
      if(!S7_HasPriorEntry(today, true))
         PrintFormat("GOTOBI WINDOW_MISSED | day %d | leg L | deadline %02d:%02d JST",
                     today, (longStart + S7_EntryGraceMinutes) / 60, (longStart + S7_EntryGraceMinutes) % 60);
     }
   if(S7_Legs != GOTOBI_LONG_ONLY && m > fixing + S7_EntryGraceMinutes && g_s7LastShortMissedDay != today)
     {
      g_s7LastShortMissedDay = today;
      if(!S7_HasPriorEntry(today, false))
         PrintFormat("GOTOBI WINDOW_MISSED | day %d | leg S | deadline %02d:%02d JST",
                     today, (fixing + S7_EntryGraceMinutes) / 60, (fixing + S7_EntryGraceMinutes) % 60);
     }
  }

void S7_Process()
  {
   if(!S7_Enabled)
      return;

   datetime utc = ServerToUtc(TimeCurrent());
   datetime jst = (datetime)((long)utc + 9 * 3600);

   S7_CloseExpired(jst);
   S7_GuardStops(utc, false);

   MqlDateTime jd;
   TimeToStruct(jst, jd);
   if(jd.year < 2000 || jd.year > 2027)
      return;

   S7_AuditPriorEvent(jst);
   if(!S7_EventPassesFilter(jst))
      return;

   int today = Ymd(jst);
   g_s7LastObservedEventDay = today;
   S7_AuditMissed(jst);

   if(S7_HasOwnPosition())
      return;

   int m = jd.hour * 60 + jd.min;
   int longStart = S7_LongHourJst * 60 + S7_LongMinuteJst;
   int fixing = S7_FixHourJst * 60 + S7_FixMinuteJst;

   if(S7_LogEvents && g_s7LastEventLogDay != today &&
      ((m >= longStart && m <= longStart + S7_EntryGraceMinutes) ||
       (m >= fixing && m <= fixing + S7_EntryGraceMinutes)))
     {
      PrintFormat("GOTOBI EVENT | jst %s | utc %s | fix %02d:%02d | calendar %d",
                  TimeToString(jst, TIME_DATE | TIME_MINUTES), TimeToString(utc, TIME_DATE | TIME_MINUTES),
                  S7_FixHourJst, S7_FixMinuteJst, (int)S7_Calendar);
      g_s7LastEventLogDay = today;
     }

   if(S7_Legs != GOTOBI_SHORT_ONLY && m >= longStart && m <= longStart + S7_EntryGraceMinutes)
      S7_TryOpenLeg(true, jst, utc);
   else if(S7_Legs != GOTOBI_LONG_ONLY && m >= fixing && m <= fixing + S7_EntryGraceMinutes)
      S7_TryOpenLeg(false, jst, utc);
  }

bool InitStrategy7()
  {
   g_s7Symbol = S7_Symbol;
   StringTrimLeft(g_s7Symbol);
   StringTrimRight(g_s7Symbol);
   if(StringLen(g_s7Symbol) == 0)
      g_s7Symbol = _Symbol;
   SymbolSelect(g_s7Symbol, true);

   if(SymbolInfoDouble(g_s7Symbol, SYMBOL_POINT) <= 0.0)
     {
      PrintFormat("S7 error: symbol '%s' is not available", g_s7Symbol);
      return false;
     }
   if(S7_RequireJpySymbol && !IsJpyExposure(g_s7Symbol))
     {
      PrintFormat("S7 error: JPY exposure required; resolved %s", g_s7Symbol);
      return false;
     }
   if(S7_RiskMode == GOTOBI_FIXED_USD)
     {
      string acct = AccountInfoString(ACCOUNT_CURRENCY);
      if(acct != "USD")
        {
         PrintFormat("S7 error: fixed USD risk requires a USD account, found %s", acct);
         return false;
        }
     }

   int entry = S7_LongHourJst * 60 + S7_LongMinuteJst;
   int fixing = S7_FixHourJst * 60 + S7_FixMinuteJst;
   int finish = S7_ShortExitHourJst * 60 + S7_ShortExitMinuteJst;
   if(S7_AtrPeriod < 1 || S7_AtrMult <= 0.0 || S7_MaxSpreadPips < 0.0 || S7_MaxSpreadBps < 0.0 ||
      S7_EntryGraceMinutes < 0 || S7_EntryGraceMinutes > 10 ||
      S7_RiskPercent < 0.0 || S7_Lots < 0.0 || S7_FixedRiskUsd <= 0.0 ||
      S7_LongHourJst < 0 || S7_LongHourJst > 23 || S7_FixHourJst < 0 || S7_FixHourJst > 23 ||
      S7_ShortExitHourJst < 0 || S7_ShortExitHourJst > 23 ||
      S7_LongMinuteJst < 0 || S7_LongMinuteJst > 59 || S7_FixMinuteJst < 0 || S7_FixMinuteJst > 59 ||
      S7_ShortExitMinuteJst < 0 || S7_ShortExitMinuteJst > 59 ||
      entry + S7_EntryGraceMinutes >= fixing ||
      fixing + S7_EntryGraceMinutes >= finish)
     {
      Print("S7 error: invalid Gotobi parameters.");
      return false;
     }

   g_s7Magic = GlobalBaseMagic + S7_OFFSET;
   g_s7LongLabel = S7_LabelPrefix + "L";
   g_s7ShortLabel = S7_LabelPrefix + "S";
   g_s7AtrHandle = iATR(g_s7Symbol, PERIOD_H1, S7_AtrPeriod);
   if(g_s7AtrHandle == INVALID_HANDLE)
     {
      PrintFormat("S7 error: could not create the ATR handle for %s", g_s7Symbol);
      return false;
     }

   g_s7LastStopGuard = 0;
   g_s7LastLongSkipDay = 0;
   g_s7LastShortSkipDay = 0;
   g_s7LastLongEntryDay = 0;
   g_s7LastShortEntryDay = 0;
   g_s7LastObservedEventDay = 0;
   g_s7LastEventLogDay = 0;
   g_s7LastLongMissedDay = 0;
   g_s7LastShortMissedDay = 0;
   g_s7LastLongAttempt = 0;
   g_s7LastShortAttempt = 0;
   g_s7EntryLongCount = 0;
   g_s7EntryShortCount = 0;
   g_s7SkipLongCount = 0;
   g_s7SkipShortCount = 0;
   g_s7EntryFailCount = 0;
   g_s7ExitFailCount = 0;
   g_s7RiskTrimmedPositions = 0;
   g_s7RiskTrimTransactions = 0;
   g_s7RiskEmergencyCloses = 0;

   S7_Trade.SetDeviationInPoints(10);
   PrintFormat("S7 GOTOBI ready: %s | legs %d | calendar %d | filter %d | risk mode %d | fixed USD %.2f | spread cap %s | prefix %s",
               g_s7Symbol, (int)S7_Legs, (int)S7_Calendar, (int)S7_Control, (int)S7_RiskMode,
               S7_FixedRiskUsd, IsFxPairName(g_s7Symbol) ? "3 pips (config)" : "3 bps (config)",
               S7_LabelPrefix);
   return true;
  }

void DeinitStrategy7()
  {
   if(g_s7AtrHandle != INVALID_HANDLE)
      IndicatorRelease(g_s7AtrHandle);
   if(S7_Enabled)
      PrintFormat("GOTOBI SUMMARY | symbol %s | entries L %d S %d | first skips L %d S %d | entry unconfirmed %d | exit unconfirmed %d | risk trimmed %d | trim transactions %d | emergency closes %d",
                  g_s7Symbol, g_s7EntryLongCount, g_s7EntryShortCount,
                  g_s7SkipLongCount, g_s7SkipShortCount, g_s7EntryFailCount, g_s7ExitFailCount,
                  g_s7RiskTrimmedPositions, g_s7RiskTrimTransactions, g_s7RiskEmergencyCloses);
  }

void OnTickStrategy7()
  {
   S7_Process();
  }

//+------------------------------------------------------------------+
//| S8: NAS100 MONDAY                                                 |
//| Buy the Monday day-open (UTC + offset), exit at the Tuesday close. |
//| No stop loss. Fixed lots or fixed USD risk using the average of    |
//| the last five completed daily ranges, floored at 0.5% of price.    |
//+------------------------------------------------------------------+
double g_s8DayOpen = 0.0;
double g_s8DayHigh = 0.0;
double g_s8DayLow = 0.0;
double g_s8DayClose = 0.0;
bool   g_s8PartialDay = false;
double g_s8Ranges[5];
int    g_s8RangeNext = 0;
int    g_s8RangeCount = 0;

bool S8_HasOwnPosition()
  {
   for(int i = PositionsTotal() - 1; i >= 0; i--)
     {
      ulong ticket = PositionGetTicket(i);
      if(ticket == 0)
         continue;
      if(PositionGetString(POSITION_SYMBOL) != g_s8Symbol)
         continue;
      if(PositionGetInteger(POSITION_MAGIC) != g_s8Magic)
         continue;
      return true;
     }
   return false;
  }

void S8_CloseOwn()
  {
   ClosePositionsByMagic(g_s8Symbol, g_s8Magic);
  }

double S8_AvgRange()
  {
   if(g_s8RangeCount <= 0)
      return 0.0;
   double sum = 0.0;
   for(int i = 0; i < g_s8RangeCount; i++)
      sum += g_s8Ranges[i];
   return sum / g_s8RangeCount;
  }

double S8_Lots(const double entryPrice)
  {
   double step = SymbolInfoDouble(g_s8Symbol, SYMBOL_VOLUME_STEP);
   double minLot = SymbolInfoDouble(g_s8Symbol, SYMBOL_VOLUME_MIN);
   double maxLot = SymbolInfoDouble(g_s8Symbol, SYMBOL_VOLUME_MAX);
   if(step <= 0.0)
      return 0.0;

   double lots = minLot;

   if(S8_FixedLots > 0.0)
      lots = MathFloor(S8_FixedLots / step) * step;
   else
      if(S8_FixedRisk > 0.0)
        {
         double priceRisk = MathMax(S8_AvgRange(), entryPrice * 0.005);
         double tickValue = SymbolInfoDouble(g_s8Symbol, SYMBOL_TRADE_TICK_VALUE);
         double tickSize = SymbolInfoDouble(g_s8Symbol, SYMBOL_TRADE_TICK_SIZE);
         if(priceRisk > 0.0 && tickValue > 0.0 && tickSize > 0.0)
           {
            double perLot = priceRisk / tickSize * tickValue;
            if(perLot > 0.0)
               lots = MathFloor(S8_FixedRisk / perLot / step) * step;
           }
        }

   if(S8_MaxLots > 0.0)
     {
      double capLots = MathMin(maxLot, S8_MaxLots);
      if(lots > capLots)
         lots = MathFloor(capLots / step) * step;
     }

   if(lots < minLot)
      lots = minLot;
   if(lots > maxLot)
      lots = MathFloor(maxLot / step) * step;
   return NormalizeDouble(lots, 8);
  }

bool S8_Enter(const int day, const double openPrice)
  {
   // A disabled Monday strategy must not enter late when it is re-enabled.
   // Mark the day as handled; existing positions still run management.
   if(!g_meqCtl.s8)
      return true;
   if(openPrice <= 0.0)
      return true;

   if(PortfolioGuardBlocked())
     {
      PrintFormat("MONDAY GUARD | day %d | blocked by the portfolio guard - no entry", day);
      return true;
     }
   if(DirectionCapBlocked(g_s8Symbol, true, "MONDAY"))
      return true;

   datetime utcDayStart = (datetime)((long)YmdToTime(day) - (long)S8_BrokerUtcOffsetHours * 3600);
   datetime since = UtcToServer(utcDayStart);
   if(HistoryHasEntry(g_s8Magic, g_s8Symbol, true, since))
     {
      PrintFormat("MONDAY DUPLICATE GUARD | day %d | a long entry already exists today - NOT placed again", day);
      return true;
     }

   double lots = S8_Lots(openPrice);
   if(lots <= 0.0)
     {
      PrintFormat("MONDAY SKIP | day %d | lots 0", day);
      return true;
     }

   static datetime lastDeferLog = 0;
   string why = "";
   if(!SymbolCanTrade(g_s8Symbol, true, why))
     {
      if(TimeCurrent() - lastDeferLog >= 60)
        {
         lastDeferLog = TimeCurrent();
         PrintFormat("MONDAY DEFERRED | day %d | %s - will retry when the market opens", day, why);
        }
      return false;
     }

   double ask = 0.0, bid = 0.0;
   if(!SymbolQuoteReady(g_s8Symbol, ask, bid, why))
     {
      if(TimeCurrent() - lastDeferLog >= 60)
        {
         lastDeferLog = TimeCurrent();
         PrintFormat("MONDAY DEFERRED | day %d | %s - will retry", day, why);
        }
      return false;
     }

   S8_Trade.SetExpertMagicNumber(g_s8Magic);
   S8_Trade.SetTypeFillingBySymbol(g_s8Symbol);
   // Price 0 = current ask at send time; this strategy has no stop loss.
   if(S8_Trade.Buy(lots, g_s8Symbol, 0.0, 0.0, 0.0, GlobalComment))
     {
      PrintFormat("MONDAY BUY | day %d | open %.2f | lots %.2f", day, openPrice, lots);
      return true;
     }

   uint rc = S8_Trade.ResultRetcode();
   PrintFormat("MONDAY BUY FAILED | day %d | open %.2f | lots %.2f | retcode %d (%s)",
               day, openPrice, lots, rc, S8_Trade.ResultRetcodeDescription());
   return !RetcodeRetryable(rc);
  }

void S8_DayClosed(const int day, const int dow, const double closePrice)
  {
   double range = g_s8DayHigh - g_s8DayLow;
   if(!g_s8PartialDay && range > 0.0)
     {
      g_s8Ranges[g_s8RangeNext] = range;
      g_s8RangeNext = (g_s8RangeNext + 1) % 5;
      if(g_s8RangeCount < 5)
         g_s8RangeCount++;
     }

   if(g_s8HaveMonday && dow == 2)
     {
      PrintFormat("MONDAY EXIT | day %d | close %.2f | PTS %.1f",
                  day, closePrice, closePrice - g_s8MondayOpen);
      S8_CloseOwn();
      g_s8HaveMonday = false;
     }
  }

void S8_Process()
  {
   if(!S8_Enabled && !S8_HasOwnPosition())
      return;

   datetime utc = ServerToUtc(TimeCurrent());
   datetime strat = (datetime)((long)utc + (long)S8_BrokerUtcOffsetHours * 3600);
   int day = Ymd(strat);

   // A closed CFD feed keeps returning the last Friday tick. Wait for a live
   // quote so the day rollover and the Monday entry happen at the real open,
   // not at a synthetic midnight with the market shut (retcode 10018).
   double ask = 0.0, bid = 0.0;
   string why = "";
   if(!SymbolQuoteReady(g_s8Symbol, ask, bid, why))
      return;

   if(day != g_s8CurDay)
     {
      if(g_s8CurDay != 0)
         S8_DayClosed(g_s8CurDay, g_s8CurDow, g_s8DayClose);

      g_s8CurDay = day;
      g_s8CurDow = WeekdayOfYmd(day);
      g_s8DayOpen = ask;
      g_s8DayHigh = ask;
      g_s8DayLow = bid;
      g_s8DayClose = bid;
      g_s8PartialDay = false;

      if(g_s8CurDow == 1 && S8_Enabled && S8_TradeLongs && !g_s8HaveMonday)
        {
         g_s8MondayOpen = g_s8DayOpen;
         if(S8_Enter(day, g_s8MondayOpen))
            g_s8HaveMonday = true;
        }
      return;
     }

   if(g_s8DayOpen <= 0.0)
     {
      g_s8DayOpen = ask;
      g_s8DayHigh = ask;
      g_s8DayLow = bid;
      g_s8DayClose = bid;
      g_s8PartialDay = true;
      return;
     }

   g_s8DayClose = bid;
   if(ask > g_s8DayHigh)
      g_s8DayHigh = ask;
   if(bid < g_s8DayLow)
      g_s8DayLow = bid;

   // A Monday entry deferred by a closed market or a stale quote is retried
   // on every fresh tick until it is accepted or permanently rejected.
   if(g_s8CurDow == 1 && S8_Enabled && S8_TradeLongs && !g_s8HaveMonday && g_s8DayOpen > 0.0)
     {
      g_s8MondayOpen = g_s8DayOpen;
      if(S8_Enter(day, g_s8MondayOpen))
         g_s8HaveMonday = true;
     }
  }

bool InitStrategy8()
  {
   g_s8Symbol = S8_Symbol;
   StringTrimLeft(g_s8Symbol);
   StringTrimRight(g_s8Symbol);
   string u = g_s8Symbol;
   StringToUpper(u);
   if(u == "CURRENT" || StringLen(g_s8Symbol) == 0)
      g_s8Symbol = _Symbol;
   SymbolSelect(g_s8Symbol, true);
   if(SymbolInfoDouble(g_s8Symbol, SYMBOL_POINT) <= 0.0)
     {
      PrintFormat("S8 error: symbol '%s' is not available", g_s8Symbol);
      return false;
     }

   g_s8Magic = GlobalBaseMagic + S8_OFFSET;
   g_s8CurDay = 0;
   g_s8CurDow = -1;
   g_s8HaveMonday = false;
   g_s8MondayOpen = 0.0;
   g_s8LastEntryDay = 0;
   g_s8DayOpen = 0.0;
   g_s8DayHigh = 0.0;
   g_s8DayLow = 0.0;
   g_s8DayClose = 0.0;
   g_s8PartialDay = false;
   g_s8RangeNext = 0;
   g_s8RangeCount = 0;
   for(int i = 0; i < 5; i++)
      g_s8Ranges[i] = 0.0;

   S8_Trade.SetDeviationInPoints(10);
   if(StringFind(g_s8Symbol, "NAS100") < 0)
      PrintFormat("MONDAY WARNING | %s does not look like NAS100; the Monday edge was validated on NAS100 only", g_s8Symbol);
   PrintFormat("S8 MONDAY ready: %s | day offset %dh | fixed risk %.2f | fixed lots %.2f | max lots %.2f | longs %s",
               g_s8Symbol, S8_BrokerUtcOffsetHours, S8_FixedRisk, S8_FixedLots, S8_MaxLots,
               S8_TradeLongs ? "on" : "off");
   return true;
  }

void DeinitStrategy8()
  {
  }

void OnTickStrategy8()
  {
   S8_Process();
  }

//+------------------------------------------------------------------+
//| S9: INDEX FADE                                                    |
//| On the last closed daily bar: a down day that closed in the lower  |
//| part of its range -> buy at the next daily open; stop at StopPct   |
//| of price; close after MaxHold daily bars. Equal money risk.        |
//+------------------------------------------------------------------+
double g_ifRiskMoney = 0.0;

bool S9_HasPosition(const string sym, const int magic)
  {
   for(int i = PositionsTotal() - 1; i >= 0; i--)
     {
      ulong ticket = PositionGetTicket(i);
      if(ticket == 0)
         continue;
      if(PositionGetString(POSITION_SYMBOL) != sym)
         continue;
      if(PositionGetInteger(POSITION_MAGIC) != magic)
         continue;
      return true;
     }
   return false;
  }

void S9_Process(IFData &d)
  {
   datetime barTime = iTime(d.symbol, PERIOD_D1, 0);
   if(barTime <= 0)
      return;

   bool newBar = (barTime != d.lastBar);

   if(Bars(d.symbol, PERIOD_D1) < 4)
     {
      if(newBar)
         d.lastBar = barTime;
      return;
     }

   // ---- exit after MaxHold daily bars (checked on every timer tick) ----
   for(int i = PositionsTotal() - 1; i >= 0; i--)
     {
      ulong ticket = PositionGetTicket(i);
      if(ticket == 0)
         continue;
      if(PositionGetString(POSITION_SYMBOL) != d.symbol)
         continue;
      if(PositionGetInteger(POSITION_MAGIC) != d.magic)
         continue;

      datetime entryTime = (datetime)PositionGetInteger(POSITION_TIME);
      int entryShift = iBarShift(d.symbol, PERIOD_D1, entryTime, false);
      if(entryShift < 0)
         entryShift = 0;
      if(entryShift >= S9_MaxHoldBars)
        {
         double net = PositionGetDouble(POSITION_PROFIT) + PositionGetDouble(POSITION_SWAP);
         if(!SymbolSessionOpen(d.symbol, TimeCurrent()))
           {
            static datetime lastExitWarn = 0;
            if(TimeCurrent() - lastExitWarn >= 300)
              {
               lastExitWarn = TimeCurrent();
               PrintFormat("INDEXFADE EXIT DEFERRED %s #%I64u | market closed - will retry",
                           d.symbol, ticket);
              }
           }
         else
            if(CloseTicket(ticket))
               PrintFormat("INDEXFADE EXIT %s #%I64u | held %d bar(s) (max %d) | net %.2f",
                           d.symbol, ticket, entryShift, S9_MaxHoldBars, net);
            else
              {
               static datetime lastCloseFail = 0;
               if(TimeCurrent() - lastCloseFail >= 60)
                 {
                  lastCloseFail = TimeCurrent();
                  PrintFormat("INDEXFADE close failed for %s #%I64u | retcode %d (%s)",
                              d.symbol, ticket, S9_Trade.ResultRetcode(),
                              S9_Trade.ResultRetcodeDescription());
                 }
              }
        }
      break;
     }

   // ---- entry ----
   if(!S9_Enabled || !S9_AllowLong || !g_meqCtl.s9)
     {
      if(newBar)
        {
         d.lastBar = barTime;
         d.pendingBar = 0;
        }
      return;
     }

   // A deferred entry is retried on every timer tick until the market opens.
   if(!newBar && d.pendingBar != barTime)
      return;

   double hi = iHigh(d.symbol, PERIOD_D1, 1);
   double lo = iLow(d.symbol, PERIOD_D1, 1);
   double c1 = iClose(d.symbol, PERIOD_D1, 1);
   double c0 = iClose(d.symbol, PERIOD_D1, 2);
   if(hi <= lo || c1 <= 0.0 || c0 <= 0.0)
     {
      d.lastBar = barTime;
      d.pendingBar = 0;
      return;
     }
   if(!(c1 < c0))
     {
      d.lastBar = barTime;
      d.pendingBar = 0;
      return;
     }

   double posInRange = (c1 - lo) / (hi - lo);
   if(posInRange > S9_ClosePosThreshold)
     {
      d.lastBar = barTime;
      d.pendingBar = 0;
      return;
     }

   datetime sigOpen = iTime(d.symbol, PERIOD_D1, 1);

   if(PortfolioGuardBlocked())
     {
      PrintFormat("INDEXFADE GUARD | %s | signal %s blocked by the portfolio guard - no entry",
                  d.symbol, TimeToString(sigOpen, TIME_DATE));
      d.lastBar = barTime;
      d.pendingBar = 0;
      return;
     }
   if(DirectionCapBlocked(d.symbol, true, "INDEXFADE"))
     {
      d.lastBar = barTime;
      d.pendingBar = 0;
      return;
     }

   if(HistoryHasEntry(d.magic, d.symbol, true, sigOpen))
     {
      PrintFormat("INDEXFADE DUPLICATE GUARD | %s | signal %s - a long entry already exists",
                  d.symbol, TimeToString(sigOpen, TIME_DATE));
      d.lastBar = barTime;
      d.pendingBar = 0;
      return;
     }

   // The daily bar opens at the broker's rollover (23:00 on this server) when
   // an index CFD is still closed. Defer instead of firing into 10018.
   string why = "";
   if(!SymbolCanTrade(d.symbol, true, why))
     {
      if(d.pendingBar != barTime)
        {
         d.pendingBar = barTime;
         PrintFormat("INDEXFADE DEFERRED | %s | signal %s | %s - will retry when the market opens",
                     d.symbol, TimeToString(sigOpen, TIME_DATE), why);
        }
      return;
     }

   double price = 0.0, bid = 0.0;
   if(!SymbolQuoteReady(d.symbol, price, bid, why))
     {
      if(d.pendingBar != barTime)
        {
         d.pendingBar = barTime;
         PrintFormat("INDEXFADE DEFERRED | %s | signal %s | %s - will retry",
                     d.symbol, TimeToString(sigOpen, TIME_DATE), why);
        }
      return;
     }

   double pip = PipSizeFor(d.symbol);
   if(pip <= 0.0)
     {
      d.lastBar = barTime;
      d.pendingBar = 0;
      return;
     }

   double riskMoney = S9_RiskFromCurrentBalance
                      ? AccountInfoDouble(ACCOUNT_BALANCE) * S9_RiskPct / 100.0
                      : g_ifRiskMoney;
   double stopDistance = price * S9_StopPct / 100.0;
   if(stopDistance <= 0.0 || riskMoney <= 0.0)
     {
      d.lastBar = barTime;
      d.pendingBar = 0;
      return;
     }

   double tickValue = SymbolInfoDouble(d.symbol, SYMBOL_TRADE_TICK_VALUE);
   double tickSize = SymbolInfoDouble(d.symbol, SYMBOL_TRADE_TICK_SIZE);
   double step = SymbolInfoDouble(d.symbol, SYMBOL_VOLUME_STEP);
   double minLot = SymbolInfoDouble(d.symbol, SYMBOL_VOLUME_MIN);
   double maxLot = SymbolInfoDouble(d.symbol, SYMBOL_VOLUME_MAX);
   if(tickValue <= 0.0 || tickSize <= 0.0 || step <= 0.0)
     {
      d.lastBar = barTime;
      d.pendingBar = 0;
      return;
     }

   double perLot = stopDistance / tickSize * tickValue;
   if(perLot <= 0.0)
     {
      d.lastBar = barTime;
      d.pendingBar = 0;
      return;
     }
   double lots = MathFloor(riskMoney / perLot / step) * step;
   if(lots < minLot)
      lots = minLot;
   if(lots > maxLot)
      lots = MathFloor(maxLot / step) * step;
   lots = NormalizeDouble(lots, 8);
   if(lots <= 0.0)
     {
      d.lastBar = barTime;
      d.pendingBar = 0;
      return;
     }

   double sl = AlignPriceToTick(d.symbol, price - stopDistance);
   if(!MarketOrderLevelsOk(d.symbol, true, sl, 0.0, why))
     {
      PrintFormat("INDEXFADE SKIP | %s | signal %s | %s", d.symbol,
                  TimeToString(sigOpen, TIME_DATE), why);
      d.lastBar = barTime;
      d.pendingBar = 0;
      return;
     }

   S9_Trade.SetExpertMagicNumber(d.magic);
   S9_Trade.SetTypeFillingBySymbol(d.symbol);
   // Price 0 = current ask at send time, so a one-tick move between the quote
   // read and the order does not turn into "invalid price".
   if(S9_Trade.Buy(lots, d.symbol, 0.0, sl, 0.0, GlobalComment))
     {
      d.lastBar = barTime;
      d.pendingBar = 0;
      PrintFormat("INDEXFADE BUY %s | signal %s | posInRange %.3f | lots %.2f | stop %.2f (%s%%) | risk %.2f",
                  d.symbol, TimeToString(sigOpen, TIME_DATE), posInRange, lots,
                  stopDistance, DoubleToString(S9_StopPct, 2), riskMoney);
     }
   else
     {
      uint rc = S9_Trade.ResultRetcode();
      PrintFormat("INDEXFADE order failed %s: retcode %d (%s)",
                  d.symbol, rc, S9_Trade.ResultRetcodeDescription());
      if(RetcodeRetryable(rc))
        {
         if(d.pendingBar != barTime)
           {
            d.pendingBar = barTime;
            PrintFormat("INDEXFADE RETRY | %s | signal %s | retcode %d - will retry",
                        d.symbol, TimeToString(sigOpen, TIME_DATE), rc);
           }
        }
      else
        {
         d.lastBar = barTime;
         d.pendingBar = 0;
        }
     }
  }

bool InitStrategy9()
  {
   string text = S9_Symbols;
   StringReplace(text, ";", "|");
   StringReplace(text, ",", "|");
   string names[];
   int n = ParseList(text, names);
   if(n == 0)
     {
      ArrayResize(names, 1);
      names[0] = _Symbol;
      n = 1;
     }

   ArrayResize(g_ifData, 0);
   for(int i = 0; i < n; i++)
     {
      string sym = names[i];
      SymbolSelect(sym, true);
      if(SymbolInfoDouble(sym, SYMBOL_POINT) <= 0.0)
        {
         PrintFormat("INDEXFADE | symbol '%s' is not available - skipped", sym);
         continue;
        }

      bool already = false;
      for(int k = 0; k < ArraySize(g_ifData); k++)
         if(g_ifData[k].symbol == sym)
           {
            already = true;
            break;
           }
      if(already)
         continue;

      int idx = ArraySize(g_ifData);
      ArrayResize(g_ifData, idx + 1);
      g_ifData[idx].symbol = sym;
      g_ifData[idx].magic = GlobalBaseMagic + S9_OFFSET + idx;
      g_ifData[idx].lastBar = 0;
      g_ifData[idx].pendingBar = 0;
      g_ifData[idx].lastSignalDay = 0;
     }

   if(ArraySize(g_ifData) == 0)
     {
      Print("INDEXFADE | no usable symbols - strategy idle.");
      return false;
     }

   g_ifRiskMoney = AccountInfoDouble(ACCOUNT_BALANCE) * S9_RiskPct / 100.0;
   S9_Trade.SetDeviationInPoints(10);

   string list = "";
   for(int i = 0; i < ArraySize(g_ifData); i++)
     {
      if(i > 0)
         list += ", ";
      list += g_ifData[i].symbol;
     }
   PrintFormat("INDEXFADE ready: %d symbol(s) [%s] | close-position <= %.2f | risk %.2f%% (%.2f at start, %s) | stop %.2f%% | max hold %d bar(s) | long %s",
               ArraySize(g_ifData), list, S9_ClosePosThreshold, S9_RiskPct, g_ifRiskMoney,
               S9_RiskFromCurrentBalance ? "recomputed per trade" : "fixed at start",
               S9_StopPct, S9_MaxHoldBars, S9_AllowLong ? "on" : "off");
   return true;
  }

void DeinitStrategy9()
  {
  }

void OnTickStrategy9()
  {
   for(int i = 0; i < ArraySize(g_ifData); i++)
      S9_Process(g_ifData[i]);
  }

//+------------------------------------------------------------------+
bool InitStrategy3()
  {
   string t = S3_Symbols;
   StringReplace(t, ",", "|");
   string symbols[];

   int n = ParseSymbols(t, symbols, S3_DEFAULT_ALL);
   if(n == 0)
     {
      Print("S3 error: no symbols configured.");
      return false;
     }

   if(!ParseTime(S3_EntryTime, g_s3EntryHour, g_s3EntryMin))
     {
      Print("S3 error: invalid S3_EntryTime format. Use HH:MM");
      return false;
     }

   ArrayResize(S3_Data, n);

   for(int i = 0; i < n; i++)
     {
      SymbolSelect(symbols[i], true);

      S3_Data[i].symbol      = symbols[i];
      S3_Data[i].magic       = GlobalBaseMagic + S3_OFFSET + i;
      S3_Data[i].lastBarTime = 0;
      S3_Data[i].pendingDir  = 0;
      S3_Data[i].pendingDue  = 0;

      S3_Data[i].entryEmaHandle =
         iMA(symbols[i], S3_EntryTF, S3_EntryEMA_Period, 0, MODE_EMA, PRICE_CLOSE);
      S3_Data[i].dailyFastHandle =
         iMA(symbols[i], PERIOD_D1, S3_DailyFastEMA, 0, MODE_EMA, PRICE_CLOSE);
      S3_Data[i].dailySlowHandle =
         iMA(symbols[i], PERIOD_D1, S3_DailySlowEMA, 0, MODE_EMA, PRICE_CLOSE);
      S3_Data[i].atrHandle =
         iATR(symbols[i], S3_EntryTF, S3_ATR_Period);

      if(S3_Data[i].entryEmaHandle == INVALID_HANDLE ||
         S3_Data[i].dailyFastHandle == INVALID_HANDLE ||
         S3_Data[i].dailySlowHandle == INVALID_HANDLE ||
         S3_Data[i].atrHandle       == INVALID_HANDLE)
        {
         PrintFormat("S3 error: indicator handle creation failed for %s", symbols[i]);
         return false;
        }

      PrintFormat("S3 ready: %s | %s | magic %d",
                  symbols[i], EnumToString(S3_EntryTF), S3_Data[i].magic);
     }

   PrintFormat("S3 settings: entry EMA %d on %s | daily EMA %d/%d | ATR %d | SL x%.2f | trail x%.2f | risk %.2f%% of balance",
               S3_EntryEMA_Period, EnumToString(S3_EntryTF),
               S3_DailyFastEMA, S3_DailySlowEMA, S3_ATR_Period,
               S3_ATR_SL_Multiplier, S3_ATR_Trail_Multiplier, S3_RiskPercent);

   return true;
  }

void DeinitStrategy3()
  {
   for(int i = 0; i < ArraySize(S3_Data); i++)
     {
      if(S3_Data[i].entryEmaHandle != INVALID_HANDLE)
         IndicatorRelease(S3_Data[i].entryEmaHandle);
      if(S3_Data[i].dailyFastHandle != INVALID_HANDLE)
         IndicatorRelease(S3_Data[i].dailyFastHandle);
      if(S3_Data[i].dailySlowHandle != INVALID_HANDLE)
         IndicatorRelease(S3_Data[i].dailySlowHandle);
      if(S3_Data[i].atrHandle != INVALID_HANDLE)
         IndicatorRelease(S3_Data[i].atrHandle);
     }
  }

//---------- S3 helpers ----------
double S3_Value(const int handle, const int shift)
  {
   double buffer[];
   ArraySetAsSeries(buffer, true);

   if(CopyBuffer(handle, 0, shift, 1, buffer) <= 0)
      return 0.0;

   return buffer[0];
  }

double S3_Normalize(const string sym, const double price)
  {
   int digits = (int)SymbolInfoInteger(sym, SYMBOL_DIGITS);
   return NormalizeDouble(price, digits);
  }

bool S3_HasOpenPosition(const string sym, const int magic)
  {
   return (GetPositionCount(sym, magic) > 0);
  }

// Daily bullish regime: fast EMA over slow, close above fast, and up on the day.
bool S3_DailyBullishRegime(S3SymbolData &d)
  {
   string sym = d.symbol;

   double dailyClose     = iClose(sym, PERIOD_D1, 1);
   double previousClose  = iClose(sym, PERIOD_D1, 2);
   double fastEMA        = S3_Value(d.dailyFastHandle, 1);
   double slowEMA        = S3_Value(d.dailySlowHandle, 1);

   if(dailyClose <= 0 || previousClose <= 0 || fastEMA <= 0 || slowEMA <= 0)
      return false;

   return (fastEMA > slowEMA &&
           dailyClose > fastEMA &&
           dailyClose > previousClose);
  }

// Pullback into the entry EMA, then a bullish close that breaks the prior bar high.
bool S3_PullbackLongSignal(S3SymbolData &d)
  {
   string sym = d.symbol;

   double ema1 = S3_Value(d.entryEmaHandle, 1);
   double ema2 = S3_Value(d.entryEmaHandle, 2);

   if(ema1 <= 0 || ema2 <= 0)
      return false;

   double open1  = iOpen(sym, S3_EntryTF, 1);
   double close1 = iClose(sym, S3_EntryTF, 1);
   double high2  = iHigh(sym, S3_EntryTF, 2);
   double low1   = iLow(sym, S3_EntryTF, 1);
   double close2 = iClose(sym, S3_EntryTF, 2);

   bool pulledBackIntoEMA = (low1 <= ema1 || close2 < ema2);
   bool bullishClose      = (close1 > open1);
   bool closeAboveEMA     = (close1 > ema1);
   bool momentumBreak     = (close1 > high2);

   return (pulledBackIntoEMA && bullishClose && closeAboveEMA && momentumBreak);
  }

double S3_CalculateLotSize(const string sym, double slDistance)
  {
   double riskMoney = AccountInfoDouble(ACCOUNT_BALANCE) * S3_RiskPercent / 100.0;

   double tickValue = SymbolInfoDouble(sym, SYMBOL_TRADE_TICK_VALUE);
   double tickSize  = SymbolInfoDouble(sym, SYMBOL_TRADE_TICK_SIZE);
   double minLot    = SymbolInfoDouble(sym, SYMBOL_VOLUME_MIN);
   double maxLot    = SymbolInfoDouble(sym, SYMBOL_VOLUME_MAX);
   double lotStep   = SymbolInfoDouble(sym, SYMBOL_VOLUME_STEP);

   if(tickValue <= 0 || tickSize <= 0 || slDistance <= 0 || lotStep <= 0)
      return 0.0;

   double lots = riskMoney / (slDistance / tickSize * tickValue);

   lots = MathFloor(lots / lotStep) * lotStep;
   lots = MathMax(minLot, MathMin(maxLot, lots));

   return lots;
  }

void S3_ExecuteEntry(S3SymbolData &d, const double refPrice)
  {
   string sym = d.symbol;

   if(!g_meqCtl.s3)
      return;
   if(!CanOpenNewPosition())
      return;

   if(GlobalPreventOverlap && OtherStrategyHasPosition(sym, true, S3_OFFSET))
     {
      PrintFormat("S3 %s: entry skipped, another strategy already holds this direction", sym);
      return;
     }

   double atr = S3_Value(d.atrHandle, 1);
   if(atr <= 0)
      return;

   double slDistance = atr * S3_ATR_SL_Multiplier;

   if(S3_EnableRandomization && S3_RandomSlJitterPoints != 0.0)
     {
      double pt = SymbolInfoDouble(sym, SYMBOL_POINT);
      if(pt > 0.0)
        {
         double jittered = slDistance + SymRand(S3_RandomSlJitterPoints) * pt;
         if(jittered > 0.0)
            slDistance = jittered;
        }
     }

   double ask = SymbolInfoDouble(sym, SYMBOL_ASK);
   if(ask <= 0)
      return;

   double stopLoss = S3_Normalize(sym, ask - slDistance);

   double lots = S3_CalculateLotSize(sym, slDistance);
   if(lots <= 0)
      return;

   if(S3_EnableRandomization && S3_RandomVolumeJitterPct != 0.0)
      lots = JitterVolume(sym, lots, S3_RandomVolumeJitterPct);

   S3_Trade.SetExpertMagicNumber(d.magic);
   S3_Trade.SetTypeFillingBySymbol(sym);

   string why = "";
   if(!SymbolCanTrade(sym, true, why))
     {
      PrintFormat("S3 %s: entry skipped (%s)", sym, why);
      return;
     }
   if(!MarketOrderLevelsOk(sym, true, stopLoss, 0.0, why))
     {
      PrintFormat("S3 %s: entry skipped (%s)", sym, why);
      return;
     }

   // Price 0 = current ask at send time.
   bool ok = S3_Trade.Buy(lots, sym, 0.0, stopLoss, 0.0, GlobalComment);

   if(ok)
      PrintFormat("S3 %s: Long entry opened | Lots: %.2f | SL: %s",
                  sym, lots, DoubleToString(stopLoss, (int)SymbolInfoInteger(sym, SYMBOL_DIGITS)));
   else
      PrintFormat("S3 %s: entry failed. Error: %d | retcode %d (%s)",
                  sym, GetLastError(), S3_Trade.ResultRetcode(), S3_Trade.ResultRetcodeDescription());
  }

void S3_CheckEntry(S3SymbolData &d)
  {
   string sym = d.symbol;

   if(S3_HasOpenPosition(sym, d.magic))
      return;

   if(!S3_DailyBullishRegime(d))
      return;

   if(!S3_PullbackLongSignal(d))
      return;

   double refPrice = iClose(sym, S3_EntryTF, 1);
   if(refPrice <= 0)
      return;

   if(S3_EnableRandomization && S3_RandomDelayMaxSec > 0.0)
     {
      int delay = (int)((double)MathRand() / 32767.0 * S3_RandomDelayMaxSec);
      d.pendingDir  = 1;
      d.pendingDue  = TimeCurrent() + delay;
      d.pendingPrice = refPrice;
      PrintFormat("S3 %s: randomizer deferred the entry %ds", sym, delay);
      return;
     }

   S3_ExecuteEntry(d, refPrice);
  }

// ATR trail: bid - ATR x multiplier, stop only ever moves up.
void S3_ManageTrailingStop(S3SymbolData &d)
  {
   string sym = d.symbol;
   int magic = d.magic;

   double atr = S3_Value(d.atrHandle, 1);
   if(atr <= 0)
      return;

   double bid = SymbolInfoDouble(sym, SYMBOL_BID);
   if(bid <= 0)
      return;

   double newSL = S3_Normalize(sym, bid - atr * S3_ATR_Trail_Multiplier);

   for(int i = PositionsTotal() - 1; i >= 0; i--)
     {
      ulong ticket = PositionGetTicket(i);
      if(ticket == 0)
         continue;

      if(!PositionSelectByTicket(ticket))
         continue;
      if(PositionGetString(POSITION_SYMBOL) != sym)
         continue;
      if(PositionGetInteger(POSITION_MAGIC) != magic)
         continue;
      if(PositionGetInteger(POSITION_TYPE) != POSITION_TYPE_BUY)
         continue;

      double currentSL = PositionGetDouble(POSITION_SL);

      if(newSL > currentSL)
        {
         string why = "";
         if(!PositionStopOk(sym, true, newSL, why))
            continue;
         S3_Trade.SetExpertMagicNumber(magic);
         S3_Trade.SetTypeFillingBySymbol(sym);

         if(S3_Trade.PositionModify(ticket, newSL, 0.0))
            PrintFormat("S3 %s: trailing SL updated to %s",
                        sym, DoubleToString(newSL, (int)SymbolInfoInteger(sym, SYMBOL_DIGITS)));
         else
            PrintFormat("S3 %s: trailing SL update failed. Error: %d | retcode %d (%s)",
                        sym, GetLastError(), S3_Trade.ResultRetcode(), S3_Trade.ResultRetcodeDescription());
        }
     }
  }

void S3_Process(S3SymbolData &d)
  {
   // Deferred randomizer entry.
   if(d.pendingDir != 0 && TimeCurrent() >= d.pendingDue)
     {
      double refPrice = d.pendingPrice;
      d.pendingDir = 0;
      d.pendingDue = 0;

      if(!S3_HasOpenPosition(d.symbol, d.magic))
         S3_ExecuteEntry(d, refPrice);
      else
         PrintFormat("S3 %s: randomizer deferred entry dropped (position open)", d.symbol);
     }

   // Trail on every timer pass.
   S3_ManageTrailingStop(d);

   // Entry only once per new EntryTF bar.
   datetime currentBar = iTime(d.symbol, S3_EntryTF, 0);
   if(currentBar <= 0)
      return;
   if(currentBar == d.lastBarTime)
      return;

   d.lastBarTime = currentBar;

   S3_CheckEntry(d);
  }

void OnTickStrategy3()
  {
   for(int i = 0; i < ArraySize(S3_Data); i++)
      S3_Process(S3_Data[i]);
  }


//+------------------------------------------------------------------+
//| Portfolio maintenance and timer fan-out                           |
//+------------------------------------------------------------------+
void RunPortfolioMaintenance()
  {
   PortfolioGuardCheck();
   DirectionCapSweep();
   ProfitLockProcess();
  }

void RunTimerStrategies()
  {
   if(g_TradingDisabled)
      return;

   if(S3_Enabled)
      OnTickStrategy3();

   if(S3B_Enabled)
      OnTickStrategy3B();

   if(S4_Enabled)
      OnTickStrategy4();

   if(S5_Enabled)
      OnTickStrategy5();

   if(S6_Enabled)
      OnTickStrategy6();

   if(S7_Enabled)
      OnTickStrategy7();

   if(g_s8Symbol != "")
      OnTickStrategy8();

   if(ArraySize(g_ifData) > 0)
      OnTickStrategy9();
  }

// One timer drives every strategy that is not tied to the chart symbol's ticks.
void OnTimer()
  {
   MEQ_RefreshControl();

   if(!CheckGlobalRisk())
     {
      CloseAllPositions();
      CancelAllOurPending();
      return;
     }

   RunPortfolioMaintenance();
   RunTimerStrategies();
  }

//+------------------------------------------------------------------+
//| Chart status                                                      |
//+------------------------------------------------------------------+
void OutputStatusToChart()
  {
   double equity = AccountInfoDouble(ACCOUNT_EQUITY);

   string text = "\n\rMOTHER EA\n\r";
   text += "Server time: " + TimeToString(TimeCurrent(), TIME_DATE | TIME_MINUTES) + "\n\r";
   text += StringFormat("Equity: %.2f | Peak: %.2f | Day start: %.2f\n\r", equity, g_peakEquity, g_dayStartEquity);
   text += StringFormat("Open positions: %d%s\n\r", CountOurPositions(),
                        GlobalMaxOpenPositions > 0 ? StringFormat(" / %d", GlobalMaxOpenPositions) : "");

   if(g_TradingDisabled)
      text += g_totalBreached ? "STATUS: STOPPED (total drawdown limit)\n\r" : "STATUS: PAUSED (daily drawdown limit)\n\r";
   else
      text += "STATUS: trading\n\r";

   text += StringFormat("S1 %s | S2A %s | S2B %s | S3 %s | S3B %s\n\r",
                        g_meqCtl.s1 ? "on" : "off", g_meqCtl.s2a ? "on" : "off",
                        g_meqCtl.s2b ? "on" : "off", g_meqCtl.s3 ? "on" : "off",
                        g_meqCtl.s3b ? "on" : "off");
   text += StringFormat("S4 ORB %s | S5 BOA %s | S6 BOB %s | S7 GTO %s | S8 MON %s | S9 IFD %s\n\r",
                        g_meqCtl.s4 ? "on" : "off", g_meqCtl.s5 ? "on" : "off",
                        g_meqCtl.s6 ? "on" : "off", g_meqCtl.s7 ? "on" : "off",
                        g_meqCtl.s8 ? "on" : "off", g_meqCtl.s9 ? "on" : "off");
   text += StringFormat("Manager control: %s\n\r",
                        g_meqCtl.ready ? "live" : "FAIL-CLOSED (control.ini missing/corrupt)");
   text += StringFormat("Guard: %s | direction cap: %s | profit lock rules: %d\n\r",
                        PortfolioGuardEnabled ? "on" : "off",
                        DirectionCapEnabled ? "on" : "off",
                        ArraySize(g_plRules));

   Comment(text);
  }

//+------------------------------------------------------------------+
//| ORB R ledger: capture risk at fill and realised R at close        |
//+------------------------------------------------------------------+
void OnTradeTransaction(const MqlTradeTransaction &trans,
                        const MqlTradeRequest &request,
                        const MqlTradeResult &result)
  {
   if(trans.type != TRADE_TRANSACTION_DEAL_ADD)
      return;

   ulong deal = trans.deal;
   if(deal == 0)
      return;
   if(!HistoryDealSelect(deal))
      return;

   long magic = HistoryDealGetInteger(deal, DEAL_MAGIC);
   if(!IsOurMagic(magic))
      return;
   if(StrategyOffset(magic) != S4_OFFSET)
      return;

   long entry = HistoryDealGetInteger(deal, DEAL_ENTRY);
   ulong posId = (ulong)HistoryDealGetInteger(deal, DEAL_POSITION_ID);
   string sym = HistoryDealGetString(deal, DEAL_SYMBOL);

   if(entry == DEAL_ENTRY_IN)
     {
      double priceRisk = (HistoryDealGetInteger(deal, DEAL_TYPE) == DEAL_TYPE_BUY)
                         ? g_s4PlannedRiskLong : g_s4PlannedRiskShort;
      double volume = HistoryDealGetDouble(deal, DEAL_VOLUME);
      double tickValue = SymbolInfoDouble(sym, SYMBOL_TRADE_TICK_VALUE);
      double tickSize = SymbolInfoDouble(sym, SYMBOL_TRADE_TICK_SIZE);
      if(priceRisk > 0.0 && volume > 0.0 && tickValue > 0.0 && tickSize > 0.0)
        {
         double riskMoney = priceRisk / tickSize * tickValue * volume;
         OrbRiskStore(posId, riskMoney, Ymd(S4_LocalNow()));
        }
      return;
     }

   if(entry == DEAL_ENTRY_OUT || entry == DEAL_ENTRY_OUT_BY)
     {
      double risk = OrbRiskMoney(posId);
      if(risk <= 0.0)
         return;
      double net = HistoryDealGetDouble(deal, DEAL_PROFIT)
                   + HistoryDealGetDouble(deal, DEAL_SWAP)
                   + HistoryDealGetDouble(deal, DEAL_COMMISSION);
      g_s4TodayR += net / risk;
      PrintFormat("ORB R | #%I64u %s | net %.2f / risk %.2f = %.3fR | today %.3fR",
                  posId, sym, net, risk, net / risk, g_s4TodayR);
      OrbRiskRemove(posId);
     }
  }

//+------------------------------------------------------------------+
//| Expert initialization                                             |
//+------------------------------------------------------------------+
int OnInit()
  {
   MEQ_RefreshControl();

   if(S1_Enabled && !InitStrategy1())
      return INIT_FAILED;

   if(S2A_Enabled && !InitStrategy2A())
      return INIT_FAILED;

   if(S2B_Enabled && !InitStrategy2B())
      return INIT_FAILED;

   if(S3_Enabled && !InitStrategy3())
      return INIT_FAILED;

   if(S3B_Enabled && !InitStrategy3B())
      return INIT_FAILED;

   if(S4_Enabled && !InitStrategy4())
      return INIT_FAILED;

   if(S5_Enabled && !InitStrategy5())
      return INIT_FAILED;

   if(S6_Enabled && !InitStrategy6())
      return INIT_FAILED;

   if(S7_Enabled && !InitStrategy7())
      return INIT_FAILED;

   bool s8Needed = S8_Enabled || (CountPositionsByOffset(S8_OFFSET) > 0);
   if(s8Needed && !InitStrategy8())
      return INIT_FAILED;

   bool s9Needed = S9_Enabled || (CountPositionsByOffset(S9_OFFSET) > 0);
   if(s9Needed && !InitStrategy9())
      return INIT_FAILED;

   if(!S1_Enabled && !S2A_Enabled && !S2B_Enabled && !S3_Enabled &&
      !S3B_Enabled && !S4_Enabled && !S5_Enabled && !S6_Enabled &&
      !S7_Enabled && !S8_Enabled && !S9_Enabled && !s8Needed && !s9Needed)
     {
      Print("Mother EA: all strategies are disabled.");
      return INIT_FAILED;
     }

   if(GlobalRandomSeed != 0)
      MathSrand(GlobalRandomSeed);
   else
      MathSrand(GetTickCount());

   g_peakEquity     = AccountInfoDouble(ACCOUNT_EQUITY);
   g_dayStartEquity = g_peakEquity;

   MqlDateTime dt;
   TimeToStruct(TimeCurrent(), dt);
   g_riskDay = dt.year * 10000 + dt.mon * 100 + dt.day;

   DirectionCapStart();
   ProfitLockStart();

   PrintFormat("Mother EA initialized | comment '%s' | base magic %d | overlap guard %s | fixed balance %s",
               GlobalComment, GlobalBaseMagic, GlobalPreventOverlap ? "on" : "off",
               GlobalUseFixedBalance ? "on" : "off");
   PrintFormat("Randomizer: S1 %s | S2A %s | S2B %s | S3 %s | S3B %s",
               S1_EnableRandomization ? "ON" : "OFF", S2A_EnableRandomization ? "ON" : "OFF",
               S2B_EnableRandomization ? "ON" : "OFF", S3_EnableRandomization ? "ON" : "OFF",
               S3B_EnableRandomization ? "ON" : "OFF");
   PrintFormat("Portfolio guard: %s | max combined %d | daily loss %.2f | flatten %s",
               PortfolioGuardEnabled ? "ON" : "off", MaxCombinedPositions,
               MaxPortfolioDailyLossUsd, FlattenOnPortfolioGuardTrip ? "ON" : "off");

   EventSetTimer(1);
   Print("Mother EA timer armed - one chart drives every timer strategy.");
   return INIT_SUCCEEDED;
  }

//+------------------------------------------------------------------+
//| Expert deinitialization                                           |
//+------------------------------------------------------------------+
void OnDeinit(const int reason)
  {
   if(S1_Enabled)
      DeinitStrategy1();

   if(S2A_Enabled)
      DeinitStrategy2A();

   if(S2B_Enabled)
      DeinitStrategy2B();

   if(S3_Enabled)
      DeinitStrategy3();

   if(S3B_Enabled)
      DeinitStrategy3B();

   if(S4_Enabled)
      DeinitStrategy4();

   if(S5_Enabled)
      DeinitStrategy5();

   if(S6_Enabled)
      DeinitStrategy6();

   if(S7_Enabled)
      DeinitStrategy7();

   if(g_s8Symbol != "")
      DeinitStrategy8();

   if(ArraySize(g_ifData) > 0)
      DeinitStrategy9();

   EventKillTimer();
   Comment("");
  }

//+------------------------------------------------------------------+
//| Main loop                                                         |
//+------------------------------------------------------------------+
void OnTick()
  {
   MEQ_RefreshControl();

   // FIRST: global risk.
   if(!CheckGlobalRisk())
     {
      CloseAllPositions();
      CancelAllOurPending();
      return;
     }

   if(!TerminalInfoInteger(TERMINAL_TRADE_ALLOWED))
     {
      static datetime lastWarn = 0;
      if(TimeCurrent() - lastWarn >= 60)
        {
         lastWarn = TimeCurrent();
         Print("Trading not allowed!");
        }
      return;
     }

   // Tick-driven strategies.
   if(S1_Enabled && !g_TradingDisabled)
      OnTickStrategy1();

   if(S2A_Enabled && !g_TradingDisabled)
      OnTickStrategy2A();

   if(S2B_Enabled && !g_TradingDisabled)
      OnTickStrategy2B();

   RunPortfolioMaintenance();

   if(!MQLInfoInteger(MQL_TESTER))
      OutputStatusToChart();
  }
//+------------------------------------------------------------------+
// END OF FILE