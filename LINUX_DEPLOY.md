# GuardRails on a Linux VPS (Amazon Linux 2023)

This is the production layout: the Discord/FastAPI backend, the cTrader Open API
connector, and one MT5 terminal under Wine all run on a single Linux VPS. No
Windows host is involved.

The examples use Amazon Linux 2023 and `ec2-user` with passwordless sudo. On
Ubuntu, substitute `apt` for `dnf` and `ubuntu` for `ec2-user`; the paths and
systemd units are the same.

---

## 1. Base host

- 2 vCPU / 2 GB RAM is comfortable. 1 GB works with 4 GB of swap and Docker
  disabled, but memory stays tight.
- Open only SSH (22) in the AWS Security Group. The backend binds to
  `127.0.0.1:8000`; public HTTPS is provided by ngrok.
- Allow roughly 8–10 GB of disk for the OS, Wine, the MT5 tree, and the venv.

```bash
sudo dnf update -y
sudo dnf install -y git python3 python3-pip python3-devel gcc gcc-c++ make   xorg-x11-server-Xvfb xorg-x11-xauth tar xz curl   freetype gnutls libX11 libXext libXrender
```

## 2. Service user and deployment

```bash
sudo useradd -r -m -d /opt/guardrails -s /bin/bash guardrails
sudo mkdir -p /opt/guardrails
sudo chown -R guardrails:guardrails /opt/guardrails

sudo -u guardrails git clone https://github.com/jobmwitah/GuardRails.git /opt/guardrails
cd /opt/guardrails
sudo -u guardrails python3 -m venv .venv
sudo -u guardrails .venv/bin/pip install --upgrade pip
sudo -u guardrails .venv/bin/pip install -r requirements.txt
```

Deploy updates with `git pull --ff-only` as the `guardrails` user; keep runtime
state (`bot_data.db`, `.env`, `mt5/`, `.config/`) out of git.

## 3. Environment file

Create `/opt/guardrails/.env`, owned by `guardrails`, mode `600`:

```env
BRIDGE_TOKEN=<long-random-string>
DISCORD_TOKEN=<bot-token>
OWNER_DISCORD_ID=<your-discord-user-id>
ALERT_CHANNEL_ID=<alert-channel-id>
GUILD_ID=<guild-id>

PUBLIC_BASE_URL=https://<your-public-host>
CREDENTIAL_KEY=<generated-fernet-key>

CTRADER_CLIENT_ID=<open-api-client-id>
CTRADER_CLIENT_SECRET=<open-api-client-secret>
CTRADER_CONNECTOR_TOKEN=<long-random-connector-secret>
CTRADER_REDIRECT_URI=https://<your-public-host>/ctrader/callback

MOBILE_API_TOKEN=<long-random-mobile-secret>
MOBILE_READ_ONLY_ACCOUNTS=1515282832
```

Generate the Fernet key once:

```bash
/opt/guardrails/.venv/bin/python -c   "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

`BRIDGE_TOKEN` accepts a comma-separated list during rotation. Put the new token
first, update the EA preset, confirm `/report` returns 200, then remove the old
value and restart.

## 4. Backend service

```bash
sudo cp guardrails.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now guardrails
sudo systemctl status guardrails --no-pager
curl -fsS http://127.0.0.1:8000/health
```

Slash commands re-sync when the service restarts. If they do not appear, set
`GUILD_ID` and restart; guild sync is immediate.

## 5. Public HTTPS with ngrok

Install the official ngrok binary (the VPS uses `3.39.13` at
`/usr/local/bin/ngrok`), then configure it as the `guardrails` user:

```bash
sudo -u guardrails ngrok config add-authtoken <NGROK_AUTHTOKEN>
```

The systemd unit uses the reserved domain and starts at boot:

```ini
[Unit]
Description=GuardRails public HTTPS tunnel
After=network.target guardrails.service
Wants=guardrails.service

[Service]
User=guardrails
ExecStart=/usr/local/bin/ngrok http 8000 --domain=<your-reserved-domain> --log=stdout
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now guardrails-ngrok
sudo journalctl -u guardrails-ngrok -n 30 --no-pager
```

Verify from outside the VPS:

```bash
curl -sS -o /dev/null -w '%{http_code}\n' https://<your-public-host>/health
```

Set `PUBLIC_BASE_URL` to that exact HTTPS origin. Free ngrok shows a one-time
browser warning; API clients should send `ngrok-skip-browser-warning: true`.

## 6. cTrader connector

Copy the unit and enable it after the backend is healthy:

```bash
sudo cp guardrails-ctrader.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now guardrails-ctrader
sudo journalctl -u guardrails-ctrader -f
```

Register the callback URL in the cTrader Open API portal exactly as it appears
in `CTRADER_REDIRECT_URI`. The connector polls the backend for grants, discovers
demo accounts, and refuses live accounts by design. OAuth tokens are encrypted
in the backend with `CREDENTIAL_KEY`.

### Broker-side history

The connector also imports closed broker deals into the PWA history so manual
trades (and labeled GuardRails closes from before a migration) remain visible.
These rows live in the separate `broker_trades` table and are **display-only**:
they never change the daily loss count, block state, position limits, or any
other risk rule. GuardRails' own closes still arrive through `TRADE_CLOSED`, and
the history API hides the broker duplicate by cTrader position id.

The connector syncs 90 days on startup and the last 2 days every 120 seconds.
Nothing needs to be imported by hand.

---

## 7. MT5 under Wine

MT5 runs headless under **Wine Staging 11.22** on an Xvfb display. The backend
and the file-bridge relay run as separate systemd units on the same host.

> Run only one MT5 terminal per account. Never start a second terminal for the
> same login, and never point two backends at the same terminal.

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

### 7.1 Install the runtime

```bash
sudo dnf install -y xorg-x11-server-Xvfb xdotool ImageMagick tesseract \
  cabextract freetype gnutls libX11 libXext libXrender
```

Wine Staging 11.22 is built from the Wine 11.22 source plus the matching
staging patches and installed at `/opt/wine-staging`. Verify:

```bash
/opt/wine-staging/bin/wine --version    # wine-11.22 (Staging)
```

The prefix used by the units is
`/opt/guardrails/mt5/wineprefix-staging`. Keep it owned by `guardrails`, or
`wineserver -k` fails with `is not owned by you` when run as root.

### 7.2 Install the units and helper scripts

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
`MQL5/Experts/EA_Bridge.ex5`, and `MQL5/Presets/EA_Bridge.set`. Set
`AccountToken` in the preset to a value the backend accepts: either the global
`BRIDGE_TOKEN` or a per-account token whose SHA-256 is stored in the account's
`bridge_token_hash`.

### 7.3 First login

A fresh MT5 tree has no saved credentials. Stop the terminal, add the trading
password to `start.ini`, start it once, then scrub the password:

```bash
sudo systemctl stop guardrails-mt5 guardrails-mt5-bridge
sudo -u guardrails nano /opt/guardrails/mt5/61603648/start.ini
```

`start.ini` is UTF-16LE. Add one line under `[Common]`, next to `Login=`:

```ini
Password=<MT5 trading password>
```

Keep the file mode `600` and owned by `guardrails`. Start the display and
terminal:

```bash
sudo systemctl start guardrails-xvfb
sleep 2
sudo systemctl start guardrails-mt5
sudo journalctl -u guardrails-mt5 -f
```

Wait for both of these in the terminal log:

```text
'61603648': authorized on <broker-server>
EA_Bridge started. Backend: http://127.0.0.1:8000
```

Then remove the `Password=` line from `start.ini` and restart the terminal to
prove it can log in from `Config/accounts.dat` alone:

```bash
sudo systemctl restart guardrails-mt5
sudo systemctl start guardrails-mt5-bridge
```

If MT5 rejects the login, check the server name, that you used the **trading**
password (not the investor password), and whether the account requires 2FA.
Accounts with 2FA cannot be automated.

Enable the units for boot:

```bash
sudo systemctl enable guardrails-xvfb guardrails-mt5 guardrails-mt5-bridge
```

### 7.4 Passwordless restarts

Once MT5 has logged in, it stores the account in `Config/accounts.dat`.
Normal restarts need no password:

```bash
sudo systemctl restart guardrails-mt5
```

`ExecStop=/opt/wine-staging/bin/wineserver -k` runs as `guardrails`, so the
prefix shuts down cleanly. Keep `Config/accounts.dat`, `start.ini` and
`MQL5/Presets/EA_Bridge.set` at mode `600`, owned by `guardrails`.

If the login is lost (prefix or `accounts.dat` deleted), repeat the one-time
login above.

### 7.5 Why the file bridge exists

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

### 7.6 Health checks

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

### 7.7 Recovery and password changes

- **MT5 password changed:** repeat the one-time login in 7.3, then scroll back
  and confirm `start.ini` has no `Password=` line.
- **Terminal crashes at startup:** check `journalctl -u guardrails-mt5` and the
  MT5 log under `mt5/<login>/logs/`. Remove stale `/tmp/.X1-lock` and
  `/tmp/.X11-unix/X1` only while the Xvfb unit is stopped.
- **Wine prefix rebuilt:** the account must log in again; `accounts.dat` is
  inside the terminal tree, not the prefix.
- **Account removed from Discord:** `/removeaccount` deletes the backend record
  only. Stop the systemd unit separately if the terminal should no longer run.

Never close or modify live trades as part of a maintenance or migration step.

## 8. Secret rotation

Rotate any credential that was ever pasted into chat, a ticket, or a log. Do the
portal steps yourself; never paste the new value into Discord.

### ngrok authtoken

1. In the ngrok dashboard, create a new authtoken and disable the old one.
2. On the VPS, replace the token without printing it:

   ```bash
   sudo -u guardrails /usr/local/bin/ngrok config add-authtoken '<new-token>'
   sudo systemctl restart guardrails-ngrok
   sudo journalctl -u guardrails-ngrok -n 20 --no-pager
   ```

3. Confirm `https://<your-public-host>/health` still returns `200`.

### cTrader client secret

1. Rotate the client secret in the cTrader Open API portal.
2. Edit `/opt/guardrails/.env` as the `guardrails` user and replace only
   `CTRADER_CLIENT_SECRET`. Keep the file mode `600`.
3. Restart the backend and connector:

   ```bash
   sudo systemctl restart guardrails guardrails-ctrader
   curl -fsS http://127.0.0.1:8000/health
   sudo journalctl -u guardrails-ctrader -n 30 --no-pager
   ```

Existing OAuth grants normally survive a secret rotation because the refresh
token is independent of the client secret. If cTrader revokes the grant, run
`/addctrader` again and authorize the demo cTID.

### Bridge and mobile tokens

`BRIDGE_TOKEN` accepts a comma-separated list: put the new token first, update
the EA preset, confirm `/report` returns `200`, then remove the old value and
restart. Rotating `MOBILE_API_TOKEN` signs every PWA/APK session out; enter the
new token in the app afterward.

