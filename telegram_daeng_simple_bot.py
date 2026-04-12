from __future__ import annotations

import json
import logging
import os
import re
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional

import requests
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("daeng_simple_bot")

API_BASE_URL = os.getenv("DAENG_API_BASE_URL", "https://api.daengdiamondstore.com").rstrip("/")
DAENG_BEARER_TOKEN = os.getenv("DAENG_BEARER_TOKEN", "").strip()
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
ALLOWED_USER_ID = os.getenv("ALLOWED_USER_ID", "").strip()
CALLBACK_URL = os.getenv("DAENG_CALLBACK_URL", "").strip()

ORDER_ENDPOINT = "/v2/order"
CHECK_ENDPOINT = "/v2/check"
SERVICES_ENDPOINT = "/v2/services"
INFO_ENDPOINT = "/v2/info"

PER_PAGE = 8


@dataclass
class Product:
    code: str
    name: str
    category: str = "Lainnya"
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class Draft:
    product_code: Optional[str] = None
    product_name: Optional[str] = None
    product_category: Optional[str] = None
    fields: list[str] = field(default_factory=list)
    values: dict[str, str] = field(default_factory=dict)
    current_index: int = 0
    ref_id: Optional[str] = None
    callback_url: Optional[str] = None

    def is_complete(self) -> bool:
        return self.current_index >= len(self.fields)


def is_allowed(user_id: int) -> bool:
    if not ALLOWED_USER_ID:
        return True
    return str(user_id) == str(ALLOWED_USER_ID)


def api_headers() -> dict[str, str]:
    headers = {}
    if DAENG_BEARER_TOKEN:
        headers["Authorization"] = f"Bearer {DAENG_BEARER_TOKEN}"
    return headers


def api_post(path: str, form_data: dict[str, str] | None = None) -> Any:
    if not DAENG_BEARER_TOKEN:
        raise RuntimeError("DAENG_BEARER_TOKEN belum diisi di file .env")

    url = f"{API_BASE_URL}{path}"
    response = requests.post(url, data=form_data or {}, headers=api_headers(), timeout=45)
    response.raise_for_status()
    try:
        return response.json()
    except Exception:
        return {"raw_text": response.text}


def get_services_raw() -> Any:
    return api_post(SERVICES_ENDPOINT, {})


def extract_products(payload: Any) -> list[Product]:
    products: list[Product] = []

    if not isinstance(payload, dict):
        return products

    data = payload.get("data", [])
    if not isinstance(data, list):
        return products

    for category_block in data:
        if not isinstance(category_block, dict):
            continue

        category_name = str(category_block.get("categories_name", "Lainnya")).strip() or "Lainnya"
        services = category_block.get("services", [])

        if not isinstance(services, list):
            continue

        for svc in services:
            if not isinstance(svc, dict):
                continue

            code = str(svc.get("code", "")).strip()
            name = str(svc.get("name", "")).strip()
            price_silver = str(svc.get("price_silver", "")).strip()
            price_gold = str(svc.get("price_gold", "")).strip()

            if not code or not name:
                continue

            display_name = name
            if price_silver:
                display_name += f" | Silver {price_silver}"
            if price_gold:
                display_name += f" | Gold {price_gold}"

            products.append(
                Product(
                    code=code,
                    name=display_name,
                    category=category_name,
                    raw=svc,
                )
            )

    dedup: dict[str, Product] = {}
    for p in products:
        dedup[f"{p.code}|{p.name}"] = p

    return list(dedup.values())

def first_nonempty(item: dict, keys: list[str]) -> Optional[str]:
    for key in keys:
        value = item.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    return None


def normalize(text: str) -> str:
    text = text.lower().strip()
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def infer_fields(product: Product) -> list[str]:
    raw = product.raw

    possible_keys = ["fields", "required_fields", "input_fields", "inputs", "data_fields"]
    for key in possible_keys:
        value = raw.get(key)
        fields = parse_fields_from_any(value)
        if fields:
            return fields

    name_norm = normalize(product.name + " " + product.category)

    if "mobile legends" in name_norm or name_norm.startswith("ml "):
        return ["User ID", "Server ID"]
    if "free fire" in name_norm or name_norm == "ff":
        return ["User ID"]
    if "pubg" in name_norm:
        return ["User ID"]
    if "arena breakout" in name_norm:
        return ["User ID"]
    if "roblox" in name_norm and "login" in name_norm:
        return ["Username", "Password"]
    if "roblox" in name_norm:
        return ["Username"]
    if "honkai" in name_norm or "genshin" in name_norm:
        return ["User ID", "Server"]
    if "weekly" in name_norm or "membership" in name_norm:
        return ["User ID", "Server ID"]

    return ["User ID"]


def parse_fields_from_any(value: Any) -> list[str]:
    if not value:
        return []

    if isinstance(value, list):
        out: list[str] = []
        for item in value:
            if isinstance(item, str) and item.strip():
                out.append(item.strip())
            elif isinstance(item, dict):
                name = (
                    item.get("name")
                    or item.get("label")
                    or item.get("field")
                    or item.get("title")
                )
                if name:
                    out.append(str(name).strip())
        return unique_keep_order(out)

    if isinstance(value, dict):
        out = []
        for k, v in value.items():
            if isinstance(v, dict):
                name = v.get("name") or v.get("label") or k
            else:
                name = k
            if name:
                out.append(str(name).strip())
        return unique_keep_order(out)

    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            parsed_fields = parse_fields_from_any(parsed)
            if parsed_fields:
                return parsed_fields
        except Exception:
            pass

        parts = re.split(r"[,|\n;/]+", value)
        out = [p.strip() for p in parts if p.strip()]
        return unique_keep_order(out)

    return []


def unique_keep_order(items: list[str]) -> list[str]:
    seen = set()
    out = []
    for item in items:
        key = normalize(item)
        if key and key not in seen:
            seen.add(key)
            out.append(item)
    return out


def build_main_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("Pilih Produk", callback_data="menu_produk")],
            [InlineKeyboardButton("Refresh Produk", callback_data="menu_refresh")],
            [InlineKeyboardButton("Cek Status", callback_data="menu_check")],
            [InlineKeyboardButton("Info Akun", callback_data="menu_info")],
        ]
    )


def build_categories_keyboard(categories: list[str]) -> InlineKeyboardMarkup:
    rows = []
    for cat in categories[:40]:
        rows.append([InlineKeyboardButton(cat, callback_data=f"cat|{cat}")])
    rows.append([InlineKeyboardButton("Cari Produk", callback_data="menu_search")])
    rows.append([InlineKeyboardButton("Menu Utama", callback_data="menu_home")])
    return InlineKeyboardMarkup(rows)


def build_products_keyboard(products: list[Product], page: int, prefix: str) -> InlineKeyboardMarkup:
    start = page * PER_PAGE
    page_items = products[start:start + PER_PAGE]
    rows = []
    for p in page_items:
        rows.append([InlineKeyboardButton(p.name[:55], callback_data=f"prd|{p.code}")])

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("<< Prev", callback_data=f"{prefix}|{page-1}"))
    if start + PER_PAGE < len(products):
        nav.append(InlineKeyboardButton("Next >>", callback_data=f"{prefix}|{page+1}"))
    if nav:
        rows.append(nav)

    rows.append([InlineKeyboardButton("Kembali", callback_data="menu_produk")])
    rows.append([InlineKeyboardButton("Menu Utama", callback_data="menu_home")])
    return InlineKeyboardMarkup(rows)


def build_confirm_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("Kirim Order", callback_data="send_order")],
            [InlineKeyboardButton("Batal", callback_data="cancel_order")],
        ]
    )


def get_products_cache(context: ContextTypes.DEFAULT_TYPE) -> list[Product]:
    cache = context.bot_data.get("products_cache")
    if isinstance(cache, list):
        return cache
    return refresh_products_cache(context)


def refresh_products_cache(context: ContextTypes.DEFAULT_TYPE) -> list[Product]:
    raw = get_services_raw()
    products = extract_products(raw)
    products.sort(key=lambda x: (normalize(x.category), normalize(x.name)))
    context.bot_data["products_cache"] = products
    return products


def get_categories(products: list[Product]) -> list[str]:
    seen = []
    keys = set()
    for p in products:
        key = normalize(p.category)
        if key and key not in keys:
            keys.add(key)
            seen.append(p.category)
    return seen


def get_draft(context: ContextTypes.DEFAULT_TYPE) -> Draft:
    draft = context.user_data.get("draft")
    if isinstance(draft, Draft):
        return draft
    draft = Draft()
    context.user_data["draft"] = draft
    return draft


def reset_draft(context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data["draft"] = Draft()
    context.user_data["awaiting"] = None


def build_data_array(draft: Draft) -> list[dict[str, str]]:
    return [{field: draft.values.get(field, "")} for field in draft.fields]


def format_order_preview(draft: Draft) -> str:
    lines = [
        "Konfirmasi Order",
        "",
        f"Produk: {draft.product_name}",
        f"Kode: {draft.product_code}",
        f"Ref ID: {draft.ref_id or '-'}",
        "",
        "Data Pelanggan:",
    ]
    for field in draft.fields:
        lines.append(f"- {field}: {draft.values.get(field, '-')}")
    return "\n".join(lines)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user or not is_allowed(user.id):
        await update.effective_message.reply_text("Akses ditolak.")
        return
    reset_draft(context)
    await update.effective_message.reply_text(
        "Bot Daeng siap.\n\nTinggal pilih produk, isi data pelanggan, lalu kirim order.",
        reply_markup=build_main_menu(),
    )


async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    user = query.from_user
    if not is_allowed(user.id):
        await query.edit_message_text("Akses ditolak.")
        return

    data = query.data or ""
    if data == "menu_home":
        reset_draft(context)
        await query.edit_message_text("Menu utama:", reply_markup=build_main_menu())
        return

    if data == "menu_refresh":
        try:
            products = refresh_products_cache(context)
            await query.edit_message_text(
                f"Produk berhasil di-refresh.\nTotal produk terbaca: {len(products)}",
                reply_markup=build_main_menu(),
            )
        except Exception as exc:
            await query.edit_message_text(f"Gagal refresh produk:\n{exc}", reply_markup=build_main_menu())
        return

    if data == "menu_produk":
        try:
            products = get_products_cache(context)
            categories = get_categories(products)
            await query.edit_message_text(
                "Pilih kategori produk:",
                reply_markup=build_categories_keyboard(categories),
            )
        except Exception as exc:
            await query.edit_message_text(f"Gagal mengambil produk:\n{exc}", reply_markup=build_main_menu())
        return

    if data == "menu_search":
        context.user_data["awaiting"] = "search_keyword"
        await query.edit_message_text(
            "Ketik nama produk yang mau dicari.\nContoh: mobile legends 86 diamond"
        )
        return

    if data == "menu_check":
        context.user_data["awaiting"] = "check_invoice"
        await query.edit_message_text("Masukkan invoice untuk cek status.")
        return

    if data == "menu_info":
        try:
            result = api_post(INFO_ENDPOINT, {})
            text = "Info akun:\n\n" + json.dumps(result, ensure_ascii=False, indent=2)
        except Exception as exc:
            text = f"Gagal ambil info akun:\n{exc}"
        await query.edit_message_text(text[:4000], reply_markup=build_main_menu())
        return

    if data.startswith("cat|"):
        category = data.split("|", 1)[1]
        products = [p for p in get_products_cache(context) if p.category == category]
        await query.edit_message_text(
            f"Pilih produk di kategori: {category}",
            reply_markup=build_products_keyboard(products, 0, f"pagecat|{category}"),
        )
        return

    if data.startswith("pagecat|"):
        _, category, page_str = data.split("|", 2)
        products = [p for p in get_products_cache(context) if p.category == category]
        await query.edit_message_text(
            f"Pilih produk di kategori: {category}",
            reply_markup=build_products_keyboard(products, int(page_str), f"pagecat|{category}"),
        )
        return

    if data.startswith("pagesearch|"):
        page = int(data.split("|", 1)[1])
        products = context.user_data.get("search_results", [])
        if not isinstance(products, list):
            products = []
        await query.edit_message_text(
            "Hasil pencarian produk:",
            reply_markup=build_products_keyboard(products, page, "pagesearch"),
        )
        return

    if data.startswith("prd|"):
        code = data.split("|", 1)[1]
        products = get_products_cache(context)
        product = next((p for p in products if p.code == code), None)
        if not product:
            await query.edit_message_text("Produk tidak ditemukan.", reply_markup=build_main_menu())
            return

        draft = get_draft(context)
        draft.product_code = product.code
        draft.product_name = product.name
        draft.product_category = product.category
        draft.fields = infer_fields(product)
        draft.values = {}
        draft.current_index = 0
        draft.ref_id = f"tg-{user.id}-{uuid.uuid4().hex[:8]}"
        draft.callback_url = CALLBACK_URL or ""
        context.user_data["awaiting"] = "customer_field"

        first_field = draft.fields[0] if draft.fields else "User ID"
        await query.edit_message_text(
            f"Produk dipilih:\n{product.name}\n\nMasukkan {first_field}:"
        )
        return

    if data == "send_order":
        draft = get_draft(context)
        if not draft.product_code or not draft.is_complete():
            await query.edit_message_text("Draft order belum lengkap.", reply_markup=build_main_menu())
            return

        try:
            result = api_post(
                ORDER_ENDPOINT,
                {
                    "data": json.dumps(build_data_array(draft), ensure_ascii=False),
                    "services": draft.product_code,
                    "ref_id": draft.ref_id or "",
                    "callback_url": draft.callback_url or "",
                },
            )
            text = "Order berhasil diproses.\n\n" + json.dumps(result, ensure_ascii=False, indent=2)
        except Exception as exc:
            text = f"Gagal kirim order:\n{exc}"

        reset_draft(context)
        await query.edit_message_text(text[:4000], reply_markup=build_main_menu())
        return

    if data == "cancel_order":
        reset_draft(context)
        await query.edit_message_text("Order dibatalkan.", reply_markup=build_main_menu())
        return


async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user or not is_allowed(user.id):
        await update.effective_message.reply_text("Akses ditolak.")
        return

    text = (update.effective_message.text or "").strip()
    awaiting = context.user_data.get("awaiting")

    if awaiting == "search_keyword":
        products = get_products_cache(context)
        q = normalize(text)
        results = [
            p for p in products
            if q in normalize(p.name) or q in normalize(p.category) or q in normalize(p.code)
        ]
        context.user_data["search_results"] = results
        context.user_data["awaiting"] = None

        if not results:
            await update.effective_message.reply_text("Produk tidak ditemukan.", reply_markup=build_main_menu())
            return

        await update.effective_message.reply_text(
            f"Ditemukan {len(results)} produk. Pilih salah satu:",
            reply_markup=build_products_keyboard(results, 0, "pagesearch"),
        )
        return

    if awaiting == "check_invoice":
        context.user_data["awaiting"] = None
        invoice = text
        try:
            result = api_post(CHECK_ENDPOINT, {"invoice": invoice})
            out = "Hasil cek status:\n\n" + json.dumps(result, ensure_ascii=False, indent=2)
        except Exception as exc:
            out = f"Gagal cek status:\n{exc}"
        await update.effective_message.reply_text(out[:4000], reply_markup=build_main_menu())
        return

    if awaiting == "customer_field":
        draft = get_draft(context)
        if not draft.fields or draft.current_index >= len(draft.fields):
            context.user_data["awaiting"] = None
            await update.effective_message.reply_text("Draft tidak valid.", reply_markup=build_main_menu())
            return

        field_name = draft.fields[draft.current_index]
        draft.values[field_name] = text
        draft.current_index += 1

        if draft.current_index < len(draft.fields):
            next_field = draft.fields[draft.current_index]
            await update.effective_message.reply_text(f"Masukkan {next_field}:")
            return

        context.user_data["awaiting"] = None
        await update.effective_message.reply_text(
            format_order_preview(draft),
            reply_markup=build_confirm_keyboard(),
        )
        return

    await update.effective_message.reply_text(
        "Silakan pilih menu dulu.",
        reply_markup=build_main_menu(),
    )


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await start(update, context)


def main() -> None:
    if not TELEGRAM_BOT_TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN belum diisi di file .env")
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CallbackQueryHandler(callback_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))

    logger.info("Bot berjalan...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()