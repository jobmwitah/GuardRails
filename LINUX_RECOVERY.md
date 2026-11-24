# Recovering inbound access to the Linux VPS

Use this when the GuardRails app still works through ngrok but direct SSH (and
other inbound ports) time out.

## Symptom

- `https://<ngrok-domain>/health` returns `{"status":"ok"}`.
- MT5 and cTrader accounts keep reporting fresh `last_report_ts` values.
- Direct TCP to the VPS public IP times out on port 22 and other ports from
  multiple external networks.
- The ngrok response includes `Ngrok-Agent-Ips: <vps-public-ip>`, proving the
  public IP is still associated with the instance.

That combination means the instance and all GuardRails services are healthy;
only the **inbound** path is broken. Do not stop or remove accounts while
diagnosing it.

## 1. Check the Oracle Cloud ingress rules

Oracle Cloud Console → **Compute → Instances → &lt;instance&gt; → Networking**:

1. Confirm the instance still has the expected public IP.
2. Open the subnet's **Security lists** and check the ingress rules for
   **TCP 22**. It must allow your current admin IP, or temporarily
   `0.0.0.0/0`, then be narrowed again.
3. Check any **Network security groups** attached to the VNIC for the same
   rule.
4. If the instance uses a **reserved public IP**, do not release or reassign
   it. If it uses an ephemeral IP, prefer **Reboot** over **Stop/Start** so the
   address is not changed.

## 2. Check the host firewall over the serial console

Oracle Cloud Console → **Instance → Console connections** → create a serial
console connection, then log in as `ubuntu`.

```bash
sudo ss -ltnp | grep ':22'
sudo systemctl status ssh --no-pager
sudo ufw status verbose
sudo iptables -S INPUT
sudo ip -brief addr
```

Expected:

- `sshd` is listening on `0.0.0.0:22` or `:::22`.
- UFW is inactive, or active with `22/tcp ALLOW`.
- The INPUT policy is `ACCEPT`, or there is an explicit SSH accept rule.

Fix a host-firewall problem:

```bash
sudo systemctl enable --now ssh
sudo ufw allow 22/tcp
sudo ufw reload

# Only if UFW is inactive and the INPUT policy is DROP:
sudo iptables -I INPUT -p tcp --dport 22 -j ACCEPT
sudo netfilter-persistent save 2>/dev/null || true
```

## 3. If sshd and the host firewall are correct

Then the cloud public-IP mapping is broken. Reboot the instance from the
Oracle console (**Reboot**, not Stop/Start). All GuardRails units are enabled
and start automatically:

- `guardrails`
- `guardrails-ctrader`
- `guardrails-xvfb`
- `guardrails-mt5-provision`
- `guardrails-mt5@<login>`
- `guardrails-mt5-bridge@<login>`
- `guardrails-ngrok`

After it comes back:

```bash
systemctl is-active guardrails guardrails-ctrader guardrails-xvfb \
  guardrails-mt5-provision guardrails-mt5@61603648 \
  guardrails-mt5-bridge@61603648
sudo journalctl -u guardrails-mt5@61603648 \
  -u guardrails-mt5-bridge@61603648 -n 40 --no-pager
```

Check the MT5 report age:

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

From outside, `nc -vz <vps-public-ip> 22` should succeed.

## 4. Make management access durable

- Attach a **reserved public IP** so a reboot cannot change the address.
- Keep a second management path: Oracle Cloud Agent **Run Command** or a
  Tailscale/WireGuard interface. SSH should not be the only way in.
- Persist UFW rules and confirm UFW is enabled at boot
  (`/etc/ufw/user.rules`, `ufw enable`).
- Do not route SSH through Cloudflare WARP. WARP can accept the TCP connection
  locally and then fail during the SSH banner exchange, which looks like a VPS
  outage.
- After any reboot, test inbound SSH from outside before assuming the terminal
  is reachable.

## Current state at the time of this incident

- Public IP: unchanged (confirmed by the `Ngrok-Agent-Ips` response header).
- App: healthy through ngrok; `/health` returns 200.
- MT5 `61603648`: `running`, reports fresh, no positions.
- cTrader demo accounts: both reporting, no positions.
- Inbound TCP: times out from multiple external networks.
- No trades were open and no account data was changed during diagnosis.
