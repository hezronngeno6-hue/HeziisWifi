# WiFi Billing System

Self-service WiFi billing for a **MikroTik** hotspot, paid with **M-Pesa**, running on a
Windows PC. Built for a small site (up to a few hundred devices — comfortably 15).

```
customer  ->  connects to WiFi
          ->  captive portal (this app)   GET  /
          ->  picks a plan, enters phone  POST  /api/pay        -> Safaricom STK push
          ->  enters M-Pesa PIN on phone
Safaricom ->  POST /mpesa/callback        -> payment recorded
app       ->  creates a hotspot user on the router + logs the device in
router    ->  enforces the time/speed limit; app sweeps and cleans up when it expires
```

The router does the enforcement. **If this service stops, already-connected customers stay
online** — that is deliberate.

---

## 1. Quick start

```powershell
cd C:\Users\f\Downloads\wifi-billing
python -m pip install -r requirements.txt
Copy-Item config.example.json config.json
notepad config.json            # set site name, admin password, router IP/password
python -m uvicorn app.main:app --host 0.0.0.0 --port 8090
```

Open <http://localhost:8090> for the portal and <http://localhost:8090/admin> for the
dashboard (HTTP Basic — username anything, password = `server.admin_password`).

Out of the box `mpesa.mock` is **true**, so you can click through the entire purchase flow
with no Safaricom account: press Pay, wait ~4 seconds, and the device is granted access.

---

## 2. MikroTik setup

Run these in the router terminal (Winbox → New Terminal, or SSH). Replace
`192.168.88.1` / `SERVER-IP` / the password with your own.

```rsc
# --- an API user for this app only ---
/user add name=billing-api password="CHANGE-THIS-STRONG-PASSWORD" group=full

# --- enable the REST API (v7). Pick ONE ---
/ip service enable www-ssl          # https://<router>:443/rest  (recommended)
/ip service enable www              # http://<router>:80/rest   (lan only)

# --- the base profile; the app creates plan-<CODE> profiles under it ---
/ip hotspot user profile add name=billing shared-users=1

# --- let unauthenticated customers reach the portal ---
/ip hotspot walled-garden ip add action=accept \
    dst-address=SERVER-IP comment="billing portal"
```

Then point the hotspot login page at the portal. Create `login.html` in the router's
hotspot folder:

```html
<html><head><meta http-equiv="refresh"
  content="0; url=http://SERVER-IP:8090/?mac=$(mac)&ip=$(ip)"></head>
<body>Redirecting…</body></html>
```

```rsc
/ip hotspot profile set [find] html-directory=hotspot
```

`$(mac)` and `$(ip)` are RouterOS hotspot variables — that's how the portal knows which
device is asking.

Set the same values in `config.json`:

```json
"mikrotik": { "host": "192.168.88.1", "port": 443, "use_tls": true,
              "username": "billing-api", "password": "…", "verify_tls": false }
```

> **Dry run.** Set `"dry_run": true` to log every router command without sending it — the
> fastest way to see what the app would do before touching a live hotspot.

Check it: <http://localhost:8090/healthz> should report the router's identity.

---

## 3. M-Pesa (Daraja)

Repeat every step for **sandbox first**, then production.

1. Create an app at <https://developer.safaricom.co.ke> → get **Consumer Key**,
   **Consumer Secret**, and for STK push the **Passkey** and **Shortcode**.
2. Safaricom must reach your callback. Your PC is on a LAN, so put a tunnel in front:

   ```powershell
   cloudflared tunnel --url http://localhost:8090
   ```

   Copy the `https://….trycloudflare.com` URL it prints.
3. Set it as the public base — **must be https, no trailing slash**:

   ```json
   "server": { "public_base_url": "https://your-tunnel.trycloudflare.com" },
   "mpesa":  { "mock": false, "environment": "sandbox",
               "consumer_key": "…", "consumer_secret": "…",
               "passkey": "…", "shortcode": "174379" }
   ```
4. Restart, buy a plan with a real phone, and watch the log:

   ```
   INFO mpesa: STK push sent to 254712****89 for KES 50 (ws_CO_21092026…)
   INFO app:   callback ws_CO_… -> The service request is processed successfully.
   INFO billing: access_granted DAY1 until 2026-09-22T14:03:11
   ```

For production: `environment` → `"production"`, and use your own Paybill/Till shortcode
with its matching passkey. Every callback is stored raw in `payments.raw_callback`, so you
can always reconcile against the M-Pesa statement.

**Keep credentials out of the file** if the PC is shared:

```powershell
$env:WIFI_MPESA_CONSUMER_KEY = "…"
$env:WIFI_MPESA_CONSUMER_SECRET = "…"
$env:WIFI_MPESA_PASSKEY = "…"
```

---

## 3b. Your Till and your router MAC — the Setup screen

Open **<http://localhost:8090/admin/setup>** (HTTP Basic; password = `server.admin_password`).
Everything here writes straight into `config.json` — you never have to hand-edit JSON.

**Section 1 — Your Till / Paybill**

| Field | What to put |
|---|---|
| Type | **Buy Goods — Till number** for a Till, or **Paybill** if you have one |
| Till / Paybill number | your shortcode, digits only (this is where the money lands) |
| Environment | `sandbox` while testing, `production` for live money |
| Public https URL | the `cloudflared` tunnel URL, so Safaricom can deliver the callback |
| Consumer Key / Secret / Passkey | from your Daraja app — leave blank to keep what's saved |
| Test mode | ON = simulate, take no money. Turn OFF when the credentials are right |

The buttons let you **Save & test M-Pesa login** (asks Safaricom for a token) and
**Send a test STK push** to your own phone — the only way to be sure money actually
arrives on your Till before customers use it.

**Section 2 — Your Router**

Fill in the router IP, API port (`443` for `www-ssl`), API username/password, then the
**Router MAC address**. Click **Save & test router**: the app reports the router's
identity, RouterOS version, board and serial, and lists every MAC it can see, so you can
click the right one instead of hunting for it in Winbox.

Tick **Refuse to sell on a different router** and the app checks that MAC on every
purchase — and blocks the sale *before* the customer is charged if the router doesn't
match. If the router is unreachable it **fails closed** rather than selling access it
can't deliver.

Untick **Dry run** once the connection test passes, or the app will keep logging router
commands instead of sending them.

### How the money reaches your Till

Money does **not** route by router MAC. An STK push is raised against *your* shortcode
using *your* Daraja keys, so Safaricom credits *your* Till — the router MAC is an
unrelated safety check that the app is selling on the right hardware. Each sale records`shortcode` and `till_type` in the `payments` table for reconciliation against your
M-Pesa statement.

Buy Goods and Paybill use **different API transaction types** (`CustomerBuyGoodsOnline`
vs `CustomerPayBillOnline`). Choosing the wrong one makes Safaricom reject every push, so
pick carefully in Section 1.

---

## 4. Selling access

**Self-service (M-Pesa).** Whatever is in `plans` shows on the portal. Edit `config.json`
and restart to change prices — plans are re-seeded on boot.

**Vouchers (cash / resellers).** Print sheets of codes; each QR opens the portal with the
code pre-filled:

```powershell
python -m tools.vouchers --plan DAY1 --count 12 --create
```

12 cards per A4 page. The dashboard's **Print 12 vouchers** button does the same.

---

## 5. Running it as a service

The portal must be up whenever you're selling. Use [NSSM](https://nssm.cc):

```powershell
nssm install WiFiBilling "C:\Users\f\AppData\Local\Programs\Python\Python312\python.exe" `
    "-m uvicorn app.main:app --host 0.0.0.0 --port 8090"
nssm set WiFiBilling AppDirectory "C:\Users\f\Downloads\wifi-billing"
nssm set WiFiBilling AppStdout "C:\Users\f\Downloads\wifi-billing\service.log"
nssm set WiFiBilling AppStderr "C:\Users\f\Downloads\wifi-billing\service.log"
nssm set WiFiBilling Start SERVICE_AUTO_START
nssm start WiFiBilling
```

Also disable sleep/hibernate on that machine, and add the tunnel as a second service.

---

## 6. Layout

```
config.json                 your settings + credentials (never commit)
app/
  main.py                   FastAPI: portal, callbacks, admin
  config.py                 config loading + env overrides
  db.py                     SQLite schema and helpers
  mikrotik.py               RouterOS REST client (users, profiles, accounting)
  mpesa.py                  Daraja STK push + callback parsing (+ mock mode)
  billing.py                the money -> access state machine
  templates/portal.html     what the customer sees
  templates/admin.html      what you see
  static/style.css
tools/vouchers.py           voucher PDF printer (QR codes)
data/billing.sqlite3        the database (created on first run)
service.log
```

### Routes

| Route | Purpose |
|---|---|
| `GET /` | captive portal (`?mac=&ip=`) |
| `POST /api/pay` | start STK push |
| `GET /api/payment/{id}` | poll payment state (portal polls every 2s) |
| `POST /mpesa/callback` | Safaricom callback — public |
| `POST /api/voucher` | redeem a printed code |
| `POST /api/reconnect` | re-log a device with a live session |
| `GET /admin` | dashboard (Basic auth) |
| `GET /admin/setup` | **Till number, router MAC and credentials** (Basic auth) |
| `POST /admin/setup` | save settings to `config.json` and reload |
| `POST /admin/setup/test-mpesa` | ask Safaricom for a token |
| `POST /admin/setup/test-router` | router identity + MAC check |
| `POST /admin/setup/test-charge` | send a real test STK push |
| `POST /admin/api/kick?mac=` | disconnect a device |
| `GET /admin/vouchers.pdf` | printable voucher sheet |
| `GET /healthz` | router + system health |

---

## 7. Troubleshooting

| Symptom | Cause / fix |
|---|---|
| Portal says *"didn't receive a device address"* | The hotspot isn't passing `?mac=`. Check the redirect in `login.html`. |
| Payment succeeds but no internet | Router rejected the login. Check `audit` for `grant_router_error`, and that `/ip hotspot user` has a `dev-…` user. |
| *"must be a public https URL"* | `server.public_base_url` isn't https, or is still `localhost`. Re-run the tunnel. |
| Callback never arrives | Tunnel down, or `public_base_url` doesn't match the tunnel URL. Test with `curl https://your-url/healthz`. |
| Callback arrives twice | Harmless — the payment state machine ignores repeat callbacks. |
| Devices reconnect and get nothing | Router rebooted and dropped the login; tap **Reconnect** or `POST /api/reconnect`. |
| Session expired but the device still browses | The sweep runs every 20s — check the service logs, and `/healthz` for router errors. |

---

## 8. Before you go live

- Change `server.admin_password` and `server.secret_key`. The defaults are public.
- Restrict `/admin` to your LAN, or put it behind the tunnel only.
- Back up `data/billing.sqlite3` — it is your revenue record (WAL mode: copy the
  `-wal` file too, or use `.backup`).
- M-Pesa money moves on the *payment*, not the login. If a grant fails, the payment is
  still in the DB as `SUCCESS` with a `grant_router_error` audit line — reconcile from there.
- Kenyan record-keeping: the `payments` table keeps phone, amount, receipt and timestamp
  for every sale.

## 9. What this deliberately does *not* do

It bills **your own** hotspot, on **your own** router, using the credentials you supply.
It doesn't touch anyone else's network or gateway — that's a different thing entirely, and
one I won't help with.
