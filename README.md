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
provisioning agent that creates and supervises one portable MT5 terminal per
account. The reference implementation is a **Windows agent**; on a Linux VPS
the one-time `tools/linux_mt5_capture.py` watcher performs the same Wine-hosted
first login. Passwords are collected through a single-use HTTPS form, never
through Discord. See **[PROVISIONING.md](PROVISIONING.md)** and
**[LINUX_DEPLOY.md](LINUX_DEPLOY.md)** for both paths.

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
   `build_template.ps1` copies `EA_Bridge.ex5` from the repo (or the live
   terminal data directory) into the template.
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
     other EAs on the same account (default `100000–119999`). GuardRails
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
- `/addctrader phase:<challenge|funded>` — connect a **cTrader demo** account
  through the official Open API. The bot DMs a one-time link; authorize the
  cTID there and the VPS connector registers the demo account automatically.
  Live cTrader accounts are skipped and left untouched in this build.
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

---

## Automatic behavior — nothing you have to ask for

- **Manual market trades get reversed.** Anything opened at market
  directly on the chart or phone (not through the bot) gets closed within
  about a second. This is detection-and-reversal, not pre-click blocking —
  MT5 has no mechanism for the latter. Positions whose magic falls in
  `ExternalMagicMin…ExternalMagicMax` are exempt, so another EA can run on
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
  %) ÷ that one-lot loss, rounded to the broker's lot step (the nearer
  step, but never more than 5% above the target risk), then
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

## Running everything on a Linux VPS (backend + MT5 under Wine)

The backend, cTrader connector and a single MT5 terminal can share one Ubuntu
VPS. MT5 runs headless under **Wine Staging 11.22** on an Xvfb display, and a
small file-bridge relay carries the EA's `/report` and `/commands` traffic to
the local backend. **LINUX_DEPLOY.md** has the complete walkthrough:

- dedicated `guardrails` user + venv + systemd service with auto-restart;
- Wine prefix, Xvfb and MT5 systemd units;
- one-time password login, then passwordless restarts from
  `Config/accounts.dat`;
- the file bridge used when MT5's WebRequest allow-list cannot be populated;
- health checks for authorization, reports and command consumption.

Size the VPS at **2 GB RAM** if possible. A 1 GB instance works with 4 GB of
swap and Docker disabled, but it is tight. Keep the Windows agent and the Linux
MT5 terminal mutually exclusive for the same login: never run both at once.

If you prefer to split hosts, the same guide still covers the backend-only
setup plus the EA over HTTP/HTTPS or an SSH tunnel.

### Backend-only split (MT5 stays on Windows)

1. Follow steps 1–4 in **LINUX_DEPLOY.md** — they cover the dedicated
   `guardrails` user, venv and systemd unit.

2. Docker alternative: `docker compose up -d` from the repo root (uses
   `Dockerfile` + `docker-compose.yml`).

3. Critical: use a **strong random `BRIDGE_TOKEN`** in `.env` on the VPS — the
   EA sends it over the public internet. If you use plain HTTP, restrict
   inbound 8000 to your Windows IP via UFW
   (`sudo ufw allow from <windows-ip> to any port 8000`). HTTPS with a real
   cert (Let's Encrypt + Nginx) is preferred.

4. After the backend is up, in MT5 change **`BackendURL`** to your VPS URL
   (e.g., `https://bot.yourdomain.com`) and add that exact URL to
   **Tools > Options > Expert Advisors > Allow WebRequest for listed URL**.
   The EA will re-register on its next poll.

---

## cTrader demo accounts (official Open API)

GuardRails can also trade **cTrader demo** accounts through Spotware's official
Open API. MT5 accounts keep using the EA; cTrader accounts use a separate
`ctrader_connector` service on the VPS. Both speak the same `/report`,
`/commands` and `/event` contract, so all Discord commands and risk rules work
the same way.

This first release is deliberately demo-only. Live cTrader accounts returned by
the OAuth grant are reported to the backend and skipped; they are never
registered, authenticated, or traded. Manual-trade parity (auto-adopting manual
pending orders and closing manual market trades) is not implemented for cTrader
yet.

### 1. Register the Open API application

1. Sign in at <https://openapi.ctrader.com> with the cTID that owns the demo
   account.
2. Create the application, then open **Applications -> Edit -> Redirect URIs**.
   Add the callback URL. `<PUBLIC_BASE_URL>` is a placeholder, not literal
   text: use the real HTTPS value from the VPS `.env` (the same URL used for
   `/addaccount` setup links), for example:

   ```
   https://bot.yourdomain.com/ctrader/callback
   ```

   Enter it with no angle brackets, spaces, or trailing slash before
   `/ctrader/callback`. The portal rejects bare IP addresses and `http://`
   URLs. If `PUBLIC_BASE_URL` is a free ngrok tunnel, its hostname changes on
   restart; use a stable/reserved domain so the registered callback keeps
   working.
3. Request the **trading** scope. Spotware reviews new applications, so the
   client ID/secret only work after approval. The app's website URL field is
   not the redirect URI.
4. Put `CTRADER_CLIENT_ID`, `CTRADER_CLIENT_SECRET` and (if it differs from the
   derived URL) `CTRADER_REDIRECT_URI` in the VPS `.env`. Generate a long random
   `CTRADER_CONNECTOR_TOKEN` there too. Restart the backend after editing `.env`.

The backend owns the OAuth grant. Access and refresh tokens are encrypted with
`CREDENTIAL_KEY`; refresh-token rotation is persisted atomically. The connector
never stores tokens.

### 2. Run the connector service

From `/opt/guardrails` on the VPS (same venv as the backend):

```bash
sudo -u guardrails /opt/guardrails/.venv/bin/python -m ctrader_connector.main
```

For a permanent install, copy `guardrails-ctrader.service` to
`/etc/systemd/system/`, then:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now guardrails-ctrader
sudo journalctl -u guardrails-ctrader -f
```

The service polls the backend for grants, discovers demo accounts with
`GET_ACCOUNTS_REQ`, registers them, and then runs one TCP session per account.
It reconnects with backoff if cTrader drops the session and asks the backend for
a fresh access token on every reconnect.

### 3. Connect the demo account

Run `/addctrader phase:challenge` in Discord, open the DM link, and authorize the
cTID. The link is single-use and expires after 15 minutes. When the connector
discovers the demo account, you get a Discord message with the account key
(`ctrader:<ctidTraderAccountId>`), phase, risk, and symbols.

Use the same commands as MT5: `/status`, `/buy`, `/sell`, `/marketbuy`,
`/marketsell`, `/close`, `/closeall`, `/symbols`, `/addsymbol`, `/weekly`. Use
`/removeaccount` to forget a cTrader account (this also revokes the stored grant
when it was the last account on that grant).

### cTrader behaviour worth knowing

- **Broker symbol names differ.** The connector resolves a Discord root such as
  `EURUSD` against the broker's list (`EUR/USD`, `EURUSD.raw`, ...). If a root is
  ambiguous or unavailable, set `CTRADER_SYMBOL_ALIASES`, for example
  `CTRADER_SYMBOL_ALIASES='{"US100": "US100.cash", "XAUUSD": "GOLD"}'`.
- **Risk sizing is conservative.** Position size mirrors the EA: balance × risk %
  ÷ the loss between entry and SL. Quote-currency risk is converted to the
  deposit currency using the side that overstates the loss, and the highest
  available conversion path wins. Margin is capped at 80% of equity by default;
  a broker minimum lot that does not fit is rejected with
  `margin_cap_below_min_lot`.
- **Market orders get SL/TP immediately after the fill.** cTrader does not accept
  SL/TP on MARKET orders. The connector amends them right after the fill; if the
  amend fails it closes the position and posts `ORDER_REJECTED ...
  protection_failed`.
- **Stale quotes are not tradable.** Quotes older than 3 minutes are reported as
  zero (market considered closed); orders require a quote younger than 60
  seconds.
- **Bot ownership uses the `GuardRails` label.** Only positions and orders whose
  `tradeData.label` starts with `GuardRails` are reported/closed. Account
  balance, equity, and margin still include everything on the account.
- **Netted accounts are warned about.** The connector tracks the volume it
  opened, closes only that volume, and prorates net P&L when a broker position
  also contains manual volume. A hedged account is still safer: on a netted
  account a manual trade in the opposite direction can reduce or merge with the
  bot position, so parity is best-effort rather than exact.
- **Closing deals are replayed.** If a position closes while the connector is
  offline, it queries the deal history and still posts one aggregated
  `TRADE_CLOSED` event with the correct net P&L.

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
- Discord symbols are bare broker roots with their original casing; the EA
  appends `SymbolSuffix`. If a root isn't available in that broker's Market
  Watch, its quotes come back as zero and orders are rejected with
  `symbol_unavailable`.
- Broker suffix is stored per account for provisioning, but a
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
  outside it is treated as a manual trade and auto-closed.
- The cTrader MVP is demo-only: live accounts from an OAuth grant are skipped
  without being authenticated or traded. Manual pending-order adoption and
  manual market-trade reversal are MT5-only for now. cTrader positions are
  attributed by the `GuardRails` label; a manual order that overwrites the
  label on a netted position can make that position look manual, so hedged
  accounts remain the recommended cTrader setup.
