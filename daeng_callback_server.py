"""
Daeng Callback Server
Receives webhook callbacks from Daeng API and sends Telegram notifications.
Uses shared utilities to prevent duplicate notifications with watcher.
"""

from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import logging
import os
from urllib.parse import urlparse, parse_qs

from daeng_shared import (
    pick_value,
    format_target_value,
    normalize_status,
    is_final_status,
    should_notify_status,
    status_icon,
    pretty_status,
    make_dedupe_key,
    is_already_notified,
    mark_as_notified,
    send_telegram_notification,
    format_order_notification,
    compact_payload_lines,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

HOST = "127.0.0.1"
PORT = 9097


def get_final_status(payload: dict) -> str:
    """Extract final status from callback payload."""
    # Try various possible field names for status
    status = pick_value(payload, "order_status", "orderStatus")
    if status:
        return status
    status = pick_value(payload, "status")
    if status:
        return status
    return ""


class Handler(BaseHTTPRequestHandler):
    """HTTP request handler for Daeng callback endpoints."""

    def log_message(self, format, *args):
        """Suppress default logging."""
        return

    def _json_response(self, code: int, data: dict):
        """Send JSON response."""
        body = json.dumps(data).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def parse_payload(self, body_text: str, ctype: str) -> dict:
        """Parse request body based on content type."""
        try:
            if "application/json" in ctype:
                return json.loads(body_text) if body_text else {}
            if "application/x-www-form-urlencoded" in ctype:
                form = parse_qs(body_text, keep_blank_values=True)
                return {k: (v[0] if isinstance(v, list) and v else "") for k, v in form.items()}
            # Try JSON first, then form data
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
        """Handle POST requests for callbacks."""
        path = urlparse(self.path).path
        if path != "/callback":
            self._json_response(404, {"ok": False, "message": "Not found"})
            return

        # Read request body
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length > 0 else b""
        body_text = raw.decode("utf-8", errors="ignore")
        ctype = (self.headers.get("Content-Type") or "").lower()

        # Parse payload
        payload = self.parse_payload(body_text, ctype)
        logging.info("Callback diterima: %s", payload)

        # Extract order information
        status = get_final_status(payload)

        # Only notify for final statuses
        if should_notify_status(status):
            # Extract identifiers for deduplication
            ref_id = pick_value(payload, "ref_id", "refid", "reference_id")
            trx_id = pick_value(payload, "trxid", "trx_id", "id")
            invoice = pick_value(payload, "invoice", "invoice_id", "inv")

            # Check if already notified (prevents duplicates with watcher)
            if is_already_notified(invoice, status, ref_id, trx_id):
                logging.info("Skip notif duplikat untuk invoice=%s status=%s", invoice, status)
            else:
                # Format and send notification
                service = pick_value(payload, "service", "services", "product", "product_name", "layanan")
                note = pick_value(payload, "message", "msg", "description", "keterangan")
                sn = pick_value(payload, "sn", "serial_number", "serial")

                target_raw = payload.get("data")
                if target_raw in (None, "", [], {}):
                    target_raw = pick_value(payload, "target", "tujuan", "user_id", "userid")
                target = format_target_value(target_raw)

                message = format_order_notification(
                    status=status,
                    product=service,
                    target=target,
                    invoice=invoice,
                    ref_id=ref_id,
                    trx_id=trx_id,
                    sn=sn,
                    note=note,
                    include_detail=True,
                    detail_data=payload
                )

                send_telegram_notification(message)
                mark_as_notified(invoice, status, ref_id, trx_id)
                logging.info("Notif terkirim untuk invoice=%s status=%s", invoice, status)
        else:
            logging.info("Skip notif untuk status non-final: %s", status)

        self._json_response(200, {"ok": True})

    def do_GET(self):
        """Handle GET requests for health checks."""
        path = urlparse(self.path).path
        if path == "/health":
            self._json_response(200, {"ok": True, "service": "daeng-callback"})
            return
        self._json_response(404, {"ok": False, "message": "Not found"})


def main():
    """Start the callback server."""
    server = HTTPServer((HOST, PORT), Handler)
    logging.info("Daeng callback server jalan di http://%s:%s", HOST, PORT)
    server.serve_forever()


if __name__ == "__main__":
    main()
