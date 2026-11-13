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
- `PUBLIC_BASE_URL` — the exact HTTPS URL users will open. It must be reachable
  from your phone/browser. For ngrok, this is the public tunnel URL.

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

The one-time links and the agent both use `PUBLIC_BASE_URL`. A free ngrok URL
can change when the tunnel restarts, which breaks:

- newly generated setup links,
- the agent's `backend_url`,
- the MT5 WebRequest allow-list hash in the template.

Use a reserved/static ngrok domain, a Cloudflare Tunnel, or a normal domain
with Nginx + Let's Encrypt. If the URL does change, update
`PUBLIC_BASE_URL`, the agent `config.json`, and the template's WebRequest rule
(rebuild the template or add the new URL in MT5).

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

If auto-detection finds the wrong data folder, pass it explicitly:

```powershell
.\build_template.ps1 -SourceData "C:\Users\Administrator\AppData\Roaming\MetaQuotes\Terminal\<instance-id>"
```

The script:

- copies `terminal64.exe` and the runtime files (about 232 MB),
- includes `MetaEditor64.exe` and `metatester64.exe` so a fresh portable copy does not trigger a first-run LiveUpdate,
- excludes `Bases`, `logs`, `Profiles`, `Sounds`, and the uninstaller,
- copies `EA_Bridge.ex5` and `servers.dat`,
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

A healthy log contains `GuardRails agent 1.0.2 starting` and no repeating
`heartbeat failed` / `job poll failed` lines.

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
- `suffix` — broker symbol suffix (`0` for none, e.g. `.pro`, `b`).

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
rejects `US100.CASH`).

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

## 7. First live test (do this on demo)

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
9. `/removeaccount` on the demo, confirm the folder under
   `C:\GuardRails\instances\<login>` is gone and the account disappears.

Only then repeat with a live account.

---

## 8. Troubleshooting

| Symptom | Likely cause / fix |
|---|---|
| `/addaccount` says provisioning is not configured | `CREDENTIAL_KEY` or `PUBLIC_BASE_URL` missing on the backend. Set and restart. |
| Link says expired/used | Links are single-use and expire in 15 minutes. Run `/provisionaccount`. |
| Agent shows offline | Scheduled task not running, user signed out, or wrong `AGENT_TOKEN`. Check `agent.log` and the task. |
| Job stays `queued` | Agent offline or cannot reach `backend_url`. Check `agent.log` for `backend unreachable`. |
| Provision fails with "MT5 rejected the login" | Wrong login, server, or trading password; or 2FA/investor password. Run `/provisionaccount`. |
| Provision times out waiting for authorization | Server name mismatch, missing broker `servers.dat`, broker blocks the IP, or MT5 shows a dialog. Check the agent log for `using broker server list` / `no broker server list matched`, then the instance journal under `instances\<login>\logs`. |
| Provision fails with "MT5 exited before confirming the login" | MT5 closed before/while logging in (often a first-run LiveUpdate). The agent waits 30s for a relaunched terminal. Rebuild the template with the current `build_template.ps1` and retry. |
| EA never reports after "live" | WebRequest rule missing/stale in the template, wrong `BackendURL`, or EA not attached. Check `instances\<login>\MQL5\Logs` and the terminal journal. |
| All instances fail after changing ngrok URL | `PUBLIC_BASE_URL`, agent `backend_url`, and the template WebRequest rule must all be updated to the new URL. |
| Terminal starts but EA is not attached | `start.ini` missing `[StartUp]` or the preset file is missing. Re-run `/provisionaccount`. |
| Not enough RAM | Upgrade the Windows host. The agent has no hard cap but each terminal needs ~200 MB+ free. |
| Instance removed but folder remains | The terminal was still holding a file lock. Run `/removeaccount` again; the agent retries deletion. |

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

## 9. Manual fallback

The original manual workflow still works. If you do not install the Windows
agent, `/addaccount` registers the account and gives you a setup link, but the
job stays queued and no terminal is created. You can keep using one manually
installed MT5 terminal with the EA's `AccountLoginOverride` input, exactly as
before.

---

## 10. Security notes

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
