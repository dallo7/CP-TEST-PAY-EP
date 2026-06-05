import hashlib
import hmac
import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any


DB_PATH = os.environ.get("CAPITALPAY_NOTIFICATION_DB", "capitalpay_notifications.sqlite3")


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


@contextmanager
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        init_db(conn)
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db(conn=None):
    should_close = conn is None
    conn = conn or sqlite3.connect(DB_PATH)
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS notifications (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                received_at TEXT NOT NULL,
                ip_address TEXT NOT NULL,
                method TEXT NOT NULL,
                headers_json TEXT NOT NULL,
                raw_payload TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                status TEXT NOT NULL,
                valid_hash INTEGER NOT NULL,
                hash_value TEXT,
                expected_hash TEXT,
                invoice_number TEXT,
                client_invoice_ref TEXT,
                phone_number TEXT,
                payment_date TEXT,
                payment_channel TEXT,
                currency TEXT,
                invoice_amount TEXT,
                last_payment_amount TEXT,
                amount_paid TEXT,
                payment_references_json TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS capitalpay_ips (
                ip_address TEXT PRIMARY KEY,
                first_seen TEXT NOT NULL,
                last_seen TEXT NOT NULL,
                hit_count INTEGER NOT NULL DEFAULT 1
            )
            """
        )
        conn.commit()
    finally:
        if should_close:
            conn.close()


def _normalize_key(key: str) -> str:
    return "".join(ch for ch in key.lower() if ch.isalnum())


def _walk_values(payload: Any):
    if isinstance(payload, dict):
        for key, value in payload.items():
            yield _normalize_key(str(key)), value
            yield from _walk_values(value)
    elif isinstance(payload, list):
        for item in payload:
            yield from _walk_values(item)


def _first(payload: Any, *names: str, default: str = "") -> str:
    wanted = {_normalize_key(name) for name in names}
    for key, value in _walk_values(payload):
        if key in wanted and value not in (None, ""):
            return str(value)
    return default


def _amount(value: Any) -> str:
    if value in (None, ""):
        return "0.00"
    try:
        return f"{Decimal(str(value).replace(',', '')):.2f}"
    except (InvalidOperation, ValueError):
        return "0.00"


def _load_payload(request) -> tuple[dict[str, Any], str]:
    raw = request.get_data(as_text=True) or ""
    if request.is_json:
        return request.get_json(silent=True) or {}, raw
    if request.form:
        return request.form.to_dict(flat=False), raw
    try:
        return json.loads(raw), raw
    except ValueError:
        return {"raw": raw}, raw


def get_client_ip(request) -> str:
    forwarded_for = request.headers.get("X-Forwarded-For", "")
    if forwarded_for:
        return forwarded_for.split(",")[0].strip()
    return (
        request.headers.get("X-Real-IP")
        or request.headers.get("CF-Connecting-IP")
        or request.remote_addr
        or "unknown"
    )


def _payment_references(payload: dict[str, Any]) -> list[dict[str, str]]:
    refs = None
    for key, value in _walk_values(payload):
        if key in {"paymentreferences", "paymentrefs", "payments"} and isinstance(value, list):
            refs = value
            break

    if refs is None:
        payment_ref = _first(payload, "payment_reference", "payment_ref", "reference", "receipt")
        refs = [
            {
                "payment_reference": payment_ref,
                "payment_date": _first(payload, "payment_date", "paid_at", "transaction_date"),
                "currency": _first(payload, "currency", default="TZS"),
                "amount": _amount(_first(payload, "last_payment_amount", "amount_paid", "amount")),
            }
        ] if payment_ref else []

    cleaned = []
    for ref in refs:
        if not isinstance(ref, dict):
            continue
        cleaned.append(
            {
                "payment_reference": _first(ref, "payment_reference", "payment_ref", "reference", "receipt"),
                "payment_date": _first(ref, "payment_date", "paid_at", "transaction_date"),
                "currency": _first(ref, "currency", default="TZS"),
                "amount": _amount(_first(ref, "amount", "amount_paid", "value")),
            }
        )
    return cleaned


def _status(payload: dict[str, Any]) -> str:
    raw_status = _first(
        payload,
        "status",
        "payment_status",
        "invoice_status",
        "transaction_status",
        "state",
        default="pending",
    ).lower()
    if any(word in raw_status for word in ("settled", "paid", "success", "complete")):
        return "settled"
    if any(word in raw_status for word in ("failed", "cancel", "reject", "error")):
        return "failed"
    return "pending"


def _candidate_hashes(payload: dict[str, Any], raw_payload: str, secret: str) -> list[str]:
    if not secret:
        return []
    values = [
        _first(payload, "invoice_number", "invoice_no"),
        _first(payload, "client_invoice_ref", "bill_ref_number", "billRefNumber"),
        _first(payload, "amount_paid", "amount", "last_payment_amount"),
        _first(payload, "currency", default="TZS"),
        _status(payload),
    ]
    compact_json = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    candidates = [raw_payload, compact_json, "".join(values), "|".join(values)]
    return [hmac.new(secret.encode(), c.encode(), hashlib.sha256).hexdigest() for c in candidates if c]


def _hash_check(payload: dict[str, Any], raw_payload: str, secret: str) -> tuple[bool, str, str]:
    supplied = _first(payload, "secureHash", "secure_hash", "hash", "signature", "checksum")
    if not supplied:
        return True, "", ""
    candidates = _candidate_hashes(payload, raw_payload, secret)
    supplied_clean = supplied.strip().lower()
    valid = any(hmac.compare_digest(supplied_clean, candidate.lower()) for candidate in candidates)
    return valid, supplied, candidates[0] if candidates else ""


def _event_from_payload(payload: dict[str, Any], raw_payload: str, ip_address: str, api_secret: str) -> dict[str, Any]:
    refs = _payment_references(payload)
    amount_paid = _amount(_first(payload, "amount_paid", "total_paid", "paid_amount"))
    if amount_paid == "0.00" and refs:
        total = sum(Decimal(ref["amount"]) for ref in refs)
        amount_paid = f"{total:.2f}"

    valid_hash, hash_value, expected_hash = _hash_check(payload, raw_payload, api_secret)
    return {
        "received_at": utc_now(),
        "ip_address": ip_address,
        "status": _status(payload),
        "valid_hash": valid_hash,
        "hash_value": hash_value,
        "expected_hash": expected_hash,
        "invoice_number": _first(payload, "invoice_number", "invoice_no", "invoice"),
        "client_invoice_ref": _first(payload, "client_invoice_ref", "bill_ref_number", "billRefNumber"),
        "phone_number": _first(payload, "phone_number", "msisdn", "clientMSISDN"),
        "payment_date": _first(payload, "payment_date", "paid_at", "transaction_date"),
        "payment_channel": _first(payload, "payment_channel", "channel", "gateway"),
        "currency": _first(payload, "currency", default="TZS"),
        "invoice_amount": _amount(_first(payload, "invoice_amount", "amount_expected", "amountExpected")),
        "last_payment_amount": _amount(_first(payload, "last_payment_amount", "amount", "amount_paid")),
        "amount_paid": amount_paid,
        "payment_references": refs,
    }


def record_notification(request, api_secret: str) -> dict[str, Any]:
    payload, raw_payload = _load_payload(request)
    ip_address = get_client_ip(request)
    event = _event_from_payload(payload, raw_payload, ip_address, api_secret)
    headers_json = json.dumps(dict(request.headers), sort_keys=True)
    payload_json = json.dumps(payload, sort_keys=True)
    refs_json = json.dumps(event["payment_references"], sort_keys=True)

    with get_db() as db:
        db.execute(
            """
            INSERT INTO capitalpay_ips (ip_address, first_seen, last_seen, hit_count)
            VALUES (?, ?, ?, 1)
            ON CONFLICT(ip_address) DO UPDATE SET
                last_seen = excluded.last_seen,
                hit_count = capitalpay_ips.hit_count + 1
            """,
            (ip_address, event["received_at"], event["received_at"]),
        )
        db.execute(
            """
            INSERT INTO notifications (
                received_at, ip_address, method, headers_json, raw_payload, payload_json,
                status, valid_hash, hash_value, expected_hash, invoice_number,
                client_invoice_ref, phone_number, payment_date, payment_channel,
                currency, invoice_amount, last_payment_amount, amount_paid,
                payment_references_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event["received_at"],
                ip_address,
                request.method,
                headers_json,
                raw_payload,
                payload_json,
                event["status"],
                1 if event["valid_hash"] else 0,
                event["hash_value"],
                event["expected_hash"],
                event["invoice_number"],
                event["client_invoice_ref"],
                event["phone_number"],
                event["payment_date"],
                event["payment_channel"],
                event["currency"],
                event["invoice_amount"],
                event["last_payment_amount"],
                event["amount_paid"],
                refs_json,
            ),
        )
    return event


def get_notifications(limit: int = 50) -> list[dict[str, Any]]:
    with get_db() as db:
        rows = db.execute(
            "SELECT * FROM notifications ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()

    events = []
    for row in rows:
        event = dict(row)
        event["valid_hash"] = bool(event["valid_hash"])
        event["payment_references"] = json.loads(event.pop("payment_references_json") or "[]")
        events.append(event)
    return events


def clear_notifications():
    with get_db() as db:
        db.execute("DELETE FROM notifications")
        db.execute("DELETE FROM capitalpay_ips")


def inject_test():
    now = utc_now()
    payload = {
        "status": "PAID",
        "invoice_number": "TEST-TZS-001",
        "client_invoice_ref": "TEST-TZS-REF",
        "phone_number": "+255712345678",
        "payment_date": now,
        "payment_channel": "TEST",
        "currency": "TZS",
        "invoice_amount": "1000.00",
        "amount_paid": "1000.00",
        "payment_references": [
            {
                "payment_reference": "TEST-CAPITALPAY-REF",
                "payment_date": now,
                "currency": "TZS",
                "amount": "1000.00",
            }
        ],
    }
    class TestRequest:
        method = "POST"
        headers = {"X-Forwarded-For": "197.250.0.10", "Content-Type": "application/json"}
        remote_addr = "127.0.0.1"
        is_json = True
        form = {}

        def get_data(self, as_text=False):
            data = json.dumps(payload)
            return data if as_text else data.encode()

        def get_json(self, silent=True):
            return payload

    return record_notification(TestRequest(), "")
