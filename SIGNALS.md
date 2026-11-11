# Inbound signal webhooks

GuardRails can trade directly from an alert on OpenMarket, TradingView
(plans with webhooks), or any script that can POST JSON. The Discord
commands still work; both paths feed the same command queue and the same
hardcoded guardrails.

```
Alerting platform ──HTTPS──> POST /signal ──> guardrails ──> EA command queue
                                                    │
                                                    └──> Discord alert
```

This is deliberately **not** a second trading engine: the backend extracts
side, symbol and stop-loss, then validates the signal exactly like a
`/marketbuy` or `/marketsell` command. The EA fetches the live entry price
at the moment it executes and calculates TP at 3.25R.

---

## Endpoint

```http
POST /signal?token=<SIGNAL_TOKEN>&account=<MT5 login>
Content-Type: application/json

{
  "symbol": "EURUSD",
  "side": "buy",
  "sl": 1.0800,
  "message": "BUY EURUSD SL=1.0800"
}
```

A minimal plain-text body also works:

```http
POST /signal?token=<SIGNAL_TOKEN>&account=1345657215
Content-Type: text/plain

BUY EURUSD SL=1.0800
```

### Authentication

`SIGNAL_TOKEN` is used when set; otherwise the endpoint accepts
`BRIDGE_TOKEN`. The token may be supplied in any of these places, which
covers platforms that only let you edit a URL, a JSON body, or a message:

| Where | Example |
|---|---|
| Query string | `?token=...` (also `signal_token`, `key`, `secret`) |
| Header | `X-Signal-Token: ...` |
| Bearer header | `Authorization: Bearer ...` |
| JSON / form field | `"signal_token": "..."`, `"token": "..."`, `"secret": "..."` |
| Alert message | `BUY EURUSD SL=1.0800 TOKEN=...` |

Use a long random value, different from `BRIDGE_TOKEN`. If the token is in
the URL, remember that ngrok and any reverse proxy may keep request logs;
rotate it if that URL leaks.

### Account selection

Priority order:

1. `?account=` query parameter
2. `account` (or `login`, `account_id`, ...) in the payload
3. the Discord default account (`/setdefault`)

The account must already be registered with `/addaccount`. An unknown
account is rejected — it is never auto-created from a webhook.

### Required fields

| Field | Accepted forms |
|---|---|
| side | `buy`, `long`, `bullish`, `bid`, `above`, `crossing_up`, `cross_up` → BUY; `sell`, `short`, `bearish`, `ask`, `below`, `crossing_down`, `cross_down` → SELL |
| symbol | `EURUSD`, `EUR/USD`, `EUR_USD`, `FX_OTC:EURUSD`, `EURUSD.m`, `XAU/USD`, `US30` |
| sl | any positive number; `1.0800`, `"1,0800"`, `SL: 1.0800` |

A side is mandatory. A price alert that only says "EUR/USD crossed
1.0850" has no direction and no stop-loss, so it is rejected. Put the full
instruction in the alert message or JSON.

The symbol must be enabled on that account with `/addsymbol`. Symbols are
bare broker roots; the EA appends its own `SymbolSuffix`.

---

## Payload tolerance

The parser flattens nested objects and lists, expands JSON strings, and
then checks a set of aliases. Unknown extra fields are ignored.

| Logical field | Aliases recognised |
|---|---|
| account | `account`, `account_id`, `account_number`, `login`, `login_id`, `mt5_account`, `mt5_login`, `trading_account` |
| symbol | `symbol`, `symbol_name`, `ticker`, `ticker_symbol`, `pair`, `currency_pair`, `instrument`, `trading_symbol`, `security`, `asset`; looser: `market`, `market_name`, `epic` |
| side | `side`, `direction`, `action`, `order_type`, `position`, `trend`, `bias`, `condition`, `alert_condition`, `crossing`, `operator`, `comparison` |
| SL | `sl`, `stop_loss`, `stop`, `stop_price`, `stop_rate`, `stop_loss_price`, `sl_price`, `stop_level`, `stop_value`, `loss` |
| message | `message`, `text`, `alert_message`, `alert`, `body`, `content`, `description`, `note`, `notification`, `msg`, `subject`, `title` |
| platform | `platform`, `source`, `venue`, `exchange`, `provider`, `service`, `origin` |

Examples that parse correctly:

```json
{"symbol": "EUR/USD", "side": "buy", "sl": 1.08,
 "message": "EUR/USD crossed above 1.0850"}
```

```json
{"ticker": "GBP/USD", "action": "sell", "stop_loss": "1.2700"}
```

```json
{"data": {"instrument": {"symbol": "XAU/USD"},
          "condition": "crossing_down", "price": 2400.5},
 "text": "XAU/USD crossed below 2400.5, SELL SL=2405"}
```

```json
{"payload": "{\"symbol\":\"EURUSD\",\"side\":\"sell\",\"sl\":1.09}"}
```

If the structured fields and the message disagree (for example JSON says
`buy` while the message says `SELL`), the signal is rejected rather than
guessing.

---

## Guardrails applied

Every signal goes through the same checks as a Discord trade:

1. Account registered (`/addaccount`).
2. Symbol enabled for that account (`/addsymbol`) and valid broker root.
3. Not blocked after `MAX_LOSSES_PER_DAY` losses.
4. Fewer than `MAX_OPEN_POSITIONS` (2) open positions.
5. Market not reported closed (live ask/bid not zero).
6. SL on the correct side of the latest quote: BUY SL below ask, SELL SL
   above bid. If no quote has arrived yet, this check fails open and the
   EA/broker gets the final word.
7. Duplicate window (`SIGNAL_DEDUPE_SECONDS`, default 300s). Add
   `&force=1` to intentionally override it.

Accepted signals queue:

```
OPEN_MARKET <BUY|SELL> <symbol> <sl> <risk_pct> 3.25
```

So position size, TP at 3.25R, the EA's local checks, and the daily loss
cap all behave identically to a manual command.

---

## Responses

| HTTP | `status` | Meaning |
|---|---|---|
| 200 | `queued` | Passed guardrails and is in the EA queue |
| 200 | `duplicate` | Same signal already claimed within the dedupe window; intentionally treated as success so the platform stops retrying |
| 200 | `dry_run` | Parsed only; nothing claimed or queued |
| 400 | `rejected` | No account selected and no default |
| 401 | — | Missing/wrong token |
| 404 | `rejected` | Account not registered |
| 409 | `rejected` | Blocked, position limit, market closed, or SL on the wrong side |
| 422 | `rejected` | Missing/conflicting fields, invalid symbol, or symbol not enabled |
| 413 | — | Body larger than 64 KB |

Rejections are also posted to the Discord alert channel, including the
reason. Unauthorized requests are **not** posted (to avoid token-probing
spam), but they are printed to the backend log.

---

## Dry-run and raw-payload capture

Use dry-run while configuring a new platform. It authenticates and parses,
prints the redacted payload, returns the extracted fields, and does not
claim, queue, or trade:

```bash
curl -sS -X POST \
  "https://<host>/signal?token=$SIGNAL_TOKEN&account=1345657215&dry_run=1" \
  -H "Content-Type: application/json" \
  -d '{"symbol":"EURUSD","side":"buy","sl":1.08,"message":"BUY EURUSD SL=1.08"}'
```

Response:

```json
{
  "ok": true,
  "status": "dry_run",
  "account": "1345657215",
  "symbol": "EURUSD",
  "side": "BUY",
  "sl": 1.08,
  "entry": null,
  "message": "BUY EURUSD SL=1.08",
  "missing": [],
  "conflicts": []
}
```

Every real (non-dry-run) signal stores a redacted raw payload for 30 days.
List recent captures:

```bash
curl -sS "https://<host>/signals?token=$SIGNAL_TOKEN&limit=10"
```

Add `&include_raw=1` to see the payloads. On the VPS directly:

```bash
sqlite3 /opt/guardrails/bot_data.db \
  "select datetime(created_at,'unixepoch'), status, login, symbol, side, sl, raw
   from signal_events order by created_at desc limit 5;"
```

This is specifically for mapping an undocumented vendor schema: send one
real alert, inspect `raw`, and adjust `signals.py` aliases if needed.

---

## OpenMarket setup (free plan)

OpenMarket's free plan includes price alerts and generic HTTPS webhook
delivery; its exact JSON schema is not publicly documented. GuardRails
therefore captures the raw payload and parses tolerantly, but the alert
message is the reliable part you control.

1. **Expose the backend over HTTPS.** Oracle ingress for port 8000 was not
   reachable from Windows in this setup, so use the ngrok tunnel (or a
   reverse proxy/domain). The public base URL is the ngrok HTTPS URL.
2. **Create a webhook destination** in OpenMarket with:

   ```
   https://<ngrok-host>/signal?token=<SIGNAL_TOKEN>&account=1345657215
   ```

3. **Configure the message preset.** If OpenMarket lets you edit a JSON
   body with placeholders, use:

   ```json
   {
     "account": "1345657215",
     "symbol": "{{symbol}}",
     "side": "{{side}}",
     "sl": "{{sl}}",
     "message": "{{message}}"
   }
   ```

   Placeholder names vary and are not guaranteed. The safest fallback is a
   plain message preset containing the complete instruction, because the
   parser understands this without any vendor fields:

   ```
   BUY EURUSD SL=1.0800
   ```

4. **Create the price alert.** Use the `FX_OTC` forex feed (`EUR/USD`,
   `GBP/USD`, `XAU/USD`, ...), choose the level and crossing direction,
   and attach the webhook destination plus the message preset. Price alerts
   are level-crossing only. OpenMarket cannot calculate a dynamic SL for
   you, so each alert carries a fixed SL in its message:

   | Alert | Message preset |
   |---|---|
   | EUR/USD crosses above 1.0850 | `BUY EURUSD SL=1.0800` |
   | GBP/USD crosses below 1.2700 | `SELL GBPUSD SL=1.2750` |
   | XAU/USD crosses above 2400 | `BUY XAUUSD SL=2385` |

   If you set a "crossing down" alert but intend to buy (mean reversion),
   put the intended side in the message and do not rely on the condition
   word; a conflict between the two is rejected on purpose.

5. **Test with `&dry_run=1` first**, then inspect the real capture:

   ```bash
   curl -sS "https://<ngrok-host>/signals?token=$SIGNAL_TOKEN&limit=3&include_raw=1"
   ```

6. Make sure the account is registered (`/addaccount account:... phase:...`)
   and the symbol is enabled (`/addsymbol symbols:EURUSD,GBPUSD,XAUUSD`).

### Things OpenMarket cannot do

- No dynamic stop-loss or risk calculation — the SL is fixed in the alert.
- No multi-leg/OCO management; each alert is independent.
- A level-crossing alert can fire once per crossing, so a whipsaw may
  produce repeated messages. The 5-minute dedupe protects against platform
  retries of the *same* message, not against a genuinely new crossing.
- Free-plan alert limits and features can change; verify them in your
  OpenMarket account.

---

## Custom script example

Any language that can make an HTTPS request can trade:

```bash
curl -sS -X POST \
  "https://<host>/signal" \
  -H "X-Signal-Token: $SIGNAL_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
        "account": "1345657215",
        "symbol": "EURUSD",
        "side": "buy",
        "sl": 1.0800,
        "message": "my-strategy-v3 BUY EURUSD SL=1.0800"
      }'
```

Set the SL from your strategy, not from the current market price at send
time. The EA calculates the entry and TP when it executes, so the signal is
still valid if the webhook is delayed a few seconds.

---

## Troubleshooting

| Symptom | Check |
|---|---|
| `401 unauthorized` | `SIGNAL_TOKEN` on the server matches the token in URL/header/body; restart the service after changing `.env` |
| `missing side` / `missing symbol` / `missing sl` | Send a message like `BUY EURUSD SL=1.0800`; use `/signals?include_raw=1` to see what actually arrived |
| `symbol isn't enabled` | `/addsymbol symbols:EURUSD` on that account, then resend after the dedupe window |
| `duplicate` | Same content already processed within `SIGNAL_DEDUPE_SECONDS`; wait, change the SL, add `&force=1` for an intentional resend, or use the Discord command if it is genuinely a new trade |
| `SL is not below the current ask` | Side/SL are inconsistent with the current market; fix the alert message |
| `Account ... is not registered` | `/addaccount account:<login> phase:<challenge\|funded>` |
| Slack/Discord alerts but no trade | Check `/status`; the EA may be offline, trading blocked, at the position limit, or the symbol quote is zero |
| Nothing in Discord | Check `journalctl -u guardrails -n 100 --no-pager`; unauthorized requests and Discord send failures are printed, not posted |

Logs (systemd):

```bash
sudo journalctl -u guardrails -f
```

Remember that accepted webhook signals are real trades. Use a demo account
for the first live test, and keep `SIGNAL_TOKEN` private.
