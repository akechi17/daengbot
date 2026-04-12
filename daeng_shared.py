"""
Shared utilities for Daeng bot systems.
Provides common functions for deduplication, formatting, and state management
used by both the callback server and order watcher.
"""

import json
import logging
import os
from pathlib import Path
from typing import Any, Optional

# Status constants
SUCCESS_STATUSES = {"success", "sukses", "completed", "done", "berhasil", "true"}
FAIL_STATUSES = {"failed", "gagal", "error", "cancel", "canceled", "cancelled", "dibatalkan", "false"}

# State file paths
STATE_DIR = Path(os.getenv("DAENG_STATE_DIR", "/root/daengbot"))
NOTIFIED_STATE_FILE = STATE_DIR / "notified_orders.json"

# Telegram configuration
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_ID = os.getenv("ALLOWED_USER_ID", "").strip()


def normalize_status(status: str) -> str:
    """Normalize status string to lowercase for comparison."""
    return (status or "").strip().lower()


def is_final_status(status: str) -> bool:
    """Check if status is a final status (success or failed)."""
    s = normalize_status(status)
    return s in SUCCESS_STATUSES or s in FAIL_STATUSES


def should_notify_status(status: str) -> bool:
    """Check if we should send notification for this status."""
    return is_final_status(status)


def status_icon(status: str) -> str:
    """Get emoji icon for status."""
    s = normalize_status(status)
    if s in SUCCESS_STATUSES:
        return "✅"
    if s in FAIL_STATUSES:
        return "❌"
    return "📦"


def pretty_status(status: str) -> str:
    """Get pretty formatted status string."""
    s = normalize_status(status)
    if s in SUCCESS_STATUSES:
        return "SUCCESS"
    if s in FAIL_STATUSES:
        return "GAGAL"
    return (status or "-").upper()


def pick_value(data: dict, *keys: str) -> str:
    """Pick first non-empty value from dict using given keys."""
    for key in keys:
        if key in data:
            val = data.get(key)
            if val not in (None, "", [], {}):
                return str(val)
    return ""


def format_target_value(value: Any) -> str:
    """Format target value from various formats to string."""
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


def make_dedupe_key(invoice: str, status: str, ref_id: str = "", trx_id: str = "") -> str:
    """
    Create unified deduplication key for an order.
    Priority: invoice > ref_id > trx_id
    """
    invoice = str(invoice or "").strip()
    ref_id = str(ref_id or "").strip()
    trx_id = str(trx_id or "").strip()
    status = normalize_status(status)

    # Use the most reliable identifier available
    primary = invoice or ref_id or trx_id
    if not primary:
        # Fallback to prevent key collisions
        primary = f"{invoice}::{ref_id}::{trx_id}".strip(":")

    return f"{primary}::{status}"


def load_notified_state() -> dict:
    """Load notified orders state from file."""
    try:
        if NOTIFIED_STATE_FILE.exists():
            return json.loads(NOTIFIED_STATE_FILE.read_text(encoding="utf-8"))
    except Exception as e:
        logging.warning("Gagal baca notified state: %s", e)
    return {}


def save_notified_state(state: dict) -> None:
    """Save notified orders state to file."""
    try:
        # Ensure directory exists
        NOTIFIED_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)

        # Trim state if too large (keep last 3000 entries)
        if len(state) > 5000:
            items = list(state.items())[-3000:]
            state = dict(items)

        NOTIFIED_STATE_FILE.write_text(
            json.dumps(state, ensure_ascii=False, indent=2),
            encoding="utf-8"
        )
    except Exception as e:
        logging.exception("Gagal simpan notified state: %s", e)


def is_already_notified(invoice: str, status: str, ref_id: str = "", trx_id: str = "") -> bool:
    """Check if order has already been notified."""
    key = make_dedupe_key(invoice, status, ref_id, trx_id)
    state = load_notified_state()
    return key in state


def mark_as_notified(invoice: str, status: str, ref_id: str = "", trx_id: str = "") -> None:
    """Mark order as notified to prevent duplicates."""
    key = make_dedupe_key(invoice, status, ref_id, trx_id)
    state = load_notified_state()
    state[key] = True
    save_notified_state(state)


def send_telegram_notification(text: str) -> None:
    """Send notification to Telegram."""
    if not TELEGRAM_BOT_TOKEN or not CHAT_ID:
        logging.warning("TELEGRAM_BOT_TOKEN / ALLOWED_USER_ID kosong, skip notif")
        return

    try:
        import requests
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            data={"chat_id": CHAT_ID, "text": text[:4000]},
            timeout=20,
        )
    except Exception as e:
        logging.exception("Gagal kirim notif Telegram: %s", e)


def format_order_notification(
    status: str,
    product: str = "",
    target: str = "",
    invoice: str = "",
    ref_id: str = "",
    trx_id: str = "",
    sn: str = "",
    note: str = "",
    include_detail: bool = False,
    detail_data: dict = None
) -> str:
    """
    Format unified order notification message.
    This ensures consistent formatting across watcher and callback.
    """
    icon = status_icon(status)

    lines = [
        f"{icon} UPDATE ORDER DAENG",
        "━━━━━━━━━━━━━━",
    ]

    # Core order information
    if product:
        lines.append(f"Produk  : {product}")
    if target:
        lines.append(f"Tujuan  : {target}")
    if ref_id:
        lines.append(f"Ref ID  : {ref_id}")
    if trx_id:
        lines.append(f"Trx ID  : {trx_id}")
    if invoice:
        lines.append(f"Invoice : {invoice}")

    # Status
    lines.append(f"Status  : {pretty_status(status)}")

    # Additional info
    if sn:
        lines.append(f"SN      : {sn}")
    elif note:
        lines.append(f"Catatan : {note}")

    # Optional detail section (for callback server)
    if include_detail and detail_data:
        detail_text = compact_payload_lines(detail_data)
        if len(detail_text) > 2500:
            detail_text = detail_text[:2500] + "\n... (dipotong)"

        lines.extend([
            "",
            "Detail Callback:",
            detail_text
        ])

    return "\n".join(lines).strip()


def compact_payload_lines(payload: dict) -> str:
    """Convert payload dict to compact string format."""
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
