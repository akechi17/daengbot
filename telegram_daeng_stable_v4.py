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
logger = logging.getLogger("daeng_stable_v4")

API_BASE_URL = os.getenv("DAENG_API_BASE_URL", "https://api.daengdiamondstore.com").rstrip("/")
DAENG_BEARER_TOKEN = os.getenv("DAENG_BEARER_TOKEN", "").strip()
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
ALLOWED_USER_ID = os.getenv("ALLOWED_USER_ID", "").strip()
CALLBACK_URL = os.getenv("DAENG_CALLBACK_URL", "").strip()

ORDER_ENDPOINT = "/v2/order"
CHECK_ENDPOINT = "/v2/check"
SERVICES_ENDPOINT = "/v2/services"
INFO_ENDPOINT = "/v2/info"

GAME_PER_PAGE = 10
PRODUCT_PER_PAGE = 8

FIELD_RULES = [
    (r"roblox", ["Username", "Password", "Backup Code"]),
    (r"mobile legends|mlbb|ml ", ["User ID", "Zone ID"]),
    (r"free fire|\bff\b", ["User ID"]),
    (r"arena breakout", ["User ID"]),
    (r"pubg", ["User ID"]),
    (r"genshin", ["User ID", "Server"]),
    (r"honkai", ["User ID", "Server"]),
    (r"steam", ["Login / Email"]),
    (r"valorant", ["Username"]),
    (r"weekly|membership|pass", ["User ID", "Zone ID"]),
]

MANUAL_GAME_MAP = {
    "mobile legends": "Mobile Legends",
    "mlbb": "Mobile Legends",
    "free fire": "Free Fire",
    "ff": "Free Fire",
    "roblox": "Roblox",
    "arena breakout": "Arena Breakout",
    "pubg": "PUBG",
    "honkai": "Honkai",
    "genshin": "Genshin",
}


@dataclass
class Product:
    code: str
    name: str
    game: str
    category: str = "Lainnya"
    price_silver: str = ""
    price_gold: str = ""
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def display_name(self) -> str:
        text = self.name
        if self.price_silver:
            text += f" | Silver {self.price_silver}"
        if self.price_gold:
            text += f" | Gold {self.price_gold}"
        return text


@dataclass
class Draft:
    product_code: Optional[str] = None
    product_name: Optional[str] = None
    game: Optional[str] = None
    category: Optional[str] = None
    fields: list[str] = field(default_factory=list)
    values: dict[str, str] = field(default_factory=dict)
    current_index: int = 0
    ref_id: Optional[str] = None
    callback_url: Optional[str] = None

    def is_complete(self) -> bool:
        return self.current_index >= len(self.fields)


def normalize(text: str) -> str:
    text = (text or "").lower().strip()
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def title_case(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip()).title()


def guess_game_name(category_name: str, service_name: str) -> str:
    combo = normalize(f"{category_name} {service_name}")
    for key, value in MANUAL_GAME_MAP.items():
        if key in combo:
            return value

    cat = re.sub(r"\b\d[\w\s.+-]*", "", category_name).strip()
    cat = re.sub(r"\b(silver|gold|diamond|diamonds|uc|cp|point|points|membership|weekly|monthly)\b", "", cat, flags=re.I).strip()
    cat = re.sub(r"\s+", " ", cat).strip()
    if cat:
        return title_case(cat)
    return title_case(category_name) or "Lainnya"


def parse_fields_from_any(value: Any) -> list[str]:
    if not value:
        return []
    if isinstance(value, list):
        out = []
        for item in value:
            if isinstance(item, str) and item.strip():
                out.append(item.strip())
            elif isinstance(item, dict):
                name = item.get("name") or item.get("label") or item.get("field") or item.get("title")
                if name:
                    out.append(str(name).strip())
        return unique_keep_order(out)
    if isinstance(value, dict):
        out = []
        for k, v in value.items():
            name = (v.get("name") or v.get("label") or k) if isinstance(v, dict) else k
            if name:
                out.append(str(name).strip())
        return unique_keep_order(out)
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            got = parse_fields_from_any(parsed)
            if got:
                return got
        except Exception:
            pass
        return unique_keep_order([p.strip() for p in re.split(r"[,|\n;/]+", value) if p.strip()])
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


def infer_fields_from_game(game: str, product_name: str, raw: dict[str, Any]) -> list[str]:
    combo = normalize(f"{game} {product_name}")
    for pattern, fields in FIELD_RULES:
        if re.search(pattern, combo):
            return fields[:]

    # fallback only if API one day includes field metadata
    for key in ("fields", "required_fields", "input_fields", "inputs", "data_fields"):
        value = raw.get(key)
        parsed = parse_fields_from_any(value)
        if parsed:
            return parsed

    return ["User ID"]


def is_allowed(user_id: int) -> bool:
    if not ALLOWED_USER_ID:
        return True
    return str(user_id) == str(ALLOWED_USER_ID)


def api_headers() -> dict[str, str]:
    if not DAENG_BEARER_TOKEN:
        raise RuntimeError("DAENG_BEARER_TOKEN belum diisi di file .env")
    return {"Authorization": f"Bearer {DAENG_BEARER_TOKEN}"}


def api_post(path: str, form_data: dict[str, str] | None = None) -> Any:
    url = f"{API_BASE_URL}{path}"
    response = requests.post(url, data=form_data or {}, headers=api_headers(), timeout=45)
    response.raise_for_status()
    try:
        return response.json()
    except Exception:
        return {"raw_text": response.text}


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

            game = guess_game_name(category_name, name)
            products.append(
                Product(
                    code=code,
                    name=name,
                    game=game,
                    category=category_name,
                    price_silver=price_silver,
                    price_gold=price_gold,
                    raw=svc,
                )
            )

    dedup: dict[str, Product] = {}
    for p in products:
        dedup[f"{p.code}|{p.name}|{p.category}"] = p
    return list(dedup.values())


def get_products_cache(context: ContextTypes.DEFAULT_TYPE) -> list[Product]:
    cache = context.bot_data.get("products_cache")
    if isinstance(cache, list):
        return cache
    return refresh_products_cache(context)


def refresh_products_cache(context: ContextTypes.DEFAULT_TYPE) -> list[Product]:
    raw = api_post(SERVICES_ENDPOINT, {})
    products = extract_products(raw)
    products.sort(key=lambda p: (normalize(p.game), normalize(p.name)))
    context.bot_data["products_cache"] = products
    return products


def get_games(products: list[Product]) -> list[str]:
    seen = set()
    games: list[str] = []
    for p in products:
        key = normalize(p.game)
        if key and key not in seen:
            seen.add(key)
            games.append(p.game)
    return games


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


def menu_main() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Order", callback_data="menu_games")],
        [InlineKeyboardButton("Cari Produk", callback_data="menu_search")],
        [InlineKeyboardButton("Cek Status", callback_data="menu_check")],
        [InlineKeyboardButton("Info Akun", callback_data="menu_info")],
        [InlineKeyboardButton("Refresh Produk", callback_data="menu_refresh")],
    ])


def nav_row(back_cb: str | None = None) -> list[InlineKeyboardButton]:
    row = []
    if back_cb:
        row.append(InlineKeyboardButton("Back", callback_data=back_cb))
    row.append(InlineKeyboardButton("Home", callback_data="menu_home"))
    return row


def paginate(items: list[Any], page: int, per_page: int) -> list[Any]:
    start = page * per_page
    return items[start:start + per_page]


def games_keyboard(games: list[str], page: int) -> InlineKeyboardMarkup:
    page_items = paginate(games, page, GAME_PER_PAGE)
    rows = [[InlineKeyboardButton(game[:55], callback_data=f"game|{game}|0")] for game in page_items]
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("<< Prev", callback_data=f"games_page|{page-1}"))
    if (page + 1) * GAME_PER_PAGE < len(games):
        nav.append(InlineKeyboardButton("Next >>", callback_data=f"games_page|{page+1}"))
    if nav:
        rows.append(nav)
    rows.append(nav_row())
    return InlineKeyboardMarkup(rows)


def products_keyboard(products: list[Product], game: str, page: int, search_mode: bool = False) -> InlineKeyboardMarkup:
    page_items = paginate(products, page, PRODUCT_PER_PAGE)
    rows = [[InlineKeyboardButton(p.display_name[:60], callback_data=f"prd|{p.code}")] for p in page_items]
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("<< Prev", callback_data=f"search_page|{page-1}" if search_mode else f"game|{game}|{page-1}"))
    if (page + 1) * PRODUCT_PER_PAGE < len(products):
        nav.append(InlineKeyboardButton("Next >>", callback_data=f"search_page|{page+1}" if search_mode else f"game|{game}|{page+1}"))
    if nav:
        rows.append(nav)
    rows.append(nav_row("menu_search" if search_mode else "menu_games"))
    return InlineKeyboardMarkup(rows)


def confirm_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Kirim Order", callback_data="send_order")],
        [InlineKeyboardButton("Batal", callback_data="cancel_order")],
        nav_row("menu_games"),
    ])


def format_order_preview(draft: Draft) -> str:
    lines = [
        "Konfirmasi Order",
        "",
        f"Game: {draft.game or '-'}",
        f"Produk: {draft.product_name or '-'}",
        f"Kode: {draft.product_code or '-'}",
        "",
        "Data Pelanggan:",
    ]
    for field in draft.fields:
        lines.append(f"- {field}: {draft.values.get(field, '-')}")
    lines.append("")
    lines.append("Kalau sudah benar, klik Kirim Order.")
    return "\n".join(lines)


def format_check_result(result: dict[str, Any]) -> str:
    lines = ["Hasil Cek Status", ""]
    lines.append(f"Invoice: {result.get('invoice', '-')}")
    lines.append(f"Produk: {result.get('product', '-')}")
    lines.append(f"Status: {result.get('order_status', '-')}")
    data_raw = result.get("data", "")
    if data_raw:
        try:
            parsed = json.loads(data_raw) if isinstance(data_raw, str) else data_raw
            if isinstance(parsed, list):
                lines.append("")
                lines.append("Data:")
                for item in parsed:
                    if isinstance(item, dict):
                        for k, v in item.items():
                            lines.append(f"- {k}: {v}")
        except Exception:
            pass
    return "\n".join(lines)


def format_info_result(result: dict[str, Any]) -> str:
    name = result.get("name") or result.get("username") or result.get("user") or result.get("email") or "-"
    balance = result.get("balance") or result.get("saldo") or result.get("deposit") or result.get("credit") or "-"
    lines = ["Info Akun", ""]
    lines.append(f"Nama: {name}")
    lines.append(f"Saldo: {balance}")
    return "\n".join(lines)


async def show_main(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str = "Daeng Order Bot\n\nTinggal pencet tombol di bawah.") -> None:
    if update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=menu_main())
    else:
        await update.effective_message.reply_text(text, reply_markup=menu_main())


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user or not is_allowed(user.id):
        await update.effective_message.reply_text("Akses ditolak.")
        return
    reset_draft(context)
    await show_main(update, context)


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
        await show_main(update, context)
        return

    if data == "menu_refresh":
        try:
            products = refresh_products_cache(context)
            games = get_games(products)
            await query.edit_message_text(
                f"Refresh berhasil.\n\nTotal game: {len(games)}\nTotal produk: {len(products)}",
                reply_markup=menu_main(),
            )
        except Exception as exc:
            await query.edit_message_text(f"Gagal refresh produk:\n{exc}", reply_markup=menu_main())
        return

    if data == "menu_games":
        try:
            products = get_products_cache(context)
            games = get_games(products)
            await query.edit_message_text("Pilih Game", reply_markup=games_keyboard(games, 0))
        except Exception as exc:
            await query.edit_message_text(f"Gagal mengambil game:\n{exc}", reply_markup=menu_main())
        return

    if data.startswith("games_page|"):
        page = int(data.split("|", 1)[1])
        games = get_games(get_products_cache(context))
        await query.edit_message_text("Pilih Game", reply_markup=games_keyboard(games, page))
        return

    if data.startswith("game|"):
        _, game, page_str = data.split("|", 2)
        page = int(page_str)
        products = [p for p in get_products_cache(context) if p.game == game]
        await query.edit_message_text(
            f"{game}\n\nPilih produk:",
            reply_markup=products_keyboard(products, game, page),
        )
        return

    if data == "menu_search":
        context.user_data["awaiting"] = "search_keyword"
        await query.edit_message_text("Ketik nama game atau produk.\nContoh: roblox, mobile legends, weekly", reply_markup=InlineKeyboardMarkup([nav_row()]))
        return

    if data.startswith("search_page|"):
        page = int(data.split("|", 1)[1])
        results = context.user_data.get("search_results", [])
        if not isinstance(results, list):
            results = []
        await query.edit_message_text("Hasil Pencarian", reply_markup=products_keyboard(results, "Search", page, search_mode=True))
        return

    if data == "menu_check":
        context.user_data["awaiting"] = "check_invoice"
        await query.edit_message_text("Masukkan invoice untuk cek status.", reply_markup=InlineKeyboardMarkup([nav_row()]))
        return

    if data == "menu_info":
        try:
            result = api_post(INFO_ENDPOINT, {})
            text = format_info_result(result if isinstance(result, dict) else {})
        except Exception as exc:
            text = f"Gagal ambil info akun:\n{exc}"
        await query.edit_message_text(text[:4000], reply_markup=menu_main())
        return

    if data.startswith("prd|"):
        code = data.split("|", 1)[1]
        product = next((p for p in get_products_cache(context) if p.code == code), None)
        if not product:
            await query.edit_message_text("Produk tidak ditemukan.", reply_markup=menu_main())
            return

        draft = get_draft(context)
        draft.product_code = product.code
        draft.product_name = product.display_name
        draft.game = product.game
        draft.category = product.category
        draft.fields = infer_fields_from_game(product.game, product.name, product.raw)
        draft.values = {}
        draft.current_index = 0
        draft.ref_id = f"tg-{user.id}-{uuid.uuid4().hex[:8]}"
        draft.callback_url = CALLBACK_URL or ""
        context.user_data["awaiting"] = "customer_field"

        first_field = draft.fields[0] if draft.fields else "User ID"
        await query.edit_message_text(
            f"Produk dipilih\n\n{product.display_name}\n\nMasukkan {first_field}:",
            reply_markup=InlineKeyboardMarkup([nav_row("menu_games")]),
        )
        return

    if data == "send_order":
        draft = get_draft(context)
        if not draft.product_code or not draft.is_complete():
            await query.edit_message_text("Draft order belum lengkap.", reply_markup=menu_main())
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
            invoice = result.get("invoice", "-")
            message = result.get("message", "-")
            text = f"Order berhasil dikirim\n\nInvoice: {invoice}\nMessage: {message}"
        except Exception as exc:
            text = f"Gagal kirim order:\n{exc}"
        reset_draft(context)
        await query.edit_message_text(text[:4000], reply_markup=menu_main())
        return

    if data == "cancel_order":
        reset_draft(context)
        await query.edit_message_text("Order dibatalkan.", reply_markup=menu_main())
        return


async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user or not is_allowed(user.id):
        await update.effective_message.reply_text("Akses ditolak.")
        return

    text = (update.effective_message.text or "").strip()
    awaiting = context.user_data.get("awaiting")

    if awaiting == "search_keyword":
        q = normalize(text)
        products = get_products_cache(context)
        results = [p for p in products if q in normalize(p.game) or q in normalize(p.name) or q in normalize(p.category) or q in normalize(p.code)]
        context.user_data["search_results"] = results
        context.user_data["awaiting"] = None
        if not results:
            await update.effective_message.reply_text("Produk tidak ditemukan.", reply_markup=menu_main())
            return
        await update.effective_message.reply_text(
            f"Ditemukan {len(results)} produk.",
            reply_markup=products_keyboard(results, "Search", 0, search_mode=True),
        )
        return

    if awaiting == "check_invoice":
        context.user_data["awaiting"] = None
        try:
            result = api_post(CHECK_ENDPOINT, {"invoice": text})
            out = format_check_result(result if isinstance(result, dict) else {})
        except Exception as exc:
            out = f"Gagal cek status:\n{exc}"
        await update.effective_message.reply_text(out[:4000], reply_markup=menu_main())
        return

    if awaiting == "customer_field":
        draft = get_draft(context)
        if not draft.fields or draft.current_index >= len(draft.fields):
            context.user_data["awaiting"] = None
            await update.effective_message.reply_text("Draft tidak valid.", reply_markup=menu_main())
            return

        field_name = draft.fields[draft.current_index]
        draft.values[field_name] = text
        draft.current_index += 1

        if draft.current_index < len(draft.fields):
            next_field = draft.fields[draft.current_index]
            await update.effective_message.reply_text(f"Masukkan {next_field}:", reply_markup=InlineKeyboardMarkup([nav_row("menu_games")]))
            return

        context.user_data["awaiting"] = None
        await update.effective_message.reply_text(
            format_order_preview(draft),
            reply_markup=confirm_keyboard(),
        )
        return

    await update.effective_message.reply_text("Pilih tombol di bawah.", reply_markup=menu_main())


def main() -> None:
    if not TELEGRAM_BOT_TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN belum diisi di file .env")
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(callback_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))
    logger.info("Bot berjalan...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
