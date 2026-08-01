# Discord-controlled MT5 bot — setup guide

## Overview
- `EA_Bridge.mq5` runs inside MetaTrader 5 (one copy per account/terminal),
  reports account state, executes commands, auto-flattens manual trades,
  handles breakeven, and pushes live event alerts.
- `main.py` + `rules.py` is the backend: FastAPI server the EA(s) talk to,
  plus a Discord bot. Tracks multiple accounts, each with its own rules,
  risk %, and trade log.

## Multi-account model
- Run one MT5 terminal (portable install) per account, each with its own
  copy of `EA_Bridge.mq5` pointed at the **same** `BackendURL` and
  `AccountToken`. The backend tells accounts apart by their login number,
  which the EA reads automatically from the terminal — you don't set it.
- An account only becomes usable for trading once you run `/addaccount` —
  before that it can still report status (shows as "unregistered") but
  `/buy`/`/sell` will refuse to queue trades on it.

## Blocking is enforced twice, on purpose, with no manual override
Once blocked (daily loss limit, or a $-loss/drawdown rule), new trades are
refused at **two layers**:
1. **Discord-side, immediately** — `/buy`, `/sell`, `/marketbuy`,
   `/marketsell` all check the account's last-known blocked status before
   even queuing anything. If blocked, you get a clear refusal message right
   away instead of a misleading "queued" confirmation.
2. **EA-side, always** — even if layer 1 somehow missed it (e.g. the
   backend's picture of "blocked" is a few seconds stale), the EA itself
   refuses any `OPEN`/`OPEN_MARKET` command while `tradingBlocked` is true,
   and sends an `ORDER_REJECTED ... trading_blocked` alert. This is the
   layer that actually matters — it's enforced on the terminal placing the
   trade, not just in the bot's own bookkeeping.

There is no `/pause` or `/resume` command — deliberately. Once blocked, it
stays blocked until the daily reset (UTC midnight), same principle as the
`/removeaccount` lock during a losing period: no way to talk yourself out
of the rule mid-session. `/closeall` still works even while blocked, so you
can always flatten existing positions — you just can't open new ones.

## Final touches — closing the silence gaps
These address exactly the kind of confusion you just hit (queued trade,
nothing happened, no explanation):
- **One alert per close, not two.** The EA used to send both a
  `TRADE_CLOSED` event and a separate `SL_HIT`/`TP_HIT` event for the same
  close — two Discord messages for one trade. It now sends one, with the
  reason folded into it.
- **Alert ordering fixed.** The "trade closed" alert now always sends
  before the "hit N losses, blocked" alert, not after — previously they
  could arrive backwards.
- **Bot-forced closures still count as losses if they lost money.** Every
  losing close counts toward the daily limit and `/weekly`, no matter who
  closed it (SL, TP, manual, or the bot itself via a rule/override) — a
  loss is a loss. What's deduped instead is the *alert*: once the daily
  limit has been hit and the "blocked" alert has fired, it won't fire again
  for further losses that same day (the block is already in effect either
  way).
- **Partial-fill deduplication.** MT5 can fill or close a single trade as
  multiple separate deals (common on market orders, especially on volatile
  symbols). Each deal used to be treated as a distinct event, so one real
  trade could produce multiple alerts and multiple `/weekly` entries with
  understated profit. Opens/triggers are now deduplicated by position ID;
  closes are buffered for a short window (`CloseAggregationMs`, default
  1500ms) and combined into one alert with the correct total profit once no
  further partial-close deals arrive for that position.
- **Market order fills now alert too** — previously only pending-order
  triggers sent an alert; a straightforward market buy/sell went silent.
- **Every rejection now alerts, with a reason** — blocked trading, missing
  SL/TP, unavailable symbol, closed market/no quotes, zero calculated lot
  size, and broker-side `OrderSend` failures all now send an
  `ORDER_REJECTED <symbol> <reason>` alert instead of only logging locally
  in MT5's Experts tab (which is easy to miss). This specifically covers the
  closed-market case you ran into.
- **Connection watchdog** — if an account hasn't reported in over 2 minutes
  (EA stopped, terminal closed, network drop), you get an alert. A second
  alert fires when it starts reporting again. This is a background task
  that runs alongside the server and Discord bot.
- **Startup alert** — the bot posts a message when it comes online, showing
  how many accounts it loaded and which is default, so you can confirm a
  restart actually picked up your persisted state correctly.
State lives in a local SQLite file (`bot_data.db` by default, next to
`main.py` — override the path with `DB_PATH` in `.env`). On startup the
backend reloads every registered account, its rule progress, its full trade
history, and your default account. No separate database server to install
or run — it's a single file.

## Hardcoded — cannot be changed via any Discord command
- **Risk per trade:** 1% on `challenge` accounts, 0.5% on `funded` accounts.
- **Removal lock:** `/removeaccount` refused during a losing period (loss in
  last 24h, or currently down for the day).
- **Max 2 losses per day:** on the 2nd losing trade in a UTC day, the
  account is automatically blocked until the next UTC day. You'll get an
  alert when it triggers. No manual override exists — see "Blocking is
  enforced twice" above.
- Also fixed the **daily $-loss-limit / drawdown rules never actually
  resetting** — they now roll over correctly at UTC midnight, same
  mechanism the loss-count rule uses.

## Default account
The **first account you `/addaccount`** becomes your default automatically.
After that, `account` is optional on every command — omit it and it uses
whatever `/setdefault` last pointed at. Other accounts you register just sit
there, trackable via `/accounts` or by passing `account:` explicitly, until
you `/setdefault` them or name them directly.

## TP is always auto-calculated — one less field to type
Every trade's TP is derived automatically at a fixed **5.25 risk-reward
ratio** (`TP distance = 5.25 × SL distance`). This is hardcoded, not a
Discord-settable value — same pattern as risk % and the other fixed rules.

- `/buy entry:<price> sl:<price> [account]` — TP calculated for you
- `/sell entry:<price> sl:<price> [account]` — same
- `/marketbuy sl:<price> [account]` — no entry to type; the EA fetches the
  current ask itself at the exact moment it executes, and calculates TP
  from that live price
- `/marketsell sl:<price> [account]` — same, using bid

**How this is now fast**: two separate delays existed before, both fixed —
1. The backend used to pre-compute entry/TP from whatever price it last
   heard from the EA (up to `PollSeconds` old). Now `/marketbuy`/`/marketsell`
   send a bare `OPEN_MARKET` command with no price attached — the EA fetches
   `SYMBOL_ASK`/`SYMBOL_BID` itself right before sending the order, so the
   entry used is always current at execution time, not a guess from earlier.
2. The EA used to only check for new commands once every `PollSeconds`
   (5s by default). It now also checks on every price tick (rate-limited to
   at most once per `FastPollMs`, default 1000ms) — so during active market
   hours, a command is usually picked up within about a second, not up to 5.
   The slower timer-based check still runs too, as a fallback for quiet
   periods with no ticks.

`/status` and `/accounts` still show the last-known ask/bid for reference,
but that number is no longer what actually gets used to open the trade.

## Fixed trading symbol, per-account suffix
`TRADE_SYMBOL` in `.env` is the shared base instrument name (e.g. `XAUUSD`)
— the same across every account, since you're trading the same thing
everywhere. The **broker suffix** (e.g. `b`, `.m`, `c`) is set per account
instead, at registration time:
```
/addaccount account:12345678 phase:challenge suffix:b
```
`suffix` is optional — omit it or pass `0` if that broker uses no suffix.
The two combine automatically (`XAUUSD` + `b` = `XAUUSDb`) for every
`/buy`/`/sell` on that account. Check `/accounts` to see each account's
resolved symbol. There's still no symbol parameter on `/buy`/`/sell`
themselves — it's always derived from the account.

## Rules — no longer a Discord command
`/setrule` has been removed. Rule limits (daily loss, trailing drawdown,
max lot size, max positions) are now set once via `.env`
(`DAILY_LOSS_LIMIT`, `MAX_DRAWDOWN_PCT`, `MAX_LOT_SIZE`, `MAX_POSITIONS`) and
apply to every account. Same "hardcoded, not remotely changeable" pattern as
the risk-%-by-phase rule — edit `.env` and restart the backend to change them.

## Commands
- `/addaccount account:<login> phase:<challenge|funded>` — register an
  account. First one registered becomes your default automatically.
- `/removeaccount account:<login>` — remove (blocked during a losing period)
- `/setdefault account:<login>` — change which account commands use when
  `account` is omitted
- `/accounts` — list all tracked accounts, phase, risk %, balance/equity,
  lock status, and which is currently default (⭐)
- `/status [account]` — balance, equity, margin, blocked state, open positions
- `/buy entry:<price> sl:<price> [account]` — TP auto-calculated at 5.25 RRR
- `/sell` — same shape as `/buy`
- `/marketbuy sl:<price> [account]` — no entry needed, TP auto-calculated
- `/marketsell sl:<price> [account]` — same, sell side
- `/close ticket:<n> [account]` — close one position
- `/closeall [account]` — close every bot-opened position
- `/pending [account]` — list pending (unfilled) limit/stop orders
- `/cancelpending ticket:<n> [account]` — cancel a pending order
- `/weekly [account]` — trade count, win rate, net P/L, TP/SL/manual close
  breakdown, best/worst trade over the last 7 days

## Automatic behavior (no command needed)
- **Breakeven at 2R** — once a trade's floating profit reaches 2× its
  original stop-loss distance, SL moves to entry automatically.
- **Manual trade auto-close** — any position or pending order not tagged by
  the bot gets closed/deleted within about a second of appearing.
- **Live alerts** — posted to the channel set by `ALERT_CHANNEL_ID` for:
  a pending order triggering, SL hit, TP hit, breakeven triggered, and every
  trade close (used to build the weekly summary).

## Setting up alerts (new — `ALERT_CHANNEL_ID`)
1. In Discord, enable Developer Mode (User Settings → Advanced) if you
   haven't already.
2. Right-click the text channel you want alerts posted in → **Copy Channel ID**.
3. Add it to `.env`:
   ```
   ALERT_CHANNEL_ID=that-id-here
   ```
4. Make sure your bot has **Send Messages** permission in that channel
   (it was granted broadly when you invited it earlier, but double check
   if the channel has custom permission overrides).

## Everything else (AWS/EC2, MT5 install, initial Discord bot setup,
`.env` basics) is unchanged from the earlier setup — see prior instructions.

## Safety notes — read before connecting a live account
- **Test on a demo account first**: register it with `/addaccount`, place a
  market order, a limit order, a stop order, let one hit SL and one hit TP,
  manually click the chart (confirm auto-close), let a trade run to 2R
  (confirm breakeven), and run `/weekly` afterward to confirm the numbers
  match what you actually did.
- **Sanity-check calculated lot sizes** — tick value handling varies by
  instrument (metals/indices vs forex pairs especially), and a mistake here
  directly controls position size.
- `OWNER_DISCORD_ID` is your only access control on commands. `BRIDGE_TOKEN`
  is the only thing stopping fake POSTs to your backend if the port is ever
  exposed — keep both long, random, and the port closed to the public
  internet where possible.
- The "losing period" lock and daily loss counters now persist across
  backend restarts (SQLite), so they're reliable even if you restart mid-day.
- Trade history (`/weekly`) also persists — it survives restarts now.
- Nothing here retries gracefully if the EA-backend connection drops — the
  EA logs an error and keeps trying. Don't treat silence as "everything's fine."
