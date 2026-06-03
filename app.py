import os
import queue
import re
import sys
import uuid
import hashlib
import hmac
import base64
import json
import logging
import sqlite3
import threading
import time
import webbrowser
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

import requests
from flask import Flask, Response, jsonify, render_template_string, request

# ─── Credentials (tcamsBankTest — Account 46, TZS) ────────────────────────────
BASE_URL = "https://app.capitalpay.co.tz/api"
CHECKOUT_URL = "https://app.capitalpay.co.tz/PaymentAPI/invoice/checkout"

ACCOUNT_ID = 46
API_KEY = "tmlLFcEcOy+e6ihv"
API_SECRET = "Txe/Gd97FaH9jsuqrDsr9jaKuJhVm0A/"
CURRENCY = "TZS"

LOCAL_PORT = int(os.environ.get("PORT", 5055))
HOST = os.environ.get("RENDER_EXTERNAL_URL", f"http://127.0.0.1:{LOCAL_PORT}")
PUBLIC_HOST = "https://app.capitalpay.co.tz"
PRIVATE_HOSTS = (
    "https://192.168.92.110",
    "http://192.168.92.110",
)

DB_PATH = Path(os.environ.get('DB_PATH', str(Path(__file__).parent))) / 'capitalpay.db'

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%SZ",
)

# ─── Terminal log capture ─────────────────────────────────────────────────────
# Thread-safe queue; stores the last 500 log lines for the /logs Dash UI.
LOG_QUEUE: queue.Queue = queue.Queue(maxsize=500)


class _TeeLogger:
    """Writes to the real stdout AND pushes each line into LOG_QUEUE."""

    def __init__(self, real):
        self._real = real

    def write(self, msg: str):
        self._real.write(msg)
        if msg.strip():
            ts = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
            line = f"{ts}  {msg.rstrip()}"
            if LOG_QUEUE.full():
                try:
                    LOG_QUEUE.get_nowait()
                except queue.Empty:
                    pass
            try:
                LOG_QUEUE.put_nowait(line)
            except queue.Full:
                pass

    def flush(self):
        self._real.flush()

    def __getattr__(self, name):
        return getattr(self._real, name)


sys.stdout = _TeeLogger(sys.stdout)


def get_log_lines() -> list[str]:
    """Return all buffered log lines newest-first."""
    items = list(LOG_QUEUE.queue)
    items.reverse()
    return items


app = Flask(__name__)
_checkout_forms: dict[str, dict[str, str]] = {}


# ─── Database ─────────────────────────────────────────────────────────────────

def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


@contextmanager
def get_db():
    conn = _get_conn()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    with get_db() as db:
        db.executescript("""
            CREATE TABLE IF NOT EXISTS invoices (
                id                 INTEGER PRIMARY KEY AUTOINCREMENT,
                client_invoice_ref TEXT    NOT NULL UNIQUE,
                invoice_number     TEXT,
                amount             TEXT    NOT NULL,
                currency           TEXT    NOT NULL,
                name               TEXT    NOT NULL,
                id_number          TEXT    NOT NULL,
                desc               TEXT    NOT NULL,
                msisdn             TEXT,
                email              TEXT,
                secure_hash        TEXT    NOT NULL,
                created_at         TEXT    NOT NULL
            );

            CREATE TABLE IF NOT EXISTS notifications (
                id                   INTEGER PRIMARY KEY AUTOINCREMENT,
                received_at          TEXT NOT NULL,
                valid_hash           INTEGER NOT NULL DEFAULT 0,
                status               TEXT,
                phone_number         TEXT,
                payment_date         TEXT,
                payment_channel      TEXT,
                last_payment_amount  TEXT,
                invoice_number       TEXT,
                invoice_amount       TEXT,
                currency             TEXT,
                client_invoice_ref   TEXT,
                amount_paid          TEXT,
                payment_references   TEXT,
                raw                  TEXT
            );

            CREATE TABLE IF NOT EXISTS capitalpay_ips (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                ip_address  TEXT NOT NULL,
                first_seen  TEXT NOT NULL,
                last_seen   TEXT NOT NULL,
                hit_count   INTEGER NOT NULL DEFAULT 1
            );

            CREATE UNIQUE INDEX IF NOT EXISTS idx_ip
                ON capitalpay_ips(ip_address);
            CREATE INDEX IF NOT EXISTS idx_notif_ref
                ON notifications(client_invoice_ref);
            CREATE INDEX IF NOT EXISTS idx_notif_status
                ON notifications(status);
            CREATE INDEX IF NOT EXISTS idx_invoice_ref
                ON invoices(client_invoice_ref);
        """)


# ─── Invoice store ─────────────────────────────────────────────────────────────

def save_invoice_meta(
        *, client_invoice_ref, invoice_number, amount,
        name, id_number, desc, msisdn, email, secure_hash,
) -> None:
    with get_db() as db:
        db.execute(
            """
            INSERT INTO invoices
                (client_invoice_ref, invoice_number, amount, currency,
                 name, id_number, desc, msisdn, email, secure_hash, created_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(client_invoice_ref) DO UPDATE SET
                invoice_number = excluded.invoice_number,
                secure_hash    = excluded.secure_hash,
                created_at     = excluded.created_at
            """,
            (
                client_invoice_ref, invoice_number, amount, CURRENCY,
                name, id_number, desc, msisdn, email, secure_hash,
                datetime.utcnow().isoformat(timespec="seconds") + "Z",
            ),
        )


def get_invoice_meta(client_invoice_ref: str) -> dict | None:
    with get_db() as db:
        row = db.execute(
            "SELECT * FROM invoices WHERE client_invoice_ref = ?",
            (client_invoice_ref,),
        ).fetchone()
    return dict(row) if row else None


# ─── Notification store ────────────────────────────────────────────────────────

def save_notification(event: dict, valid_hash: bool) -> None:
    with get_db() as db:
        db.execute(
            """
            INSERT INTO notifications
                (received_at, valid_hash, status, phone_number, payment_date,
                 payment_channel, last_payment_amount, invoice_number,
                 invoice_amount, currency, client_invoice_ref, amount_paid,
                 payment_references, raw)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                datetime.utcnow().isoformat(timespec="seconds") + "Z",
                1 if valid_hash else 0,
                event.get("status", ""),
                event.get("phone_number", ""),
                event.get("payment_date", ""),
                event.get("payment_channel", ""),
                event.get("last_payment_amount", ""),
                event.get("invoice_number", ""),
                event.get("invoice_amount", ""),
                event.get("currency", ""),
                event.get("client_invoice_ref", ""),
                event.get("amount_paid", ""),
                json.dumps(event.get("payment_reference", [])),
                json.dumps(event),
            ),
        )


def get_notifications(limit: int = 500) -> list[dict]:
    with get_db() as db:
        rows = db.execute(
            "SELECT * FROM notifications ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    result = []
    for row in rows:
        d = dict(row)
        d["valid_hash"] = bool(d["valid_hash"])
        d["payment_references"] = json.loads(d["payment_references"] or "[]")
        d["raw"] = json.loads(d["raw"] or "{}")
        result.append(d)
    return result


def clear_notifications() -> None:
    with get_db() as db:
        db.execute("DELETE FROM notifications")


# ─── IP tracking ──────────────────────────────────────────────────────────────

def record_ip(ip: str) -> None:
    now = datetime.utcnow().isoformat(timespec="seconds") + "Z"
    with get_db() as db:
        existing = db.execute(
            "SELECT id, hit_count, first_seen FROM capitalpay_ips WHERE ip_address = ?",
            (ip,),
        ).fetchone()
        if existing:
            db.execute(
                "UPDATE capitalpay_ips SET last_seen = ?, hit_count = hit_count + 1 WHERE ip_address = ?",
                (now, ip),
            )
            app.logger.info(
                f"[IP] {ip} — seen again (first: {existing['first_seen']}, hits: {existing['hit_count'] + 1})"
            )
        else:
            db.execute(
                "INSERT INTO capitalpay_ips (ip_address, first_seen, last_seen, hit_count) VALUES (?,?,?,1)",
                (ip, now, now),
            )
            app.logger.info(f"[IP] NEW IP observed: {ip} — confirm with CapitalPay support then add to allowlist")


# ─── Secure hash ──────────────────────────────────────────────────────────────

def compute_secure_hash(
        api_client_id: str,
        amount: str,
        service_id: str,
        client_id_number: str,
        bill_ref_number: str,
        bill_desc: str,
        client_name: str,
) -> str:
    data_string = (
            api_client_id
            + amount
            + service_id
            + client_id_number
            + CURRENCY
            + bill_ref_number
            + bill_desc
            + client_name
            + API_SECRET
    )
    raw_hash = hmac.new(API_KEY.encode(), data_string.encode(), hashlib.sha256).digest()
    return base64.b64encode(raw_hash).decode()


def validate_notification_hash(event: dict) -> bool:
    received = event.get("secure_hash", "")
    if not received:
        return False
    ref = event.get("client_invoice_ref", "")
    meta = get_invoice_meta(ref)
    if not meta:
        return False
    account_id_str = str(ACCOUNT_ID)
    expected = compute_secure_hash(
        api_client_id=account_id_str,
        amount=meta["amount"],
        service_id=account_id_str,
        client_id_number=meta["id_number"],
        bill_ref_number=ref,
        bill_desc=meta["desc"],
        client_name=meta["name"],
    )
    return hmac.compare_digest(received, expected)


# ─── CapitalPay API helpers ────────────────────────────────────────────────────

def generate_token() -> str | None:
    try:
        resp = requests.post(
            f"{BASE_URL}/oauth/generate/token",
            json={"key": API_KEY, "secret": API_SECRET},
            headers={"Content-Type": "application/json"},
            timeout=30,
        )
        resp.raise_for_status()
        return resp.json().get("token")
    except Exception as e:
        print(f"  [1/5] ✗ Token generation failed: {e}")
        return None


def normalize_checkout_html(html: str) -> str:
    for private_host in PRIVATE_HOSTS:
        html = html.replace(private_host, PUBLIC_HOST)
    return html


def extract_invoice_number(payload: dict | str) -> str | None:
    if isinstance(payload, dict):
        invoice = payload.get("invoice")
        if isinstance(invoice, dict) and invoice.get("invoice_number"):
            return invoice["invoice_number"]
        for key in ("invoice_number", "invoice_ref"):
            if payload.get(key):
                return payload[key]
        nested = payload.get("data")
        if isinstance(nested, dict) and nested.get("invoice_number"):
            return nested["invoice_number"]
        return None
    match = re.search(r'invoice_no="([A-Z0-9]+)"', payload)
    if match:
        return match.group(1)
    match = re.search(
        r"PAYMENT REF[\s\S]*?<h2[^>]*>\s*([A-Z0-9]+)\s*</h2>", payload, re.I,
    )
    return match.group(1) if match else None


def create_invoice(token: str, payload: dict) -> dict:
    resp = requests.post(
        f"{BASE_URL}/invoice/create",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json, text/html;q=0.9, */*;q=0.8",
        },
        json=payload,
        timeout=30,
    )
    resp.raise_for_status()
    text = resp.text or ""
    content_type = (resp.headers.get("Content-Type") or "").lower()
    stripped = text.lstrip()
    if (
            "text/html" in content_type
            or stripped.startswith("<!DOCTYPE")
            or stripped.startswith("<html")
    ):
        html = normalize_checkout_html(text)
        return {"kind": "html", "html": html, "invoice_number": extract_invoice_number(html)}
    try:
        data = resp.json()
    except ValueError:
        if stripped.startswith("<"):
            html = normalize_checkout_html(text)
            return {"kind": "html", "html": html, "invoice_number": extract_invoice_number(html)}
        raise
    return {"kind": "json", "data": data}


def build_checkout_params(
        *, name, msisdn, email, id_number, amount,
        bill_ref, desc, callback_url, notif_url,
) -> dict[str, str]:
    account_id_str = str(ACCOUNT_ID)
    amount_str = f"{float(amount):.2f}"
    params = {
        "apiClientID": account_id_str,
        "secureHash": compute_secure_hash(
            account_id_str, amount_str, account_id_str,
            id_number, bill_ref, desc, name,
        ),
        "billDesc": desc,
        "billRefNumber": bill_ref,
        "currency": CURRENCY,
        "serviceID": account_id_str,
        "clientMSISDN": msisdn,
        "clientName": name,
        "clientIDNumber": id_number,
        "clientEmail": email,
        "notificationURL": notif_url,
        "amountExpected": amount_str,
    }
    if callback_url:
        params["callBackURLOnSuccess"] = callback_url
    return params


def store_checkout_form(params: dict[str, str]) -> str:
    checkout_id = uuid.uuid4().hex
    _checkout_forms[checkout_id] = params
    return checkout_id


def fetch_checkout_page(params: dict[str, str]) -> str:
    response = requests.post(CHECKOUT_URL, data=params, timeout=30)
    if not response.ok:
        raise RuntimeError(f"Checkout error ({response.status_code}): {response.text[:300]}")
    return normalize_checkout_html(response.text)


# ─── HTML: Checkout page ───────────────────────────────────────────────────────

CHECKOUT_HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>CapitalPay Checkout</title>
<link href="https://fonts.googleapis.com/css2?family=DM+Serif+Display&family=DM+Sans:wght@300;400;500;600&display=swap" rel="stylesheet"/>
<style>
  :root{
    --bg:#0d0f14;--surface:#161a22;--border:#252b38;
    --accent:#e05a1e;--accent2:#f07a3a;--text:#e8eaf0;--muted:#6b7280;
    --success:#22c55e;--error:#ef4444;--radius:12px;
  }
  *{box-sizing:border-box;margin:0;padding:0}
  body{background:var(--bg);color:var(--text);font-family:'DM Sans',sans-serif;
    min-height:100vh;display:flex;align-items:center;justify-content:center;padding:24px;
    background-image:
      radial-gradient(ellipse 60% 40% at 80% 10%,rgba(224,90,30,.08) 0%,transparent 60%),
      radial-gradient(ellipse 40% 60% at 10% 90%,rgba(240,122,58,.05) 0%,transparent 60%)}
  .card{background:var(--surface);border:1px solid var(--border);border-radius:20px;
    width:100%;max-width:560px;overflow:hidden;box-shadow:0 32px 64px rgba(0,0,0,.5)}
  .card-header{padding:32px 36px 24px;border-bottom:1px solid var(--border);
    display:flex;align-items:center;gap:16px}
  .logo-mark{width:44px;height:44px;background:linear-gradient(135deg,var(--accent),var(--accent2));
    border-radius:10px;display:flex;align-items:center;justify-content:center;
    font-family:'DM Serif Display',serif;font-size:20px;color:#fff;flex-shrink:0}
  .card-title{font-family:'DM Serif Display',serif;font-size:22px;line-height:1.2}
  .card-sub{font-size:13px;color:var(--muted);margin-top:2px}
  .hd-right{margin-left:auto;display:flex;align-items:center;gap:8px}
  .badge{display:inline-block;font-size:11px;font-weight:600;
    background:rgba(224,90,30,.2);color:var(--accent2);
    border-radius:6px;padding:2px 8px;letter-spacing:.04em}
  .monitor-link{font-size:11px;font-weight:600;color:var(--accent2);
    text-decoration:none;padding:3px 10px;border:1px solid rgba(224,90,30,.3);
    border-radius:20px;background:rgba(224,90,30,.08)}
  .monitor-link:hover{background:rgba(224,90,30,.16)}
  .card-body{padding:32px 36px}
  .field{margin-bottom:18px}
  label{display:block;font-size:12px;font-weight:600;letter-spacing:.06em;
    text-transform:uppercase;color:var(--muted);margin-bottom:6px}
  input{width:100%;background:#1e2330;border:1px solid var(--border);
    border-radius:var(--radius);color:var(--text);font-family:'DM Sans',sans-serif;
    font-size:14px;padding:11px 14px;outline:none}
  input:focus{border-color:var(--accent)}
  .row2{display:grid;grid-template-columns:1fr 1fr;gap:14px}
  .currency-tag{display:flex;align-items:center;background:#1e2330;
    border:1px solid var(--border);border-radius:var(--radius);
    padding:11px 14px;color:var(--accent2);font-size:14px;font-weight:600;
    letter-spacing:.04em}
  .btn{width:100%;padding:14px;background:linear-gradient(135deg,var(--accent),var(--accent2));
    border:none;border-radius:var(--radius);color:#fff;font-family:'DM Sans',sans-serif;
    font-size:15px;font-weight:600;cursor:pointer;margin-top:8px;
    transition:opacity .2s,transform .1s;letter-spacing:.02em}
  .btn:hover{opacity:.9}.btn:active{transform:scale(.98)}.btn:disabled{opacity:.5;cursor:not-allowed}
  .status{margin-top:18px;padding:14px 16px;border-radius:var(--radius);
    font-size:13px;display:none}
  .status.show{display:block}
  .status.info{background:rgba(224,90,30,.12);border:1px solid rgba(224,90,30,.3);color:#f0a070}
  .status.success{background:rgba(34,197,94,.1);border:1px solid rgba(34,197,94,.3);color:var(--success)}
  .status.error{background:rgba(239,68,68,.1);border:1px solid rgba(239,68,68,.3);color:var(--error)}
  .spinner{display:inline-block;width:14px;height:14px;
    border:2px solid rgba(255,255,255,.3);border-top-color:#fff;
    border-radius:50%;animation:spin .7s linear infinite;
    margin-right:8px;vertical-align:middle}
  @keyframes spin{to{transform:rotate(360deg)}}
</style>
</head>
<body>
<div class="card">
  <div class="card-header">
    <div class="logo-mark">C</div>
    <div>
      <div class="card-title">CapitalPay Checkout <span class="badge">Live</span></div>
      <div class="card-sub">Account ID: {{ account_id }} &nbsp;·&nbsp; TZS &nbsp;·&nbsp; Secure Payment Gateway</div>
    </div>
    <div class="hd-right">
      <a class="monitor-link" href="/monitor" target="_blank" rel="noopener noreferrer">📊 Monitor</a>
      <a class="monitor-link" href="{{ logs_url }}" target="_blank" rel="noopener noreferrer">🖥 Terminal</a>
    </div>
  </div>
  <div class="card-body">
    <form id="payForm">
      <div class="row2">
        <div class="field"><label>Full Name</label>
          <input name="name" type="text" placeholder="e.g. John Doe" required/></div>
        <div class="field"><label>Phone (MSISDN)</label>
          <input name="msisdn" type="text" placeholder="+255712345678" required/></div>
      </div>
      <div class="field"><label>Email (optional)</label>
        <input name="email" type="email" placeholder="you@example.com"/></div>
      <div class="field"><label>ID / Passport Number</label>
        <input name="id_number" type="text" placeholder="National ID or passport" required/></div>
      <div class="row2">
        <div class="field"><label>Amount (TZS)</label>
          <input name="amount" type="number" step="0.01" min="1" placeholder="10000.00" required/></div>
        <div class="field"><label>Currency</label>
          <div class="currency-tag">TZS — Tanzanian Shilling</div></div>
      </div>
      <div class="field"><label>Invoice / Bill Reference</label>
        <input name="bill_ref" type="text" placeholder="INV-2026-001" required/></div>
      <div class="field"><label>Description</label>
        <input name="desc" type="text" placeholder="Payment description" required/></div>
      <div class="field"><label>Callback URL (on success)</label>
        <input name="callback_url" type="url" placeholder="https://yoursite.com/success"/></div>
      <div class="field"><label>Notification URL (IPN) *</label>
        <input name="notification_url" type="url" id="notif-url"
               placeholder="https://yoursite.com/notify" required/></div>
      <button class="btn" type="submit" id="submitBtn">Proceed to Payment</button>
    </form>
    <div class="status" id="statusBox"></div>
  </div>
</div>
<script>
(function(){
  const el = document.getElementById('notif-url');
  if (!el.value) el.value = '{{ flask_host }}' + '/notify';
})();

const form      = document.getElementById('payForm');
const btn       = document.getElementById('submitBtn');
const statusBox = document.getElementById('statusBox');

function setStatus(msg, type) {
  statusBox.className = 'status show ' + type;
  statusBox.innerHTML = msg;
}

form.addEventListener('submit', async (e) => {
  e.preventDefault();
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner"></span>Creating Invoice...';
  setStatus('Generating token and creating invoice. Please wait...', 'info');
  const data = Object.fromEntries(new FormData(form).entries());
  try {
    const res  = await fetch('/checkout', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(data),
    });
    const json = await res.json();
    if (!res.ok || json.error) throw new Error(json.error || 'Unknown error from server');
    if (json.checkout_url) {
      setStatus('Invoice <strong>' + (json.invoice_ref || data.bill_ref) + '</strong> created. Opening CapitalPay checkout in a new tab...', 'success');
      window.open(json.checkout_url, '_blank', 'noopener,noreferrer');
    } else if (json.iframe_html) {
      setStatus('Invoice created. Opening CapitalPay checkout in a new tab...', 'success');
      const w = window.open('', '_blank', 'noopener,noreferrer');
      w.document.open(); w.document.write(json.iframe_html); w.document.close();
    } else {
      setStatus('Invoice created. Reference: <strong>' + (json.invoice_ref || data.bill_ref) + '</strong>', 'success');
    }
  } catch (err) {
    setStatus(err.message, 'error');
  } finally {
    btn.disabled = false;
    btn.textContent = 'Proceed to Payment';
  }
});
</script>
</body>
</html>
"""

# ─── HTML: Monitor page ────────────────────────────────────────────────────────

MONITOR_HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>CapitalPay · Payment Monitor</title>
<link href="https://fonts.googleapis.com/css2?family=DM+Serif+Display&family=DM+Sans:wght@300;400;500;600&display=swap" rel="stylesheet"/>
<style>
  :root{
    --bg:#0d0f14;--surface:#161a22;--border:#252b38;
    --accent:#e05a1e;--accent2:#f07a3a;--text:#e8eaf0;--muted:#6b7280;
    --success:#22c55e;--error:#ef4444;--warn:#f59e0b;--radius:12px;
  }
  *{box-sizing:border-box;margin:0;padding:0}
  body{background:var(--bg);color:var(--text);font-family:'DM Sans',sans-serif;
    min-height:100vh;padding:32px 16px 64px;
    background-image:radial-gradient(ellipse 60% 40% at 80% 10%,rgba(224,90,30,.06) 0%,transparent 60%)}
  .page{max-width:1100px;margin:0 auto}
  .hd{display:flex;align-items:center;gap:14px;margin-bottom:28px}
  .logo-mark{width:42px;height:42px;background:linear-gradient(135deg,var(--accent),var(--accent2));
    border-radius:10px;display:flex;align-items:center;justify-content:center;
    font-family:'DM Serif Display',serif;font-size:18px;color:#fff;flex-shrink:0}
  .hd-text h1{font-family:'DM Serif Display',serif;font-size:22px;color:var(--text)}
  .hd-text p{font-size:12px;color:var(--muted);margin-top:2px}
  .hd-right{margin-left:auto;display:flex;align-items:center;gap:10px}
  .pill{background:rgba(34,197,94,.15);border:1px solid rgba(34,197,94,.3);
    color:var(--success);border-radius:20px;padding:3px 12px;font-size:11px;font-weight:700}
  .nav-link{background:rgba(224,90,30,.12);border:1px solid rgba(224,90,30,.25);
    color:var(--accent2);border-radius:20px;padding:4px 12px;
    font-size:11px;font-weight:700;text-decoration:none}
  .nav-link:hover{background:rgba(224,90,30,.2)}
  .stats{display:grid;grid-template-columns:repeat(4,1fr);gap:14px;margin-bottom:24px}
  @media(max-width:640px){.stats{grid-template-columns:1fr 1fr}}
  .stat{background:var(--surface);border:1px solid var(--border);border-radius:14px;padding:18px 20px}
  .stat .lbl{font-size:11px;font-weight:600;color:var(--muted);
    text-transform:uppercase;letter-spacing:.07em;margin-bottom:6px}
  .stat .val{font-size:26px;font-weight:700;color:var(--text);line-height:1}
  .stat .val.green{color:var(--success)}.stat .val.red{color:var(--error)}
  .filters{display:flex;gap:10px;flex-wrap:wrap;margin-bottom:18px;
    background:var(--surface);border:1px solid var(--border);
    border-radius:12px;padding:14px 18px}
  .filters input,.filters select{padding:7px 12px;border:1px solid var(--border);
    border-radius:8px;font-family:'DM Sans',sans-serif;font-size:13px;
    color:var(--text);background:#1e2330;outline:none}
  .filters input:focus,.filters select:focus{border-color:var(--accent)}
  .filters input{flex:1;min-width:180px}
  .filters select option{background:#1e2330}
  .btn-sm{padding:7px 14px;border-radius:8px;font-family:'DM Sans',sans-serif;
    font-size:13px;font-weight:600;cursor:pointer;border:1px solid transparent}
  .btn-clear{background:rgba(239,68,68,.12);color:var(--error);border-color:rgba(239,68,68,.3)}
  .btn-clear:hover{background:rgba(239,68,68,.2)}
  .btn-test{background:rgba(224,90,30,.12);color:var(--accent2);border-color:rgba(224,90,30,.3)}
  .btn-test:hover{background:rgba(224,90,30,.2)}
  .empty{text-align:center;padding:60px 20px;color:var(--muted);
    background:var(--surface);border:1px solid var(--border);border-radius:14px}
  .empty .icon{font-size:36px;margin-bottom:12px}
  .empty p{font-size:14px;line-height:1.7}
  .empty code{font-size:13px;background:#1e2330;padding:2px 6px;border-radius:4px;color:var(--accent2)}
  .event{background:var(--surface);border:1px solid var(--border);
    border-radius:14px;margin-bottom:14px;overflow:hidden}
  .event-header{display:flex;align-items:center;gap:10px;flex-wrap:wrap;
    padding:14px 20px;border-bottom:1px solid var(--border);background:#1a1f2b}
  .badge{border-radius:20px;padding:3px 11px;font-size:11px;
    font-weight:700;text-transform:uppercase;letter-spacing:.04em}
  .b-settled{background:rgba(34,197,94,.15);color:var(--success);border:1px solid rgba(34,197,94,.3)}
  .b-pending{background:rgba(245,158,11,.15);color:var(--warn);border:1px solid rgba(245,158,11,.3)}
  .b-failed{background:rgba(239,68,68,.15);color:var(--error);border:1px solid rgba(239,68,68,.3)}
  .b-default{background:rgba(107,114,128,.15);color:var(--muted);border:1px solid rgba(107,114,128,.3)}
  .b-hashok{background:rgba(34,197,94,.15);color:var(--success);border:1px solid rgba(34,197,94,.3)}
  .b-hashbad{background:rgba(239,68,68,.15);color:var(--error);border:1px solid rgba(239,68,68,.3)}
  .event-meta{margin-left:auto;font-size:12px;color:var(--muted)}
  .event-body{padding:18px 20px}
  .grid3{display:grid;grid-template-columns:repeat(3,1fr);gap:0}
  @media(max-width:640px){.grid3{grid-template-columns:1fr}}
  .kv{padding:5px 0}
  .kv dt{font-size:11px;color:var(--muted);font-weight:600;
    text-transform:uppercase;letter-spacing:.06em}
  .kv dd{font-size:13px;color:var(--text);font-family:monospace;margin-top:2px}
  .divider{border:none;border-top:1px solid var(--border);margin:14px 0}
  .ref-table{width:100%;border-collapse:collapse;font-size:12px}
  .ref-table th{text-align:left;padding:7px 10px;font-size:10.5px;font-weight:700;
    text-transform:uppercase;letter-spacing:.07em;color:var(--muted);
    background:#1a1f2b;border-bottom:1px solid var(--border)}
  .ref-table td{padding:8px 10px;border-bottom:1px solid var(--border);color:var(--text)}
  .ref-table tr:last-child td{border-bottom:none}
  .ref-table .mono{font-family:monospace}
  .ref-table .amt{text-align:right;font-weight:700;color:var(--success)}
  details{margin-top:10px}
  summary{font-size:12px;color:var(--muted);cursor:pointer;user-select:none;padding:4px 0}
  summary:hover{color:var(--accent2)}
  pre.raw{margin-top:8px;padding:12px;background:#0d1117;border-radius:8px;
    font-size:11px;font-family:monospace;color:#8b949e;
    white-space:pre-wrap;max-height:260px;overflow-y:auto}
  .section-hd{display:flex;align-items:center;gap:12px;margin:40px 0 14px}
  .section-hd h2{font-family:'DM Serif Display',serif;font-size:18px;color:var(--text)}
  .section-hd span{font-size:12px;color:var(--muted)}
  .section-hd .hint{margin-left:auto;font-size:11px;color:var(--muted)}
  @keyframes pulse{0%,100%{opacity:1}50%{opacity:.4}}
  .dot{display:inline-block;animation:pulse 2s ease-in-out infinite}
</style>
</head>
<body>
<div class="page">
  <div class="hd">
    <div class="logo-mark">C</div>
    <div class="hd-text">
      <h1>Payment Monitor</h1>
      <p>Live TZS notifications · auto-refreshes every 5 s · persisted in SQLite</p>
    </div>
    <div class="hd-right">
      <span class="pill"><span class="dot">●</span> LIVE</span>
      <a class="nav-link" href="/" target="_blank" rel="noopener noreferrer">+ New Checkout</a>
    </div>
  </div>

  <div class="stats">
    <div class="stat"><div class="lbl">Total received</div><div class="val" id="s-total">—</div></div>
    <div class="stat"><div class="lbl">Settled</div><div class="val green" id="s-settled">—</div></div>
    <div class="stat"><div class="lbl">Hash mismatches</div><div class="val red" id="s-bad">—</div></div>
    <div class="stat"><div class="lbl">Total paid (TZS)</div><div class="val" id="s-amount">—</div></div>
  </div>

  <div class="filters">
    <input id="f-search" type="text" placeholder="Search invoice, ref, phone…" oninput="render()"/>
    <select id="f-status" onchange="render()">
      <option value="">All statuses</option>
      <option value="settled">Settled</option>
      <option value="pending">Pending</option>
      <option value="failed">Failed</option>
    </select>
    <select id="f-hash" onchange="render()">
      <option value="">All</option>
      <option value="valid">Hash valid</option>
      <option value="invalid">Hash invalid</option>
    </select>
    <button class="btn-sm btn-test"  onclick="injectTest()">Inject test event</button>
    <button class="btn-sm btn-clear" onclick="clearAll()">Clear all</button>
  </div>

  <div id="feed"></div>

  <div class="section-hd">
    <h2>CapitalPay Server IPs</h2>
    <span id="ip-count"></span>
    <span class="hint">Confirm with CapitalPay support · refreshes every 10 s</span>
  </div>
  <div style="background:var(--surface);border:1px solid var(--border);border-radius:14px;overflow:hidden">
    <table class="ref-table" style="width:100%">
      <thead><tr>
        <th>IP Address</th><th>First seen (UTC)</th>
        <th>Last seen (UTC)</th><th style="text-align:center">Hits</th>
      </tr></thead>
      <tbody id="ip-body">
        <tr><td colspan="4" style="text-align:center;color:var(--muted);padding:24px">
          No POST requests received yet
        </td></tr>
      </tbody>
    </table>
  </div>
</div>

<script>
var allEvents = [];

function statusBadge(s) {
  var c = {settled:'b-settled',pending:'b-pending',failed:'b-failed'}[s.toLowerCase()] || 'b-default';
  return '<span class="badge ' + c + '">' + s + '</span>';
}
function hashBadge(v) {
  return v
    ? '<span class="badge b-hashok">&#10003; Hash OK</span>'
    : '<span class="badge b-hashbad">&#9888; Hash mismatch</span>';
}
function kv(l, v) {
  return '<div class="kv"><dt>' + l + '</dt><dd>' + (v || '—') + '</dd></div>';
}
function refRows(refs) {
  if (!refs || !refs.length)
    return '<em style="font-size:12px;color:var(--muted)">No payment references</em>';
  var rows = refs.map(function(r) {
    return '<tr>'
      + '<td class="mono">' + (r.payment_reference||'—') + '</td>'
      + '<td>' + (r.payment_date||'—') + '</td>'
      + '<td>' + (r.inserted_at||'—') + '</td>'
      + '<td>' + (r.currency||'—') + '</td>'
      + '<td class="mono amt">' + (r.amount||'—') + '</td>'
      + '</tr>';
  }).join('');
  return '<table class="ref-table"><thead><tr>'
    + '<th>Reference #</th><th>Payment date</th><th>Inserted at</th>'
    + '<th>CCY</th><th style="text-align:right">Amount</th>'
    + '</tr></thead><tbody>' + rows + '</tbody></table>';
}
function eventCard(ev) {
  return '<div class="event">'
    + '<div class="event-header">'
    + statusBadge(ev.status) + hashBadge(ev.valid_hash)
    + '<span class="badge b-default">' + (ev.payment_channel||'—') + '</span>'
    + '<span class="event-meta">' + ev.received_at + ' · ' + ev.invoice_number + '</span>'
    + '</div>'
    + '<div class="event-body">'
    + '<div class="grid3">'
    + '<div>'
    + kv('Phone number', ev.phone_number)
    + kv('Payment date', ev.payment_date)
    + kv('Payment channel', ev.payment_channel)
    + '</div><div>'
    + kv('Client invoice ref', ev.client_invoice_ref)
    + kv('Invoice number', ev.invoice_number)
    + kv('Currency', ev.currency)
    + '</div><div>'
    + kv('Invoice amount', ev.invoice_amount + ' TZS')
    + kv('Last payment amount', ev.last_payment_amount + ' TZS')
    + kv('Amount paid', '<strong style="color:var(--success)">' + ev.amount_paid + ' TZS</strong>')
    + '</div></div>'
    + '<hr class="divider"/>'
    + '<div style="font-size:11px;font-weight:700;color:var(--muted);text-transform:uppercase;letter-spacing:.07em;margin-bottom:8px">Payment references</div>'
    + refRows(ev.payment_references)
    + '<details><summary>Show raw JSON</summary>'
    + '<pre class="raw">' + JSON.stringify(ev.raw, null, 2) + '</pre>'
    + '</details></div></div>';
}
function render() {
  var q      = document.getElementById('f-search').value.toLowerCase();
  var status = document.getElementById('f-status').value.toLowerCase();
  var hash   = document.getElementById('f-hash').value;
  var f = allEvents;
  if (status) f = f.filter(function(e){ return e.status.toLowerCase() === status; });
  if (hash === 'valid')   f = f.filter(function(e){ return  e.valid_hash; });
  if (hash === 'invalid') f = f.filter(function(e){ return !e.valid_hash; });
  if (q) f = f.filter(function(e){
    return (e.invoice_number||'').toLowerCase().includes(q)
        || (e.client_invoice_ref||'').toLowerCase().includes(q)
        || (e.phone_number||'').toLowerCase().includes(q)
        || (e.payment_references||[]).some(function(r){ return (r.payment_reference||'').toLowerCase().includes(q); });
  });
  var feed = document.getElementById('feed');
  feed.innerHTML = f.length ? f.map(eventCard).join('') :
    '<div class="empty"><div class="icon">&#128237;</div>'
    + '<p>No notifications yet.<br/>Point your <code>notification_url</code> to <code>/notify</code> and complete a payment.</p></div>';
}
function poll() {
  fetch('/api/notifications')
    .then(function(r){ return r.json(); })
    .then(function(data){
      allEvents = data.events;
      var settled   = allEvents.filter(function(e){ return e.status.toLowerCase() === 'settled'; });
      var bad       = allEvents.filter(function(e){ return !e.valid_hash; });
      var paid      = settled.reduce(function(s,e){ try{ return s + parseFloat(e.amount_paid); } catch(x){ return s; } }, 0);
      document.getElementById('s-total').textContent   = allEvents.length;
      document.getElementById('s-settled').textContent = settled.length;
      document.getElementById('s-bad').textContent     = bad.length;
      document.getElementById('s-amount').textContent  = paid.toLocaleString('en-TZ',{minimumFractionDigits:2,maximumFractionDigits:2});
      render();
    }).catch(function(e){ console.error(e); });
}
function pollIPs() {
  fetch('/api/ips')
    .then(function(r){ return r.json(); })
    .then(function(data){
      var ips = data.ips;
      document.getElementById('ip-count').textContent =
        ips.length + ' IP' + (ips.length === 1 ? '' : 's') + ' observed';
      document.getElementById('ip-body').innerHTML = ips.length
        ? ips.map(function(r){
            return '<tr>'
              + '<td class="mono" style="color:var(--accent2)">' + r.ip_address + '</td>'
              + '<td>' + r.first_seen + '</td>'
              + '<td>' + r.last_seen + '</td>'
              + '<td style="text-align:center;font-weight:700">' + r.hit_count + '</td>'
              + '</tr>';
          }).join('')
        : '<tr><td colspan="4" style="text-align:center;color:var(--muted);padding:24px">No POST requests received yet</td></tr>';
    }).catch(function(e){ console.error(e); });
}
function clearAll()    { fetch('/api/notifications',{method:'DELETE'}).then(poll); }
function injectTest()  { fetch('/test-notify',{method:'POST'}).then(poll); }

poll();
pollIPs();
setInterval(poll,   5000);
setInterval(pollIPs,10000);
</script>
</body>
</html>
"""


# ─── Flask routes ──────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template_string(
        CHECKOUT_HTML,
        account_id=ACCOUNT_ID,
        flask_host=HOST,
        logs_url=f"{HOST}/logs/",
    )


@app.route("/monitor")
def monitor():
    return render_template_string(MONITOR_HTML)


@app.route("/notify", methods=["POST"])
def notify():
    ip = (
            request.headers.get("X-Forwarded-For", "").split(",")[0].strip()
            or request.remote_addr
            or "unknown"
    )
    record_ip(ip)
    try:
        payload = request.get_json(force=True, silent=True)
    except Exception:
        return "", 200
    if not payload:
        return "", 200

    events = payload if isinstance(payload, list) else [payload]
    sep = "─" * 54
    print(f"\n{sep}")
    print(f"  INCOMING WEBHOOK NOTIFICATION")
    print(f"  Source IP  : {ip}")
    print(f"  Events     : {len(events)}")
    print(sep)
    for i, event in enumerate(events, 1):
        ref = event.get("client_invoice_ref", "?")
        status = event.get("status", "?")
        amount = event.get("amount_paid", "?")
        invoice = event.get("invoice_number", "?")
        channel = event.get("payment_channel", "?")
        print(f"  Event {i}:")
        print(f"    Invoice    : {invoice} (ref: {ref})")
        print(f"    Status     : {status.upper()}")
        print(f"    Amount paid: {amount} TZS")
        print(f"    Channel    : {channel}")
        valid = validate_notification_hash(event)
        if valid:
            print(f"    Hash       : ✓ VALID")
        else:
            print(f"    Hash       : ✗ INVALID — possible tampering from {ip}")
            app.logger.warning(f"[HASH] Invalid secure_hash from {ip} — ref: {ref}")
        save_notification(event, valid)
    print(f"{sep}\n")
    return "", 200


@app.route("/test-notify", methods=["POST"])
def test_notify():
    sample = [{
        "status": "settled",
        "secure_hash": "TEST_HASH_NOT_VALID",
        "phone_number": "+255712345678",
        "payment_reference": [{
            "payment_reference": "CB26022010031969",
            "payment_date": "2026-02-20T14:11:38Z",
            "inserted_at": "2026-02-20T14:11:38",
            "currency": "TZS",
            "amount": "27000",
        }],
        "payment_date": "2026-02-20 17:11:38+03:00 EAT Africa/Dar_es_Salaam",
        "payment_channel": "NBC TZS PILOT",
        "last_payment_amount": "27000",
        "invoice_number": "CPAYKNEWZM",
        "invoice_amount": "27000.00",
        "currency": "TZS",
        "client_invoice_ref": "3T54G18",
        "amount_paid": "27000.00",
    }]
    for event in sample:
        save_notification(event, False)
    return jsonify({"injected": len(sample)}), 200


@app.route("/api/notifications", methods=["GET"])
def api_notifications():
    return jsonify({"events": get_notifications()})


@app.route("/api/notifications", methods=["DELETE"])
def api_clear():
    clear_notifications()
    return jsonify({"cleared": True})


@app.route("/api/ips", methods=["GET"])
def api_ips():
    with get_db() as db:
        rows = db.execute(
            "SELECT ip_address, first_seen, last_seen, hit_count "
            "FROM capitalpay_ips ORDER BY hit_count DESC"
        ).fetchall()
    return jsonify({"ips": [dict(r) for r in rows]})


@app.route("/go/<checkout_id>")
def hosted_checkout_redirect(checkout_id: str):
    params = _checkout_forms.get(checkout_id)
    if not params:
        return Response("Checkout session not found or expired.", status=404)
    try:
        html = fetch_checkout_page(params)
    except RuntimeError as exc:
        return Response(str(exc), status=502)
    return Response(html, mimetype="text/html")


@app.route("/checkout", methods=["POST"])
def checkout():
    data = request.get_json() or {}

    name = data.get("name", "")
    msisdn = data.get("msisdn", "")
    email = data.get("email", "")
    id_number = data.get("id_number", "")
    amount = data.get("amount", "0")
    bill_ref = data.get("bill_ref", "")
    desc = data.get("desc", "")
    callback_url = data.get("callback_url") or ""
    notif_url = data.get("notification_url") or ""
    account_id_str = str(ACCOUNT_ID)
    amount_str = f"{float(amount):.2f}"

    sep = "─" * 54
    print(f"\n{sep}")
    print(f"  NEW CHECKOUT REQUEST")
    print(sep)
    print(f"  Customer   : {name} | {msisdn} | {email}")
    print(f"  ID/Passport: {id_number}")
    print(f"  Bill ref   : {bill_ref}")
    print(f"  Description: {desc}")
    print(f"  Amount     : {amount_str} TZS")
    print(f"  Notif URL  : {notif_url or '(none)'}")
    print(f"  Callback   : {callback_url or '(none)'}")
    print(sep)

    print("  [1/5] Generating CapitalPay token…")
    token = generate_token()
    if not token:
        return jsonify({"error": "Failed to obtain API token. Check credentials."}), 500
    print(f"  [1/5] ✓ Token received (truncated): {token[:24]}…")

    print("  [2/5] Computing secure hash…")
    secure_hash = compute_secure_hash(
        api_client_id=account_id_str,
        amount=amount_str,
        service_id=account_id_str,
        client_id_number=id_number,
        bill_ref_number=bill_ref,
        bill_desc=desc,
        client_name=name,
    )
    print(f"  [2/5] ✓ secure_hash: {secure_hash[:32]}…")

    print("  [3/5] Creating invoice with CapitalPay API…")
    invoice_payload = {
        "account_id": account_id_str,
        "amount_expected": amount_str,
        "amount_settled_offline": 0,
        "callback_url": callback_url,
        "client_invoice_ref": bill_ref,
        "currency": CURRENCY,
        "email": email,
        "format": "json",
        "id_number": id_number,
        "items": [{
            "account_id": ACCOUNT_ID,
            "desc": desc,
            "item_ref": bill_ref,
            "price": amount_str,
            "quantity": "1",
            "require_settlement": "false",
        }],
        "msisdn": msisdn,
        "name": name,
        "notification_url": notif_url,
        "payment_gateway_id": 1,
        "send_stk": False,
    }

    try:
        result = create_invoice(token, invoice_payload)
    except Exception as e:
        print(f"  [3/5] ✗ Invoice creation failed: {e}")
        return jsonify({"error": f"Invoice creation failed: {e}"}), 500

    if result["kind"] != "json":
        print("  [3/5] ✗ Unexpected HTML response from invoice API")
        return jsonify({"error": "Unexpected HTML response from invoice API."}), 500

    api_data = result["data"]
    invoice_ref = extract_invoice_number(api_data) or bill_ref
    print(f"  [3/5] ✓ Invoice created: {invoice_ref}")

    print("  [4/5] Saving invoice meta to SQLite…")
    save_invoice_meta(
        client_invoice_ref=bill_ref,
        invoice_number=invoice_ref,
        amount=amount_str,
        name=name,
        id_number=id_number,
        desc=desc,
        msisdn=msisdn,
        email=email,
        secure_hash=secure_hash,
    )
    print(f"  [4/5] ✓ Saved — ref: {bill_ref}, hash stored for notification validation")

    print("  [5/5] Building checkout session…")
    checkout_params = build_checkout_params(
        name=name, msisdn=msisdn, email=email, id_number=id_number,
        amount=amount_str, bill_ref=bill_ref, desc=desc,
        callback_url=callback_url, notif_url=notif_url,
    )
    checkout_id = store_checkout_form(checkout_params)
    checkout_url = f"{HOST}/go/{checkout_id}"
    print(f"  [5/5] ✓ Checkout URL: {checkout_url}")
    print(f"{sep}\n")

    return jsonify({
        "invoice_ref": invoice_ref,
        "checkout_url": checkout_url,
        "api_response": api_data,
    })


# ─── Entry point ───────────────────────────────────────────────────────────────

# ─── Dash terminal UI (mounted on same Flask server at /logs/) ────────────────

def build_dash_app():
    try:
        import dash
        import dash_auth
        from dash import dcc, html, Input, Output
    except ImportError as e:
        print(f"\n  ⚠  Terminal UI disabled: {e}")
        print(f"  ⚠  Run:  pip install dash dash-auth")
        print(f"  ⚠  Then restart the app.\n")
        return None

    print("  ✓ dash + dash_auth found — mounting Terminal UI at /logs/")

    dash_app = dash.Dash(
        __name__,
        server=app,  # ← share the Flask server
        url_base_pathname="/logs/",
        title="CapitalPay · Terminal Logs",
        suppress_callback_exceptions=True,
    )

    dash_auth.BasicAuth(dash_app, {"CP": "CP123"})

    def stat_card(label, value, color="#e8eaf0"):
        return html.Div(style={
            "background": "#161a22", "border": "1px solid #252b38",
            "borderRadius": "10px", "padding": "10px 16px", "minWidth": "120px",
        }, children=[
            html.Div(label, style={"fontSize": "10px", "color": "#6b7280",
                                   "textTransform": "uppercase", "letterSpacing": ".06em",
                                   "fontFamily": "sans-serif", "marginBottom": "4px"}),
            html.Div(str(value), style={"fontSize": "22px", "fontWeight": "700",
                                        "color": color, "fontFamily": "sans-serif", "lineHeight": "1"}),
        ])

    dash_app.layout = html.Div(style={
        "background": "#0d0f14", "minHeight": "100vh",
        "fontFamily": "'Fira Mono','Courier New',monospace", "margin": "0", "padding": "0",
    }, children=[

        # ── Header ──────────────────────────────────────────────────────
        html.Div(style={
            "background": "#161a22", "borderBottom": "1px solid #252b38",
            "padding": "16px 28px", "display": "flex", "alignItems": "center", "gap": "14px",
        }, children=[
            html.Div("C", style={
                "width": "36px", "height": "36px", "flexShrink": "0",
                "background": "linear-gradient(135deg,#e05a1e,#f07a3a)",
                "borderRadius": "8px", "display": "flex", "alignItems": "center",
                "justifyContent": "center", "color": "#fff", "fontWeight": "700",
                "fontSize": "16px", "fontFamily": "sans-serif",
            }),
            html.Div([
                html.Span("Terminal Logs", style={
                    "color": "#e8eaf0", "fontWeight": "600", "fontSize": "15px",
                    "fontFamily": "sans-serif",
                }),
                html.Span(f" · Account {ACCOUNT_ID} · TZS", style={
                    "color": "#6b7280", "fontSize": "12px",
                    "fontFamily": "sans-serif", "marginLeft": "8px",
                }),
            ]),
            html.Div(style={"marginLeft": "auto", "display": "flex", "gap": "8px"}, children=[
                html.Span("● LIVE", id="live-badge", style={
                    "background": "rgba(34,197,94,.15)", "border": "1px solid rgba(34,197,94,.3)",
                    "color": "#22c55e", "borderRadius": "20px", "padding": "3px 12px",
                    "fontSize": "11px", "fontWeight": "700", "fontFamily": "sans-serif",
                }),
                html.A("← Checkout", href=f"{HOST}/", target="_blank", style={
                    "background": "rgba(224,90,30,.12)", "border": "1px solid rgba(224,90,30,.25)",
                    "color": "#f07a3a", "borderRadius": "20px", "padding": "3px 12px",
                    "fontSize": "11px", "fontWeight": "700", "textDecoration": "none",
                    "fontFamily": "sans-serif",
                }),
                html.A("📊 Monitor", href=f"{HOST}/monitor", target="_blank", style={
                    "background": "rgba(224,90,30,.12)", "border": "1px solid rgba(224,90,30,.25)",
                    "color": "#f07a3a", "borderRadius": "20px", "padding": "3px 12px",
                    "fontSize": "11px", "fontWeight": "700", "textDecoration": "none",
                    "fontFamily": "sans-serif",
                }),
            ]),
        ]),

        # ── Stats bar ────────────────────────────────────────────────────
        html.Div(id="stats-bar", style={
            "display": "flex", "gap": "12px", "padding": "16px 28px",
            "borderBottom": "1px solid #252b38", "flexWrap": "wrap",
        }),

        # ── Filter bar ───────────────────────────────────────────────────
        html.Div(style={
            "display": "flex", "gap": "10px", "padding": "12px 28px",
            "alignItems": "center", "borderBottom": "1px solid #252b38", "flexWrap": "wrap",
        }, children=[
            dcc.Input(
                id="filter-text", type="text",
                placeholder="Filter logs…", debounce=True,
                style={
                    "background": "#1e2330", "border": "1px solid #252b38",
                    "borderRadius": "8px", "color": "#e8eaf0", "padding": "7px 12px",
                    "fontSize": "12px", "fontFamily": "monospace", "width": "260px", "outline": "none",
                }
            ),
            dcc.Dropdown(
                id="filter-level",
                options=[
                    {"label": "All lines", "value": "all"},
                    {"label": "✓ VALID hashes", "value": "VALID"},
                    {"label": "✗ INVALID hashes", "value": "INVALID"},
                    {"label": "Webhook hits", "value": "WEBHOOK"},
                    {"label": "Checkout only", "value": "CHECKOUT"},
                    {"label": "IP events", "value": "[IP]"},
                    {"label": "Errors only", "value": "✗"},
                ],
                value="all", clearable=False,
                style={
                    "width": "200px", "fontSize": "12px",
                    "fontFamily": "sans-serif",
                }
            ),
            html.Span(id="line-count", style={
                "color": "#6b7280", "fontSize": "12px",
                "fontFamily": "sans-serif", "marginLeft": "auto",
            }),
            html.Button("Clear", id="btn-clear", n_clicks=0, style={
                "background": "rgba(239,68,68,.12)", "border": "1px solid rgba(239,68,68,.3)",
                "color": "#ef4444", "borderRadius": "8px", "padding": "6px 14px",
                "fontSize": "12px", "cursor": "pointer", "fontFamily": "sans-serif",
            }),
        ]),

        # ── Log output ───────────────────────────────────────────────────
        html.Div(id="log-output", style={
            "padding": "16px 28px",
            "minHeight": "calc(100vh - 220px)",
        }),

        dcc.Store(id="clear-rev", data=0),
        dcc.Interval(id="tick", interval=2000, n_intervals=0),
    ])

    # ── Callbacks ────────────────────────────────────────────────────────

    @dash_app.callback(
        Output("stats-bar", "children"),
        Output("log-output", "children"),
        Output("line-count", "children"),
        Input("tick", "n_intervals"),
        Input("clear-rev", "data"),
        Input("filter-text", "value"),
        Input("filter-level", "value"),
    )
    def refresh(_, clear_rev, ftext, flevel):
        lines = get_log_lines()

        total = len(lines)
        webhooks = sum(1 for l in lines if "INCOMING WEBHOOK" in l)
        checkouts = sum(1 for l in lines if "NEW CHECKOUT" in l)
        valid_h = sum(1 for l in lines if "✓ VALID" in l)
        invalid_h = sum(1 for l in lines if "✗ INVALID" in l or "✗ UNVERIFIABLE" in l)

        stats = [
            stat_card("Total lines", total),
            stat_card("Webhooks", webhooks, "#f07a3a"),
            stat_card("Checkouts", checkouts, "#f07a3a"),
            stat_card("Hash valid", valid_h, "#22c55e"),
            stat_card("Hash invalid", invalid_h, "#ef4444" if invalid_h else "#6b7280"),
        ]

        filtered = list(lines)
        if clear_rev:
            filtered = []
        if flevel and flevel != "all":
            filtered = [l for l in filtered if flevel in l]
        if ftext:
            q = ftext.lower()
            filtered = [l for l in filtered if q in l.lower()]

        count = f"{len(filtered):,} line{'s' if len(filtered) != 1 else ''}"

        if not filtered:
            output = html.Div(
                "No log lines yet — waiting for activity…",
                style={"color": "#6b7280", "fontSize": "13px",
                       "fontFamily": "sans-serif", "padding": "12px 0"},
            )
        else:
            def line_color(line):
                if any(x in line for x in ["✓ VALID", "[1/5] ✓", "[2/5] ✓", "[3/5] ✓", "[4/5] ✓", "[5/5] ✓"]):
                    return "#22c55e"
                if any(x in line for x in ["✗ INVALID", "✗ UNVERIFIABLE", "✗ Failed", "failed"]):
                    return "#ef4444"
                if any(x in line for x in ["INCOMING WEBHOOK", "NEW CHECKOUT REQUEST"]):
                    return "#f07a3a"
                if any(x in line for x in ["Source IP", "[IP]", "allowlist"]):
                    return "#a78bfa"
                if "─" * 8 in line or "=" * 8 in line:
                    return "#252b38"
                return "#8b949e"

            output = html.Div([
                html.Div(line, style={
                    "color": line_color(line),
                    "fontSize": "12px", "lineHeight": "1.8",
                    "whiteSpace": "pre-wrap", "wordBreak": "break-all",
                    "borderBottom": "1px solid rgba(37,43,56,.4)",
                    "padding": "1px 0",
                })
                for line in filtered
            ])

        return stats, output, count

    @dash_app.callback(
        Output("clear-rev", "data"),
        Input("btn-clear", "n_clicks"),
        prevent_initial_call=True,
    )
    def clear_logs(n):
        while not LOG_QUEUE.empty():
            try:
                LOG_QUEUE.get_nowait()
            except queue.Empty:
                break
        return n

    print(f"  ✓ Terminal UI ready at {HOST}/logs/  (user: CP  pass: CP123)")
    return dash_app


# ─── Entry point ───────────────────────────────────────────────────────────────

def open_browser():
    time.sleep(1.5)
    webbrowser.open(f"http://127.0.0.1:{LOCAL_PORT}")
    time.sleep(0.3)
    webbrowser.open(f"http://127.0.0.1:{LOCAL_PORT}/logs/")


if __name__ == "__main__":
    init_db()

    # Mount Dash on the same Flask app (single port, no ERR_CONNECTION_REFUSED)
    dash_result = build_dash_app()
    if dash_result is None:
        print("  Continuing without Terminal UI — install dash + dash-auth to enable it.")

    sep = "=" * 54
    print(f"\n{sep}")
    print(f"  CapitalPay Checkout  |  Account ID: {ACCOUNT_ID}  |  TZS")
    print(sep)
    print(f"  DB          →  {DB_PATH}")
    print(f"  Checkout    →  {HOST}/")
    print(f"  Monitor     →  {HOST}/monitor")
    print(f"  Webhook     →  {HOST}/notify")
    print(f"  Terminal UI →  {HOST}/logs/   (user: CP  pass: CP123)")
    print(sep)
    print(f"  ┌─ IP Allowlist Note ──────────────────────────────────┐")
    print(f"  │  Every IP that POSTs to /notify is printed here and  │")
    print(f"  │  saved to the DB. Check /monitor → Server IPs table  │")
    print(f"  │  after a real payment to get CapitalPay\'s IP.       │")
    print(f"  └──────────────────────────────────────────────────────┘")
    print(f"{sep}\n")

    is_render = bool(os.environ.get("RENDER_EXTERNAL_URL"))
    if not is_render:
        threading.Thread(target=open_browser, daemon=True).start()

    app.run(host="0.0.0.0", port=LOCAL_PORT, debug=False, use_reloader=False)