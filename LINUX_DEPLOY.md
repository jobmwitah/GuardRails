# Manual Linux VPS Setup Guide (Ubuntu / Debian)

Follow these steps on your Linux VPS to run the GuardRails backend independently of the Windows MT5 machine.

---

### Step 1: Clone or copy the repo to your VPS
```bash
sudo useradd -r -m -d /opt/guardrails -s /bin/bash guardrails
sudo mkdir -p /opt/guardrails
sudo chown -R guardrails:guardrails /opt/guardrails

# Clone your repository (or SCP the files from your Windows machine)
sudo apt update && sudo apt install -y git python3-pip python3-venv
sudo -u guardrails git clone https://github.com/jobmwitah/GuardRails.git /opt/guardrails
```

### Step 2: Set up Python Virtual Environment & Dependencies
```bash
cd /opt/guardrails
sudo -u guardrails python3 -m venv .venv
sudo -u guardrails .venv/bin/pip install --upgrade pip
sudo -u guardrails .venv/bin/pip install -r requirements.txt
```

### Step 3: Create your `.env` configuration file
Create `/opt/guardrails/.env` owned by `guardrails`:
```env
BRIDGE_TOKEN=your_long_random_secret_here
DISCORD_TOKEN=your_discord_bot_token_here
OWNER_DISCORD_ID=your_discord_user_id
ALERT_CHANNEL_ID=your_discord_alert_channel_id
GUILD_ID=your_discord_server_id

# Automatic provisioning (see PROVISIONING.md)
PUBLIC_BASE_URL=https://your-public-host
# Optional: where new EAs send traffic. Set to the local SSH forward when the
# public URL is metered (free ngrok): AGENT_BACKEND_URL=http://127.0.0.1:8000
CREDENTIAL_KEY=your_generated_fernet_key
AGENT_TOKEN=your_long_random_agent_secret
PROVISION_TOKEN_TTL=900
AGENT_STALE_SECONDS=90
```

Generate the two provisioning secrets once:

```bash
python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
python3 -c "import secrets; print(secrets.token_urlsafe(48))"
```

`PUBLIC_BASE_URL` must be the exact HTTPS URL used by the one-time password
forms. A free ngrok URL can change when the tunnel restarts, so claim the
account's static domain. `AGENT_BACKEND_URL`, when set, is what new MT5
instances actually talk to — pin it to `http://127.0.0.1:8000` and run the SSH
forward below so EA polling never touches the public tunnel.

#### Rotating `BRIDGE_TOKEN` without downtime

`BRIDGE_TOKEN` accepts a comma-separated list. Put the new token first and
leave the old one in place while you update the EA's `AccountToken` input:

```env
BRIDGE_TOKEN=new_long_random_secret,old_secret
```

Restart the service, update `AccountToken` in MT5 (and reattach the EA so the
new input takes effect), confirm `/report` is returning 200 again, then remove
the old token from `.env` and restart once more.

### Step 4: Install and start the systemd service
```bash
sudo cp guardrails.service /etc/systemd/system/guardrails.service
sudo systemctl daemon-reload
sudo systemctl enable --now guardrails
sudo systemctl status guardrails
```

### Updating an existing deployment

Slash commands re-sync when the service restarts.

```bash
cd /opt/guardrails
sudo -u guardrails git fetch origin
sudo -u guardrails git status --short        # stop if there are local edits
sudo -u guardrails git pull --ff-only origin main
sudo -u guardrails .venv/bin/pip install -r requirements.txt
sudo systemctl restart guardrails
sudo systemctl status guardrails --no-pager
curl -fsS http://127.0.0.1:8000/health
```

Confirm the running commit:

```bash
sudo -u guardrails git -C /opt/guardrails log --oneline -1
```

Then update the Windows side:

1. Pull the same commit on the MT5 host.
2. Copy the new `EA_Bridge.ex5` from the repo (or compile `EA_Bridge.mq5` in
   MetaEditor).
3. Rebuild the portable template:
   ```powershell
   cd C:\Users\Administrator\GuardRails\agent
   powershell -ExecutionPolicy Bypass -File .\build_template.ps1
   ```
4. Restart the agent task so `mt5_agent.py` 1.0.5 is loaded:
   ```powershell
   Stop-ScheduledTask GuardRailsAgent
   Start-ScheduledTask GuardRailsAgent
   Get-Content .\agent.log -Tail 5
   ```

If the Discord commands do not appear, set `GUILD_ID` in the backend `.env` and
restart the service; guild sync is immediate, while global command propagation
can take a few minutes.

---

### Connecting your Windows MT5 EA to the Linux VPS

Because your MT5 terminal runs on Windows and the backend runs on your Linux VPS, **communication goes over the public internet (HTTP or HTTPS)**.

1. **If using HTTP (IP restriction recommended via UFW/firewall on the Linux VPS):**
   - In MT5 (Expert Advisor Inputs), set **`BackendURL`** to `http://<your-linux-vps-ip>:8000`.
   - In MT5 (**Tools > Options > Expert Advisors**), check **"Allow WebRequest for listed URL"** and add `http://<your-linux-vps-ip>:8000`.

2. **If using HTTPS (Recommended for security):**
   - Put a reverse proxy like **Nginx + Certbot (Let's Encrypt SSL)** in front of port 8000 on your Linux VPS with a domain name (e.g., `bot.yourdomain.com`).
   - In MT5 inputs, set **`BackendURL`** to `https://bot.yourdomain.com`.
   - In MT5 **"Allow WebRequest for listed URL"**, add `https://bot.yourdomain.com`.
   - *(Note: MT5 `WebRequest()` automatically uses port 443 when given an `https://` URL).*

---

### Public setup links over ngrok (browser traffic only)

Setup links need a public HTTPS URL the owner can open from a phone. Keep that
tunnel for browser traffic only — never for the EA. A free account has a
monthly request limit, and an EA polling every 5 seconds burns through it;
see the SSH tunnel section below for the EA path.

Install the CLI and authenticate as the service user:

```bash
sudo snap install ngrok
sudo -u ubuntu /snap/bin/ngrok config add-authtoken YOUR_NGROK_AUTHTOKEN
```

Claim one free static domain in the dashboard (**Domains → New Domain**).
With exactly one reserved domain, `ngrok http 8000` uses it automatically, so
the URL survives restarts.

Use the snap **wrapper** in the unit, not the versioned binary:

```ini
# /etc/systemd/system/ngrok-guardrails.service
[Unit]
Description=ngrok tunnel for GuardRails
After=network.target guardrails.service
Wants=guardrails.service

[Service]
User=ubuntu
ExecStart=/snap/bin/ngrok http 8000 --log=stdout
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now ngrok-guardrails
sudo journalctl -u ngrok-guardrails -n 30 --no-pager
```

Do not point `ExecStart` at `/snap/ngrok/<rev>/ngrok` directly. That bypasses
the `snap run` wrapper, so ngrok reads `~/.config/ngrok/ngrok.yml` while
`ngrok config add-authtoken` writes `~/snap/ngrok/<rev>/.config/ngrok/ngrok.yml`
— the service then quietly keeps using the old token and the old domain.

Verify from outside the VPS:

```bash
curl -sS -o /dev/null -w '%{http_code}\n' https://<your-domain>/health
python3 -c "import json,urllib.request; print(json.load(urllib.request.urlopen('https://<your-domain>/health')))"
```

Then set the backend's `PUBLIC_BASE_URL` to `https://<your-domain>`. Leave
`AGENT_BACKEND_URL` pointing at the local end of the SSH forward:

```bash
PUBLIC_BASE_URL="https://<your-domain>"
AGENT_BACKEND_URL="http://127.0.0.1:8000"
```

That split is what keeps `ERR_NGROK_727` from taking the bot down: only a
handful of setup-page requests touch the metered tunnel.

Free ngrok shows a one-time "You are about to visit…" browser warning before
the setup form. It is harmless and appears once per browser per week. Setup
links themselves have no such problem — the backend's own clients fetch them
directly.

### SSH tunnel fallback (no HTTP request quota)

A free ngrok account has a monthly HTTP request limit. MT5 polls every few
seconds, so a busy account can exhaust it (`ERR_NGROK_727`). An SSH local
forward avoids the quota entirely and keeps the backend bound to the VPS
loopback interface.

On the Windows MT5 host, create `C:\GuardRails\start_tunnel.ps1`:

```powershell
$ErrorActionPreference = 'SilentlyContinue'
$key = 'C:\Users\<you>\.ssh\guardrails_vps'
$target = 'ubuntu@<vps-ip>'

while ($true) {
    $listener = Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue
    if (-not $listener) {
        & ssh -i $key -N -o BatchMode=yes -o ExitOnForwardFailure=yes `
            -o ServerAliveInterval=30 -o ServerAliveCountMax=3 `
            -L 8000:127.0.0.1:8000 $target
    }
    Start-Sleep -Seconds 5
}
```

Register it at logon (the agent and MT5 also need the user signed in):

```powershell
$action = New-ScheduledTaskAction -Execute 'powershell.exe' `
  -Argument '-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File "C:\GuardRails\start_tunnel.ps1"'
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries `
  -DontStopIfGoingOnBatteries -ExecutionTimeLimit 0 `
  -MultipleInstances IgnoreNew -StartWhenAvailable
Register-ScheduledTask -TaskName 'GuardRailsTunnel' -Action $action `
  -Trigger $trigger -Settings $settings -RunLevel Highest -Force
Start-ScheduledTask -TaskName 'GuardRailsTunnel'
```

Then point both clients at the local end of the tunnel:

- Windows agent `config.json`: `"backend_url": "http://127.0.0.1:8000"`
- MT5 `EA_Bridge` input `BackendURL`: `http://127.0.0.1:8000`
- MT5 **Tools → Options → Expert Advisors → Allow WebRequest for listed
  URL** must include `http://127.0.0.1:8000`.

Check it from Windows:

```powershell
Invoke-RestMethod http://127.0.0.1:8000/health
```

The tunnel is SSH-encrypted, survives reconnects, and does not expose port
8000 publicly. If the VPS is rebuilt, update the key path and host in the
script and the scheduled task.

---

## Running MT5 on the same Linux VPS (Wine, optional)

The backend, the cTrader connector and one MT5 terminal can share a single
Ubuntu VPS. MT5 runs headless under **Wine Staging 11.22** on an Xvfb display.
This is what replaces the Windows MT5 host after migration.

> Run only one MT5 terminal per account. Stop the Windows terminal and disable
> `GuardRailsAgent` / `GuardRailsTunnel` before starting the Linux one. Never
> let both connect with the same login.

### Sizing

- **2 vCPU / 2 GB RAM** is comfortable. A 1 GB VPS works with 4 GB of swap
  (`/swapfile` plus `/swapfile2`) and Docker disabled, but memory stays tight.
- Allow roughly 500 MB of disk for the portable MT5 tree.
- The Docker `mt5linux` image is **not** used: its Alpine Wine 9 build fails
  MT5's anti-debug check. Host Wine Staging 11.22 launches MT5 normally.

Add a second swap file on small instances:

```bash
sudo fallocate -l 2G /swapfile2
sudo chmod 600 /swapfile2
sudo mkswap /swapfile2
sudo swapon /swapfile2
echo '/swapfile2 none swap sw 0 0' | sudo tee -a /etc/fstab
```

### 1. Install the runtime

```bash
sudo apt update
sudo apt install -y xvfb xdotool imagemagick tesseract-ocr cabextract \
  libfreetype6 libgnutls30 libx11-6 libxext6 libxrender1
```

Download the **Wine Staging 11.22 amd64 tarball** from
[Kron4ek/Wine-Builds](https://github.com/Kron4ek/Wine-Builds/releases) and
extract it to `/opt/wine-staging`:

```bash
sudo mkdir -p /opt/wine-staging
sudo tar -xJf wine-11.22-staging-amd64.tar.xz \
  -C /opt/wine-staging --strip-components=1
/opt/wine-staging/bin/wine --version    # wine-11.22 (Staging)
```

The prefix used by the units is
`/opt/guardrails/mt5/wineprefix-staging`. Keep it owned by `guardrails`, or
`wineserver -k` fails with `is not owned by you` when run as root.

### 2. Install the units and helper scripts

From the repo root:

```bash
sudo install -m 644 guardrails-xvfb.service guardrails-mt5.service \
  guardrails-mt5-bridge.service /etc/systemd/system/
sudo install -m 755 tools/run-mt5.sh \
  /opt/guardrails/tools/run-mt5.sh
sudo install -m 755 tools/linux_mt5_bridge.py \
  /opt/guardrails/tools/linux_mt5_bridge.py
sudo systemctl daemon-reload
```

The units ship configured for login `61603648` at
`/opt/guardrails/mt5/61603648`:

- `guardrails-xvfb.service` - `Xvfb :1 -screen 0 1280x800x24`
- `guardrails-mt5.service` - Wine + `terminal64.exe /portable /config:...`
- `guardrails-mt5-bridge.service` - relays the EA file bridge to
  `http://127.0.0.1:8000`

For a different login, update `MT5_LOGIN` in `tools/run-mt5.sh`, the
`WorkingDirectory` and instance path in `guardrails-mt5.service`, and
`MT5_LOGIN` / `MT5_BRIDGE_DIR` / `MT5_PRESET` in
`guardrails-mt5-bridge.service`.

The terminal needs its portable tree (including `terminal64.exe`),
`MQL5/Experts/EA_Bridge.ex5`, and `MQL5/Presets/EA_Bridge.set`. The capture
watcher writes the preset; do not copy a preset containing another account's
token.

### 3. First login (one-time password form)

A fresh MT5 tree has no saved credentials. Queue a provisioning job with
`/provisionaccount` (or `/addaccount`). The default setup link lives for
`PROVISION_TOKEN_TTL` seconds (900). For the first Wine login, temporarily
raise it to one hour in `.env` and restart the backend:

```env
PROVISION_TOKEN_TTL=3600
```

Start the capture watcher **before** submitting the form. It watches for a new
job for up to an hour, and the lock file prevents a second copy:

```bash
sudo nohup /opt/guardrails/.venv/bin/python \
  /opt/guardrails/tools/linux_mt5_capture.py \
  >/var/log/guardrails-mt5-capture.out 2>&1 &
sudo tail -f /var/log/guardrails-mt5-capture.log
```

Open the setup link from Discord and submit the MT5 password. The watcher:

1. decrypts the pending job and writes the one-time password to `start.ini`;
2. starts Xvfb and MT5 under Wine;
3. waits up to 4 minutes for `authorized on <server>` and the EA's first
   `/report`;
4. scrubs the password from `start.ini`;
5. marks the account `running` / `linux-1` and clears the job.

On failure the encrypted job is left `pending` so a fixed retry can reuse it.
Do not clear it unless the password itself was wrong. If the job payload is
gone, queue a fresh setup link.

After the first successful capture, restart the backend once so its in-memory
account list picks up the new `instance_status`:

```bash
sudo systemctl restart guardrails
```

Then enable the units for boot:

```bash
sudo systemctl enable guardrails-xvfb guardrails-mt5 guardrails-mt5-bridge
```

Do not enable the capture watcher; it is a one-time bootstrap tool.

### 4. Passwordless restarts

Once MT5 has logged in, it stores the account in `Config/accounts.dat`.
Normal restarts need no password:

```bash
sudo systemctl restart guardrails-mt5
```

`ExecStop=/opt/wine-staging/bin/wineserver -k` runs as `guardrails`, so the
prefix shuts down cleanly. Keep `Config/accounts.dat`, `start.ini` and
`MQL5/Presets/EA_Bridge.set` at mode `600`, owned by `guardrails`.

If the login is lost (prefix or `accounts.dat` deleted), the account must be
provisioned again.

### 5. Why the file bridge exists

MT5's WebRequest allow-list is stored in an encrypted per-data-folder config.
On a fresh Wine prefix it cannot be populated reliably from `start.ini`, so the
EA falls back to its file-bridge transport:

- the EA writes URL-encoded request bodies into
  `MQL5/Files/guardrails/*.body`;
- `guardrails-mt5-bridge.service` posts them to the local backend;
- `/commands` replies are written back as `cmd_*.txt` files, which the EA
  consumes and deletes;
- the bridge token is read from `MQL5/Presets/EA_Bridge.set`.

Nothing extra is exposed publicly; the bridge talks only to
`http://127.0.0.1:8000`.

### 6. Health checks

```bash
systemctl status guardrails guardrails-ctrader \
  guardrails-xvfb guardrails-mt5 guardrails-mt5-bridge --no-pager
sudo journalctl -u guardrails-mt5 -u guardrails-mt5-bridge -n 40 --no-pager
```

A healthy terminal and EA log:

```bash
iconv -f UTF-16LE -t UTF-8 \
  /opt/guardrails/mt5/61603648/logs/$(date -u +%Y%m%d).log | tail -30
iconv -f UTF-16LE -t UTF-8 \
  /opt/guardrails/mt5/61603648/MQL5/logs/$(date -u +%Y%m%d).log | tail -30
```

Look for:

```text
'61603648': authorized on Pepperstone-Demo
EA_Bridge started. Backend: http://127.0.0.1:8000
```

Check the last report age:

```bash
sudo /opt/guardrails/.venv/bin/python - <<'PY'
import sqlite3, time
db = sqlite3.connect('/opt/guardrails/bot_data.db')
status, ts = db.execute(
    "SELECT instance_status, last_report_ts FROM accounts WHERE login='61603648'"
).fetchone()
print(status, round(time.time() - float(ts or 0), 1), 'sec since report')
PY
```

`running` with an age under ~15 seconds is healthy. cTrader accounts report
`instance_status = external` because their connector runs separately.

Check that the EA is consuming commands (the backlog should stay small, not
grow):

```bash
sudo ls /opt/guardrails/mt5/61603648/MQL5/Files/guardrails/cmd_*.txt \
  2>/dev/null | wc -l
sudo cat /opt/guardrails/mt5/61603648/MQL5/Files/guardrails/cmd_*.txt \
  2>/dev/null | head -8
```

A healthy reply contains lines such as:

```text
SYMBOLS EURUSD,GBPUSD,NAS100
RISK 0.5
MAXPOS 2
RRR 3.25
```

If the files accumulate, the EA is not polling; check the EA log and confirm
`EA_Bridge` is attached to a chart.

Verify the password was scrubbed:

```bash
sudo /opt/guardrails/.venv/bin/python - <<'PY'
from pathlib import Path
text = Path('/opt/guardrails/mt5/61603648/start.ini').read_bytes().decode('utf-16')
print('password_present=', any(
    line.strip().lower().startswith('password=')
    for line in text.splitlines()
))
PY
```

### 7. Windows retirement

After the Linux terminal has reported for a full session:

1. stop the Windows MT5 terminal;
2. disable the `GuardRailsAgent` and `GuardRailsTunnel` scheduled tasks;
3. archive the Windows instance directory;
4. remove the retired account from the backend only after Linux is stable.

Do not close or modify live trades as part of the migration.
