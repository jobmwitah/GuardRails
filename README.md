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

For fully automatic multi-account setup there is a fourth piece: a small
**Windows provisioning agent** that creates and supervises one portable MT5
terminal per account. Passwords are collected through a single-use HTTPS form,
never through Discord. See **[PROVISIONING.md](PROVISIONING.md)** for the full
step-by-step setup.

- **`EA_Bridge.mq5`** — an Expert Advisor that runs inside MT5. It reports
  account state and live prices every few seconds, executes whatever
  commands the backend queues for it (open, close, cancel), and enforces
  safety rules locally too (so a trade is blocked even if the backend is
  briefly unreachable). It also supervises the optional MetaEq chart.
- **`MetaEq.mq5`** — the portfolio EA that runs on its own chart. GuardRails
  adds a small runtime control-file check to each strategy's entry point, so
  Discord can disable new entries without restarting MT5 or changing EA
  inputs.
- **`metaeq.py`** — the MetaEq strategy catalog (pair membership and
  ranging-market sensitivity) and the renderer for `control.ini`.
- **`main.py`** — the backend. A FastAPI server the EA talks to, plus a
  Discord bot you talk to. Holds all the account state, risk rules, and
  trade history.
- **`storage.py`** — SQLite persistence, so a backend restart doesn't lose
  your accounts, loss counts, or trade history.
- **`provisioning.py`** — one-time setup links, Fernet encryption for
  provisioning payloads, and agent authentication helpers.
- **`agent/mt5_agent.py`** — optional Windows agent that creates, logs in,
  attaches the EA to, restarts, and removes portable MT5 instances.
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

### Quick start — automatic provisioning

If you want `/addaccount` to create and start MT5 terminals for you:

1. Add `PUBLIC_BASE_URL`, `CREDENTIAL_KEY`, and `AGENT_TOKEN` to the backend
   `.env`, then deploy `main.py`, `storage.py`, and `provisioning.py`.
2. On Windows, build the portable template and install the agent:
   ```powershell
   cd C:\Users\Administrator\GuardRails\agent
   .\build_template.ps1
   .\install_agent.ps1 -BackendUrl "https://your-public-host" -AgentToken "<AGENT_TOKEN>"
   ```
   `build_template.ps1` copies `EA_Bridge.ex5`, `MetaEq.ex5`, and
   `MetaEq.set` from the repo (or the live terminal data directory) into the
   template. `MetaEq.ex5`/`MetaEq.mq5` and `MetaEq.set` must be present before
   the first `/metaeq attach`; compile `MetaEq.mq5` in MetaEditor first if
   the repo copy is missing.
3. Use `/addaccount` with the login, phase, server, and suffix. The bot DMs a
   one-time link for the password.

The complete guide — backend env, template, scheduled task, testing, and
troubleshooting — is in **[PROVISIONING.md](PROVISIONING.md)**.

The manual setup below still works if you prefer one terminal per account
managed by hand.

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
   - `AccountLoginOverride` — leave `0` to report the terminal's own MT5
     login. Set it to the backend account key only when that account was
     registered under a different number; reports and command polls then
     use the override.
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

# Automatic provisioning (see PROVISIONING.md)
PUBLIC_BASE_URL=https://your-public-host
# Optional: separate URL for EA/agent traffic, e.g. an SSH forward when the
# public tunnel is metered: AGENT_BACKEND_URL=http://127.0.0.1:8000
CREDENTIAL_KEY=<generated-fernet-key>
AGENT_TOKEN=<long-random-agent-secret>
```
`CREDENTIAL_KEY` and `AGENT_TOKEN` are only needed for automatic provisioning.
The backend returns a clear "provisioning is not configured" error until they
are set.
Then:
```
python main.py
```
You should see it log in, sync commands, and post an "online" alert to
your alert channel.

---

## Commands

**Setup**
- `/addaccount account:<login> phase:<challenge|funded> server:<name>
  [suffix]` — register an account and queue its portable MT5 instance. The
  bot DMs a one-time HTTPS link for the trading password; the password never
  appears in Discord. First account becomes your default.
- `/provisionaccount account:<login>` — generate a fresh one-time link after a
  failed provisioning attempt.
- `/restartaccount account:<login>` — stop and restart that account's MT5
  instance.
- `/stopaccount account:<login>` — stop the instance but keep the account.
- `/removeaccount account:<login>` — stop the instance and delete the account
  (blocked while in a losing period).
- `/accountstatus account:<login>` — instance status, PID, last EA report,
  balance/equity, and agent health.
- `/setdefault account:<login>` — change which account commands default to
- `/accounts` — list every tracked account: phase, instance status, symbols,
  risk %, balance, equity, blocked state, loss/position counts, live prices

**Symbols** — set per account, bare broker roots only (no suffix)
- `/symbols [account]` — list enabled symbols and their live ask/bid
- `/addsymbol symbols:<EURUSD,XAUUSD> [account]` — enable one or more
  symbols. Commas or spaces both work; `XAU/USD` is normalised to `XAUUSD`.
  Casing is preserved because some brokers use case-sensitive suffixes
  (e.g. FTMO's `US100.cash`, not `US100.CASH`)
- `/removesymbol symbol:<EURUSD> [account]` — disable one. The last
  symbol can't be removed, and a symbol with an open position or pending
  order must be closed first

New accounts start with EURUSD and GBPUSD. Use `/addsymbol` for anything
else (indices, metals, crypto, other FX pairs). Type the symbol exactly as
MT5's Market Watch shows it: case matters for suffixes such as `US100.cash`.
Pepperstone commonly uses a `.a` suffix (for example `EURUSD.a`); type the
full name, or register the account with `suffix:.a` and add bare roots.
The EA appends its own `SymbolSuffix` to each root and starts reporting quotes
on its next poll.

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

**MetaEq Manager** — attach the portfolio EA and toggle its strategies live
- `/metaeq attach [account] [suffix]` — install the MetaEq files and ask
  `EA_Bridge` to open the MetaEq chart. All 11 strategies start enabled.
- `/metaeq detach [account]` — close the MetaEq chart and remove its
  control file. Saved toggles are kept for the next attach.
- `/metaeq status [account]` — attach state, live EA detection, the gates
  the running EA actually applied, and every strategy grouped by pair and
  ranging sensitivity.
- `/metaeq toggle strategy:<S1|S2A|...> enabled:<true|false> [account]` —
  switch one strategy's new entries on or off. Existing positions keep
  being managed.
- `/metaeq pair pair:<NAS100|XAUUSD|USDJPY> enabled:<true|false> [account]` —
  bulk switch every strategy in one pair.
- `/metaeq regime pair:<...> regime:<trending|ranging> [account]` — switch
  only that pair's ranging-sensitive strategies. `ranging` disables them;
  `trending` re-enables them. Strategies marked "not affected" are left
  alone.
- `/metaeq sync [account]` — rewrite the live control file from the saved
  toggles (useful after a manual file edit or a failed sync).

### MetaEq pairs

Each strategy belongs to exactly one pair. MetaEq is configured so a
strategy never subscribes to, or trades, another pair's symbol.

| Pair | Ranging-sensitive | Not affected |
|---|---|---|
| `NAS100` | `S1`, `S3`, `S4`, `S8` | `S9` |
| `XAUUSD` | `S2B`, `S6` | (none) |
| `USDJPY` | `S2A`, `S3B`, `S5`, `S7` | (none) |

Toggles are runtime gates, not EA inputs. The EA's Inputs tab always shows
the preset from `MetaEq.set`; the live applied values appear in the chart's
top-left status block and in `/metaeq status` under `EA-applied`.

The backend appends the account suffix exactly as entered (for example
`.a`), preserving case. A zero suffix means bare roots (`NAS100`,
`XAUUSD`, `USDJPY`). The MetaEq magic bank is `100000 + offset`
(`101000` ... `111000`), inside `EA_Bridge`'s
`ExternalMagicMin..ExternalMagicMax` exemption window, so GuardRails
never closes, adopts, or counts MetaEq positions.

### How attach works

1. The agent writes `MQL5\Files\MetaEq\control.ini` and
   `MQL5\Files\GuardRails\metaeq.ini`, installs `MetaEq.ex5` and
   `MetaEq.set` from the portable template if they are missing, and creates
   `MQL5\Profiles\Templates\MetaEq.tpl`.
2. `EA_Bridge` reads `metaeq.ini` on its timer. If `active=1` and no chart
   with the MetaEq EA exists, it opens the configured symbol/timeframe and
   applies `MQL5\Profiles\Templates\MetaEq.tpl`. If the template revision
   changes (symbol or suffix change), it reapplies the template. It does not
   open duplicate MetaEq charts.
3. Each strategy's entry function in `MetaEq.mq5` checks
   `control.ini` about once per second. Missing, partial, or corrupt files
   fail closed: every new entry is blocked, but open positions and their
   exits continue normally.
4. Toggling a strategy only rewrites `control.ini`; it does not restart MT5
   or reload the EA. Turning off `S3B`, `S4`, `S5`, or `S6` also cancels
   that strategy's still-working pending orders.
5. After ingesting `control.ini`, `MetaEq` writes
   `MQL5\Files\MetaEq\status.ini`; `EA_Bridge` forwards that state in its
   report. This lets `/metaeq status` show what the running EA actually
   applied, independent of the saved Discord state.

The control file is generated and overwritten by the backend. Do not edit
it by hand during normal operation; use `/metaeq ...` commands instead.

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
- **Instance supervisor** — if the Windows agent is installed, it restarts a
  crashed MT5 terminal with backoff, reports status on every heartbeat, and
  stops/removes the terminal when you ask from Discord.
- **MetaEq chart supervisor** — when MetaEq is attached, `EA_Bridge` opens
  its chart, applies the account-specific template, reports `metaeq=1` plus
  the chart symbol in every `/report`, and reopens the chart if it is
  closed. detaching closes the chart and removes the control file.
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
- **Lot size comes from the broker's own profit calculator.** The EA asks
  MT5 what one lot would lose between entry and SL (`OrderCalcProfit`)
  instead of trusting `SYMBOL_TRADE_TICK_VALUE`, because some CFD brokers
  report a gold tick value that is 100× too small. Lots = (balance × risk
  %) ÷ that one-lot loss, rounded down to the broker's lot step, then
  capped so total margin stays under 80% of equity.
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
- [ ] If MetaEq is attached: tested `/metaeq status`, one `/metaeq toggle`,
      one `/metaeq regime`, and `/metaeq detach`/`attach`; confirmed the
      journal shows `control loaded` and that deleting `control.ini` logs
      `FAIL-CLOSED` (new entries blocked, existing positions still managed)

## Known limitations
- Risk rules apply to the whole bot, not per-account — if you
  eventually need different phases to behave very differently per account
  beyond what's here, that'd need extending. Symbols are the exception:
  each account has its own enabled list.
- Discord symbols are bare broker roots with their original casing; the EA
  appends `SymbolSuffix`. If a root isn't available in that broker's Market
  Watch, its quotes come back as zero and orders are rejected with
  `symbol_unavailable`.
- Broker suffix is stored per account for provisioning and MetaEq, but a
  manually installed EA still relies on its own `SymbolSuffix` input. If you
  run a manual terminal, keep that input in sync with the suffix registered
  in Discord.
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
- The MetaEq Manager catalog is deliberately hardcoded (`metaeq.py`):
  `S1`/`S3`/`S4`/`S8`/`S9` on `NAS100`, `S2B`/`S6` on `XAUUSD`, and
  `S2A`/`S3B`/`S5`/`S7` on `USDJPY`. If you change strategy names, pair
  membership, or the `Verified.set` preset, update `metaeq.py` and its tests
  too. The manager assumes one MetaEq chart per account.
- MetaEq's control file is read from the terminal's local
  `MQL5\Files\MetaEq\control.ini`. If it is deleted, hand-edited into an
  invalid form, or the terminal profile is replaced, MetaEq fails closed
  until `/metaeq sync` or `/metaeq attach` rewrites it.
