"""
Daeng Order Watcher
Background service that polls pending orders and checks their status.
Works with callback server to provide comprehensive order tracking:
- Callback-enabled orders: polled infrequently as fallback
- Manual orders: polled normally (no callback expected)
Uses shared utilities to prevent duplicate notifications.
"""

import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

from daeng_shared import (
    is_final_status,
    is_already_notified,
    mark_as_notified,
    send_telegram_notification,
    format_order_notification,
    pick_value,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

API_BASE_URL = os.getenv("DAENG_API_BASE_URL", "https://api.daengdiamondstore.com").rstrip("/")
DAENG_BEARER_TOKEN = os.getenv("DAENG_BEARER_TOKEN", "").strip()

CHECK_ENDPOINT = "/v2/check"
STATE_DIR = Path(os.getenv("DAENG_STATE_DIR", "/root/daengbot"))
PENDING_FILE = STATE_DIR / "pending_orders.json"


def utc_now():
    """Get current UTC datetime."""
    return datetime.now(timezone.utc)


def parse_dt(value: str):
    """Parse datetime string to UTC datetime."""
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return utc_now()


def load_json(path, default):
    """Load JSON file with fallback to default."""
    try:
        if path.exists():
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(default, list) and isinstance(data, list):
                    return data
                if isinstance(default, dict) and isinstance(data, dict):
                    return data
    except Exception as e:
        logging.exception("Gagal baca %s: %s", path, e)
    return default


def save_json(path, data):
    """Save data to JSON file."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logging.exception("Gagal simpan %s: %s", path, e)


def api_headers():
    """Get API headers with authorization."""
    headers = {"Accept": "application/json"}
    if DAENG_BEARER_TOKEN:
        headers["Authorization"] = f"Bearer {DAENG_BEARER_TOKEN}"
    return headers


def check_invoice(invoice: str) -> dict:
    """Check invoice status via Daeng API."""
    url = API_BASE_URL + CHECK_ENDPOINT
    resp = requests.post(
        url,
        data={"invoice": invoice},
        headers=api_headers(),
        timeout=45
    )
    resp.raise_for_status()
    data = resp.json()
    return data if isinstance(data, dict) else {}


def has_callback_enabled(row: dict) -> bool:
    """Check if order has callback URL configured."""
    callback_url = row.get("callback_url") or row.get("callbackUrl") or ""
    return bool(callback_url and str(callback_url).strip())


def get_poll_interval_seconds(row: dict) -> int:
    """
    Get polling interval based on order age and callback status.
    Callback-enabled orders are polled less frequently as fallback.
    """
    created_at_str = row.get("created_at", "")
    if not created_at_str:
        return 60  # Default 1 minute

    created_at = parse_dt(created_at_str)
    age = (utc_now() - created_at).total_seconds()

    # Check if callback is enabled
    is_callback_order = has_callback_enabled(row)

    if is_callback_order:
        # Callback orders: poll frequently as fallback due to Daeng API delays
        # Start polling immediately since callbacks are often delayed 10-30 minutes
        if age < 10 * 60:
            return 60  # Every 1 minute (first 10 min)
        if age < 30 * 60:
            return 120  # Every 2 minutes (10-30 min age)
        if age < 2 * 60 * 60:
            return 300  # Every 5 minutes (30 min - 2 hour age)
        if age < 6 * 60 * 60:
            return 600  # Every 10 minutes (2-6 hour age)
        return 900  # Every 15 minutes after 6 hours
    else:
        # Manual orders: poll normally
        if age < 15 * 60:
            return 30  # Every 30 seconds (15 min age)
        if age < 60 * 60:
            return 60  # Every minute (1 hour age)
        if age < 6 * 60 * 60:
            return 300  # Every 5 minutes (6 hour age)
        if age < 24 * 60 * 60:
            return 900  # Every 15 minutes (24 hour age)
        return -1  # Expired after 24 hours


def should_check_order(row: dict) -> tuple[bool, str]:
    """
    Check if order should be polled now.
    Returns (should_check, reason).
    """
    interval = get_poll_interval_seconds(row)

    if interval < 0:
        return (False, "expired" if interval < -1 else "too_new_callback")

    last_checked = row.get("last_checked_at")
    if not last_checked:
        return (True, "first_check")

    last_dt = parse_dt(last_checked)
    age = (utc_now() - last_dt).total_seconds()
    if age >= interval:
        return (True, f"interval_{interval}s")

    return (False, "too_soon")


def build_notification_from_result(row: dict, result: dict) -> str:
    """Build notification message from API check result and row data."""
    status = result.get("order_status") or result.get("status") or row.get("last_status") or ""
    product = result.get("product") or row.get("product") or ""
    invoice = result.get("invoice") or row.get("invoice") or ""
    target = row.get("target") or ""
    trx_id = result.get("trxid") or result.get("trx_id") or result.get("id") or ""
    ref_id = result.get("ref_id") or result.get("refid") or result.get("reference_id") or ""
    sn = result.get("sn") or result.get("serial_number") or result.get("serial") or ""
    note = result.get("message") or result.get("msg") or ""

    return format_order_notification(
        status=status,
        product=product,
        target=target,
        invoice=invoice,
        ref_id=ref_id,
        trx_id=trx_id,
        sn=sn,
        note=note
    )


def build_expired_notification(row: dict) -> str:
    """Build notification for orders that expired without final status."""
    return (
        "⚠️ UPDATE ORDER DAENG\n"
        "━━━━━━━━━━━━━━\n"
        f"Produk  : {row.get('product', '-')}\n"
        f"Tujuan  : {row.get('target', '-')}\n"
        f"Invoice : {row.get('invoice', '-')}\n"
        "Status  : BELUM FINAL 24 JAM\n"
        "Catatan : Cek manual di dashboard."
    )


def main():
    """Main watcher loop."""
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

            should_check, reason = should_check_order(row)

            if reason == "expired":
                # Order expired without final status
                status = row.get("last_status") or "timeout"
                if not is_already_notified(invoice, status):
                    send_telegram_notification(build_expired_notification(row))
                    mark_as_notified(invoice, status)
                    logging.info("Notif expired untuk invoice=%s", invoice)
                changed = True
                continue

            if reason == "too_new_callback":
                # Callback order too new, skip polling (wait for callback)
                logging.debug("Skip polling untuk callback order invoice=%s (terlalu baru)", invoice)
                new_pending.append(row)
                continue

            if should_check:
                try:
                    result = check_invoice(invoice)
                    status = result.get("order_status") or result.get("status") or row.get("last_status") or ""
                    row["last_status"] = str(status)
                    row["last_checked_at"] = utc_now().isoformat()
                    changed = True

                    logging.info("Watcher check %s => %s (reason: %s)", invoice, status, reason)

                    if is_final_status(status):
                        # Extract identifiers for deduplication
                        ref_id = pick_value(result, "ref_id", "refid", "reference_id")
                        trx_id = pick_value(result, "trxid", "trx_id", "id")

                        if not is_already_notified(invoice, status, ref_id, trx_id):
                            send_telegram_notification(build_notification_from_result(row, result))
                            mark_as_notified(invoice, status, ref_id, trx_id)
                            logging.info("Notif terkirim untuk invoice=%s status=%s", invoice, status)
                        else:
                            logging.info("Skip notif duplikat untuk invoice=%s status=%s", invoice, status)
                        # Remove from pending as it's final
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
