# GuardRails

*Discipline you can't argue with.*

GuardRails is a self-hosted Discord bot that puts hard, non-negotiable
guardrails around MT5 trading. You trade by Discord command; GuardRails
calculates position size from a fixed risk percentage, sets take-profit
automatically at a set risk-reward ratio, and manages the trade from there
(live alerts on every fill, close, and rejection).

What makes it different from a normal trading bot is what happens when
things go wrong: risk limits, daily loss caps, and position limits are
hardcoded — not settings you can quietly loosen mid-session. Once a
limit is hit, trading stops until the next daily reset. No pause
command, no override, no way to argue with it in the moment. It's built
for one job: making sure the version of you that's calm and rested sets
the rules, and the version of you that's three losses deep can't change
them.

It's a self-hosted alternative to commercial tools like PipsGuard —
free beyond your own server cost, and fully under your control.

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
| Risk-reward ratio | 3.25 — TP is always `3.25 × SL distance` | `main.py` |
| Max losses per day | 2 — blocks the account until the next Nairobi-midnight reset | `main.py` |
| Max open positions | 2 — further trades refused until one closes | `main.py` |
| Account removal lock | Locked for the rest of the Nairobi day a loss happened on, clears at the next Nairobi midnight | `main.py` |
| No manual pause/resume | Once blocked, there is no override command — it waits out the reset | by design |

Symbols are deliberately **not** hardcoded: they're broker-specific, so each
account has its own enabled symbol list managed from Discord (`/addsymbol`,
`/removesymbol`, `/symbols`). New accounts start with EURUSD and GBPUSD.

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
     none. The EA appends it to every bare symbol root you enable in
     Discord, and to the quote keys it reports, so the suffix is set in
     exactly one place.
   - `ExternalMagicMin` / `ExternalMagicMax` — magic range reserved for
     other EAs on the same account (MetaEq's magic bank starts at
     `100000`; the default `100000–119999` range covers it). GuardRails
     will not close, delete, report, or count those positions/orders.

Enable the symbols you actually trade per account from Discord with
`/addsymbol`; the EA receives the list on its next poll (about a second).

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
- `/accounts` — list every tracked account: phase, symbols, risk %,
  balance, equity, blocked state, loss/position counts, live prices

**Symbols** — set per account, bare broker roots only (no suffix)
- `/symbols [account]` — list enabled symbols and their live ask/bid
- `/addsymbol symbols:<EURUSD,XAUUSD> [account]` — enable one or more
  symbols. Commas or spaces both work; `XAU/USD` is normalised to `XAUUSD`
- `/removesymbol symbol:<EURUSD> [account]` — disable one. The last
  symbol can't be removed, and a symbol with an open position or pending
  order must be closed first

New accounts start with EURUSD and GBPUSD. Use `/addsymbol` for anything
else (indices, metals, crypto, other FX pairs). The EA appends its own
`SymbolSuffix` to each root and starts reporting quotes on its next poll.

**Trading** — `account` is optional everywhere once you have a default
- `/buy symbol:<EURUSD> entry sl [account]` — TP calculated for you
- `/sell symbol entry sl [account]` — same, sell side
- `/marketbuy symbol sl [account]` — no entry needed; the EA fetches the
  live price itself at the exact moment it executes
- `/marketsell symbol sl [account]` — same, sell side

The symbol must already be enabled on that account with `/addsymbol`.
You can also place a pending limit/stop order on your phone: GuardRails
adopts it within about a second, keeps your entry and SL, and re-places it
with the correct risk-based lot size and a 3.25R TP.

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

- **Manual market trades get reversed.** Anything opened at market
  directly on the chart or phone (not through the bot) gets closed within
  about a second. This is detection-and-reversal, not pre-click blocking —
  MT5 has no mechanism for the latter. Positions whose magic falls in
  `ExternalMagicMin…ExternalMagicMax` are exempt, so MetaEq can run on
  the same account independently; GuardRails also hides them from
  `/status`, the position-limit count, and its close commands.
- **Manual pending orders get adopted.** Place a buy/sell limit or stop on
  your phone and GuardRails keeps your entry and SL but deletes the
  original order and re-places it with the account's fixed risk %, the
  correct lot size, and TP at 3.25R. The symbol must be enabled, the
  account must not be blocked, the SL must be present and on the correct
  side, and the account must be below its open-position limit; otherwise
  the order is deleted and the reason is posted to Discord. Stop-limit
  orders are deleted and reported as rejected because their two prices
  can't be safely recreated.
- **Live alerts** for every trade opened, closed, pending-order trigger,
  and every rejected order (with the reason — blocked, market closed,
  missing SL, etc.). Nothing fails silently.
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
- **Symbols and limits sync to the EA.** The backend includes `SYMBOLS ...`,
  `RISK ...`, `MAXPOS ...` and `RRR ...` in every command poll. The EA
  refreshes its Market Watch list, reports ask/bid for each enabled symbol
  under a lower-case key (`EURUSD.m` becomes `eurusd_m`), and uses the risk
  and limit values when adopting manual pending orders. `/status`,
  `/symbols` and the market-closed check read those quote fields.
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

## Running the backend on a Linux VPS (MT5 stays on Windows)

If you prefer to host the Python backend on a Linux VPS while MT5 runs on a separate Windows machine:

1. Follow the step-by-step in **LINUX_DEPLOY.md** — it covers:
   - Creating a dedicated `guardrails` user + venv
   - systemd service with auto-restart
   - Connecting the EA over HTTP or HTTPS

2. Docker alternative: `docker compose up -d` from the repo root (uses `Dockerfile` + `docker-compose.yml`).

3. Critical: use a **strong random `BRIDGE_TOKEN`** in `.env` on the VPS — the EA sends it over the public internet. If you use plain HTTP, restrict inbound 8000 to your Windows IP via UFW (`sudo ufw allow from <windows-ip> to any port 8000`). HTTPS with a real cert (Let's Encrypt + Nginx) is preferred.

4. After the backend is up, in MT5 change **`BackendURL`** to your VPS URL (e.g., `https://bot.yourdomain.com`) and add that exact URL to **Tools > Options > Expert Advisors > Allow WebRequest for listed URL**. The EA will re-register on its next poll.

---

## Safety checklist before touching a live account

- [ ] Tested on demo: a market order, a limit order (entry away from
      price), a stop order, a manual chart click (confirm auto-reversal),
      a trade reaching TP at 3.25R (confirm the auto TP), and 2 losing trades
      (confirm the block and its refusal messages)
- [ ] Confirmed `OWNER_DISCORD_ID` is correct — it's your only access
      control
- [ ] `BRIDGE_TOKEN` is long and random, and your backend port isn't
      exposed to the public internet
- [ ] Sanity-checked at least one calculated lot size against your own
      manual math — tick-value handling varies by instrument
- [ ] Comfortable that there is genuinely no override once blocked

## Known limitations
- Risk rules apply to the whole bot, not per-account — if you
  eventually need different phases to behave very differently per account
  beyond what's here, that'd need extending. Symbols are the exception:
  each account has its own enabled list.
- Discord symbols are bare broker roots; the EA appends `SymbolSuffix`.
  If a root isn't available in that broker's Market Watch, its quotes come
  back as zero and orders are rejected with `symbol_unavailable`.
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
- GuardRails ignores positions/orders inside `ExternalMagicMin…Max`. Any
  other EA on the same account must use a magic in that range — anything
  outside it is treated as a manual trade and auto-closed. MetaEq's
  `GlobalBaseMagic` default is `100000`; the EA's default exemption range
  covers its bank.
