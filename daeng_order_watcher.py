import json
import logging
import os
import time
from datetime import datetime, timezone
import requests

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

API_BASE_URL = os.getenv("DAENG_API_BASE_URL", "https://api.daengdiamondstore.com").rstrip("/")
DAENG_BEARER_TOKEN = os.getenv("DAENG_BEARER_TOKEN", "").strip()
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_ID = os.getenv("ALLOWED_USER_ID", "").strip()

CHECK_ENDPOINT = "/v2/check"
PENDING_FILE = "/root/daengbot/pending_orders.json"
NOTIFIED_FILE = "/root/daengbot/notified_orders.json"

SUCCESS_STATUSES = {"success", "sukses", "completed", "done", "berhasil"}
FAIL_STATUSES = {"failed", "gagal", "error", "cancel", "canceled", "cancelled", "dibatalkan"}

def utc_now():
    return datetime.now(timezone.utc)

def parse_dt(value: str):
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return utc_now()

def load_json(path, default):
    try:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(default, list) and isinstance(data, list):
                    return data
                if isinstance(default, dict) and isinstance(data, dict):
                    return data
    except Exception:
        logging.exception("Gagal baca %s", path)
    return default

def save_json(path, data):
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception:
        logging.exception("Gagal simpan %s", path)

def normalize_status(status):
    return str(status or "").strip().lower()

def is_final(status):
    s = normalize_status(status)
    return s in SUCCESS_STATUSES or s in FAIL_STATUSES

def pretty_status(status):
    s = normalize_status(status)
    if s in SUCCESS_STATUSES:
        return "SUCCESS"
    if s in FAIL_STATUSES:
        return "GAGAL"
    return str(status or "-").upper()

def status_icon(status):
    s = normalize_status(status)
    if s in SUCCESS_STATUSES:
        return "✅"
    if s in FAIL_STATUSES:
        return "❌"
    return "📦"

def dedupe_key(invoice, status):
    return f"{str(invoice).strip()}::{normalize_status(status)}"

def already_notified(invoice, status):
    data = load_json(NOTIFIED_FILE, {})
    return dedupe_key(invoice, status) in data

def mark_notified(invoice, status):
    data = load_json(NOTIFIED_FILE, {})
    data[dedupe_key(invoice, status)] = True
    if len(data) > 5000:
        items = list(data.items())[-3000:]
        data = dict(items)
    save_json(NOTIFIED_FILE, data)

def api_headers():
    headers = {"Accept": "application/json"}
    if DAENG_BEARER_TOKEN:
        headers["Authorization"] = f"Bearer {DAENG_BEARER_TOKEN}"
    return headers

def check_invoice(invoice):
    url = API_BASE_URL + CHECK_ENDPOINT
    resp = requests.post(url, data={"invoice": invoice}, headers=api_headers(), timeout=45)
    resp.raise_for_status()
    data = resp.json()
    return data if isinstance(data, dict) else {}

def send_telegram(text):
    if not TELEGRAM_BOT_TOKEN or not CHAT_ID:
        logging.warning("Telegram env kosong")
        return
    requests.post(
        f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
        data={"chat_id": CHAT_ID, "text": text[:4000]},
        timeout=20,
    )

def format_message(row, result):
    status = result.get("order_status") or result.get("status") or row.get("last_status") or "-"
    product = result.get("product") or row.get("product") or "-"
    invoice = result.get("invoice") or row.get("invoice") or "-"
    target = row.get("target") or "-"
    trx_id = result.get("trxid") or result.get("trx_id") or result.get("id") or ""
    sn = result.get("sn") or result.get("serial_number") or result.get("serial") or ""
    note = result.get("message") or result.get("msg") or ""

    lines = [
        f"{status_icon(status)} UPDATE ORDER DAENG",
        "━━━━━━━━━━━━━━",
        f"Produk  : {product}",
        f"Tujuan  : {target}",
        f"Invoice : {invoice}",
        f"Status  : {pretty_status(status)}",
    ]
    if trx_id:
        lines.append(f"Trx ID  : {trx_id}")
    if sn:
        lines.append(f"SN      : {sn}")
    elif note:
        lines.append(f"Catatan : {note}")
    return "\n".join(lines)

def next_interval_seconds(created_at_str):
    created_at = parse_dt(created_at_str)
    age = (utc_now() - created_at).total_seconds()

    if age < 15 * 60:
        return 30
    if age < 60 * 60:
        return 60
    if age < 6 * 60 * 60:
        return 300
    if age < 24 * 60 * 60:
        return 900
    return -1

def should_check(row):
    interval = next_interval_seconds(row.get("created_at", ""))
    if interval < 0:
        return "expired"

    last_checked = row.get("last_checked_at")
    if not last_checked:
        return True

    last_dt = parse_dt(last_checked)
    age = (utc_now() - last_dt).total_seconds()
    return age >= interval

def main():
    logging.info("Daeng order watcher aktif")
    while True:
        pending = load_json(PENDING_FILE, [])
        if not isinstance(pending, list):
            pending = []

        changed = False
        new_pending = []

        for row in pending:
            invoice = str(row.get("invoice", "")).strip()
            if not invoice:
                changed = True
                continue

            check_flag = should_check(row)
            if check_flag == "expired":
                status = row.get("last_status") or "timeout"
                if not already_notified(invoice, status):
                    send_telegram(
                        "⚠️ UPDATE ORDER DAENG\n"
                        "━━━━━━━━━━━━━━\n"
                        f"Produk  : {row.get('product', '-')}\n"
                        f"Tujuan  : {row.get('target', '-')}\n"
                        f"Invoice : {invoice}\n"
                        "Status  : BELUM FINAL 24 JAM\n"
                        "Catatan : Cek manual di dashboard."
                    )
                    mark_notified(invoice, status)
                changed = True
                continue

            if check_flag is True:
                try:
                    result = check_invoice(invoice)
                    status = result.get("order_status") or result.get("status") or row.get("last_status") or ""
                    row["last_status"] = str(status)
                    row["last_checked_at"] = utc_now().isoformat()
                    changed = True

                    logging.info("Watcher check %s => %s", invoice, status)

                    if is_final(status):
                        if not already_notified(invoice, status):
                            send_telegram(format_message(row, result))
                            mark_notified(invoice, status)
                        else:
                            logging.info("Skip notif duplikat watcher %s", invoice)
                        continue
                except Exception as e:
                    row["last_checked_at"] = utc_now().isoformat()
                    row["last_error"] = str(e)
                    changed = True
                    logging.warning("Gagal cek invoice %s: %s", invoice, e)

            new_pending.append(row)

        if changed:
            save_json(PENDING_FILE, new_pending)

        time.sleep(15)

if __name__ == "__main__":
    main()
