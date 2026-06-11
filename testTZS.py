import json
import os
import queue
import secrets
import sys
import threading
import time
import webbrowser
from datetime import datetime, timezone

from dash import Dash, Input, Output, State, callback_context, dcc, html
from flask import Response, jsonify, render_template_string, request

import capitalpay_notifications as notifications
import tcamsBankTest as capitalpay


capitalpay.ACCOUNT_ID = 46
capitalpay.LOCAL_PORT = int(os.environ.get("PORT", "5052"))

HOST = os.environ.get("RENDER_EXTERNAL_URL", f"http://127.0.0.1:{capitalpay.LOCAL_PORT}")
VALID_USERS = {"CP": "CP123"}
# Empty allowlist = accept and record traffic from any IP (current default).
CAPITALPAY_ALLOWED_IPS = {
    ip.strip()
    for ip in os.environ.get("CAPITALPAY_ALLOWED_IPS", "").split(",")
    if ip.strip()
}

DARK = "#0d0f14"
SURF = "#161a22"
BORD = "#252b38"
ACC = "#e05a1e"
ACC2 = "#f07a3a"
TEXT = "#e8eaf0"
MUTED = "#6b7280"
GREEN = "#22c55e"
RED = "#ef4444"
WARN = "#f59e0b"
PURP = "#a78bfa"
IPN_BLUE = "#60a5fa"
REQUEST_PAYLOAD = "[REQUEST-PAYLOAD]"
IPN_PAYLOAD = "[IPN-PAYLOAD]"
LOG_RULE = "=" * 72
LOG_DIV = "-" * 72

LAST_CP_OUTBOUND: dict = {
    "at": None,
    "invoice_url": None,
    "invoice_request": None,
    "invoice_response": None,
    "checkout_page_url": None,
    "checkout_params": None,
}

LOG_QUEUE: queue.Queue[str] = queue.Queue(maxsize=500)


def _record_cp_outbound(**fields):
    LAST_CP_OUTBOUND.update(fields)
    if fields.get("at") is None:
        LAST_CP_OUTBOUND["at"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


class TeeLogger:
    def __init__(self, real):
        self._real = real

    def write(self, msg: str):
        self._real.write(msg)
        if msg.strip():
            ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
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


if not isinstance(sys.stdout, TeeLogger):
    sys.stdout = TeeLogger(sys.stdout)


def get_log_lines() -> list[str]:
    items = list(LOG_QUEUE.queue)
    items.reverse()
    return items


def _mask(value, keep=6):
    if value in (None, ""):
        return value
    text = str(value)
    if len(text) <= keep * 2:
        return text[:2] + "..." if len(text) > 2 else "***"
    return f"{text[:keep]}...{text[-keep:]}"


def _redact(obj):
    if isinstance(obj, dict):
        redacted = {}
        for key, value in obj.items():
            lowered = str(key).lower()
            if any(word in lowered for word in ("secret", "key", "token", "authorization", "securehash", "secure_hash")):
                redacted[key] = _mask(value)
            elif isinstance(value, (dict, list)):
                redacted[key] = _redact(value)
            else:
                redacted[key] = value
        return redacted
    if isinstance(obj, list):
        return [_redact(item) for item in obj]
    return obj


def _pretty(obj, limit=3500):
    if isinstance(obj, str):
        text = obj
    else:
        text = json.dumps(_redact(obj), indent=2, ensure_ascii=False)
    if len(text) > limit:
        return text[:limit] + f"\n... truncated {len(text) - limit} chars"
    return text


def _maybe_unpack(value):
    if isinstance(value, dict):
        return {key: _maybe_unpack(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_maybe_unpack(item) for item in value]
    if not isinstance(value, str):
        return value

    stripped = value.strip()
    if not stripped:
        return value
    if stripped.startswith(("{", "[")):
        try:
            return _maybe_unpack(json.loads(stripped))
        except ValueError:
            return value
    return value


def _request_payload_parts():
    raw_body = request.get_data(as_text=True) or ""
    parts = {
        "query": request.args.to_dict(flat=False),
        "form": request.form.to_dict(flat=False),
        "json": request.get_json(silent=True) if request.is_json else None,
        "raw": raw_body,
    }
    if parts["json"] is None and raw_body.strip().startswith(("{", "[")):
        try:
            parts["json"] = json.loads(raw_body)
        except ValueError:
            pass
    return _maybe_unpack(parts), raw_body


def _first_payload_value(payload, *keys):
    wanted = {key.lower() for key in keys}

    def walk(value):
        if isinstance(value, dict):
            for key, item in value.items():
                if str(key).lower() in wanted and item not in (None, "", []):
                    if isinstance(item, list) and len(item) == 1:
                        return item[0]
                    return item
                found = walk(item)
                if found not in (None, "", []):
                    return found
        elif isinstance(value, list):
            for item in value:
                found = walk(item)
                if found not in (None, "", []):
                    return found
        return None

    found = walk(payload)
    return "" if found is None else str(found)


def _callback_status(payload):
    raw_status = _first_payload_value(
        payload,
        "status",
        "payment_status",
        "invoice_status",
        "transaction_status",
        "state",
    ).lower()
    if any(word in raw_status for word in ("settled", "paid", "success", "complete")):
        return "settled"
    if any(word in raw_status for word in ("failed", "cancel", "reject", "error")):
        return "failed"
    return "pending"


def default_callback_url():
    return request.url_root.rstrip("/") + "/callback"


def _record_callback_event(payload, raw_body):
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    source_ip = notifications.get_client_ip(request)
    notifications.record_observed_ip(
        source_ip,
        notifications.IP_ROLE_CLIENT,
        "/callback",
        now,
    )
    quality = notifications.assess_payload_quality(payload, raw_body)
    status = _callback_status(payload)
    invoice_number = _first_payload_value(payload, "invoice_number", "invoice_no", "invoice", "payment_ref")
    client_ref = _first_payload_value(payload, "client_invoice_ref", "bill_ref_number", "billRefNumber", "reference")
    amount_paid = _first_payload_value(payload, "amount_paid", "paid_amount", "amount", "total_paid") or "0.00"
    currency = _first_payload_value(payload, "currency") or "TZS"
    phone = _first_payload_value(payload, "phone_number", "msisdn", "clientMSISDN")
    payment_date = _first_payload_value(payload, "payment_date", "paid_at", "transaction_date")

    refs_json = json.dumps(
        [
            {
                "payment_reference": _first_payload_value(payload, "payment_reference", "payment_ref", "receipt"),
                "payment_date": payment_date,
                "currency": currency,
                "amount": amount_paid,
            }
        ],
        sort_keys=True,
    )
    payload_json = json.dumps(payload, sort_keys=True)
    headers_json = json.dumps(dict(request.headers), sort_keys=True)

    issues_json = json.dumps(quality["issues"], sort_keys=True)
    with notifications.get_db() as db:
        db.execute(
            """
            INSERT INTO notifications (
                received_at, ip_address, method, headers_json, raw_payload, payload_json,
                status, valid_hash, hash_value, expected_hash, invoice_number,
                client_invoice_ref, phone_number, payment_date, payment_channel,
                currency, invoice_amount, last_payment_amount, amount_paid,
                payment_references_json, payload_quality, ip_allowed, quality_issues_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                now,
                source_ip,
                request.method,
                headers_json,
                raw_body,
                payload_json,
                status,
                1,
                "",
                "",
                invoice_number,
                client_ref,
                phone,
                payment_date,
                "CALLBACK",
                currency,
                _first_payload_value(payload, "invoice_amount", "amount_expected") or amount_paid,
                amount_paid,
                amount_paid,
                refs_json,
                quality["quality"],
                1,
                issues_json,
            ),
        )

    return {
        "received_at": now,
        "ip_address": source_ip,
        "status": status,
        "invoice_number": invoice_number,
        "client_invoice_ref": client_ref,
        "amount_paid": amount_paid,
        "currency": currency,
        "payment_channel": "CALLBACK",
    }


def _print_payload_lines(content, kind: str, indent: str = "    "):
    tag = REQUEST_PAYLOAD if kind == "request" else IPN_PAYLOAD
    for line in _pretty(content).splitlines() or [""]:
        print(f"{indent}{tag} {line}")


def _log_session_start(label: str, detail: str = ""):
    print("")
    print(LOG_RULE)
    extra = f"  |  {detail}" if detail else ""
    print(f"  SESSION START  |  {label}{extra}")
    print(LOG_RULE)


def _log_request_marker(note: str = ""):
    print(LOG_DIV)
    print(f"  >>> REQUEST{('  |  ' + note) if note else ''}")
    print(LOG_DIV)


def _log_response_marker(status: str = "", note: str = ""):
    print(LOG_DIV)
    bits = ["<<< RESPONSE"]
    if status:
        bits.append(status)
    if note:
        bits.append(note)
    print(f"  {'  |  '.join(bits)}")
    print(LOG_DIV)


def _log_summary(title: str):
    print(LOG_DIV)
    print(f"  ### {title}")
    print(LOG_DIV)


def _log_session_end(label: str, outcome: str = "COMPLETE"):
    print(LOG_DIV)
    print(f"  SESSION END  |  {label}  |  {outcome}")
    print(LOG_RULE)
    print("")


def _log_detail_block(label: str, value, payload_kind: str | None = None):
    payload_labels = {"JSON", "Form Data", "Raw Body", "Parsed JSON", "Body", "HTML Preview"}
    print(f"  {label}:")
    if payload_kind and label in payload_labels:
        _print_payload_lines(value, payload_kind)
    else:
        for line in _pretty(value).splitlines() or [""]:
            print(f"    {line}")


def _log_block(title, details, payload_kind: str | None = None):
    _log_request_marker(title)
    for label, value in details:
        _log_detail_block(label, value, payload_kind)


def _install_verbose_capitalpay_logging():
    original_generate_token = capitalpay.generate_token
    original_fetch_checkout_page = capitalpay.fetch_checkout_page

    def verbose_generate_token():
        url = f"{capitalpay.BASE_URL}/oauth/generate/token"
        payload = {"key": capitalpay.API_KEY, "secret": capitalpay.API_SECRET}
        headers = {"Content-Type": "application/json"}
        _log_session_start("CAPITALPAY TOKEN", f"POST {url}")
        _log_block("OUTGOING CAPITALPAY TOKEN REQUEST", [("POST", url), ("Headers", headers), ("JSON", payload)])
        try:
            resp = capitalpay.requests.post(url, json=payload, headers=headers, timeout=30)
            body_text = resp.text or ""
            response_body = body_text
            try:
                response_body = resp.json()
            except ValueError:
                pass
            _log_response_marker(f"HTTP {resp.status_code} {resp.reason}")
            for label, value in [
                ("Status", f"{resp.status_code} {resp.reason}"),
                ("Headers", dict(resp.headers)),
                ("Body", response_body),
            ]:
                _log_detail_block(label, value)
            resp.raise_for_status()
            token = resp.json().get("token")
            print(f"  [TOKEN] Received bearer token: {_mask(token)}")
            _log_session_end("CAPITALPAY TOKEN", "OK")
            return token
        except Exception as exc:
            print(f"  [TOKEN] Failed: {exc}")
            _log_session_end("CAPITALPAY TOKEN", "FAILED")
            return original_generate_token()

    def verbose_create_invoice(token, payload):
        url = f"{capitalpay.BASE_URL}/invoice/create"
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json, text/html;q=0.9, */*;q=0.8",
        }
        _log_session_start("CAPITALPAY INVOICE", f"POST {url}")
        _record_cp_outbound(
            at=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
            invoice_url=url,
            invoice_request=_redact(payload),
            invoice_response=None,
        )
        _log_block(
            "OUTGOING CAPITALPAY INVOICE REQUEST",
            [("POST", url), ("Headers", headers), ("JSON", payload)],
            payload_kind="request",
        )
        try:
            resp = capitalpay.requests.post(url, headers=headers, json=payload, timeout=30)
            text = resp.text or ""
            response_body = text
            try:
                response_body = resp.json()
            except ValueError:
                pass
            _log_response_marker(f"HTTP {resp.status_code} {resp.reason}")
            for label, value in [
                ("Status", f"{resp.status_code} {resp.reason}"),
                ("Headers", dict(resp.headers)),
                ("Body", response_body),
            ]:
                _log_detail_block(label, value)
            _record_cp_outbound(invoice_response=_redact(response_body))
            resp.raise_for_status()

            content_type = (resp.headers.get("Content-Type") or "").lower()
            stripped = text.lstrip()
            if "text/html" in content_type or stripped.startswith(("<!DOCTYPE", "<html")):
                html_text = capitalpay.normalize_checkout_html(text)
                _log_session_end("CAPITALPAY INVOICE", "OK (HTML)")
                return {
                    "kind": "html",
                    "html": html_text,
                    "invoice_number": capitalpay.extract_invoice_number(html_text),
                }

            try:
                data = resp.json()
            except ValueError:
                if stripped.startswith("<"):
                    html_text = capitalpay.normalize_checkout_html(text)
                    _log_session_end("CAPITALPAY INVOICE", "OK (HTML)")
                    return {
                        "kind": "html",
                        "html": html_text,
                        "invoice_number": capitalpay.extract_invoice_number(html_text),
                    }
                raise
            _log_session_end("CAPITALPAY INVOICE", "OK")
            return {"kind": "json", "data": data}
        except Exception as exc:
            print(f"  [INVOICE] Failed after logged request/response: {exc}")
            _log_session_end("CAPITALPAY INVOICE", "FAILED")
            raise

    def verbose_fetch_checkout_page(params):
        _record_cp_outbound(
            checkout_page_url=capitalpay.CHECKOUT_URL,
            checkout_params=_redact(params),
        )
        _log_session_start("CAPITALPAY CHECKOUT PAGE", f"POST {capitalpay.CHECKOUT_URL}")
        _log_block(
            "OUTGOING CAPITALPAY CHECKOUT PAGE REQUEST",
            [("POST", capitalpay.CHECKOUT_URL), ("Form Data", params)],
            payload_kind="request",
        )
        try:
            resp = capitalpay.requests.post(capitalpay.CHECKOUT_URL, data=params, timeout=30)
            _log_response_marker(f"HTTP {resp.status_code} {resp.reason}")
            for label, value in [
                ("Status", f"{resp.status_code} {resp.reason}"),
                ("Headers", dict(resp.headers)),
                ("HTML Preview", resp.text or ""),
            ]:
                _log_detail_block(label, value)
            if not resp.ok:
                _log_session_end("CAPITALPAY CHECKOUT PAGE", "FAILED")
                raise RuntimeError(f"Checkout error ({resp.status_code}): {resp.text[:300]}")
            _log_session_end("CAPITALPAY CHECKOUT PAGE", "OK")
            return capitalpay.normalize_checkout_html(resp.text)
        except Exception as exc:
            print(f"  [CHECKOUT PAGE] Failed after logged request/response: {exc}")
            _log_session_end("CAPITALPAY CHECKOUT PAGE", "FAILED")
            return original_fetch_checkout_page(params)

    capitalpay.generate_token = verbose_generate_token
    capitalpay.create_invoice = verbose_create_invoice
    capitalpay.fetch_checkout_page = verbose_fetch_checkout_page


_install_verbose_capitalpay_logging()


# Gunicorn uses this variable on Render: gunicorn testTZS:server
server = capitalpay.app
server.secret_key = os.environ.get("FLASK_SECRET", "cp-secret-2026-ttcams")

app = Dash(
    __name__,
    server=server,
    url_base_pathname="/dash/",
    title="CapitalPay Dashboard",
    suppress_callback_exceptions=True,
    external_stylesheets=[
        "https://fonts.googleapis.com/css2?family=DM+Serif+Display&family=DM+Sans:wght@300;400;500;600&display=swap"
    ],
)
notifications.init_db()


@server.before_request
def protect_dashboard_routes():
    if not request.path.startswith("/dash/"):
        return None

    auth = request.authorization
    if auth and secrets.compare_digest(auth.username, "CP") and secrets.compare_digest(auth.password, VALID_USERS["CP"]):
        return None

    return Response(
        "Authentication required",
        401,
        {"WWW-Authenticate": 'Basic realm="CapitalPay Dashboard"'},
    )


def _rgba(hex_color, alpha):
    r, g, b = (int(hex_color.lstrip("#")[i : i + 2], 16) for i in (0, 2, 4))
    return f"rgba({r},{g},{b},{alpha})"


def stat_card(label, value_id, color=TEXT, initial="0"):
    return html.Div(
        style={
            "background": SURF,
            "border": f"1px solid {BORD}",
            "borderRadius": "13px",
            "padding": "16px 18px",
            "minWidth": "120px",
        },
        children=[
            html.Div(
                label,
                style={
                    "fontSize": "10px",
                    "color": MUTED,
                    "fontWeight": "600",
                    "textTransform": "uppercase",
                    "letterSpacing": ".07em",
                    "marginBottom": "5px",
                },
            ),
            html.Div(
                initial,
                id=value_id,
                style={"fontSize": "24px", "fontWeight": "700", "color": color, "lineHeight": "1"},
            ),
        ],
    )


def badge(text, color=MUTED):
    return html.Span(
        text,
        style={
            "background": _rgba(color, 0.15),
            "border": f"1px solid {_rgba(color, 0.3)}",
            "color": color,
            "borderRadius": "20px",
            "padding": "3px 10px",
            "fontSize": "11px",
            "fontWeight": "700",
            "textTransform": "uppercase",
            "letterSpacing": ".04em",
            "marginRight": "6px",
        },
    )


def kv_item(label, value):
    return html.Div(
        style={"padding": "4px 0"},
        children=[
            html.Dt(
                label,
                style={
                    "fontSize": "10px",
                    "color": MUTED,
                    "fontWeight": "600",
                    "textTransform": "uppercase",
                    "letterSpacing": ".06em",
                },
            ),
            html.Dd(
                str(value) if value not in (None, "") else "-",
                style={
                    "fontSize": "12px",
                    "color": TEXT,
                    "fontFamily": "monospace",
                    "marginTop": "1px",
                },
            ),
        ],
    )


def nav_link(text, href):
    return html.A(
        text,
        href=href,
        target="_blank",
        style={
            "background": _rgba(ACC, 0.12),
            "border": f"1px solid {_rgba(ACC, 0.25)}",
            "color": ACC2,
            "borderRadius": "20px",
            "padding": "3px 12px",
            "fontSize": "11px",
            "fontWeight": "700",
            "textDecoration": "none",
        },
    )


def tab_style(selected=False):
    return {
        "padding": "10px 24px",
        "fontFamily": "DM Sans, sans-serif",
        "fontSize": "13px",
        "fontWeight": "600",
        "color": TEXT if selected else MUTED,
        "background": SURF if selected else "transparent",
        "border": "none",
        "borderBottom": f"2px solid {ACC}" if selected else "2px solid transparent",
        "cursor": "pointer",
    }


def filter_bar(*children):
    return html.Div(
        style={
            "display": "flex",
            "gap": "10px",
            "flexWrap": "wrap",
            "alignItems": "center",
            "background": SURF,
            "border": f"1px solid {BORD}",
            "borderRadius": "11px",
            "padding": "12px 16px",
            "marginBottom": "16px",
        },
        children=list(children),
    )


def action_btn(label, btn_id, color=RED):
    return html.Button(
        label,
        id=btn_id,
        n_clicks=0,
        style={
            "background": _rgba(color, 0.12),
            "border": f"1px solid {_rgba(color, 0.3)}",
            "color": color,
            "borderRadius": "8px",
            "padding": "6px 14px",
            "fontSize": "12px",
            "cursor": "pointer",
        },
    )


def line_color(line):
    if "SESSION START" in line:
        return ACC2
    if "SESSION END" in line:
        return GREEN
    if ">>> REQUEST" in line:
        return ACC2
    if "<<< RESPONSE" in line:
        return GREEN
    if line.strip() == LOG_RULE or (len(line.strip()) >= 20 and set(line.strip()) == {"="}):
        return BORD
    if "###" in line:
        return PURP
    if REQUEST_PAYLOAD in line:
        return ACC2
    if IPN_PAYLOAD in line:
        return IPN_BLUE
    if "Payload quality : GOOD" in line:
        return GREEN
    if "Payload quality : BAD" in line or "Payload quality : PARTIAL" in line:
        return RED if "BAD" in line else WARN
    if "IP allowlist    : UNKNOWN" in line:
        return WARN
    if "[BLOCKED]" in line or "DENY" in line or "Blocked source IP" in line:
        return WARN
    if "Hash       : VALID" in line or "paid=True" in line:
        return GREEN
    if "INVALID" in line or "UNVERIFIABLE" in line or "failed" in line.lower():
        return RED
    if "INCOMING" in line or "OUTGOING" in line or "CHECKOUT SUMMARY" in line:
        return ACC2
    if "Source IP" in line or "[IP]" in line or "allowlist" in line or "whitelist" in line:
        return PURP
    return "#8b949e"


def log_line_style(line):
    style = {
        "color": line_color(line),
        "fontSize": "12px",
        "lineHeight": "1.8",
        "whiteSpace": "pre-wrap",
        "wordBreak": "break-all",
        "borderBottom": "1px solid rgba(37,43,56,.4)",
        "padding": "2px 8px",
        "borderLeft": "3px solid transparent",
    }
    stripped = line.strip()
    if stripped == LOG_RULE or (len(stripped) >= 20 and set(stripped) == {"="}):
        style.update(
            {
                "color": ACC,
                "background": "#0f1218",
                "borderBottom": "none",
                "borderTop": f"2px solid {ACC}",
                "lineHeight": "1.2",
                "padding": "6px 8px",
                "fontWeight": "700",
            }
        )
    elif stripped == LOG_DIV or (len(stripped) >= 20 and set(stripped) == {"-"}):
        style.update(
            {
                "color": BORD,
                "background": "#11151d",
                "borderBottom": "none",
                "lineHeight": "1",
            }
        )
    elif "SESSION START" in line:
        style.update(
            {
                "background": "rgba(224,90,30,.18)",
                "borderLeft": f"4px solid {ACC}",
                "color": ACC2,
                "fontWeight": "800",
                "fontSize": "13px",
                "padding": "8px 10px",
            }
        )
    elif "SESSION END" in line:
        style.update(
            {
                "background": "rgba(34,197,94,.14)",
                "borderLeft": f"4px solid {GREEN}",
                "color": GREEN,
                "fontWeight": "800",
                "fontSize": "13px",
                "padding": "8px 10px",
            }
        )
    elif ">>> REQUEST" in line:
        style.update(
            {
                "background": "rgba(240,122,58,.20)",
                "borderLeft": f"4px solid {ACC2}",
                "color": ACC2,
                "fontWeight": "800",
                "fontSize": "12px",
                "padding": "6px 10px",
            }
        )
    elif "<<< RESPONSE" in line:
        style.update(
            {
                "background": "rgba(34,197,94,.16)",
                "borderLeft": f"4px solid {GREEN}",
                "color": GREEN,
                "fontWeight": "800",
                "fontSize": "12px",
                "padding": "6px 10px",
            }
        )
    elif "###" in line:
        style.update(
            {
                "background": "rgba(167,139,250,.12)",
                "borderLeft": f"3px solid {PURP}",
                "color": PURP,
                "fontWeight": "700",
            }
        )
    elif "[BLOCKED]" in line or "Decision: DENY" in line:
        style.update(
            {
                "background": "rgba(245,158,11,.12)",
                "borderLeft": f"3px solid {WARN}",
                "color": WARN,
                "fontWeight": "700",
            }
        )
    elif "INCOMING WEBHOOK" in line or "INCOMING CALLBACK" in line:
        style.update(
            {
                "background": "rgba(240,122,58,.14)",
                "borderLeft": f"3px solid {ACC2}",
                "color": ACC2,
                "fontWeight": "700",
            }
        )
    elif "OUTGOING" in line or "CHECKOUT SUMMARY" in line or "WEBHOOK SUMMARY" in line or "CALLBACK SUMMARY" in line:
        style.update(
            {
                "background": "rgba(167,139,250,.10)",
                "borderLeft": f"3px solid {PURP}",
                "color": PURP,
                "fontWeight": "700",
            }
        )
    elif REQUEST_PAYLOAD in line:
        style.update(
            {
                "background": "rgba(240,122,58,.16)",
                "borderLeft": f"3px solid {ACC2}",
                "color": ACC2,
                "fontWeight": "600",
            }
        )
    elif IPN_PAYLOAD in line:
        style.update(
            {
                "background": "rgba(96,165,250,.14)",
                "borderLeft": f"3px solid {IPN_BLUE}",
                "color": IPN_BLUE,
                "fontWeight": "600",
            }
        )
    elif any(label in line for label in ("Headers:", "Request:", "Body:")) and REQUEST_PAYLOAD not in line and IPN_PAYLOAD not in line:
        style.update(
            {
                "background": "rgba(37,43,56,.45)",
                "borderLeft": f"3px solid {BORD}",
                "color": TEXT,
                "fontWeight": "700",
            }
        )
    elif "Responded HTTP 200" in line:
        style.update({"background": "rgba(34,197,94,.08)", "borderLeft": f"3px solid {GREEN}"})
    return style


def monitor_layout():
    return html.Div(
        [
            html.Div(
                style={"display": "flex", "gap": "12px", "flexWrap": "wrap", "marginBottom": "20px"},
                children=[
                    stat_card("Total received", "s-total"),
                    stat_card("Good payloads", "s-good", GREEN),
                    stat_card("Bad / partial", "s-bad", RED),
                    stat_card("Settled", "s-settled", GREEN),
                    stat_card("Total paid (TZS)", "s-amount", initial="0.00"),
                ],
            ),
            filter_bar(
                dcc.Input(
                    id="m-search",
                    type="text",
                    debounce=True,
                    placeholder="Search invoice, ref, phone, IP...",
                    style={
                        "background": "#1e2330",
                        "border": f"1px solid {BORD}",
                        "borderRadius": "8px",
                        "color": TEXT,
                        "padding": "6px 12px",
                        "fontSize": "12px",
                        "flex": "1",
                        "minWidth": "180px",
                        "outline": "none",
                    },
                ),
                dcc.Dropdown(
                    id="m-status",
                    value="",
                    clearable=False,
                    options=[
                        {"label": "All statuses", "value": ""},
                        {"label": "Settled", "value": "settled"},
                        {"label": "Pending", "value": "pending"},
                        {"label": "Failed", "value": "failed"},
                    ],
                    style={"width": "160px", "fontSize": "12px"},
                ),
                dcc.Dropdown(
                    id="m-hash",
                    value="",
                    clearable=False,
                    options=[
                        {"label": "All", "value": ""},
                        {"label": "Hash valid", "value": "valid"},
                        {"label": "Hash invalid", "value": "invalid"},
                    ],
                    style={"width": "150px", "fontSize": "12px"},
                ),
                dcc.Dropdown(
                    id="m-quality",
                    value="",
                    clearable=False,
                    options=[
                        {"label": "All payloads", "value": ""},
                        {"label": "Good payload", "value": "GOOD"},
                        {"label": "Partial payload", "value": "PARTIAL"},
                        {"label": "Bad payload", "value": "BAD"},
                        {"label": "Unknown IP", "value": "unknown_ip"},
                    ],
                    style={"width": "170px", "fontSize": "12px"},
                ),
                action_btn("Inject test event", "btn-test-event", ACC2),
                action_btn("Clear all", "btn-clear-notif", RED),
            ),
            html.Div(id="cp-outbound-panel"),
            html.Div(id="m-feed"),
            html.Div(
                style={"marginTop": "36px"},
                children=[
                    html.Div(
                        style={
                            "display": "flex",
                            "alignItems": "center",
                            "gap": "12px",
                            "marginBottom": "12px",
                        },
                        children=[
                            html.H2(
                                "IP Traffic Directory",
                                style={
                                    "fontFamily": "DM Serif Display,serif",
                                    "fontSize": "17px",
                                    "color": TEXT,
                                },
                            ),
                            html.Span("0 IPs tracked", id="ip-count", style={"fontSize": "12px", "color": MUTED}),
                            html.Span(
                                "Persisted permanently — no auto-clear",
                                style={"marginLeft": "auto", "fontSize": "11px", "color": MUTED},
                            ),
                        ],
                    ),
                    html.Div(id="ip-table"),
                ],
            ),
        ]
    )


def logs_layout():
    return html.Div(
        [
            html.Div(
                style={"display": "flex", "gap": "12px", "flexWrap": "wrap", "marginBottom": "20px"},
                children=[
                    stat_card("Total lines", "l-total"),
                    stat_card("Webhooks", "l-webhooks", ACC2),
                    stat_card("Checkouts", "l-checkouts", ACC2),
                    stat_card("Hash valid", "l-valid", GREEN),
                    stat_card("Hash invalid", "l-invalid", RED),
                    stat_card("Issues", "l-blocked", WARN),
                ],
            ),
            filter_bar(
                dcc.Input(
                    id="l-search",
                    type="text",
                    debounce=True,
                    placeholder="Filter logs...",
                    style={
                        "background": "#1e2330",
                        "border": f"1px solid {BORD}",
                        "borderRadius": "8px",
                        "color": TEXT,
                        "padding": "6px 12px",
                        "fontSize": "12px",
                        "fontFamily": "monospace",
                        "width": "240px",
                        "outline": "none",
                    },
                ),
                dcc.Dropdown(
                    id="l-level",
                    value="all",
                    clearable=False,
                    options=[
                        {"label": "All lines", "value": "all"},
                        {"label": "VALID hashes", "value": "VALID"},
                        {"label": "INVALID hashes", "value": "INVALID"},
                        {"label": "Webhook hits", "value": "WEBHOOK"},
                        {"label": "Session starts", "value": "SESSION START"},
                        {"label": "Requests", "value": ">>> REQUEST"},
                        {"label": "Responses", "value": "<<< RESPONSE"},
                        {"label": "Session ends", "value": "SESSION END"},
                        {"label": "Request payloads (orange)", "value": REQUEST_PAYLOAD},
                        {"label": "IPN payloads (blue)", "value": IPN_PAYLOAD},
                        {"label": "Checkout only", "value": "CHECKOUT"},
                        {"label": "IP events", "value": "[IP]"},
                        {"label": "Bad payloads", "value": "Payload quality : BAD"},
                        {"label": "Unknown IPs", "value": "UNKNOWN IP"},
                    ],
                    style={"width": "180px", "fontSize": "12px"},
                ),
                html.Span("0 lines", id="l-count", style={"color": MUTED, "fontSize": "12px", "marginLeft": "auto"}),
                action_btn("Clear", "btn-clear-logs", RED),
            ),
            html.Div(
                id="l-feed",
                style={
                    "background": "#0a0c10",
                    "border": f"1px solid {BORD}",
                    "borderRadius": "12px",
                    "padding": "16px 20px",
                    "minHeight": "500px",
                    "fontFamily": "'Fira Mono','Courier New',monospace",
                    "fontSize": "12px",
                },
            ),
        ]
    )


def _json_from_event(value, fallback):
    if not value:
        return fallback
    try:
        return _maybe_unpack(json.loads(value))
    except (TypeError, ValueError):
        return value


def captured_payload_panel(ev):
    parsed_payload = _json_from_event(ev.get("payload_json"), {})
    raw_payload = ev.get("raw_payload") or ""
    headers = _json_from_event(ev.get("headers_json"), {})
    box_style = {
        "background": "#0a0c10",
        "border": f"1px solid {BORD}",
        "borderRadius": "10px",
        "padding": "12px",
        "color": "#c9d1d9",
        "fontFamily": "'Fira Mono','Courier New',monospace",
        "fontSize": "11px",
        "lineHeight": "1.55",
        "whiteSpace": "pre-wrap",
        "wordBreak": "break-word",
        "maxHeight": "320px",
        "overflow": "auto",
    }
    label_style = {
        "fontSize": "10px",
        "color": MUTED,
        "fontWeight": "700",
        "textTransform": "uppercase",
        "letterSpacing": ".07em",
        "margin": "12px 0 6px",
    }

    quality = (ev.get("payload_quality") or "UNKNOWN").upper()
    q_color = {"GOOD": GREEN, "PARTIAL": WARN, "BAD": RED}.get(quality, MUTED)
    issues = ev.get("quality_issues") or []
    missing = [issue.replace("missing ", "") for issue in issues]

    return html.Details(
        style={
            "marginTop": "14px",
            "background": "#10131a",
            "border": f"1px solid {BORD}",
            "borderRadius": "12px",
            "padding": "10px 12px",
        },
        children=[
            html.Summary(
                "Captured INBOUND payload (from bank / browser — not your CP invoice request)",
                style={
                    "color": ACC2,
                    "fontSize": "12px",
                    "fontWeight": "700",
                    "cursor": "pointer",
                    "letterSpacing": ".03em",
                },
            ),
            html.Div(
                style={
                    "display": "flex",
                    "gap": "8px",
                    "flexWrap": "wrap",
                    "margin": "10px 0 4px",
                },
                children=[
                    badge(f"Payload {quality}", q_color),
                    badge(
                        "IP on allowlist" if ev.get("ip_allowed", True) else "Unknown IP",
                        GREEN if ev.get("ip_allowed", True) else WARN,
                    ),
                ],
            ),
            html.Div(
                [
                    html.Span(
                        "Missing required fields: " if missing else "Required fields: ",
                        style={"color": MUTED, "fontSize": "11px"},
                    ),
                    html.Span(
                        ", ".join(missing) if missing else "all present (status, invoice_number, client_invoice_ref, amount_paid, currency, payment_reference)",
                        style={"color": RED if missing else GREEN, "fontSize": "11px", "fontWeight": "600"},
                    ),
                ],
                style={"marginBottom": "8px"},
            ),
            html.Div("Parsed / unpacked payload", style=label_style),
            html.Pre(_pretty(parsed_payload, limit=8000), style=box_style),
            html.Div("Raw body", style=label_style),
            html.Pre(raw_payload or "-", style=box_style),
            html.Div("Request headers", style=label_style),
            html.Pre(_pretty(headers, limit=8000), style=box_style),
        ],
    )


def event_feed(events):
    if not events:
        return html.Div(
            style={
                "textAlign": "center",
                "padding": "36px 24px 40px",
                "borderRadius": "13px",
                "border": f"1px solid {BORD}",
                "background": (
                    f"linear-gradient(145deg, rgba(96,165,250,.12) 0%, rgba(167,139,250,.10) 45%, "
                    f"rgba(240,122,58,.08) 100%)"
                ),
                "boxShadow": "inset 0 0 0 1px rgba(96,165,250,.12)",
            },
            children=[
                html.Div(
                    style={
                        "display": "flex",
                        "justifyContent": "center",
                        "gap": "8px",
                        "flexWrap": "wrap",
                        "marginBottom": "18px",
                    },
                    children=[
                        badge("No IPN received yet", IPN_BLUE),
                        badge("Awaiting SETTLED", WARN),
                        badge("require_settlement=true", PURP),
                    ],
                ),
                html.Div(
                    "No CapitalPay /notify traffic yet",
                    style={
                        "fontSize": "20px",
                        "fontWeight": "700",
                        "color": TEXT,
                        "marginBottom": "10px",
                        "letterSpacing": ".01em",
                    },
                ),
                html.P(
                    "Invoice creation succeeded, but CapitalPay has not POSTed a settlement IPN payload to your webhook yet.",
                    style={"fontSize": "13px", "color": MUTED, "maxWidth": "520px", "margin": "0 auto 16px"},
                ),
                html.Div(
                    style={
                        "display": "inline-block",
                        "textAlign": "left",
                        "padding": "14px 18px",
                        "borderRadius": "10px",
                        "background": "rgba(10,12,16,.55)",
                        "border": f"1px solid {BORD}",
                        "marginBottom": "14px",
                    },
                    children=[
                        html.Div("Expected IPN endpoint", style={"fontSize": "10px", "color": MUTED, "fontWeight": "700", "marginBottom": "6px"}),
                        html.Code(
                            "/notify",
                            style={"color": IPN_BLUE, "fontSize": "13px", "fontWeight": "700"},
                        ),
                        html.Div(
                            "When payment settles, CapitalPay should POST status, invoice_number, amount_paid, payment_reference, etc.",
                            style={"fontSize": "11px", "color": MUTED, "marginTop": "8px", "maxWidth": "420px"},
                        ),
                    ],
                ),
                html.Div(
                    style={"display": "flex", "justifyContent": "center", "gap": "10px", "flexWrap": "wrap"},
                    children=[
                        html.Span(
                            "Tip: use Inject test event to preview a GOOD payload",
                            style={"fontSize": "11px", "color": ACC2},
                        ),
                    ],
                ),
            ],
        )

    cards = []
    th = {
        "textAlign": "left",
        "padding": "6px 10px",
        "fontSize": "10px",
        "color": MUTED,
        "background": "#1a1f2b",
        "borderBottom": f"1px solid {BORD}",
    }
    for ev in events:
        sc = {
            "settled": (GREEN, "SETTLED"),
            "pending": (WARN, "PENDING"),
            "failed": (RED, "FAILED"),
        }.get(ev["status"].lower(), (MUTED, ev["status"].upper()))
        hc = (GREEN, "Hash OK") if ev["valid_hash"] else (RED, "Hash mismatch")
        quality = (ev.get("payload_quality") or "UNKNOWN").upper()
        qc = {"GOOD": (GREEN, "GOOD payload"), "PARTIAL": (WARN, "PARTIAL payload"), "BAD": (RED, "BAD payload")}.get(
            quality, (MUTED, f"{quality} payload")
        )
        ipc = (GREEN, "IP OK") if ev.get("ip_allowed", True) else (WARN, "Unknown IP")
        refs_rows = [
            html.Tr(
                [
                    html.Td(
                        r.get("payment_reference", "-"),
                        style={
                            "fontFamily": "monospace",
                            "padding": "6px 10px",
                            "color": TEXT,
                            "fontSize": "11px",
                        },
                    ),
                    html.Td(
                        r.get("payment_date", "-"),
                        style={"padding": "6px 10px", "color": TEXT, "fontSize": "11px"},
                    ),
                    html.Td(
                        r.get("currency", "-"),
                        style={"padding": "6px 10px", "color": TEXT, "fontSize": "11px"},
                    ),
                    html.Td(
                        r.get("amount", "-"),
                        style={
                            "textAlign": "right",
                            "padding": "6px 10px",
                            "color": GREEN,
                            "fontWeight": "700",
                            "fontSize": "11px",
                        },
                    ),
                ]
            )
            for r in ev.get("payment_references", [])
        ]

        cards.append(
            html.Div(
                style={
                    "background": SURF,
                    "border": f"1px solid {BORD}",
                    "borderRadius": "13px",
                    "marginBottom": "12px",
                    "overflow": "hidden",
                },
                children=[
                    html.Div(
                        style={
                            "display": "flex",
                            "alignItems": "center",
                            "gap": "8px",
                            "flexWrap": "wrap",
                            "padding": "12px 18px",
                            "borderBottom": f"1px solid {BORD}",
                            "background": "#1a1f2b",
                        },
                        children=[
                            badge(sc[1], sc[0]),
                            badge(qc[1], qc[0]),
                            badge(hc[1], hc[0]),
                            badge(ipc[1], ipc[0]),
                            badge(ev.get("payment_channel", "-")),
                            html.Span(
                                f"{ev['received_at']} | {ev['invoice_number']}",
                                style={"marginLeft": "auto", "fontSize": "11px", "color": MUTED},
                            ),
                        ],
                    ),
                    html.Div(
                        style={"padding": "14px 18px"},
                        children=[
                            html.Div(
                                style={
                                    "display": "grid",
                                    "gridTemplateColumns": "repeat(3, minmax(0, 1fr))",
                                    "gap": "12px",
                                },
                                children=[
                                    html.Div(
                                        [
                                            kv_item("Source IP", ev.get("ip_address")),
                                            kv_item("Phone number", ev["phone_number"]),
                                            kv_item("Payment channel", ev["payment_channel"]),
                                        ]
                                    ),
                                    html.Div(
                                        [
                                            kv_item("Client ref", ev["client_invoice_ref"]),
                                            kv_item("Invoice number", ev["invoice_number"]),
                                            kv_item("Currency", ev["currency"]),
                                        ]
                                    ),
                                    html.Div(
                                        [
                                            kv_item("Invoice amount", f"{ev['invoice_amount']} TZS"),
                                            kv_item("Last payment", f"{ev['last_payment_amount']} TZS"),
                                            kv_item("Amount paid", f"{ev['amount_paid']} TZS"),
                                        ]
                                    ),
                                ],
                            ),
                            html.Hr(
                                style={"border": "none", "borderTop": f"1px solid {BORD}", "margin": "12px 0"}
                            ),
                            html.Div(
                                "Payment References",
                                style={
                                    "fontSize": "10px",
                                    "color": MUTED,
                                    "fontWeight": "600",
                                    "textTransform": "uppercase",
                                    "letterSpacing": ".07em",
                                    "marginBottom": "7px",
                                },
                            ),
                            html.Table(
                                style={"width": "100%", "borderCollapse": "collapse"},
                                children=[
                                    html.Thead(
                                        html.Tr(
                                            [
                                                html.Th("Ref #", style=th),
                                                html.Th("Date", style=th),
                                                html.Th("CCY", style=th),
                                                html.Th("Amount", style={**th, "textAlign": "right"}),
                                            ]
                                        )
                                    ),
                                    html.Tbody(
                                        refs_rows
                                        if refs_rows
                                        else [
                                            html.Tr(
                                                html.Td(
                                                    "No payment references",
                                                    colSpan=4,
                                                    style={
                                                        "padding": "8px 10px",
                                                        "color": MUTED,
                                                        "fontSize": "11px",
                                                    },
                                                )
                                            )
                                        ]
                                    ),
                                ],
                            ),
                            captured_payload_panel(ev),
                        ],
                    ),
                ],
            )
        )
    return html.Div(cards)


def _ip_role_section(ip_role: str, rows: list[dict], accent: str):
    meta = notifications.IP_ROLE_LABELS.get(ip_role, {})
    title = meta.get("title", ip_role)
    direction = meta.get("direction", "")
    endpoint = meta.get("endpoint", "-")
    th = {
        "textAlign": "left",
        "padding": "8px 12px",
        "fontSize": "10px",
        "color": MUTED,
        "textTransform": "uppercase",
        "letterSpacing": ".07em",
        "background": "#1a1f2b",
        "borderBottom": f"1px solid {BORD}",
    }
    body_rows = [
        html.Tr(
            [
                html.Td(
                    r["ip_address"],
                    style={"padding": "8px 12px", "fontFamily": "monospace", "color": accent, "fontSize": "12px"},
                ),
                html.Td(r["endpoint"], style={"padding": "8px 12px", "color": TEXT, "fontSize": "12px"}),
                html.Td(r["first_seen"], style={"padding": "8px 12px", "color": TEXT, "fontSize": "12px"}),
                html.Td(r["last_seen"], style={"padding": "8px 12px", "color": TEXT, "fontSize": "12px"}),
                html.Td(
                    str(r["hit_count"]),
                    style={
                        "padding": "8px 12px",
                        "textAlign": "center",
                        "fontWeight": "700",
                        "color": TEXT,
                        "fontSize": "12px",
                    },
                ),
            ]
        )
        for r in rows
    ] or [
        html.Tr(
            html.Td(
                "No traffic recorded yet",
                colSpan=5,
                style={"textAlign": "center", "padding": "18px", "color": MUTED, "fontSize": "12px"},
            )
        )
    ]

    return html.Div(
        style={
            "background": SURF,
            "border": f"1px solid {BORD}",
            "borderRadius": "13px",
            "overflow": "hidden",
            "marginBottom": "14px",
        },
        children=[
            html.Div(
                style={
                    "padding": "12px 16px",
                    "borderBottom": f"1px solid {BORD}",
                    "background": _rgba(accent, 0.08),
                },
                children=[
                    html.Div(
                        style={"display": "flex", "alignItems": "center", "gap": "10px", "flexWrap": "wrap"},
                        children=[
                            badge(title, accent),
                            html.Span(direction, style={"fontSize": "12px", "color": TEXT}),
                            html.Span(
                                endpoint,
                                style={
                                    "marginLeft": "auto",
                                    "fontFamily": "monospace",
                                    "fontSize": "11px",
                                    "color": accent,
                                },
                            ),
                        ],
                    ),
                ],
            ),
            html.Table(
                style={"width": "100%", "borderCollapse": "collapse"},
                children=[
                    html.Thead(
                        html.Tr(
                            [
                                html.Th("IP address", style=th),
                                html.Th("Endpoint", style=th),
                                html.Th("First seen (UTC)", style=th),
                                html.Th("Last seen (UTC)", style=th),
                                html.Th("Hits", style={**th, "textAlign": "center"}),
                            ]
                        )
                    ),
                    html.Tbody(body_rows),
                ],
            ),
        ],
    )


def ip_directory_panel(ip_data):
    grouped: dict[str, list[dict]] = {
        notifications.IP_ROLE_CAPITALPAY: [],
        notifications.IP_ROLE_CLIENT: [],
    }
    for row in ip_data:
        role = row.get("ip_role") or notifications.IP_ROLE_CAPITALPAY
        grouped.setdefault(role, []).append(row)

    return html.Div(
        [
            _ip_role_section(
                notifications.IP_ROLE_CAPITALPAY,
                grouped.get(notifications.IP_ROLE_CAPITALPAY, []),
                IPN_BLUE,
            ),
            _ip_role_section(
                notifications.IP_ROLE_CLIENT,
                grouped.get(notifications.IP_ROLE_CLIENT, []),
                GREEN,
            ),
        ]
    )


def cp_outbound_panel():
    box_style = {
        "background": "#0a0c10",
        "border": f"1px solid {BORD}",
        "borderRadius": "10px",
        "padding": "12px",
        "color": ACC2,
        "fontFamily": "'Fira Mono','Courier New',monospace",
        "fontSize": "11px",
        "lineHeight": "1.55",
        "whiteSpace": "pre-wrap",
        "wordBreak": "break-word",
        "maxHeight": "360px",
        "overflow": "auto",
    }
    if not LAST_CP_OUTBOUND.get("invoice_request"):
        return html.Div(
            style={
                "background": SURF,
                "border": f"1px solid {BORD}",
                "borderRadius": "13px",
                "padding": "16px 18px",
                "marginBottom": "16px",
            },
            children=[
                html.Div(
                    "Last CapitalPay OUTBOUND request (invoice create)",
                    style={"fontSize": "13px", "fontWeight": "700", "color": TEXT, "marginBottom": "6px"},
                ),
                html.P(
                    "Run a checkout first. This shows the JSON body POSTed to CapitalPay /invoice/create — not inbound /notify or /callback traffic.",
                    style={"fontSize": "12px", "color": MUTED, "margin": 0},
                ),
            ],
        )

    return html.Div(
        style={
            "background": SURF,
            "border": f"1px solid {BORD}",
            "borderRadius": "13px",
            "padding": "14px 18px",
            "marginBottom": "16px",
        },
        children=[
            html.Div(
                style={"display": "flex", "gap": "8px", "flexWrap": "wrap", "alignItems": "center", "marginBottom": "10px"},
                children=[
                    html.Span(
                        "Last CapitalPay OUTBOUND request",
                        style={"fontSize": "13px", "fontWeight": "700", "color": TEXT},
                    ),
                    badge("TO CAPITALPAY", ACC2),
                    html.Span(
                        LAST_CP_OUTBOUND.get("at") or "-",
                        style={"marginLeft": "auto", "fontSize": "11px", "color": MUTED},
                    ),
                ],
            ),
            html.P(
                "Orange in Terminal Logs. This is what your server sends to CP to create the invoice — separate from empty inbound callback bodies.",
                style={"fontSize": "11px", "color": MUTED, "margin": "0 0 10px"},
            ),
            html.Div("POST URL (invoice create)", style={"fontSize": "10px", "color": MUTED, "fontWeight": "700", "marginBottom": "4px"}),
            html.Pre(LAST_CP_OUTBOUND.get("invoice_url") or "-", style={**box_style, "color": TEXT, "maxHeight": "80px"}),
            html.Div("Request body JSON (cp_invoice_request)", style={"fontSize": "10px", "color": MUTED, "fontWeight": "700", "margin": "10px 0 4px"}),
            html.Pre(_pretty(LAST_CP_OUTBOUND.get("invoice_request"), limit=12000), style=box_style),
            html.Div("CapitalPay response body", style={"fontSize": "10px", "color": MUTED, "fontWeight": "700", "margin": "10px 0 4px"}),
            html.Pre(
                _pretty(LAST_CP_OUTBOUND.get("invoice_response") or {}, limit=8000),
                style={**box_style, "color": GREEN, "maxHeight": "240px"},
            ),
            html.Details(
                style={"marginTop": "10px"},
                children=[
                    html.Summary(
                        "Checkout page form params (second POST to CapitalPay)",
                        style={"color": ACC2, "fontSize": "12px", "fontWeight": "700", "cursor": "pointer"},
                    ),
                    html.Div(
                        f"POST {LAST_CP_OUTBOUND.get('checkout_page_url') or capitalpay.CHECKOUT_URL}",
                        style={"fontSize": "11px", "color": MUTED, "margin": "8px 0 4px"},
                    ),
                    html.Pre(_pretty(LAST_CP_OUTBOUND.get("checkout_params") or {}, limit=8000), style=box_style),
                ],
            ),
        ],
    )


def shell_style(active):
    return {"display": "block" if active else "none"}


app.layout = html.Div(
    style={"background": DARK, "minHeight": "100vh", "fontFamily": "DM Sans,sans-serif"},
    children=[
        html.Div(
            style={
                "background": SURF,
                "borderBottom": f"1px solid {BORD}",
                "padding": "14px 28px",
                "display": "flex",
                "alignItems": "center",
                "gap": "14px",
            },
            children=[
                html.Div(
                    "C",
                    style={
                        "width": "38px",
                        "height": "38px",
                        "flexShrink": "0",
                        "background": f"linear-gradient(135deg,{ACC},{ACC2})",
                        "borderRadius": "9px",
                        "display": "flex",
                        "alignItems": "center",
                        "justifyContent": "center",
                        "color": "#fff",
                        "fontWeight": "700",
                        "fontSize": "17px",
                    },
                ),
                html.Div(
                    [
                        html.Span("CapitalPay Dashboard", style={"color": TEXT, "fontWeight": "600", "fontSize": "15px"}),
                        html.Span(
                            f" | Account {capitalpay.ACCOUNT_ID} | TZS",
                            style={"color": MUTED, "fontSize": "12px", "marginLeft": "8px"},
                        ),
                    ]
                ),
                html.Div(
                    style={"marginLeft": "auto", "display": "flex", "gap": "8px", "alignItems": "center"},
                    children=[
                        html.Span(
                            "LIVE",
                            style={
                                "background": "rgba(34,197,94,.15)",
                                "border": "1px solid rgba(34,197,94,.3)",
                                "color": GREEN,
                                "borderRadius": "20px",
                                "padding": "3px 12px",
                                "fontSize": "11px",
                                "fontWeight": "700",
                            },
                        ),
                        nav_link("Checkout", HOST + "/"),
                    ],
                ),
            ],
        ),
        html.Div(
            style={"background": SURF, "borderBottom": f"1px solid {BORD}", "padding": "0 28px", "display": "flex"},
            children=[
                html.Button("Payment Monitor", id="tab-monitor", n_clicks=0, style=tab_style(True)),
                html.Button("Terminal Logs", id="tab-logs", n_clicks=0, style=tab_style(False)),
            ],
        ),
        html.Div(
            style={"padding": "24px 28px"},
            children=[
                html.Div(id="monitor-wrap", children=monitor_layout(), style=shell_style(True)),
                html.Div(id="logs-wrap", children=logs_layout(), style=shell_style(False)),
            ],
        ),
        dcc.Store(id="active-tab", data="monitor"),
        dcc.Interval(id="interval", interval=3000, n_intervals=0),
    ],
)


@app.callback(
    Output("active-tab", "data"),
    Output("tab-monitor", "style"),
    Output("tab-logs", "style"),
    Output("monitor-wrap", "style"),
    Output("logs-wrap", "style"),
    Input("tab-monitor", "n_clicks"),
    Input("tab-logs", "n_clicks"),
    State("active-tab", "data"),
)
def switch_tab(_, __, current):
    ctx = callback_context
    if ctx.triggered and ctx.triggered[0]["prop_id"].split(".")[0] == "tab-logs":
        return "logs", tab_style(False), tab_style(True), shell_style(False), shell_style(True)
    return current or "monitor", tab_style(True), tab_style(False), shell_style(True), shell_style(False)


@app.callback(
    Output("s-total", "children"),
    Output("s-good", "children"),
    Output("s-bad", "children"),
    Output("s-settled", "children"),
    Output("s-amount", "children"),
    Output("m-feed", "children"),
    Output("ip-table", "children"),
    Output("ip-count", "children"),
    Input("interval", "n_intervals"),
    Input("btn-clear-notif", "n_clicks"),
    Input("btn-test-event", "n_clicks"),
    State("m-search", "value"),
    State("m-status", "value"),
    State("m-hash", "value"),
    State("m-quality", "value"),
    prevent_initial_call=False,
)
def refresh_monitor(_, clr, tst, search, status_f, hash_f, quality_f):
    try:
        return _refresh_monitor_impl(_, clr, tst, search, status_f, hash_f, quality_f)
    except Exception as exc:
        print(f"[MONITOR] refresh_monitor failed: {exc}")
        return (
            "0",
            "0",
            "0",
            "0",
            "0.00",
            html.Div(f"Monitor error: {exc}", style={"color": RED, "padding": "12px"}),
            ip_directory_panel([]),
            "0 IPs tracked",
        )


def _refresh_monitor_impl(_, clr, tst, search, status_f, hash_f, quality_f):
    ctx = callback_context
    if ctx.triggered:
        trigger_id = ctx.triggered[0]["prop_id"].split(".")[0]
        if trigger_id == "btn-clear-notif":
            notifications.clear_notifications()
            print("[MONITOR] Cleared notification events and observed IPs")
        elif trigger_id == "btn-test-event":
            event = notifications.inject_test()
            print(f"[MONITOR] Injected test event paid={event['status'] == 'settled'} ip={event['ip_address']}")

    events = notifications.get_notifications(500)
    settled = [e for e in events if e["status"].lower() == "settled"]
    good = [e for e in events if (e.get("payload_quality") or "").upper() == "GOOD"]
    bad = [e for e in events if (e.get("payload_quality") or "").upper() in {"BAD", "PARTIAL"}]
    paid = sum(float(e["amount_paid"] or 0) for e in settled)

    filtered = events
    if status_f:
        filtered = [e for e in filtered if e["status"].lower() == status_f]
    if hash_f == "valid":
        filtered = [e for e in filtered if e["valid_hash"]]
    elif hash_f == "invalid":
        filtered = [e for e in filtered if not e["valid_hash"]]
    if quality_f == "unknown_ip":
        filtered = [e for e in filtered if not e.get("ip_allowed", True)]
    elif quality_f:
        filtered = [e for e in filtered if (e.get("payload_quality") or "").upper() == quality_f]
    if search:
        query = search.lower()
        filtered = [
            e
            for e in filtered
            if query in (e["invoice_number"] or "").lower()
            or query in (e["client_invoice_ref"] or "").lower()
            or query in (e["phone_number"] or "").lower()
            or query in (e.get("ip_address") or "").lower()
        ]

    ips = notifications.get_observed_ips()

    return (
        str(len(events)),
        str(len(good)),
        str(len(bad)),
        str(len(settled)),
        f"{paid:,.2f}",
        event_feed(filtered),
        ip_directory_panel(ips),
        f"{len(ips)} IP{'s' if len(ips) != 1 else ''} tracked",
    )


@app.callback(
    Output("cp-outbound-panel", "children"),
    Input("interval", "n_intervals"),
    Input("btn-clear-notif", "n_clicks"),
    Input("btn-test-event", "n_clicks"),
    prevent_initial_call=False,
)
def refresh_cp_outbound_panel(_, __, ___):
    try:
        return cp_outbound_panel()
    except Exception as exc:
        print(f"[MONITOR] cp_outbound_panel failed: {exc}")
        return html.Div(f"Outbound panel error: {exc}", style={"color": RED, "padding": "12px"})


@app.callback(
    Output("l-total", "children"),
    Output("l-webhooks", "children"),
    Output("l-checkouts", "children"),
    Output("l-valid", "children"),
    Output("l-invalid", "children"),
    Output("l-blocked", "children"),
    Output("l-feed", "children"),
    Output("l-count", "children"),
    Input("interval", "n_intervals"),
    Input("btn-clear-logs", "n_clicks"),
    State("l-search", "value"),
    State("l-level", "value"),
    prevent_initial_call=False,
)
def refresh_logs(_, clr, search, level):
    ctx = callback_context
    if ctx.triggered and ctx.triggered[0]["prop_id"].split(".")[0] == "btn-clear-logs":
        while not LOG_QUEUE.empty():
            try:
                LOG_QUEUE.get_nowait()
            except queue.Empty:
                break

    lines = get_log_lines()
    total = len(lines)
    webhooks = sum(1 for line in lines if "INCOMING WEBHOOK" in line)
    checkouts = sum(1 for line in lines if "NEW CHECKOUT" in line)
    valid = sum(1 for line in lines if "VALID" in line and "INVALID" not in line)
    invalid = sum(1 for line in lines if "INVALID" in line or "UNVERIFIABLE" in line)
    blocked = sum(1 for line in lines if "UNKNOWN IP" in line or "Payload quality : BAD" in line)

    filtered = lines
    if level and level != "all":
        filtered = [line for line in filtered if level in line]
    if search:
        query = search.lower()
        filtered = [line for line in filtered if query in line.lower()]

    if not filtered:
        feed = html.Div(
            "Waiting for activity - run a checkout or POST /notify to see logs here.",
            style={"color": MUTED, "fontSize": "12px"},
        )
    else:
        feed = html.Div(
            [
                html.Div(
                    line,
                    style=log_line_style(line),
                )
                for line in filtered
            ]
        )

    count = f"{len(filtered):,} line{'s' if len(filtered) != 1 else ''}"
    return str(total), str(webhooks), str(checkouts), str(valid), str(invalid), str(blocked), feed, count


CHECKOUT_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>CapitalPay Checkout</title>
<link href="https://fonts.googleapis.com/css2?family=DM+Serif+Display&family=DM+Sans:wght@300;400;500;600&display=swap" rel="stylesheet"/>
<style>
:root{--bg:#0d0f14;--surface:#161a22;--border:#252b38;--accent:#e05a1e;--accent2:#f07a3a;--text:#e8eaf0;--muted:#6b7280;--success:#22c55e;--error:#ef4444;--r:12px}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--text);font-family:'DM Sans',sans-serif;min-height:100vh;display:flex;align-items:center;justify-content:center;padding:24px;background-image:radial-gradient(ellipse 60% 40% at 80% 10%,rgba(224,90,30,.08) 0%,transparent 60%)}
.card{background:var(--surface);border:1px solid var(--border);border-radius:20px;width:100%;max-width:560px;overflow:hidden;box-shadow:0 32px 64px rgba(0,0,0,.5)}
.card-header{padding:28px 32px 22px;border-bottom:1px solid var(--border);display:flex;align-items:center;gap:14px}
.logo{width:42px;height:42px;background:linear-gradient(135deg,var(--accent),var(--accent2));border-radius:10px;display:flex;align-items:center;justify-content:center;font-family:'DM Serif Display',serif;font-size:19px;color:#fff;flex-shrink:0}
.card-title{font-family:'DM Serif Display',serif;font-size:21px;line-height:1.2}
.card-sub{font-size:12px;color:var(--muted);margin-top:2px}
.hd-right{margin-left:auto;display:flex;align-items:center;gap:8px}
.badge-live{font-size:11px;font-weight:600;background:rgba(224,90,30,.2);color:var(--accent2);border-radius:6px;padding:2px 8px;letter-spacing:.04em}
.nav-btn{font-size:11px;font-weight:600;color:var(--accent2);text-decoration:none;padding:3px 10px;border:1px solid rgba(224,90,30,.3);border-radius:20px;background:rgba(224,90,30,.08)}
.card-body{padding:28px 32px}
.field{margin-bottom:16px}
label{display:block;font-size:11px;font-weight:600;letter-spacing:.06em;text-transform:uppercase;color:var(--muted);margin-bottom:5px}
input{width:100%;background:#1e2330;border:1px solid var(--border);border-radius:var(--r);color:var(--text);font-family:'DM Sans',sans-serif;font-size:14px;padding:10px 13px;outline:none}
input:focus{border-color:var(--accent)}
.row2{display:grid;grid-template-columns:1fr 1fr;gap:13px}
.currency-tag{display:flex;align-items:center;background:#1e2330;border:1px solid var(--border);border-radius:var(--r);padding:10px 13px;color:var(--accent2);font-size:14px;font-weight:600}
.btn{width:100%;padding:13px;background:linear-gradient(135deg,var(--accent),var(--accent2));border:none;border-radius:var(--r);color:#fff;font-family:'DM Sans',sans-serif;font-size:15px;font-weight:600;cursor:pointer;margin-top:6px}
.btn:hover{opacity:.9}.btn:disabled{opacity:.5;cursor:not-allowed}
.status{margin-top:16px;padding:13px 15px;border-radius:var(--r);font-size:13px;display:none}
.status.show{display:block}
.status.info{background:rgba(224,90,30,.12);border:1px solid rgba(224,90,30,.3);color:#f0a070}
.status.success{background:rgba(34,197,94,.1);border:1px solid rgba(34,197,94,.3);color:var(--success)}
.status.error{background:rgba(239,68,68,.1);border:1px solid rgba(239,68,68,.3);color:var(--error)}
.section-title{font-size:11px;font-weight:700;letter-spacing:.08em;text-transform:uppercase;color:var(--accent2);margin:20px 0 10px;padding-top:4px;border-top:1px solid var(--border)}
.hint{font-size:11px;color:var(--muted);line-height:1.5;margin-top:6px}
.settlement-panel{background:#12151c;border:1px solid var(--border);border-radius:var(--r);padding:14px;margin-top:8px}
.settlement-panel.hidden{display:none !important}
.settlement-status{display:inline-block;margin-top:8px;font-size:11px;font-weight:700;padding:4px 10px;border-radius:20px;letter-spacing:.04em}
.settlement-status.off{background:rgba(107,114,128,.18);color:var(--muted);border:1px solid var(--border)}
.settlement-status.on{background:rgba(34,197,94,.12);color:var(--success);border:1px solid rgba(34,197,94,.35)}
select{width:100%;background:#1e2330;border:1px solid var(--border);border-radius:var(--r);color:var(--text);font-family:'DM Sans',sans-serif;font-size:14px;padding:10px 13px;outline:none}
select:focus{border-color:var(--accent)}
.spinner{display:inline-block;width:14px;height:14px;border:2px solid rgba(255,255,255,.3);border-top-color:#fff;border-radius:50%;animation:spin .7s linear infinite;margin-right:7px;vertical-align:middle}
@keyframes spin{to{transform:rotate(360deg)}}
</style>
</head>
<body>
<div class="card">
  <div class="card-header">
    <div class="logo">C</div>
    <div>
      <div class="card-title">CapitalPay Checkout <span class="badge-live">Live</span></div>
      <div class="card-sub">Account ID: {{ account_id }} &nbsp;|&nbsp; TZS &nbsp;|&nbsp; Secure Payment Gateway</div>
    </div>
    <div class="hd-right"><a class="nav-btn" href="/dash/" target="_blank" rel="noopener noreferrer">Dashboard</a></div>
  </div>
  <div class="card-body">
    <form id="payForm">
      <div class="row2">
        <div class="field"><label>Full Name</label><input name="name" type="text" placeholder="e.g. John Doe" required/></div>
        <div class="field"><label>Phone (MSISDN)</label><input name="msisdn" type="text" placeholder="+255712345678" required/></div>
      </div>
      <div class="field"><label>Email (optional)</label><input name="email" type="email" placeholder="you@example.com"/></div>
      <div class="field"><label>ID / Passport Number</label><input name="id_number" type="text" placeholder="National ID or passport" required/></div>
      <div class="row2">
        <div class="field"><label>Amount (TZS)</label><input name="amount" type="number" step="0.01" min="1" placeholder="10000.00" required/></div>
        <div class="field"><label>Currency</label><div class="currency-tag">TZS - Tanzanian Shilling</div></div>
      </div>
      <div class="field"><label>Invoice / Bill Reference</label><input name="bill_ref" type="text" placeholder="INV-2026-001" required/></div>
      <div class="field"><label>Description</label><input name="desc" type="text" placeholder="Payment description" required/></div>
      <div class="section-title">Settlement</div>
      <div class="field">
        <label>Settlement split (settlements[])</label>
        <select name="settlement_split" id="settlementSplit">
          <option value="false" selected>No — require_settlement true only</option>
          <option value="true">Yes — include settlements[]</option>
        </select>
        <div class="settlement-status off" id="settlementStatus">require_settlement=true — no settlements[] split</div>
        <div class="hint">
          Every invoice sends <code>require_settlement: "true"</code>.<br/>
          Enable <strong>settlement split</strong> to also include a <code>settlements[]</code> array (account + description required).
        </div>
      </div>
      <div class="settlement-panel hidden" id="settlementPanel" aria-hidden="true">
        <div class="field"><label>Settlement account number *</label><input name="settlement_account_number" id="settlementAccount" type="text" placeholder="Partner / beneficiary account number" disabled/></div>
        <div class="field"><label>Settlement description *</label><input name="settlement_desc" id="settlementDesc" type="text" placeholder="e.g. Route fees to operations account" disabled/></div>
        <div class="field"><label>Settlement value (TZS)</label><input name="settlement_value" id="settlementValue" type="number" step="0.01" min="0.01" placeholder="Leave blank to use full invoice amount" disabled/></div>
      </div>
      <div class="field"><label>Callback URL (on success)</label><input name="callback_url" type="url" value="{{ callback_url }}" placeholder="https://yoursite.com/success"/></div>
      <div class="field"><label>Notification URL (IPN) *</label><input name="notification_url" id="notif-url" type="url" value="{{ notification_url }}" required/></div>
      <button class="btn" type="submit" id="submitBtn">Proceed to Payment</button>
    </form>
    <div class="status" id="statusBox"></div>
  </div>
</div>
<script>
const form=document.getElementById('payForm');
const btn=document.getElementById('submitBtn');
const sb=document.getElementById('statusBox');
const settlementSplit=document.getElementById('settlementSplit');
const settlementPanel=document.getElementById('settlementPanel');
const settlementAccount=document.getElementById('settlementAccount');
const settlementDesc=document.getElementById('settlementDesc');
const settlementValue=document.getElementById('settlementValue');
const settlementStatus=document.getElementById('settlementStatus');
const settlementFields=[settlementAccount,settlementDesc,settlementValue];
function setStatus(msg,type){sb.className='status show '+type;sb.innerHTML=msg;}
function syncSettlementPanel(){
  const enabled=settlementSplit.value==='true';
  settlementPanel.classList.toggle('hidden',!enabled);
  settlementPanel.setAttribute('aria-hidden',enabled?'false':'true');
  settlementFields.forEach(function(field){
    field.disabled=!enabled;
    field.required=false;
  });
  if(enabled){
    settlementAccount.required=true;
    settlementDesc.required=true;
    settlementStatus.className='settlement-status on';
    settlementStatus.textContent='require_settlement=true — settlement split ON (fill fields below)';
  }else{
    settlementFields.forEach(function(field){ field.value=''; });
    settlementStatus.className='settlement-status off';
    settlementStatus.textContent='require_settlement=true — no settlements[] split';
  }
}
settlementSplit.addEventListener('change',syncSettlementPanel);
syncSettlementPanel();
form.addEventListener('submit',async function(e){
  e.preventDefault();
  if(settlementSplit.value==='true'&&(!settlementAccount.value.trim()||!settlementDesc.value.trim())){
    setStatus('Settlement split is enabled — enter settlement account number and description.','error');
    return;
  }
  btn.disabled=true;
  btn.innerHTML='<span class="spinner"></span>Creating Invoice...';
  setStatus('Generating token and creating invoice. Please wait...','info');
  const splitOn=settlementSplit.value==='true';
  const data=Object.fromEntries(new FormData(form).entries());
  data.currency='TZS';
  data.require_settlement='true';
  data.settlement_split=splitOn?'true':'false';
  delete data.settlement_account_number;
  delete data.settlement_desc;
  delete data.settlement_value;
  if(splitOn){
    data.settlement_account_number=settlementAccount.value.trim();
    data.settlement_desc=settlementDesc.value.trim();
    if(settlementValue.value.trim()) data.settlement_value=settlementValue.value.trim();
  }
  try{
    const res=await fetch('/checkout',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data)});
    const text=await res.text();
    let json={};
    try {
      json=text ? JSON.parse(text) : {};
    } catch (_) {
      throw new Error(text || 'Server returned a non-JSON response');
    }
    if(!res.ok||json.error) throw new Error(json.error||'Server error');
    const settlementNote=data.settlement_split==='true'
      ? ' require_settlement=true with settlements[] split.'
      : ' require_settlement=true (no settlements[] split).';
    const cpReq=json.cp_invoice_request?('<details style="margin-top:12px;text-align:left"><summary style="cursor:pointer;color:#f07a3a;font-weight:700">CapitalPay invoice request body (POST /invoice/create)</summary><pre style="margin-top:8px;padding:10px;background:#0a0c10;border:1px solid #252b38;border-radius:8px;overflow:auto;max-height:280px;font-size:11px;color:#f07a3a;white-space:pre-wrap">'+JSON.stringify(json.cp_invoice_request,null,2)+'</pre></details>'):'';
    const cpForm=json.cp_checkout_params?('<details style="margin-top:8px;text-align:left"><summary style="cursor:pointer;color:#f07a3a;font-weight:700">Checkout page form params (POST PaymentAPI)</summary><pre style="margin-top:8px;padding:10px;background:#0a0c10;border:1px solid #252b38;border-radius:8px;overflow:auto;max-height:220px;font-size:11px;color:#f07a3a;white-space:pre-wrap">'+JSON.stringify(json.cp_checkout_params,null,2)+'</pre></details>'):'';
    if(json.checkout_url){
      setStatus('Invoice <strong>'+(json.invoice_ref||data.bill_ref)+'</strong> created.'+settlementNote+' Opening checkout...'+cpReq+cpForm,'success');
      window.open(json.checkout_url,'_blank','noopener,noreferrer');
    } else {
      setStatus('Invoice created. Reference: <strong>'+(json.invoice_ref||data.bill_ref)+'</strong>'+cpReq+cpForm,'success');
    }
  }catch(err){setStatus(err.message,'error');}
  finally{btn.disabled=false;btn.textContent='Proceed to Payment';}
});
</script>
</body>
</html>"""


@server.before_request
def log_checkout_request():
    if request.path != "/checkout" or request.method != "POST":
        return None
    data = request.get_json(silent=True) or {}
    raw_body = request.get_data(as_text=True) or ""
    _log_session_start("BROWSER CHECKOUT", "POST /checkout")
    _log_request_marker("Browser form submission")
    print("  Method: POST /checkout")
    print("  Headers:")
    for key, value in _redact(dict(request.headers)).items():
        print(f"    {key}: {value}")
    print(f"  {REQUEST_PAYLOAD} Raw Body (checkout request):")
    _print_payload_lines(raw_body, "request")
    print(f"  {REQUEST_PAYLOAD} Parsed JSON (checkout request):")
    _print_payload_lines(data, "request")
    _log_summary("CHECKOUT REQUEST SUMMARY")
    print(f"  Customer   : {data.get('name', '')} | {data.get('msisdn', '')} | {data.get('email', '')}")
    print(f"  Bill ref   : {data.get('bill_ref', '')}")
    print(f"  Amount     : {data.get('amount', '')} TZS")
    settlement_split = str(data.get("settlement_split", "false")).lower() in {"true", "1", "yes"}
    print("  Settlement : require_settlement=true (always)")
    print(f"  Split      : settlements[]={'yes' if settlement_split else 'no'}")
    if settlement_split:
        print(f"  Settle acct : {data.get('settlement_account_number', '')}")
        print(f"  Settle desc : {data.get('settlement_desc', '')}")
        print(f"  Settle value: {data.get('settlement_value', '') or data.get('amount', '')} TZS")
    print(f"  Callback   : {data.get('callback_url', '')}")
    print(f"  Notif URL  : {data.get('notification_url', '')}")
    print(f"  CapitalPay will POST notifications to: {data.get('notification_url', '')}")
    print(f"  CapitalPay success callback URL     : {data.get('callback_url', '')}")
    return None


@server.after_request
def log_checkout_response(response):
    if request.path == "/checkout" and request.method == "POST":
        outcome = "OK" if response.status_code < 400 else "ERROR"
        _log_response_marker(f"HTTP {response.status_code}", "to browser")
        print(f"  Status: {response.status_code}")
        print("  Headers:")
        for key, value in dict(response.headers).items():
            print(f"    {key}: {value}")
        body = response.get_data(as_text=True) or ""
        print("  Body:")
        for line in _pretty(body).splitlines() or [""]:
            print(f"    {line}")
        _log_session_end("BROWSER CHECKOUT", outcome)
    return response


@server.route("/checkout-form")
def checkout_form():
    return index()


def index():
    return render_template_string(
        CHECKOUT_HTML,
        account_id=capitalpay.ACCOUNT_ID,
        callback_url=default_callback_url(),
        notification_url=capitalpay.default_notification_url(),
    )


server.view_functions["index"] = index


@server.route("/callback", methods=["GET", "POST"])
@server.route("/payment/callback", methods=["GET", "POST"])
def payment_callback():
    payload, raw_body = _request_payload_parts()
    event = _record_callback_event(payload, raw_body)
    _log_session_start("BROWSER CALLBACK", f"{request.method} /callback")
    _log_request_marker("User redirect after payment")
    print(f"  Method: {request.method} {request.path}")
    print(f"  Callback URL hit: {request.url}")
    print("  Headers:")
    for key, value in _redact(dict(request.headers)).items():
        print(f"    {key}: {value}")
    print("  Raw Body:")
    for line in _pretty(raw_body).splitlines() or [""]:
        print(f"    {line}")
    print("  Unpacked Payload:")
    for line in _pretty(payload).splitlines() or [""]:
        print(f"    {line}")
    _log_summary("CALLBACK PARSED SUMMARY")
    print(f"  Timestamp  : {event['received_at']}")
    print(f"  Source IP  : {event['ip_address']}")
    print(f"  Invoice    : {event['invoice_number']}  (ref: {event['client_invoice_ref']})")
    print(f"  Status     : {event['status'].upper()}")
    print(f"  Amount paid: {event['amount_paid']} {event['currency']}")
    print("  Saved to Monitor as payment_channel=CALLBACK")

    response = {
        "ok": True,
        "captured": True,
        "kind": "callback",
        "status": event["status"],
        "invoice_number": event["invoice_number"],
        "client_invoice_ref": event["client_invoice_ref"],
        "amount_paid": event["amount_paid"],
    }
    if request.method == "GET" and "text/html" in (request.headers.get("Accept") or ""):
        _log_response_marker("HTTP 200", "HTML confirmation page to browser")
        _log_session_end("BROWSER CALLBACK", "CAPTURED")
        return render_template_string(
            """
            <!doctype html>
            <title>CapitalPay Callback Captured</title>
            <body style="font-family:Arial,sans-serif;background:#0d0f14;color:#e8eaf0;padding:32px">
              <h2>Callback captured</h2>
              <p>Status: <strong>{{ status }}</strong></p>
              <p>Invoice: <strong>{{ invoice }}</strong></p>
              <p>Reference: <strong>{{ ref }}</strong></p>
              <p>You can close this tab and check the Dashboard monitor.</p>
            </body>
            """,
            status=event["status"],
            invoice=event["invoice_number"] or "-",
            ref=event["client_invoice_ref"] or "-",
        )
    _log_response_marker("HTTP 200", "JSON to client")
    _log_detail_block("Body", response)
    _log_session_end("BROWSER CALLBACK", "CAPTURED")
    return jsonify(response)


@server.route("/notify", methods=["GET", "POST"])
@server.route("/payment/notify", methods=["GET", "POST"])
def payment_notify():
    if request.method == "GET":
        return jsonify({"ok": True, "message": "CapitalPay notification listener is ready"})

    payload_parts, raw_body = _request_payload_parts()
    source_ip = notifications.get_client_ip(request)
    _log_session_start("BANK IPN /notify", f"{request.method} {request.path}")
    _log_request_marker("Notification IPN from CapitalPay / bank")
    print(f"  Method: {request.method} {request.path}")
    print(f"  CapitalPay notification URL hit: {request.url}")
    print(f"  Expected notification URL      : {capitalpay.default_notification_url()}")
    print("  Headers:")
    for key, value in _redact(dict(request.headers)).items():
        print(f"    {key}: {value}")
    print(f"  {IPN_PAYLOAD} Raw Body (notification IPN from bank):")
    _print_payload_lines(raw_body, "ipn")
    print(f"  {IPN_PAYLOAD} Parsed notification IPN payload:")
    _print_payload_lines(payload_parts, "ipn")
    print(f"  Source IP candidate: {source_ip}")
    allowlist_note = (
        "disabled — all IPs allowed"
        if not CAPITALPAY_ALLOWED_IPS
        else ", ".join(sorted(CAPITALPAY_ALLOWED_IPS))
    )
    print(f"  Allowlist         : {allowlist_note}")
    event = notifications.record_notification(request, capitalpay.API_SECRET, CAPITALPAY_ALLOWED_IPS)
    ip_note = (
        "all IPs allowed"
        if not CAPITALPAY_ALLOWED_IPS
        else ("on allowlist" if event.get("ip_allowed", True) else "not on allowlist (still recorded)")
    )
    _log_summary("IPN PARSED SUMMARY")
    print(f"  Timestamp       : {event['received_at']}")
    print(f"  Source IP       : {event['ip_address']}")
    print(f"  IP allowlist    : {ip_note}")
    print(f"  Payload quality : {event.get('payload_quality', 'UNKNOWN')}")
    missing = event.get("quality_missing") or []
    print(f"  Missing fields  : {', '.join(missing) if missing else 'none'}")
    print(f"  Invoice         : {event['invoice_number']}  (ref: {event['client_invoice_ref']})")
    print(f"  Status          : {event['status'].upper()}")
    print(f"  Amount paid     : {event['amount_paid']} {event['currency']}")
    print(f"  Channel         : {event['payment_channel']}")
    print(f"  Hash            : {'VALID' if event['valid_hash'] else 'INVALID'}")

    api_response = {
        "ok": True,
        "paid": event["status"] == "settled" and event.get("payload_quality") == "GOOD",
        "status": event["status"],
        "invoice_number": event["invoice_number"],
        "client_invoice_ref": event["client_invoice_ref"],
        "sender_ip": event["ip_address"],
        "valid_hash": event["valid_hash"],
        "payload_quality": event.get("payload_quality"),
        "quality_issues": event.get("quality_issues", []),
        "quality_missing": event.get("quality_missing", []),
        "ip_allowed": event.get("ip_allowed", True),
    }
    paid_flag = api_response["paid"]
    _log_response_marker("HTTP 200", f"paid={paid_flag}")
    _log_detail_block("Body", api_response)
    _log_session_end("BANK IPN /notify", "RECORDED")

    return jsonify(api_response)


@server.route("/test-notify", methods=["POST"])
def test_notify():
    event = notifications.inject_test()
    print(f"[MONITOR] Injected test event paid={event['status'] == 'settled'} ip={event['ip_address']}")
    return jsonify({"injected": 1, "event": event}), 200


@server.route("/api/last-cp-outbound", methods=["GET"])
def api_last_cp_outbound():
    return jsonify(LAST_CP_OUTBOUND)


@server.route("/api/notifications", methods=["GET"])
def api_notifications():
    return jsonify({"events": notifications.get_notifications(500)})


@server.route("/api/notifications", methods=["DELETE"])
def api_clear_notifications():
    notifications.clear_notifications()
    return jsonify({"cleared": True})


@server.route("/api/ips", methods=["GET"])
def api_ips():
    return jsonify({"ips": notifications.get_observed_ips()})


@server.route("/api/logs", methods=["GET"])
def api_logs():
    return jsonify({"lines": get_log_lines()})


@server.route("/api/logs", methods=["DELETE"])
def api_logs_clear():
    while not LOG_QUEUE.empty():
        try:
            LOG_QUEUE.get_nowait()
        except queue.Empty:
            break
    return jsonify({"cleared": True})


def open_browser():
    time.sleep(1.5)
    webbrowser.open(f"http://127.0.0.1:{capitalpay.LOCAL_PORT}/")
    time.sleep(0.3)
    webbrowser.open(f"http://127.0.0.1:{capitalpay.LOCAL_PORT}/dash/")


if __name__ == "__main__":
    notifications.init_db()
    sep = "=" * 54
    print("")
    print(sep)
    print(f"  CapitalPay  |  Account ID: {capitalpay.ACCOUNT_ID}  |  TZS")
    print(sep)
    print(f"  Checkout    ->  {HOST}/")
    print(f"  Dashboard   ->  {HOST}/dash/   (user: CP  pass: CP123)")
    print(f"  Webhook     ->  {HOST}/notify")
    print(sep)
    print("  Every POST to /notify is recorded (all IPs allowed unless CAPITALPAY_ALLOWED_IPS is set)")
    print(sep)
    print("")
    if not os.environ.get("RENDER_EXTERNAL_URL"):
        threading.Thread(target=open_browser, daemon=True).start()
    app.run_server(host="0.0.0.0", port=capitalpay.LOCAL_PORT, debug=False, use_reloader=False)
