# Automatic MT5 account provisioning

This guide turns `/addaccount` into a real provisioning command: the bot asks
for the MT5 password through a single-use HTTPS form, then a small Windows
agent creates a separate portable MT5 instance, logs in, attaches the
GuardRails EA, supervises the process, and reports status back to Discord.

No inbound port is opened on the Windows host. The agent only makes outbound
HTTPS requests to the backend.

```
Discord  ──►  Backend (Linux VPS)  ◄──  Windows agent  ──►  portable MT5 #1
  you          FastAPI + SQLite          polls jobs          portable MT5 #2
               encrypted jobs            launches/stops      portable MT5 #N
```

---

## 1. How the password flow works

1. You run `/addaccount` with the MT5 login, phase, server, and symbol suffix.
2. The bot DMs you a single-use link like
   `https://your-host/provision/<random-token>`.
3. You open the link over HTTPS and enter the MT5 **trading password**.
4. The backend encrypts the password with `CREDENTIAL_KEY` (Fernet) and queues
   a provisioning job. The link is consumed and cannot be reused.
5. The Windows agent claims the encrypted job, decrypts it in memory, creates
   the instance, and writes a temporary `start.ini` so MT5 can log in once.
6. As soon as MT5 confirms authorization, the agent rewrites `start.ini`
   without the password. MT5 keeps its own encrypted account database for
   later auto-logins.
7. The agent reports success/failure. The backend clears the encrypted job
   payload and posts a Discord notification.

The password is never sent to Discord, never written to the backend database
in plaintext, and never logged.

---

## 2. Backend configuration

Add these to `/opt/guardrails/.env` on the Linux VPS (see `.env.example`):

```env
PUBLIC_BASE_URL=https://your-public-host
# Optional, for when the public URL is metered (free ngrok):
# AGENT_BACKEND_URL=http://127.0.0.1:8000
CREDENTIAL_KEY=<generated-fernet-key>
AGENT_TOKEN=<long-random-shared-secret>
PROVISION_TOKEN_TTL=900
AGENT_STALE_SECONDS=90
```

Generate the two secrets **once** and keep them stable:

```bash
python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
python3 -c "import secrets; print(secrets.token_urlsafe(48))"
```

- `CREDENTIAL_KEY` — encrypts provisioning payloads at rest. Changing it makes
  any still-queued jobs undecryptable; completed jobs are unaffected.
- `AGENT_TOKEN` — shared secret between backend and Windows agent. Put the same
  value in the agent's `config.json`.
- `PUBLIC_BASE_URL` — the exact HTTPS URL users will open for setup links. It
  must be reachable from your phone/browser. For ngrok, this is the public
  tunnel URL.
- `AGENT_BACKEND_URL` — optional. The backend URL the Windows agent writes into
  new MT5 instances. Defaults to `PUBLIC_BASE_URL`. Set it when the public URL
  is metered and the EAs should reach the backend another way, typically the
  local end of an SSH forward:

  ```bash
  AGENT_BACKEND_URL="http://127.0.0.1:8000"
  ```

Lock down the file and restart:

```bash
sudo chown guardrails:guardrails /opt/guardrails/.env
sudo chmod 600 /opt/guardrails/.env
sudo systemctl restart guardrails
sudo systemctl status guardrails --no-pager
curl -fsS https://your-public-host/health
```

Expected health response: `{"status":"ok"}`.

### Important: use a stable public URL

The one-time links always use `PUBLIC_BASE_URL`; new MT5 instances use
`AGENT_BACKEND_URL` (which falls back to `PUBLIC_BASE_URL`). A free ngrok URL
can change when the tunnel restarts, which breaks:

- newly generated setup links,
- the agent's `backend_url` if `AGENT_BACKEND_URL` is unset,
- the MT5 WebRequest allow-list hash in the template.

Use a reserved/static ngrok domain, a Cloudflare Tunnel, or a normal domain
with Nginx + Let's Encrypt. If the URL does change, update
`PUBLIC_BASE_URL`, and — only if you don't pin `AGENT_BACKEND_URL` — the agent
`config.json` and the template's WebRequest rule (rebuild the template or add
the new URL in MT5).

Pinning `AGENT_BACKEND_URL` to a local SSH forward keeps EA traffic off the
public URL entirely, which is what makes a free tunnel viable: the setup page
is opened by a browser a few times a month, while an EA polls every 5 seconds.

---

## 3. Windows host prerequisites

On the Windows MT5 machine:

- Windows Server 2016+ or Windows 10/11.
- Python 3.10+ with `pythonw.exe` on `PATH`.
- An existing MT5 installation with the GuardRails EA compiled and working.
- The Windows user must stay signed in. RDP may be disconnected, but do not
  sign out — MT5 is a GUI application and the agent launches it in your
  interactive session.
- Enough RAM for one MT5 per account. A terminal uses roughly 190–250 MB.
  The current 3.8 GB host can realistically run 3–4 instances; upgrade the
  host for more. The agent does **not** enforce a hard account cap.

---

## 4. Build the portable MT5 template

The template is a credential-free copy of the MT5 installation plus the
compiled EA and the existing WebRequest rule.

From an elevated PowerShell in the repo's `agent` folder:

```powershell
cd C:\Users\Administrator\GuardRails\agent
powershell -ExecutionPolicy Bypass -File .\build_template.ps1
```

Default paths:

| Item | Default |
|---|---|
| Source terminal | `C:\Program Files\MetaTrader 5` |
| Source data | auto-detected from `%APPDATA%\MetaQuotes\Terminal\*\origin.txt` |
| Destination | `C:\GuardRails\mt5-template` |
| EA | `..\EA_Bridge.ex5` (fallback: the live data folder) |
| MetaEq | `..\MetaEq.ex5`, `..\MetaEq.set` (fallback: the live data folder) |

If auto-detection finds the wrong data folder, pass it explicitly:

```powershell
.\build_template.ps1 -SourceData "C:\Users\Administrator\AppData\Roaming\MetaQuotes\Terminal\<instance-id>"
```

The script:

- copies `terminal64.exe` and the runtime files (about 232 MB),
- includes `MetaEditor64.exe` and `metatester64.exe` so a fresh portable copy does not trigger a first-run LiveUpdate,
- excludes `Bases`, `logs`, `Profiles`, `Sounds`, and the uninstaller,
- copies `EA_Bridge.ex5`, `MetaEq.ex5`, `MetaEq.set`, and `servers.dat`,
- writes a sanitized `config/common.ini` with **no saved login, server,
  password, installation ID, or push ID**,
- preserves `WebRequest=1` and the `WebRequestUrl` hash,
- forces `AllowLiveTrading=1`,
- disables the built-in MCP assistant endpoints, which would otherwise fight over local ports 22345/22346 when several terminals run.

Verify:

```powershell
Get-Item C:\GuardRails\mt5-template\terminal64.exe
Select-String C:\GuardRails\mt5-template\config\common.ini -Pattern "WebRequestUrl"
```

Do not put a real account login in the template. Each instance gets its own
`start.ini` and EA preset from the agent.

### Broker server lists

MT5 can only connect to servers listed in `config\servers.dat`. A template
built from one broker's terminal does not automatically know another broker's
servers. Keep one small `.dat` file per broker in
`C:\GuardRails\server-lists`, named after the server prefix:

| Server name in Discord | File |
|---|---|
| `FTMO-Demo`, `FTMO-Server` | `ftmo.dat` |
| `FundingPips-MT5`, `FundingPips-Trial` | `fundingpips.dat` |
| `Pepperstone-Demo`, `Pepperstone-MT5-Live01` | `pepperstone.dat` |

The agent picks the longest matching prefix and copies that file into the new
instance before launch. If nothing matches, it keeps the template's own
`servers.dat` and logs a warning.

To add a broker, download its MT5 terminal from the broker's site. The
installer contains the server list even before login:

```powershell
# Example: FTMO (run once)
Invoke-WebRequest `
  -Uri "https://download.terminal.free/cdn/web/ftmo.global.markets/mt5/ftmo5setup.exe" `
  -OutFile "$env:TEMP\ftmo5setup.exe"
Start-Process "$env:TEMP\ftmo5setup.exe" -ArgumentList "/auto" -Wait

# Broker-branded installers put the list in the install folder.
Copy-Item "C:\Program Files\FTMO Global Markets MT5 Terminal\config\servers.dat" `
  C:\GuardRails\server-lists\ftmo.dat
```

If the installer only writes to `%APPDATA%\MetaQuotes\Terminal\<id>\config`,
copy the `servers.dat` from there instead. The list is a binary file; do not
edit it. Re-provision the account after adding a missing list.

Pepperstone's installer stub can be much smaller than the current server list.
Launch the Pepperstone terminal once so it downloads the full list (about
43 KB), then copy that `servers.dat`. Demo accounts use `Pepperstone-Demo`;
live accounts use `Pepperstone-MT5-Live01`. The first demo connection can take
30–60 seconds while MT5 scans access points; the agent allows up to 120
seconds.

---

## 5. Install the Windows agent

From the same elevated PowerShell:

```powershell
.\install_agent.ps1 `
  -BackendUrl "https://your-public-host" `
  -AgentToken "<the same AGENT_TOKEN as the backend>"
```

Optional parameters:

```text
-AgentId windows-1
-InstanceRoot C:\GuardRails\instances
-TemplateDir C:\GuardRails\mt5-template
-ServerListsDir C:\GuardRails\server-lists
-Pythonw C:\path\to\pythonw.exe
-SkipTask
```

The installer:

- writes `agent\config.json` containing the backend URL and agent token,
- removes inherited ACLs from `config.json` and grants only the current user,
  `SYSTEM`, and `Administrators`,
- creates `run_agent.bat` for manual starts,
- registers scheduled task `GuardRailsAgent` at logon with highest privileges,
- starts the task immediately.

Check the agent:

```powershell
Get-ScheduledTask GuardRailsAgent | Get-ScheduledTaskInfo
Get-Content .\agent.log -Tail 30
```

A healthy log contains `GuardRails agent 1.0.5 starting` and no repeating
`heartbeat failed` / `job poll failed` lines.

Agent 1.0.4 no longer treats a transient `no connection` journal line as a
failed login when MT5 later reports `authorized on` (common with Pepperstone,
which often reconnects through another access point a few seconds later).

Agent 1.0.5 adds the MetaEq Manager actions (`metaeq_apply`,
`metaeq_detach`), writes the per-account control/template files, and installs
`MetaEq.ex5`/`MetaEq.set` into each new instance. Restart the scheduled task
after updating `mt5_agent.py` so the new version is loaded.

In Discord, `/accounts` should now show:

```text
Windows provisioning agent: online (last heartbeat ...)
```

### Keep the agent running

The task runs at **logon**. After a reboot, either:

- enable auto-logon for the Windows user (recommended; use `netplwiz` or
  Sysinternals Autologon), or
- sign in manually before expecting provisioning jobs to run.

Do not sign the user out. Disconnecting RDP is fine. A task configured "run
whether user is logged on or not" runs in session 0, where a GUI MT5 terminal
may not initialize correctly.

---

## 6. Provision an account from Discord

```text
/addaccount account:12345678 phase:challenge server:FundingPips-Trial suffix:0
```

- `account` — the real MT5 login. It becomes the backend account key.
- `phase` — `challenge` (1% risk) or `funded` (0.5% risk).
- `server` — the exact server name from the MT5 login dialog. Case matters.
- `suffix` — broker symbol suffix (`0` for none, e.g. `.pro`, `.a`, `b`).

The bot DMs a one-time link. Open it, confirm the server, and enter the **MT5
trading password** (not the investor password). Accounts with 2FA cannot be
automated unless 2FA is disabled.

What happens next:

1. Backend queues an encrypted `provision` job.
2. Agent copies the template to `C:\GuardRails\instances\<login>`.
3. Agent writes `MQL5\Presets\EA_Bridge.set` with a unique per-account bridge
   token and `AccountLoginOverride=<login>`.
4. Agent writes `start.ini` with the login/server/password and launches
   `terminal64.exe /portable /config:...`.
5. Agent watches the MT5 journal for `'<login>': authorized on ...`.
6. On success it rewrites `start.ini` without the password and reports `done`.
7. Discord posts: `Account <login> is live — MT5 instance provisioned...`.

If MT5 restarts itself for a first-run update, the agent follows the new PID
and keeps `start.ini` until authorization, so the relaunched terminal still
logs in and attaches the EA.

Then configure symbols as usual:

```text
/addsymbol symbols:EURUSD,XAUUSD account:12345678
```

Type symbols exactly as they appear in MT5's Market Watch. Casing is preserved
because some brokers use case-sensitive suffixes such as `US100.cash` (FTMO
rejects `US100.CASH`). Pepperstone's MT5 symbols commonly end in `.a`
(for example `EURUSD.a`); either register the account with `suffix:.a` and add
bare roots, or add the full symbol names with `suffix:0`.

The backend sends the symbol list, risk %, max positions, and RRR to the EA on
every poll.

### Managing the instance

| Command | What it does |
|---|---|
| `/accounts` | All accounts + live instance status + agent summary |
| `/accountstatus account:<login>` | One account, instance PID, last report, balance/equity |
| `/provisionaccount account:<login>` | Fresh one-time link after a failed provision |
| `/restartaccount account:<login>` | Stop and restart the terminal |
| `/stopaccount account:<login>` | Stop the terminal, keep the account/trades |
| `/removeaccount account:<login>` | Stop the terminal and delete the account (still subject to the losing-day lock) |

The agent also auto-restarts a crashed terminal, with exponential backoff and a
configurable retry limit (`max_restarts` in `config.json`, default 5).

---

## 7. MetaEq Manager (optional)

MetaEq is a separate portfolio EA that trades three pairs: `NAS100`, `XAUUSD`,
and `USDJPY`. GuardRails can attach it to a provisioned account, open its chart
inside that account's terminal, and toggle individual strategies from Discord
without restarting MT5. MetaEq positions use magic numbers `101000`–`111000`,
inside GuardRails' external-magic exemption window, so the two EAs coexist on
one account.

### Before attaching

1. `MetaEq.mq5` must be compiled to `MetaEq.ex5` and present in the repo root
   (or already in the template).
2. `MetaEq.set` must exist. It is the authoritative strategy/input preset; the
   backend's catalog (`metaeq.py`) mirrors its pair membership.
3. Rebuild the portable template so every new instance gets the files:
   ```powershell
   cd C:\Users\Administrator\GuardRails\agent
   powershell -ExecutionPolicy Bypass -File .\build_template.ps1
   ```
   Confirm:
   ```powershell
   Get-Item C:\GuardRails\mt5-template\MQL5\Experts\MetaEq.ex5
   Get-Item C:\GuardRails\mt5-template\MQL5\Presets\MetaEq.set
   ```
4. Restart the agent task so version 1.0.5 is running:
   ```powershell
   Stop-ScheduledTask GuardRailsAgent
   Start-ScheduledTask GuardRailsAgent
   Get-Content .\agent.log -Tail 5
   ```

### Attach and verify

```text
/metaeq attach account:<login>
/metaeq status account:<login>
```

`/metaeq attach` uses the account's registered symbol suffix unless you pass
`suffix:`. It queues a `metaeq_apply` job; the agent then:

- writes `MQL5\Files\MetaEq\control.ini` (all 11 strategies enabled by
  default, plus the chart symbol),
- writes `MQL5\Files\GuardRails\metaeq.ini` with `active=1`, the template
  name, chart symbol, timeframe, and a revision hash,
- creates `MQL5\Profiles\Templates\MetaEq.tpl` if it is missing,
- copies `MetaEq.ex5` / `MetaEq.set` into the instance if the template has
  them.

`EA_Bridge` reads `metaeq.ini` on its timer and opens one MetaEq chart with
that template. It will not open a duplicate chart. The MetaEq journal should
show:

```text
EA_Bridge ... MetaEq chart opened on NAS100 with MetaEq.tpl (revision ...)
MetaEq ... MetaEq Manager: control loaded | S1=1 S2A=1 ... S9=1
MetaEq ... Mother EA initialized | comment 'MetaEq' | base magic 100000 ...
```

If MetaEq is attached, every `/report` from the terminal includes
`metaeq=1` and `metaeq_symbol`, and `/accountstatus` reflects live detection.

### Pair and regime controls

| Pair | Ranging-sensitive | Not affected |
|---|---|---|
| `NAS100` | `S1`, `S3`, `S4`, `S8` | `S9` |
| `XAUUSD` | `S2B`, `S6` | (none) |
| `USDJPY` | `S2A`, `S3B`, `S5`, `S7` | (none) |

- `/metaeq toggle strategy:<name> enabled:<true|false>` switches one strategy.
- `/metaeq pair pair:<name> enabled:<true|false>` bulk-switches a whole pair.
- `/metaeq regime pair:<name> regime:<trending|ranging>` switches only the
  ranging-sensitive strategies for that pair. `ranging` turns them off;
  `trending` turns them back on. "Not affected" strategies keep their state.
- `/metaeq sync` rewrites `control.ini` from the saved state.

Toggles are saved in SQLite immediately and pushed as a `metaeq_apply` job. The
agent rewrites `control.ini`; MetaEq re-reads it about once per second. No MT5
restart or EA recompile is needed. Disabling a strategy blocks **new entries**
only: open positions keep their stops, targets, and exit logic. Turning off
`S3B`, `S4`, `S5`, or `S6` also cancels that strategy's working pending orders.
`S8` marks a disabled Monday as handled so re-enabling it later in the day
cannot enter late.

These toggles are runtime gates, not EA inputs. Opening the MetaEq chart's
properties will still show the preset values from `MetaEq.set`; that is
expected and does not mean the toggle failed. The chart's top-left status
block shows the live gates, and `/metaeq status` adds an `EA-applied` line
built from `MQL5\Files\MetaEq\status.ini`, which MetaEq rewrites after each
control read.

### Detach

```text
/metaeq detach account:<login>
```

The agent writes `active=0` and removes `control.ini`. `EA_Bridge` closes the
MetaEq chart on its next timer tick. Saved toggles remain in SQLite and are
reapplied on the next attach. Detaching does not close existing MetaEq
positions; manage those in MT5 or re-attach and let the strategy exit them.

### MetaEq files

| Path (inside the instance) | Purpose |
|---|---|
| `MQL5\Experts\MetaEq.ex5` | Compiled portfolio EA |
| `MQL5\Presets\MetaEq.set` | Strategy inputs and pair symbols |
| `MQL5\Files\MetaEq\control.ini` | Runtime enable/disable gates |
| `MQL5\Files\MetaEq\status.ini` | Gates the running EA actually applied |
| `MQL5\Files\GuardRails\metaeq.ini` | Supervisor config for `EA_Bridge` |
| `MQL5\Files\GuardRails\metaeq.applied` | Revision applied to the open chart |
| `MQL5\Profiles\Templates\MetaEq.tpl` | Chart template used to open MetaEq |

The control file is written atomically by the agent and is consumed
read-only by the EA. If it is missing, partial, or corrupt, MetaEq fails
closed: all new entries are blocked while existing positions remain managed.
`/metaeq sync` (or `/metaeq attach`) restores it.

You do **not** need to add `NAS100`/`XAUUSD`/`USDJPY` to `/addsymbol` for
MetaEq. That list only controls GuardRails' own manual-trade commands and
adoption logic. Keep MetaEq's symbols in `MetaEq.set`/the generated control
file instead.

---

## 8. First live test (do this on demo)

1. Deploy the backend with the new env vars and confirm `/health`.
2. Build the template and install the agent. Confirm `/accounts` shows the
   agent online.
3. `/addaccount` with a **demo** login/server/phase.
4. Submit the form. Watch:
   - `agent.log` for `job ... provision`,
   - MT5 journal for `authorized on`,
   - Discord for the "live" notification.
5. `/accountstatus` — instance should be `running`, and the EA should report a
   balance within a few seconds.
6. `/addsymbol`, then place a small test trade with `/marketbuy`.
7. `/restartaccount` and confirm the EA reports again.
8. `/stopaccount`, confirm status `stopped`; `/restartaccount` to bring it back.
9. `/metaeq attach` on the demo. Confirm the MetaEq chart opens and the
   journal shows `control loaded` and all 11 strategies ready.
10. `/metaeq toggle strategy:S9 enabled:false` (or another strategy). Confirm
    `control.ini` changes and the journal logs the new gate state **without**
    an MT5 restart. Re-enable it with `/metaeq sync` or another toggle.
11. `/metaeq detach`, confirm the MetaEq chart closes and
    `MQL5\Files\MetaEq\control.ini` is removed. Re-attach and confirm it
    comes back with the saved toggles.
12. `/removeaccount` on the demo, confirm the folder under
    `C:\GuardRails\instances\<login>` is gone and the account disappears.

Only then repeat with a live account.

---

## 9. Troubleshooting

| Symptom | Likely cause / fix |
|---|---|
| `/addaccount` says provisioning is not configured | `CREDENTIAL_KEY` or `PUBLIC_BASE_URL` missing on the backend. Set and restart. |
| Link says expired/used | Links are single-use and expire in 15 minutes. Run `/provisionaccount`. |
| Agent shows offline | Scheduled task not running, user signed out, or wrong `AGENT_TOKEN`. Check `agent.log` and the task. |
| Job stays `queued` | Agent offline or cannot reach `backend_url`. Check `agent.log` for `backend unreachable`. |
| Provision fails with "MT5 rejected the login" | Wrong login, server, or trading password; or 2FA/investor password. Run `/provisionaccount`. |
| Journal says "trading has been disabled - investor mode" | You entered the investor (read-only) password. Run `/provisionaccount` and enter the master/trading password instead. |
| Provision times out waiting for authorization | Server name mismatch, missing broker `servers.dat`, broker blocks the IP, or MT5 shows a dialog. Check the agent log for `using broker server list` / `no broker server list matched`, then the instance journal under `instances\<login>\logs`. |
| Provision fails with "MT5 exited before confirming the login" | MT5 closed before/while logging in (often a first-run LiveUpdate). The agent waits 30s for a relaunched terminal. Rebuild the template with the current `build_template.ps1` and retry. |
| EA never reports after "live" | WebRequest rule missing/stale in the template, wrong `BackendURL`, or EA not attached. Check `instances\<login>\MQL5\Logs` and the terminal journal. |
| All instances fail after changing ngrok URL | If `AGENT_BACKEND_URL` is pinned (recommended), only new setup links are affected — update `PUBLIC_BASE_URL`. Otherwise also update the agent `backend_url` and the template WebRequest rule. |
| Terminal starts but EA is not attached | `start.ini` missing `[StartUp]` or the preset file is missing. Re-run `/provisionaccount`. |
| Not enough RAM | Upgrade the Windows host. The agent has no hard cap but each terminal needs ~200 MB+ free. |
| Instance removed but folder remains | The terminal was still holding a file lock. Run `/removeaccount` again; the agent retries deletion. |
| `/metaeq attach` queued but no chart opens | Agent older than 1.0.5, `MetaEq.ex5`/`MetaEq.set` missing from the template, or `EA_Bridge.ex5` older than the MetaEq supervisor. Check `agent.log`, confirm `MQL5\Files\GuardRails\metaeq.ini` has `active=1`, then restart the agent and rebuild the template. |
| Journal: `control.ini missing or unreadable` / `FAIL-CLOSED` | The agent has not written the control file yet, or it was deleted/edited. Run `/metaeq sync`. MetaEq blocks new entries but still manages open positions. |
| MetaEq chart opens but no `Mother EA initialized` | `MetaEq.tpl` is missing or points at the wrong EA, or `MetaEq.ex5` failed to install. Check `MQL5\Profiles\Templates\MetaEq.tpl` and `MQL5\Experts\MetaEq.ex5`; re-run `/metaeq attach`. |
| `/metaeq status` says the EA is not live, but the chart exists | The terminal has not reported `metaeq=1` yet (report interval), or the open chart is not the one in `metaeq.ini`. Check the next `/report`/`/accountstatus` and the journal. |
| MetaEq chart reopens repeatedly | A failed template revision is retried only after `metaeq.ini` changes; check the journal for the 60-second failure message and confirm the chart symbol exists with the account's suffix. |

Useful checks:

```powershell
Get-Content C:\Users\Administrator\GuardRails\agent\agent.log -Tail 50
Get-ChildItem C:\GuardRails\instances
Get-Process terminal64 | Select-Object Id,Path,WorkingSet64
```

```bash
sudo journalctl -u guardrails -n 80 --no-pager
```

Never paste `.env`, `config.json`, job payloads, or setup links into Discord or
an issue tracker.

---

## 10. Manual fallback

The original manual workflow still works. If you do not install the Windows
agent, `/addaccount` registers the account and gives you a setup link, but the
job stays queued and no terminal is created. You can keep using one manually
installed MT5 terminal with the EA's `AccountLoginOverride` input, exactly as
before.

---

## 11. Security notes

- **Passwords never touch Discord.** The slash command has no password option.
- **Transport:** use HTTPS. Plain HTTP exposes the form password and agent
  token to anyone on the path.
- **At rest:** job payloads are Fernet-encrypted with `CREDENTIAL_KEY`. The
  backend clears the encrypted payload when the job finishes.
- **On Windows:** the plaintext password exists only in the temporary
  `start.ini` between launch and the first successful authorization. The agent
  then rewrites the file without it.
- **Agent token:** stored in `config.json` with ACLs limited to the current
  user, SYSTEM, and Administrators. Rotate it by changing `AGENT_TOKEN` on the
  backend and in `config.json`, then restarting both.
- **Per-account bridge tokens:** each provisioned EA gets its own token. The
  global `BRIDGE_TOKEN` remains valid for the original manually installed EA.
- **No inbound Windows ports:** the agent polls outbound, so the Windows host
  needs no public listener.
- **2FA:** MT5 accounts with 2FA cannot be auto-provisioned. Disable 2FA for
  bot-managed accounts or provision them manually.
