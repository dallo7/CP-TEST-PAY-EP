import os
import re
import uuid
import hashlib
import hmac
import base64
import webbrowser
import threading
import time

import requests
from flask import Flask, Response, jsonify, render_template_string, request


# API / checkout configuration.
BASE_URL = "https://app.capitalpay.co.tz/api"
CHECKOUT_URL = "https://app.capitalpay.co.tz/PaymentAPI/invoice/checkout"

ACCOUNT_ID = 48
API_KEY = os.environ.get("CAPITALPAY_API_KEY", "tmlLFcEcOy+e6ihv")
API_SECRET = os.environ.get("CAPITALPAY_API_SECRET", "Txe/Gd97FaH9jsuqrDsr9jaKuJhVm0A/")

CALLBACK_URL = "https://dummy-merchant.example.com/payment/callback"
NOTIFICATION_URL = os.environ.get("CAPITALPAY_NOTIFICATION_URL", "")
LOCAL_PORT = 5052

PUBLIC_HOST = "https://app.capitalpay.co.tz"
PRIVATE_HOSTS = (
    "https://192.168.92.110",
    "http://192.168.92.110",
)

app = Flask(__name__)
_checkout_forms: dict[str, dict[str, str]] = {}


def generate_token() -> str | None:
    """Fetch a fresh Bearer token from CapitalPay OAuth."""
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
        print(f"[Token Error] {e}")
        return None


def normalize_checkout_html(html: str) -> str:
    """Rewrite internal CapitalPay hosts so assets load in the browser."""
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
        r"PAYMENT REF[\s\S]*?<h2[^>]*>\s*([A-Z0-9]+)\s*</h2>",
        payload,
        re.I,
    )
    return match.group(1) if match else None


def compute_secure_hash(
    api_client_id: str,
    amount: str,
    service_id: str,
    client_id_number: str,
    currency: str,
    bill_ref_number: str,
    bill_desc: str,
    client_name: str,
) -> str:
    data_string = (
        api_client_id
        + amount
        + service_id
        + client_id_number
        + currency
        + bill_ref_number
        + bill_desc
        + client_name
        + API_SECRET
    )
    raw_hash = hmac.new(API_KEY.encode(), data_string.encode(), hashlib.sha256).digest()
    return base64.b64encode(raw_hash).decode()


def build_checkout_params(
    *,
    name: str,
    msisdn: str,
    email: str,
    id_number: str,
    amount: str,
    currency: str,
    bill_ref: str,
    desc: str,
    callback_url: str,
    notif_url: str,
) -> dict[str, str]:
    account_id_str = str(ACCOUNT_ID)
    amount_str = f"{float(amount):.2f}"
    params = {
        "apiClientID": account_id_str,
        "secureHash": compute_secure_hash(
            account_id_str,
            amount_str,
            account_id_str,
            id_number,
            currency,
            bill_ref,
            desc,
            name,
        ),
        "billDesc": desc,
        "billRefNumber": bill_ref,
        "currency": currency,
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


def default_notification_url() -> str:
    if NOTIFICATION_URL:
        return NOTIFICATION_URL
    return request.url_root.rstrip("/") + "/notify"


def store_checkout_form(params: dict[str, str]) -> str:
    checkout_id = uuid.uuid4().hex
    _checkout_forms[checkout_id] = params
    return checkout_id


def fetch_checkout_page(params: dict[str, str]) -> str:
    response = requests.post(CHECKOUT_URL, data=params, timeout=30)
    if not response.ok:
        raise RuntimeError(
            f"Checkout error ({response.status_code}): {response.text[:300]}"
        )
    return normalize_checkout_html(response.text)


def create_invoice(token: str, payload: dict) -> dict:
    """Call the CapitalPay Create Invoice endpoint."""
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
        return {
            "kind": "html",
            "html": html,
            "invoice_number": extract_invoice_number(html),
        }

    try:
        data = resp.json()
    except ValueError:
        if stripped.startswith("<"):
            html = normalize_checkout_html(text)
            return {
                "kind": "html",
                "html": html,
                "invoice_number": extract_invoice_number(html),
            }
        raise

    return {"kind": "json", "data": data}


HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>CapitalPay Checkout</title>
<link href="https://fonts.googleapis.com/css2?family=DM+Serif+Display&family=DM+Sans:wght@300;400;500;600&display=swap" rel="stylesheet"/>
<style>
  :root {
    --bg: #0d0f14;
    --surface: #161a22;
    --border: #252b38;
    --accent: #e05a1e;
    --accent2: #f07a3a;
    --text: #e8eaf0;
    --muted: #6b7280;
    --success: #22c55e;
    --error: #ef4444;
    --radius: 12px;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    background: var(--bg);
    color: var(--text);
    font-family: 'DM Sans', sans-serif;
    min-height: 100vh;
    display: flex;
    align-items: center;
    justify-content: center;
    padding: 24px;
    background-image:
      radial-gradient(ellipse 60% 40% at 80% 10%, rgba(224,90,30,.08) 0%, transparent 60%),
      radial-gradient(ellipse 40% 60% at 10% 90%, rgba(240,122,58,.05) 0%, transparent 60%);
  }
  .card {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 20px;
    width: 100%;
    max-width: 560px;
    overflow: hidden;
    box-shadow: 0 32px 64px rgba(0,0,0,.5);
  }
  .card-header {
    padding: 32px 36px 24px;
    border-bottom: 1px solid var(--border);
    display: flex;
    align-items: center;
    gap: 16px;
  }
  .logo-mark {
    width: 44px; height: 44px;
    background: linear-gradient(135deg, var(--accent), var(--accent2));
    border-radius: 10px;
    display: flex; align-items: center; justify-content: center;
    font-family: 'DM Serif Display', serif;
    font-size: 20px; color: #fff; flex-shrink: 0;
  }
  .card-title { font-family: 'DM Serif Display', serif; font-size: 22px; line-height: 1.2; }
  .card-sub { font-size: 13px; color: var(--muted); margin-top: 2px; }
  .card-body { padding: 32px 36px; }
  .field { margin-bottom: 18px; }
  label {
    display: block; font-size: 12px; font-weight: 600; letter-spacing: .06em;
    text-transform: uppercase; color: var(--muted); margin-bottom: 6px;
  }
  input, select {
    width: 100%;
    background: #1e2330;
    border: 1px solid var(--border);
    border-radius: var(--radius);
    color: var(--text);
    font-family: 'DM Sans', sans-serif;
    font-size: 14px;
    padding: 11px 14px;
    outline: none;
  }
  input:focus, select:focus { border-color: var(--accent); }
  select option { background: #1e2330; }
  .row2 { display: grid; grid-template-columns: 1fr 1fr; gap: 14px; }
  .btn {
    width: 100%; padding: 14px;
    background: linear-gradient(135deg, var(--accent), var(--accent2));
    border: none; border-radius: var(--radius);
    color: #fff; font-family: 'DM Sans', sans-serif;
    font-size: 15px; font-weight: 600;
    cursor: pointer; margin-top: 8px;
    transition: opacity .2s, transform .1s;
    letter-spacing: .02em;
  }
  .btn:hover { opacity: .9; }
  .btn:active { transform: scale(.98); }
  .btn:disabled { opacity: .5; cursor: not-allowed; }
  .status {
    margin-top: 18px; padding: 14px 16px;
    border-radius: var(--radius); font-size: 13px;
    display: none;
  }
  .status.show { display: block; }
  .status.info { background: rgba(224,90,30,.12); border: 1px solid rgba(224,90,30,.3); color: #f0a070; }
  .status.success { background: rgba(34,197,94,.1); border: 1px solid rgba(34,197,94,.3); color: var(--success); }
  .status.error { background: rgba(239,68,68,.1); border: 1px solid rgba(239,68,68,.3); color: var(--error); }
  .iframe-wrap {
    margin-top: 24px; border-radius: var(--radius);
    overflow: hidden; border: 1px solid var(--border);
    display: none;
  }
  .iframe-wrap iframe { width: 100%; min-height: 720px; border: none; background:#fff; }
  body.checkout-open { align-items: flex-start; padding-top: 16px; }
  body.checkout-open .card { max-width: 960px; }
  .spinner {
    display: inline-block; width: 14px; height: 14px;
    border: 2px solid rgba(255,255,255,.3);
    border-top-color: #fff;
    border-radius: 50%;
    animation: spin .7s linear infinite;
    margin-right: 8px; vertical-align: middle;
  }
  @keyframes spin { to { transform: rotate(360deg); } }
  .badge {
    display: inline-block; font-size: 11px; font-weight: 600;
    background: rgba(224,90,30,.2); color: var(--accent2);
    border-radius: 6px; padding: 2px 8px; margin-left: 8px;
    letter-spacing: .04em;
  }
  .section-title {
    font-size: 13px;
    color: var(--accent2);
    margin: 22px 0 12px;
    font-weight: 700;
    letter-spacing: .04em;
    text-transform: uppercase;
  }
</style>
</head>
<body>
<div class="card">
  <div class="card-header">
    <div class="logo-mark">C</div>
    <div>
      <div class="card-title">CapitalPay Checkout <span class="badge">Live</span></div>
      <div class="card-sub">Account ID: {{ account_id }} &nbsp;·&nbsp; Secure Payment Gateway</div>
    </div>
  </div>
  <div class="card-body">
    <form id="payForm">
      <div class="row2">
        <div class="field">
          <label>Full Name</label>
          <input name="name" type="text" placeholder="e.g. John Doe" required/>
        </div>
        <div class="field">
          <label>Phone (MSISDN)</label>
          <input name="msisdn" type="text" placeholder="+255712345678" required/>
        </div>
      </div>
      <div class="field">
        <label>Email (optional)</label>
        <input name="email" type="email" placeholder="you@example.com"/>
      </div>
      <div class="field">
        <label>ID / Passport Number</label>
        <input name="id_number" type="text" placeholder="National ID or passport" required/>
      </div>
      <div class="row2">
        <div class="field">
          <label>Amount</label>
          <input name="amount" type="number" step="0.01" min="1" placeholder="10.00" required/>
        </div>
        <div class="field">
          <label>Currency</label>
          <select name="currency">
            <option value="USD" selected>USD</option>
            <option value="TZS">TZS</option>
          </select>
        </div>
      </div>
      <div class="field">
        <label>Invoice / Bill Reference</label>
        <input name="bill_ref" type="text" placeholder="INV-2026-001" required/>
      </div>
      <div class="field">
        <label>Description</label>
        <input name="desc" type="text" placeholder="Payment description" required/>
      </div>
      <div class="section-title">Settlement Details</div>
      <div class="field">
        <label>Settlement Account Number</label>
        <input name="settlement_account_number" type="text" placeholder="Optional settlement account number"/>
      </div>
      <div class="field">
        <label>Settlement Description</label>
        <input name="settlement_desc" type="text" placeholder="Optional settlement description"/>
      </div>
      <div class="field">
        <label>Settlement Value</label>
        <input name="settlement_value" type="number" step="0.01" min="0.01" placeholder="Leave blank to use full amount"/>
      </div>
      <div class="field">
        <label>Callback URL (on success)</label>
        <input name="callback_url" type="url" value="{{ callback_url }}" placeholder="https://yoursite.com/success"/>
      </div>
      <div class="field">
        <label>Notification URL (IPN)</label>
        <input name="notification_url" type="url" value="{{ notification_url }}" placeholder="https://yoursite.com/ipn" required/>
      </div>
      <button class="btn" type="submit" id="submitBtn">Proceed to Payment</button>
    </form>
    <div class="status" id="statusBox"></div>
    <div class="iframe-wrap" id="iframeWrap">
      <iframe id="checkoutFrame" src="about:blank" title="CapitalPay Checkout"></iframe>
    </div>
  </div>
</div>
<script>
const form = document.getElementById('payForm');
const btn = document.getElementById('submitBtn');
const status = document.getElementById('statusBox');
const wrap = document.getElementById('iframeWrap');
const frame = document.getElementById('checkoutFrame');

function setStatus(msg, type) {
  status.className = `status show ${type}`;
  status.innerHTML = msg;
}

form.addEventListener('submit', async (e) => {
  e.preventDefault();
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner"></span>Creating Invoice...';
  setStatus('Generating token and creating invoice. Please wait...', 'info');
  wrap.style.display = 'none';

  const data = Object.fromEntries(new FormData(form).entries());

  try {
    const res = await fetch('/checkout', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(data)
    });
    const json = await res.json();

    if (!res.ok || json.error) {
      throw new Error(json.error || 'Unknown error from server');
    }

    if (json.checkout_url) {
      setStatus(
        `Invoice <strong>${json.invoice_ref || data.bill_ref}</strong> created. Opening CapitalPay checkout below...`,
        'success'
      );
      document.body.classList.add('checkout-open');
      wrap.style.display = 'block';
      frame.removeAttribute('srcdoc');
      frame.src = json.checkout_url;
    } else if (json.iframe_html) {
      document.body.classList.add('checkout-open');
      wrap.style.display = 'block';
      frame.removeAttribute('src');
      frame.srcdoc = json.iframe_html;
      setStatus('Invoice created. Complete your payment below.', 'success');
    } else {
      setStatus(`Invoice created. Reference: <strong>${json.invoice_ref || data.bill_ref}</strong>`, 'success');
    }
  } catch (err) {
    setStatus(`${err.message}`, 'error');
  } finally {
    btn.disabled = false;
    btn.textContent = 'Proceed to Payment';
  }
});
</script>
</body>
</html>
"""


@app.route("/")
def index():
    return render_template_string(
        HTML,
        account_id=ACCOUNT_ID,
        callback_url=CALLBACK_URL,
        notification_url=default_notification_url(),
    )


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

    token = generate_token()
    if not token:
        return jsonify({"error": "Failed to obtain API token. Check credentials."}), 500

    name = data.get("name", "")
    msisdn = data.get("msisdn", "")
    email = data.get("email", "")
    id_number = data.get("id_number", "")
    amount = data.get("amount", "0")
    currency = data.get("currency", "USD")
    bill_ref = data.get("bill_ref", "")
    desc = data.get("desc", "")
    callback_url = data.get("callback_url") or CALLBACK_URL
    notif_url = data.get("notification_url") or default_notification_url()
    settlement_account_number = (data.get("settlement_account_number") or "").strip()
    settlement_desc = (data.get("settlement_desc") or "").strip()
    settlement_value = (data.get("settlement_value") or "").strip()
    require_settlement_raw = str(data.get("require_settlement", "false")).strip().lower()
    require_settlement = require_settlement_raw in {"true", "1", "yes"}

    account_id_str = str(ACCOUNT_ID)
    amount_str = f"{float(amount):.2f}"
    if require_settlement and not (settlement_account_number and settlement_desc):
        return jsonify(
            {
                "error": (
                    "require_settlement is true — settlement account number and "
                    "settlement description are required."
                )
            }
        ), 400

    invoice_item = {
        "account_id": ACCOUNT_ID,
        "desc": desc,
        "item_ref": bill_ref,
        "price": amount_str,
        "quantity": "1",
        "require_settlement": "true" if require_settlement else "false",
    }
    if require_settlement:
        settlement_value_str = f"{float(settlement_value or amount_str):.2f}"
        invoice_item["settlements"] = [
            {
                "account_number": settlement_account_number,
                "desc": settlement_desc,
                "value": settlement_value_str,
            }
        ]

    invoice_payload = {
        "account_id": account_id_str,
        "amount_expected": amount_str,
        "amount_settled_offline": 0,
        "callback_url": callback_url,
        "client_invoice_ref": bill_ref,
        "currency": currency,
        "email": email,
        "format": "json",
        "id_number": id_number,
        "items": [invoice_item],
        "msisdn": msisdn,
        "name": name,
        "notification_url": notif_url,
        "payment_gateway_id": 1,
        "send_stk": False,
    }

    try:
        result = create_invoice(token, invoice_payload)
    except Exception as e:
        return jsonify({"error": f"Invoice creation failed: {e}"}), 500

    if result["kind"] != "json":
        return jsonify({"error": "Unexpected HTML response from invoice API."}), 500

    api_data = result["data"]
    invoice_ref = extract_invoice_number(api_data) or bill_ref
    checkout_params = build_checkout_params(
        name=name,
        msisdn=msisdn,
        email=email,
        id_number=id_number,
        amount=amount_str,
        currency=currency,
        bill_ref=bill_ref,
        desc=desc,
        callback_url=callback_url,
        notif_url=notif_url,
    )
    checkout_id = store_checkout_form(checkout_params)
    checkout_url = f"/go/{checkout_id}"

    return jsonify(
        {
            "invoice_ref": invoice_ref,
            "checkout_url": checkout_url,
            "api_response": api_data,
            "settlement_sent": invoice_item.get("settlements", []),
        }
    )


def open_browser():
    time.sleep(1.2)
    webbrowser.open(f"http://127.0.0.1:{LOCAL_PORT}")


if __name__ == "__main__":
    print("=" * 55)
    print("  CapitalPay Checkout  |  Account ID:", ACCOUNT_ID)
    print(f"  Opening http://127.0.0.1:{LOCAL_PORT} ...")
    print("=" * 55)
    threading.Thread(target=open_browser, daemon=True).start()
    app.run(host="127.0.0.1", port=LOCAL_PORT, debug=False)
