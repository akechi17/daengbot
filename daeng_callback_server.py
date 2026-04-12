from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import logging
import os
from urllib.parse import urlparse, parse_qs
from pathlib import Path
import requests

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_ID = os.getenv("ALLOWED_USER_ID", "").strip()
HOST = "127.0.0.1"
PORT = 9097
STATE_FILE = Path("/root/daengbot/callback_notified.json")

SUCCESS_STATUSES = {"success", "sukses", "completed", "done", "berhasil", "true"}
FAIL_STATUSES = {"failed", "gagal", "error", "cancel", "canceled", "cancelled", "dibatalkan", "false"}

def load_state():
    try:
        if STATE_FILE.exists():
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        logging.exception("Gagal baca state file")
    return {}

def save_state(state):
    try:
        STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        logging.exception("Gagal simpan state file")

def send_telegram(text: str):
    if not BOT_TOKEN or not CHAT_ID:
        logging.warning("TELEGRAM_BOT_TOKEN / ALLOWED_USER_ID kosong, skip notif Telegram")
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            data={"chat_id": CHAT_ID, "text": text[:4000]},
            timeout=20,
        )
    except Exception as e:
        logging.exception("Gagal kirim notif Telegram: %s", e)

def pick(payload: dict, *keys):
    for key in keys:
        if key in payload:
            val = payload.get(key)
            if val not in (None, "", [], {}):
                return str(val)
    return ""

def normalize_status(status: str) -> str:
    return (status or "").strip().lower()

def get_final_status(payload: dict) -> str:
    status = pick(payload, "order_status", "orderStatus")
    if status:
        return status
    status = pick(payload, "status")
    if status:
        return status
    return ""

def should_notify(status: str) -> bool:
    s = normalize_status(status)
    return s in SUCCESS_STATUSES or s in FAIL_STATUSES

def status_icon(status: str) -> str:
    s = normalize_status(status)
    if s in SUCCESS_STATUSES:
        return "✅"
    if s in FAIL_STATUSES:
        return "❌"
    return "📥"

def pretty_status(status: str) -> str:
    s = normalize_status(status)
    if s in SUCCESS_STATUSES:
        return "SUCCESS"
    if s in FAIL_STATUSES:
        return "GAGAL"
    return (status or "-").upper()

def make_dedupe_key(payload: dict) -> str:
    ref_id = pick(payload, "ref_id", "refid", "reference_id")
    trx_id = pick(payload, "trxid", "trx_id", "id")
    invoice = pick(payload, "invoice", "invoice_id", "inv")
    status = normalize_status(get_final_status(payload))

    primary = ref_id or trx_id or invoice
    if not primary:
        primary = json.dumps(payload, sort_keys=True, ensure_ascii=False)

    return f"{primary}::{status}"

def already_notified(payload: dict) -> bool:
    key = make_dedupe_key(payload)
    state = load_state()
    return key in state

def mark_notified(payload: dict):
    key = make_dedupe_key(payload)
    state = load_state()
    state[key] = True
    if len(state) > 5000:
        items = list(state.items())[-3000:]
        state = dict(items)
    save_state(state)

def format_target(value):
    if isinstance(value, list):
        parts = []
        for item in value:
            if isinstance(item, dict):
                for k, v in item.items():
                    parts.append(f"{k}: {v}")
            else:
                parts.append(str(item))
        return " | ".join(parts)

    if isinstance(value, dict):
        return " | ".join(f"{k}: {v}" for k, v in value.items())

    if value in (None, "", [], {}):
        return ""

    return str(value)

def compact_payload_lines(payload: dict):
    lines = []
    for k, v in payload.items():
        if isinstance(v, list):
            if all(isinstance(x, dict) for x in v):
                joined = []
                for item in v:
                    joined.append(", ".join(f"{ik}: {iv}" for ik, iv in item.items()))
                value = " | ".join(joined)
            else:
                value = ", ".join(map(str, v))
        elif isinstance(v, dict):
            value = ", ".join(f"{ik}: {iv}" for ik, iv in v.items())
        else:
            value = str(v)
        lines.append(f"- {k}: {value}")
    return "\n".join(lines)

def build_message(payload: dict) -> str:
    ref_id = pick(payload, "ref_id", "refid", "reference_id")
    status = get_final_status(payload)
    trx_id = pick(payload, "trxid", "trx_id", "id")
    service = pick(payload, "service", "services", "product", "product_name", "layanan")
    invoice = pick(payload, "invoice", "invoice_id", "inv")
    note = pick(payload, "message", "msg", "description", "keterangan")
    sn = pick(payload, "sn", "serial_number", "serial")

    target_raw = payload.get("data")
    if target_raw in (None, "", [], {}):
        target_raw = pick(payload, "target", "tujuan", "user_id", "userid")
    target = format_target(target_raw)

    icon = status_icon(status)

    lines = [
        f"{icon} UPDATE ORDER DAENG",
        "━━━━━━━━━━━━━━",
    ]

    if service:
        lines.append(f"Produk  : {service}")
    if target:
        lines.append(f"Tujuan  : {target}")
    if ref_id:
        lines.append(f"Ref ID  : {ref_id}")
    if trx_id:
        lines.append(f"Trx ID  : {trx_id}")
    if invoice:
        lines.append(f"Invoice : {invoice}")

    lines.append(f"Status  : {pretty_status(status)}")

    if sn:
        lines.append(f"SN      : {sn}")
    elif note:
        lines.append(f"Catatan : {note}")

    detail = compact_payload_lines(payload)
    if len(detail) > 2500:
        detail = detail[:2500] + "\n... (dipotong)"

    lines.extend([
        "",
        "Detail Callback:",
        detail
    ])

    return "\n".join(lines).strip()

class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        return

    def _json_response(self, code: int, data: dict):
        body = json.dumps(data).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def parse_payload(self, body_text: str, ctype: str):
        try:
            if "application/json" in ctype:
                return json.loads(body_text) if body_text else {}
            if "application/x-www-form-urlencoded" in ctype:
                form = parse_qs(body_text, keep_blank_values=True)
                return {k: (v[0] if isinstance(v, list) and v else "") for k, v in form.items()}
            try:
                return json.loads(body_text) if body_text else {}
            except Exception:
                form = parse_qs(body_text, keep_blank_values=True)
                if form:
                    return {k: (v[0] if isinstance(v, list) and v else "") for k, v in form.items()}
                return {"raw_body": body_text}
        except Exception:
            return {"raw_body": body_text}

    def do_POST(self):
        path = urlparse(self.path).path
        if path != "/callback":
            self._json_response(404, {"ok": False, "message": "Not found"})
            return

        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length > 0 else b""
        body_text = raw.decode("utf-8", errors="ignore")
        ctype = (self.headers.get("Content-Type") or "").lower()

        payload = self.parse_payload(body_text, ctype)

        logging.info("Callback diterima: %s", payload)

        status = get_final_status(payload)
        if should_notify(status):
            if already_notified(payload):
                logging.info("Skip notif duplikat untuk status final")
            else:
                send_telegram(build_message(payload))
                mark_notified(payload)
        else:
            logging.info("Skip notif untuk status non-final: %s", status)

        self._json_response(200, {"ok": True})

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/health":
            self._json_response(200, {"ok": True, "service": "daeng-callback"})
            return
        self._json_response(404, {"ok": False, "message": "Not found"})

if __name__ == "__main__":
    server = HTTPServer((HOST, PORT), Handler)
    logging.info("Daeng callback server jalan di http://%s:%s", HOST, PORT)
    server.serve_forever()
