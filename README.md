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

For production, the backend, the cTrader connector, and MT5 itself all run on
one **Linux VPS**. MT5 runs headless under **Wine Staging** on an Xvfb display,
and a small file-bridge relay carries the EA's traffic to the local backend.
Passwords for a new Wine terminal are entered once on the VPS, never through
Discord. See **[LINUX_DEPLOY.md](LINUX_DEPLOY.md)** for the complete walkthrough.

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
- **`provisioning.py`** — Fernet encryption for cTrader OAuth tokens and
  shared token hashing helpers.
- **`ctrader_connector/`** — a separate service that trades cTrader demo
  accounts over Spotware's official Open API.
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
| Risk per trade | 0.5% (challenge accounts), 0.25% (funded accounts) | fixed by phase in `main.py`; enforced server-side on every order |
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

### Production: one Linux VPS

The supported deployment runs the backend, the cTrader connector, and MT5 on a
single Linux VPS (Amazon Linux 2023 or Ubuntu). MT5 runs under Wine Staging and
Xvfb; no Windows host is involved. **LINUX_DEPLOY.md** is the authoritative
walkthrough for the OS packages, Wine build, systemd units, ngrok tunnel, and
health checks.

1. Create the `guardrails` service user, venv, and `/opt/guardrails` deployment.
2. Install the systemd units for the backend, Xvfb, the templated MT5 terminal
   and file bridge, the MT5 provisioner, the cTrader connector, and the public
   tunnel.
3. Create the Discord application and invite the bot (Send Messages + Use
   Slash Commands). Enable Developer Mode so you can copy your user, guild, and
   alert-channel IDs.
4. Put the runtime secrets in `/opt/guardrails/.env`:

   ```env
   BRIDGE_TOKEN=some-long-random-string
   DISCORD_TOKEN=your-bot-token
   OWNER_DISCORD_ID=your-discord-user-id
   ALERT_CHANNEL_ID=channel-id-for-alerts
   GUILD_ID=your-server-id
   PUBLIC_BASE_URL=https://your-public-host
   CREDENTIAL_KEY=<generated-fernet-key>
   CTRADER_CLIENT_ID=<open-api-client-id>
   CTRADER_CLIENT_SECRET=<open-api-client-secret>
   CTRADER_CONNECTOR_TOKEN=<long-random-connector-secret>
   MOBILE_API_TOKEN=<long-random-mobile-secret>
   ```

5. Start the backend. It logs in, syncs slash commands, and posts an "online"
   alert to the alert channel.
6. Register an MT5 account with `/addaccount`. The bot DMs a one-time HTTPS
   link; the owner enters the broker server and trading password there. If the
   broker's server is not in the shared catalog, the form also accepts the
   official MetaQuotes `.exe` download link on `download.mql5.com`, which the
   provisioner installs once under Wine and caches as a broker pack. The provisioner creates an
   isolated terminal, logs in once, scrubs the password from `start.ini`, and
   reports back when the account is live. Never paste an MT5 password into
   Discord.
7. Connect a cTrader demo or live account with `/addctrader`.

### MT5 under Wine

`tools/run-mt5.sh <login>` starts that login's `terminal64.exe` under Wine on
the Xvfb display. Each login gets its own tree cloned from
`/opt/guardrails/mt5/_template`, its own `guardrails-mt5@<login>.service`, its
own bridge unit, and its own `AccountToken`. `EA_Bridge` is configured through
`MQL5/Presets/EA_Bridge.set`; the local `tools/linux_mt5_bridge.py` relay
forwards its file-bridge traffic to `http://127.0.0.1:8000`. The account
password is used only during the initial login and is scrubbed from
`start.ini`; later restarts use MT5's `Config/accounts.dat`.

One login per terminal. Never run a Windows and a Linux EA for the same login
at the same time. A 3.7 GB / 2 vCPU VPS can host roughly three or four MT5
terminals; size up before adding more.

Use `/addsymbol` to enable the broker roots you trade. New accounts start with
`EURUSD` and `GBPUSD`; add indices, metals, or crypto explicitly.

### cTrader

cTrader accounts use Spotware's official Open API through
`ctrader_connector/`, not the MT5 EA. Demo and live accounts are supported;
live accounts are authenticated on the live endpoint and use real money.
OAuth access and refresh tokens are encrypted with `CREDENTIAL_KEY` and stored
by the backend; the connector never stores them.

See the **cTrader accounts** section below for portal registration,
callback URL details, and connector behaviour.

## Commands

**Setup**
- `/addaccount account:<login> phase:<challenge|funded> server:<name>
  [suffix]` — register an MT5 account and DM a one-time HTTPS setup link. The
  password is entered on that page, never in Discord. If the server is not in
  the shared catalog, the page also takes the broker's official MetaQuotes
  `.exe` link on `download.mql5.com` so the VPS can cache that broker's
  branded terminal. The VPS clones a
  clean terminal, logs in once, scrubs the password, and confirms when the EA
  reports. First account becomes your default.
- `/addctrader phase:<challenge|funded>` — connect a **cTrader demo or live**
  account through the official Open API. The bot DMs a one-time link; authorize
  the cTID there and the VPS connector registers the demo and live accounts
  automatically. Live accounts use real money and the same fixed phase risk.
- `/removeaccount account:<login>` — delete the account from GuardRails
  (blocked while in a losing period). For MT5, a stop job queues the watcher to
  stop and disable that login's units; files are kept on the VPS for recovery.
- `/accountstatus account:<login>` — account phase, engine, last EA report,
  balance/equity, and open positions.
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

**Copy trading** — one master, any number of slaves, opt-in only
- `/copy status` — show the master, slaves, and recent copied trades
- `/copy start master:<login> slaves:<login,login>` — copy every new master
  entry and its SL to each slave. The first master report after enabling is a
  baseline: trades already open at that moment are never copied retroactively.
- `/copy stop` — stop copying new trades. Existing slave positions keep their
  SL/TP and still close when the master closes.
- `/copy addslave slave:<login>` / `/copy removeslave slave:<login>`
- `/copy reset` — forget the configuration. Broker positions stay open.

The PWA has the same controls on its **Copy** tab. A slave receives only the
master's side, entry and stop loss; it calculates its own fixed phase risk,
lot size, TP at 3.25R, position limit, block state and symbol list. Market and
pending entries are both copied, including manual trades. Closing or
cancelling a master trade closes or cancels the matching slave leg. Symbol
mapping handles broker decorations such as `EURUSD.m` and `US100.cash`, but
the instrument must be enabled on the slave with `/addsymbol`.

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
  `/status`, the position-limit count, and its close commands. The one
  exception is a **copy master**, which leaves manual market positions open
  so they can be reported and copied to its slaves.
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
  limit, drawdown baseline. Computed as a fixed offset from UTC rather than
  depending on a system timezone database; EAT has no daylight saving.

---

## Running everything on a Linux VPS

The production layout is one VPS running:

- the FastAPI/Discord backend (`guardrails.service`);
- one Wine-hosted MT5 terminal per login (`guardrails-xvfb.service` +
  `guardrails-mt5@<login>.service`);
- one file-bridge relay per login (`guardrails-mt5-bridge@<login>.service`);
- the root provisioning watcher (`guardrails-mt5-provision.service`);
- the cTrader Open API connector (`guardrails-ctrader.service`);
- a public HTTPS tunnel (`guardrails-ngrok.service`).

**LINUX_DEPLOY.md** has the complete Amazon Linux 2023 walkthrough: Wine
Staging build, Xvfb, template creation, one-time setup links, per-account
systemd units, ngrok, and health checks.

Size the VPS at **2 GB RAM** if possible. A 1 GB instance works with 4 GB of
swap and Docker disabled, but it is tight. Never run two terminals for the
same MT5 login at once.

## cTrader accounts (official Open API)

GuardRails can trade **cTrader demo and live** accounts through Spotware's
official Open API. MT5 accounts keep using the EA; cTrader accounts use a
separate `ctrader_connector` service on the VPS. Both speak the same `/report`,
`/commands` and `/event` contract, so all Discord commands and risk rules work
the same way.

Live accounts are authenticated on `live.ctraderapi.com` and demo accounts on
`demo.ctraderapi.com`; cTrader keeps the two environments fully separate. A
live account is real money and is managed with the same fixed phase risk as a
demo account. Manual-trade parity (auto-adopting manual pending orders and
closing manual market trades) is not implemented for cTrader yet.

### 1. Register the Open API application

1. Sign in at <https://openapi.ctrader.com> with the cTID that owns the trading
   account.
2. Create the application, then open **Applications -> Edit -> Redirect URIs**.
   Add the callback URL. `<PUBLIC_BASE_URL>` is a placeholder, not literal
   text: use the real HTTPS value from the VPS `.env` (the same public origin
   used by the PWA), for example:

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

The service polls the backend for grants, discovers demo and live accounts with
`GET_ACCOUNTS_REQ`, registers them, and then runs one TCP session per account.
It reconnects with backoff if cTrader drops the session and asks the backend for
a fresh access token on every reconnect.

### 3. Connect an account

Run `/addctrader phase:challenge` in Discord, open the DM link, and authorize the
cTID. The link is single-use and expires after 15 minutes. When the connector
discovers the account(s), you get a Discord message with each account key
(`ctrader:<ctidTraderAccountId>`), phase, risk, and symbols. Live accounts are
flagged as real money.

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
- Broker suffix is stored per account, but the running EA still relies on its
  own `SymbolSuffix` input. Keep that input in sync with the suffix registered
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
- cTrader supports demo and live accounts. Live accounts are authenticated on
  the live endpoint and trade real money with the same fixed phase risk.
  Manual pending-order adoption and manual market-trade reversal are MT5-only
  for now. cTrader positions are
  attributed by the `GuardRails` label; a manual order that overwrites the
  label on a netted position can make that position look manual, so hedged
  accounts remain the recommended cTrader setup.
