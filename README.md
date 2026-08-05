# Discord-controlled MT5 trading bot

A personal risk-management and trade-execution bot for MetaTrader 5,
controlled entirely from Discord slash commands. You trade EURUSD or
GBPUSD by typing a command; the bot calculates position size from your
risk %, calculates TP automatically, enforces daily loss limits, caps how
many positions you can have open, and alerts you on everything that
happens — all without needing MT5 open in front of you.

It's a self-hosted alternative to commercial tools like PipsGuard —
free beyond your own server cost, fully under your control, and built
around one core idea: **once a safety rule triggers, there's no way to
talk yourself out of it from Discord.**

---

## How it's built

Three pieces, talking to each other over plain HTTP:

```
Discord  <-->  Backend (FastAPI + discord.py)  <-->  MT5 Expert Advisor
 (you)          runs on your server, holds all       runs inside MT5,
                the rules/state, exposes commands     executes trades,
                                                       reports prices
```

- **`EA_Bridge.mq5`** — an Expert Advisor that runs inside MT5. It reports
  account state and live prices every few seconds, executes whatever
  commands the backend queues for it (open, close, cancel), and enforces
  safety rules locally too (so a trade is blocked even if the backend is
  briefly unreachable).
- **`main.py`** — the backend. A FastAPI server the EA talks to, plus a
  Discord bot you talk to. Holds all the account state, risk rules, and
  trade history.
- **`storage.py`** — SQLite persistence, so a backend restart doesn't lose
  your accounts, loss counts, or trade history.
- **`rules.py`** — a small rule engine for the optional $-based daily loss
  limit / drawdown / lot-size rules (separate from the hardcoded rules
  below).

You can run multiple MT5 terminals (one per trading account), all pointed
at the same backend — it tells them apart automatically by account login
number.

---

## What's hardcoded (and why)

These are fixed in the code, not settable from Discord. The whole design
philosophy here is: **rules you'd want to bend in the moment shouldn't be
bendable in the moment.**

| Rule | Value | Where |
|---|---|---|
| Risk per trade | 1% (challenge accounts), 0.5% (funded accounts) | set once at `/addaccount`, tied to phase |
| Risk-reward ratio | 7.00 — TP is always `7.00 × SL distance` | `main.py` |
| Max losses per day | 2 — blocks the account until the next Nairobi-midnight reset | `main.py` |
| Max open positions | 2 — further trades refused until one closes | `main.py` |
| Account removal lock | Locked for the rest of the Nairobi day a loss happened on, clears at the next Nairobi midnight | `main.py` |
| Tradeable pairs | EURUSD, GBPUSD only | `main.py` |
| No manual pause/resume | Once blocked, there is no override command — it waits out the reset | by design |

Optional $-based rules (daily loss limit, trailing drawdown %, max lot
size) can be set via `.env` — see below — but there's no Discord command
to change them either.

---

## Setup

### 1. AWS EC2 (or any Windows box)
Launch a Windows Server instance (t3.micro/small is fine). RDP into it —
this is where MT5 will run. Lock down RDP (port 3389) to your own IP in
the security group.

### 2. MT5 + the EA
1. Install MT5, log into your account (**start with a demo account**).
2. Copy `EA_Bridge.mq5` into `MQL5/Experts/` (File → Open Data Folder in
   MT5), then compile it in MetaEditor (F7).
3. **Tools → Options → Expert Advisors** → enable "Allow WebRequest for
   listed URL" → add your backend's URL.
4. Attach the EA to any chart, enable "Allow live trading" in its
   settings.
5. Set these EA inputs:
   - `BackendURL` — e.g. `http://127.0.0.1:8000` if backend runs on the
     same box
   - `AccountToken` — must exactly match `BRIDGE_TOKEN` in `.env` (below)
   - `SymbolSuffix` — your broker's suffix (e.g. `b`, `.m`) or `0` for
     none. This is the **only** place you set it — the EA appends it to
     both the pair names it reports prices for and to any trade command
     it receives from Discord, so there's no suffix config to duplicate
     or keep in sync on the Discord side.

Repeat this for each account you want to trade (each needs its own MT5
terminal — use portable-mode installs to run more than one at a time).

### 3. Discord bot
1. Create an app at https://discord.com/developers/applications → add a
   bot → copy its token.
2. OAuth2 → URL Generator → scopes `bot` + `applications.commands` →
   permissions: Send Messages, Use Slash Commands → open the generated
   URL, invite it to your server.
3. Enable Developer Mode in Discord (User Settings → Advanced) so you can
   right-click to copy IDs — you'll need your own user ID, your server
   ID, and the ID of a channel for alerts.

### 4. Backend
```
pip install -r requirements.txt
```
Create a `.env` file (see `.env.example`) with:
```
BRIDGE_TOKEN=some-long-random-string        # must match the EA's AccountToken
DISCORD_TOKEN=your-bot-token
OWNER_DISCORD_ID=your-discord-user-id       # only this user can run commands
ALERT_CHANNEL_ID=channel-id-for-alerts
GUILD_ID=your-server-id                     # makes new commands show up instantly
```
Then:
```
python main.py
```
You should see it log in, sync commands, and post an "online" alert to
your alert channel.

---

## Commands

**Setup**
- `/addaccount account:<login> phase:<challenge|funded>` —
  register an account. First one becomes your default.
- `/removeaccount account:<login>` — remove (blocked while in a losing
  period)
- `/setdefault account:<login>` — change which account commands default to
- `/accounts` — list every tracked account: phase, risk %, balance,
  equity, blocked state, loss/position counts, live prices

**Trading** — `account` is optional everywhere once you have a default
- `/buy pair:<EURUSD|GBPUSD> entry sl [account]` — TP calculated for you
- `/sell pair sl entry [account]` — same, sell side
- `/marketbuy pair sl [account]` — no entry needed; the EA fetches the
  live price itself at the exact moment it executes
- `/marketsell pair sl [account]` — same, sell side

**Managing positions**
- `/status [account]` — full account snapshot: balance, equity, blocked
  state, live prices, every open position and pending order, with
  tappable Close/Cancel buttons
- `/close ticket [account]` / `/closeall [account]`
- `/pending [account]` — list pending limit/stop orders
- `/cancelpending ticket [account]`

**Reporting**
- `/weekly [account]` — 7-day trade count, win rate, net P/L, TP/SL/manual
  breakdown, best/worst trade

---

## Automatic behavior — nothing you have to ask for

- **Breakeven at 3R** — once a trade's floating profit reaches 3× its
  original stop distance, SL moves to entry automatically.
- **Manual trades get reversed.** Anything opened on the chart directly
  (not through the bot) gets closed or deleted within about a second.
  This is detection-and-reversal, not pre-click blocking — MT5 has no
  mechanism for the latter.
- **Live alerts** for every trade opened, closed, breakeven trigger,
  pending-order trigger, and every rejected order (with the reason —
  blocked, market closed, missing SL, etc.). Nothing fails silently.
- **Connection watchdog** — alerts if an account's EA stops reporting for
  2+ minutes, and again when it recovers.
- **Self-healing block state.** If the EA restarts and loses its local
  "blocked" flag, the backend notices on the next report and re-sends
  `BLOCK` — your persisted loss count is the source of truth, not
  whatever the EA happens to remember.

---

## Design notes worth understanding

- **Two-layer enforcement.** Every safety check (blocked, market closed,
  position limit) is checked twice: once in Discord for instant feedback,
  and always in the EA itself before it'll actually place a trade. The EA
  check is the one that actually matters — Discord's is just a fast,
  honest front door to it.
- **Fast execution.** The EA checks for new commands on every price tick
  (not just a slow timer), so a command is usually picked up within about
  a second during active market hours.
- **Partial-fill safe.** MT5 can fill or close one trade as several
  separate deals. The EA aggregates these into a single alert with the
  correct total profit, instead of reporting each partial deal separately.
- **Nairobi time (EAT, UTC+3) for all daily resets** — loss count, $-loss
  limit, drawdown baseline. Computed as a fixed offset from UTC rather
  than a timezone database, since Windows doesn't ship one by default and
  EAT has no daylight saving to worry about.

---

## Safety checklist before touching a live account

- [ ] Tested on demo: a market order, a limit order (entry away from
      price), a stop order, a manual chart click (confirm auto-reversal),
      a trade reaching 2R (confirm breakeven), and 2 losing trades
      (confirm the block and its refusal messages)
- [ ] Confirmed `OWNER_DISCORD_ID` is correct — it's your only access
      control
- [ ] `BRIDGE_TOKEN` is long and random, and your backend port isn't
      exposed to the public internet
- [ ] Sanity-checked at least one calculated lot size against your own
      manual math — tick-value handling varies by instrument
- [ ] Comfortable that there is genuinely no override once blocked

## Known limitations
- Rules and pairs apply to the whole bot, not per-account — if you
  eventually need different phases to behave very differently per account
  beyond what's here, that'd need extending.
- Broker suffix is set only in the EA (`SymbolSuffix` input), not tracked
  by the backend at all — if you ever need the *same* backend to route
  different suffixes to different accounts automatically without you
  setting each EA's input correctly, that assumption would need revisiting.
- The optional $-based rules (`DAILY_LOSS_LIMIT`, `MAX_DRAWDOWN_PCT`,
  `MAX_LOT_SIZE`) live in `rules.py` and are separate from the hardcoded
  loss-count/position-count limits in `main.py`. The `MAX_POSITIONS` env
  var and `max_positions` field in `rules.py` are currently unused —
  position limiting is handled by the hardcoded `MAX_OPEN_POSITIONS` in
  `main.py` instead.
- Single backend process, in-memory command queues — fine at personal
  scale, not built for high availability.
