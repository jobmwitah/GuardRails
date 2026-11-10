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
```

### Step 4: Install and start the systemd service
```bash
sudo cp guardrails.service /etc/systemd/system/guardrails.service
sudo systemctl daemon-reload
sudo systemctl enable --now guardrails
sudo systemctl status guardrails
```

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
