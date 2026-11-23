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

### 7.2 Install the units, helpers, and pristine template

From the repo root:

```bash
sudo install -m 644 guardrails-xvfb.service guardrails-mt5@.service \
  guardrails-mt5-bridge@.service guardrails-mt5-provision.service \
  /etc/systemd/system/
sudo install -m 755 tools/run-mt5.sh tools/linux_mt5_bridge.py \
  tools/linux_mt5_provision.py tools/create_mt5_template.py \
  /opt/guardrails/tools/
sudo install -m 644 mt5_provisioning.py /opt/guardrails/mt5_provisioning.py
sudo systemctl daemon-reload
```

The templated units take the MT5 login as the instance name, so there is
nothing to edit per account:

- `guardrails-xvfb.service` - one shared `Xvfb :1 -screen 0 1280x800x24`.
- `guardrails-mt5@<login>.service` - Wine + that login's
  `terminal64.exe /portable /config:...`.
- `guardrails-mt5-bridge@<login>.service` - relays that login's EA file bridge
  to `http://127.0.0.1:8000`.
- `guardrails-mt5-provision.service` - root watcher that consumes encrypted
  setup jobs, clones the template, starts the units, verifies the first report,
  and scrubs the one-time password.

`guardrails-mt5@.service` deliberately has **no** `ExecStop=wineserver -k`.
Every terminal shares the same Wine prefix, so that command would kill every
other account's terminal too. systemd's per-unit control-group kill is enough.

Build the pristine template once, with the source terminal stopped:

```bash
# Stop the old single-account units if they are still installed.
sudo systemctl stop guardrails-mt5.service guardrails-mt5-bridge.service \
  2>/dev/null || true

# Copy the known-good tree, then strip credentials, logs, presets and caches.
sudo /opt/guardrails/.venv/bin/python \
  /opt/guardrails/tools/create_mt5_template.py --source 61603648
```

The template lives at `/opt/guardrails/mt5/_template` and must contain
`terminal64.exe`, `MQL5/Experts/EA_Bridge.ex5`, and a minimal `start.ini`. It
must not contain `Config/accounts.dat`, logs, or `EA_Bridge.set`. The script
removes those, resets `start.ini`, and normalizes ownership to `guardrails`.

Migrate the existing `61603648` tree to the templated units (its own
`start.ini` and preset are left untouched):

```bash
sudo systemctl disable --now guardrails-mt5.service \
  guardrails-mt5-bridge.service 2>/dev/null || true
sudo systemctl enable --now guardrails-mt5@61603648.service \
  guardrails-mt5-bridge@61603648.service
```

The backend generates a unique bridge token for each account and stores only
its SHA-256 in `accounts.bridge_token_hash`. The watcher writes the plaintext
token into that account's `MQL5/Presets/EA_Bridge.set`, which stays mode `600`.

### 7.3 One-time onboarding (no password in Discord)

The normal path is entirely self-service:

1. In Discord, run:

   ```text
   /addaccount account:<login> phase:<challenge|funded> server:<exact-server> [suffix:0]
   ```

2. The bot DMs a single-use HTTPS link. Open it within
   `PROVISION_TOKEN_TTL` (15 minutes by default) and submit the **trading**
   password and exact broker server name. If the broker's server is missing
   from the shared catalog, also paste the broker's official MetaQuotes `.exe`
   download link (host `download.mql5.com`). The password never appears in
   Discord, SQLite plaintext, or the access log.

3. The root watcher (`guardrails-mt5-provision.service`) claims the encrypted
   job. If the broker installer was supplied, it runs that installer once
   under Wine and caches the branded `terminal64.exe`, `servers.dat`, and
   licence in `/opt/guardrails/mt5/_brokers/<broker>/`. Known packs are also
   matched by server prefix, so later accounts at the same broker need no
   link. It then clones `_template` to `/opt/guardrails/mt5/<login>`, overlays
   the broker pack when present, writes a one-time `Password=` line and a
   fresh per-account `AccountToken`, then starts:

   ```bash
   systemctl start guardrails-mt5@<login>.service
   systemctl start guardrails-mt5-bridge@<login>.service
   ```

4. It waits for the first `/report` (proof of a live authorized session; the
   terminal log is buffered, so the auth line can appear late), then removes
   the `Password=` line from `start.ini`, restarts the terminal, and verifies
   that the saved login in `Config/accounts.dat` can reconnect without the
   password.

5. The setup page polls the job status. On success the account shows
   `running`; on failure the error is shown and a Discord notification is
   queued. Re-run `/addaccount` with the same login to retry from a clean
   clone.

If MT5 rejects the login, check the exact server name, that you used the
**trading** password (not the investor password), and whether the account
requires 2FA. Accounts with 2FA cannot be automated. If the broker's server is
not in the shared catalog, re-run `/addaccount` and paste the broker's official
MetaQuotes `.exe` download link (host `download.mql5.com`) on the setup page;
the watcher caches that broker's branded terminal and server list for every
later account. Do not point the field at a file host or password manager.

For recovery only, the watcher can be stopped and the same steps performed by
hand. Keep `start.ini`, `Config/accounts.dat`, and `EA_Bridge.set` mode `600`,
owned by `guardrails`.

Enable the shared units for boot (per-account units are enabled by the
watcher):

```bash
sudo systemctl enable guardrails-xvfb guardrails-mt5-provision
```

### 7.4 Passwordless restarts

Once MT5 has logged in, it stores the account in `Config/accounts.dat`.
Normal restarts need no password and affect only one login:

```bash
sudo systemctl restart guardrails-mt5@<login>.service
sudo systemctl restart guardrails-mt5-bridge@<login>.service
```

Do **not** run `wineserver -k` while other accounts are running; all terminals
share `/opt/guardrails/mt5/wineprefix-staging`. Keep `Config/accounts.dat`,
`start.ini`, and `MQL5/Presets/EA_Bridge.set` at mode `600`, owned by
`guardrails`.

If the login is lost (prefix or `accounts.dat` deleted), run `/addaccount`
again for that login. The watcher replaces the tree from `_template` and
repeats the one-time login.

### 7.5 Why the file bridge exists

MT5's WebRequest allow-list is stored in an encrypted per-data-folder config.
On a fresh Wine prefix it cannot be populated reliably from `start.ini`, so the
EA falls back to its file-bridge transport:

- the EA writes URL-encoded request bodies into
  `MQL5/Files/guardrails/*.body`;
- `guardrails-mt5-bridge@<login>.service` posts them to the local backend;
- `/commands` replies are written back as `cmd_*.txt` files, which the EA
  consumes and deletes;
- the bridge token is read from `MQL5/Presets/EA_Bridge.set`.

Nothing extra is exposed publicly; the bridge talks only to
`http://127.0.0.1:8000`.

### 7.6 Health checks

```bash
systemctl status guardrails guardrails-ctrader guardrails-xvfb \
  guardrails-mt5-provision guardrails-mt5@61603648 \
  guardrails-mt5-bridge@61603648 --no-pager
sudo journalctl -u guardrails-mt5@61603648 \
  -u guardrails-mt5-bridge@61603648 -n 40 --no-pager
```

A healthy terminal and EA log (`<login>` is the account you are checking):

```bash
iconv -f UTF-16LE -t UTF-8 \
  /opt/guardrails/mt5/<login>/logs/$(date -u +%Y%m%d).log | tail -30
iconv -f UTF-16LE -t UTF-8 \
  /opt/guardrails/mt5/<login>/MQL5/logs/$(date -u +%Y%m%d).log | tail -30
```

Look for:

```text
'<login>': authorized on <broker-server>
EA_Bridge started. Backend: http://127.0.0.1:8000
```

Check the last report age:

```bash
sudo /opt/guardrails/.venv/bin/python - <<'PY'
import sqlite3, time
db = sqlite3.connect('/opt/guardrails/bot_data.db')
status, ts = db.execute(
    "SELECT instance_status, last_report_ts FROM accounts WHERE login='<login>'"
).fetchone()
print(status, round(time.time() - float(ts or 0), 1), 'sec since report')
PY
```

`running` with an age under ~15 seconds is healthy. cTrader accounts report
`instance_status = external` because their connector runs separately.

Check that the EA is consuming commands (the backlog should stay small, not
grow):

```bash
sudo ls /opt/guardrails/mt5/<login>/MQL5/Files/guardrails/cmd_*.txt \
  2>/dev/null | wc -l
sudo cat /opt/guardrails/mt5/<login>/MQL5/Files/guardrails/cmd_*.txt \
  2>/dev/null | head -8
```

A healthy reply contains lines such as:

```text
SYMBOLS EURUSD,GBPUSD,NAS100
RISK 0.5
MAXPOS 2
RRR 3.25
```

The backend sends `RISK 0.5` for a challenge account and `RISK 0.25` for a
funded account. Client-supplied risk is ignored by `/api/mobile/trade`.

If the files accumulate, the EA is not polling; check the EA log and confirm
`EA_Bridge` is attached to a chart.

Verify the password was scrubbed:

```bash
sudo /opt/guardrails/.venv/bin/python - <<'PY'
from pathlib import Path
text = Path('/opt/guardrails/mt5/<login>/start.ini').read_bytes().decode('utf-16')
print('password_present=', any(
    line.strip().lower().startswith('password=')
    for line in text.splitlines()
))
PY
```

### 7.7 Recovery and password changes

- **MT5 password changed:** run `/addaccount` again for that login. The watcher
  replaces the tree and repeats the one-time login, then confirms `start.ini`
  has no `Password=` line.
- **Terminal crashes at startup:** check
  `journalctl -u guardrails-mt5@<login>` and the MT5 log under
  `mt5/<login>/logs/`. Remove stale `/tmp/.X1-lock` and `/tmp/.X11-unix/X1`
  only while the Xvfb unit is stopped.
- **Wine prefix rebuilt:** the account must log in again; `accounts.dat` is
  inside the terminal tree, not the prefix.
- **Account removed from Discord:** `/removeaccount` queues an `mt5_remove`
  job. The watcher stops and disables that login's units and keeps the tree on
  disk for recovery. Open positions remain on the broker.
- **Provisioner log:** `sudo journalctl -u guardrails-mt5-provision -n 80`
  and `/var/log/guardrails-mt5-provision.log` (mode `600`). Passwords and
  bridge tokens are never logged.

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

## 9. PWA and APK updates

The backend serves the built PWA from `mobile/dist`, and the same directory also
hosts `guardrails-android.apk` for direct download. `mobile/dist` is not tracked
by git, so replacing it drops the APK. Preserve it when publishing a new build:

```bash
sudo mv /opt/guardrails/mobile/dist /opt/guardrails/mobile/dist.bak.$(date +%s)
sudo cp -a /path/to/new/dist /opt/guardrails/mobile/dist
sudo install -o guardrails -g guardrails -m 644 \
  /path/to/app-debug.apk /opt/guardrails/mobile/dist/guardrails-android.apk
sudo chown -R guardrails:guardrails /opt/guardrails/mobile/dist
```

The GitHub Actions `Build Android APK` workflow runs on every `mobile/**` push
and uploads `guardrails-mobile-apk` as an artifact. Download the artifact,
rename `app-debug.apk` to `guardrails-android.apk`, and install it as above.
Then verify:

```bash
curl -fsS http://127.0.0.1:8000/health
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8000/guardrails-android.apk
```


