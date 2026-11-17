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
