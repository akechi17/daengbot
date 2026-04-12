from __future__ import annotations

import json
import asyncio
import logging
import os
import re
import unicodedata
import uuid
from datetime import datetime
from dataclasses import dataclass, field
from difflib import SequenceMatcher, get_close_matches
from typing import Any, Iterable, Optional
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
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
logger = logging.getLogger("daeng_all_in_one_bot_v3")

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
ALLOWED_USER_ID = os.getenv("ALLOWED_USER_ID", "").strip()

API_BASE_URL = os.getenv("DAENG_API_BASE_URL", "https://api.daengdiamondstore.com").rstrip("/")
DAENG_BEARER_TOKEN = os.getenv("DAENG_BEARER_TOKEN", "").strip()
CALLBACK_URL = os.getenv("DAENG_CALLBACK_URL", "").strip()

ORDER_ENDPOINT = "/v2/order"
CHECK_ENDPOINT = "/v2/check"
SERVICES_ENDPOINT = "/v2/services"
INFO_ENDPOINT = "/v2/info"

BASE_URL = "https://daengdiamondstore.com"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/133.0.0.0 Safari/537.36"
)

GAME_PER_PAGE = 10
PRODUCT_PER_PAGE = 8
LIST_GAME_PER_PAGE = 10

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

ALIASES = {
    "ff": "Free Fire",
    "freefire": "Free Fire",
    "free fire": "Free Fire",
    "ml": "Mobile Legends Indonesia",
    "mlbb": "Mobile Legends Indonesia",
    "mobile legends": "Mobile Legends Indonesia",
    "mobile legends indo": "Mobile Legends Indonesia",
    "mobile legends indonesia": "Mobile Legends Indonesia",
    "ml global": "Mobile Legends Global",
    "mobile legends global": "Mobile Legends Global",
    "pubg": "PUBG Mobile",
    "pubgm": "PUBG Mobile",
    "arena breakout": "Arena Breakout",
    "aov": "Arena of Valor",
    "hok": "Honor of Kings",
    "fc mobile": "EA Sports FC Mobile (ID)",
    "robux": "Roblox Via Login",
    "roblox": "Roblox Via Login",
}

IGNORE_PRODUCT_LINES = {"select a item list", "pilih service", "maintenance", "data tidak ditemukan!", "tidak ada aktifitasi data."}
SECTION_END_MARKERS = {"masukkan jumlah total", "pilih metode pembayaran", "nomor whatsapp", "pesan sekarang!", "daeng diamond store", "kemitraan"}
COMMON_NOISE = {"close", "open menu", "search", "masuk", "daftar", "beranda", "cek transaksi", "lihat semua"}
INACTIVE_PHRASES = {
    "maintenance", "sedang maintenance", "under maintenance", "nonaktif", "non aktif",
    "coming soon", "segera hadir", "sementara tutup", "unavailable", "gangguan",
    "ditutup sementara", "currently unavailable", "sold out",
}
OFF_PATTERNS = [r"\bservice off\b", r"\bproduk off\b", r"\boff sementara\b", r"\btemporarily off\b", r"\boff/closed\b", r"\bclosed\b"]


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


@dataclass
class Category:
    name: str
    items: list[tuple[str, str]]


class PriceBotError(RuntimeError):
    pass


def extract_first_number(text: str) -> int:
    text = text or ""
    m = re.search(r"(\d[\d\.,]*)", text)
    if not m:
        return 10**12
    raw = re.sub(r"[^\d]", "", m.group(1))
    return int(raw) if raw else 10**12

def natural_text_key(text: str):
    text = normalize_name(text or "")
    parts = re.split(r"(\d+)", text)
    out = []
    for part in parts:
        if not part:
            continue
        if part.isdigit():
            out.append((0, int(part)))
        else:
            out.append((1, part))
    return out

def sort_products_for_menu(items):
    def key(p):
        name = getattr(p, "name", str(p)) or ""
        code = getattr(p, "code", "") or ""
        first_num = extract_first_number(name)
        has_num = 0 if first_num != 10**12 else 1
        silver = re.sub(r"\D", "", str(getattr(p, "price_silver", "") or ""))
        silver_num = int(silver) if silver else 0
        return (
            has_num,
            first_num,
            natural_text_key(name),
            silver_num,
            normalize_name(code),
        )
    return sorted(items, key=key)






class PriceListScraper:
    def __init__(self, timeout: int = 30):
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": USER_AGENT,
            "Accept-Language": "id-ID,id;q=0.9,en-US;q=0.8,en;q=0.7",
            "Referer": BASE_URL + "/",
        })
        self.timeout = timeout
        self._product_cache: list[str] | None = None

    def expand_query_targets(self, query: str) -> list[str]:
        q = normalize_name(query)
        if q in {"ml", "mlbb", "mobile legends"}:
            return ["Mobile Legends Indonesia", "Mobile Legends Global"]
        return [query]

    def generate(self, query: str, format_mode: str = "reseller") -> tuple[str, str]:
        query = clean(query)
        if not query:
            raise PriceBotError("Query kosong.")

        targets = self.expand_query_targets(query)
        outputs: list[str] = []
        titles: list[str] = []
        for target in targets:
            title, text = self.generate_single(target, format_mode)
            titles.append(title)
            outputs.append(text)

        if len(outputs) == 1:
            return titles[0], outputs[0]

        merged_title = clean(query).title()
        merged_text = "\n\n".join(outputs).strip()
        return merged_title, merged_text

    def generate_single(self, query: str, format_mode: str = "reseller") -> tuple[str, str]:
        page_url, html = self.resolve_page(query)
        categories, title = self.extract_categories_and_title(html)
        if not categories:
            raise PriceBotError(f"List harga tidak ketemu di halaman: {page_url}")
        return title, format_text(title, categories, format_mode=format_mode)

    def resolve_page(self, query: str) -> tuple[str, str]:
        if query.startswith("http://") or query.startswith("https://"):
            html = self.fetch_html(query)
            return query, html

        resolved_name = self.resolve_product_name(query)
        slug_candidates = self.build_slug_candidates(resolved_name, original_query=query)

        for slug in slug_candidates:
            page_url = urljoin(BASE_URL, f"/order/{slug}")
            try:
                html = self.fetch_html(page_url)
                if self.looks_like_product_page(html):
                    return page_url, html
            except Exception:
                pass

        raise PriceBotError(
            f"Gagal menemukan halaman produk untuk '{query}'. "
            f"Coba pakai nama game yang lebih spesifik atau pilih dari tombol Generate List."
        )

    def resolve_product_name(self, query: str) -> str:
        norm = normalize_name(query)
        products = self.fetch_product_names(force_refresh=True)
        if not products:
            return ALIASES.get(norm, query)

        exact_map = {normalize_name(name): name for name in products}

        if norm in ALIASES:
            alias_target = ALIASES[norm]
            alias_norm = normalize_name(alias_target)
            if alias_norm in exact_map:
                return exact_map[alias_norm]
            return alias_target

        if norm in exact_map:
            return exact_map[norm]

        contained = []
        for name in products:
            name_norm = normalize_name(name)
            if norm and (norm in name_norm or name_norm in norm):
                contained.append(name)

        if contained:
            contained.sort(key=lambda x: (len(normalize_name(x)), x))
            return contained[0]

        scored = []
        for name in products:
            ratio = SequenceMatcher(None, norm, normalize_name(name)).ratio()
            scored.append((ratio, name))
        scored.sort(reverse=True)
        best_score, best_name = scored[0]
        if best_score >= 0.60:
            return best_name

        close = get_close_matches(norm, list(exact_map.keys()), n=1, cutoff=0.6)
        if close:
            return exact_map[close[0]]

        return query

    def fetch_product_names(self, force_refresh: bool = False) -> list[str]:
        if self._product_cache is not None and not force_refresh:
            return self._product_cache

        html = self.fetch_html(urljoin(BASE_URL, "/price-list"))
        soup = BeautifulSoup(html, "html.parser")
        names: list[str] = []

        for option in soup.select("option"):
            text = clean(option.get_text(" ", strip=True))
            if not text:
                continue
            lower = text.lower()
            if lower in {"pilih produk", "pilih service"}:
                continue
            if "entries" in lower:
                continue
            if text.isdigit():
                continue
            names.append(text)

        # Keep product names separate for safer button selection
        seen: set[str] = set()
        cleaned: list[str] = []
        for name in names:
            norm = normalize_name(name)
            if not norm or norm in seen:
                continue
            seen.add(norm)
            cleaned.append(name)

        # Push common names into a friendlier order
        preferred = [
            "Mobile Legends Indonesia",
            "Mobile Legends Global",
            "Free Fire",
            "Roblox Via Login",
            "PUBG Mobile",
            "Honor of Kings",
            "Genshin Impact",
        ]
        ordered = []
        used = set()
        for want in preferred:
            for item in cleaned:
                if normalize_name(item) == normalize_name(want) and item not in used:
                    ordered.append(item)
                    used.add(item)
        for item in cleaned:
            if item not in used:
                ordered.append(item)

        self._product_cache = ordered
        return ordered

    def build_slug_candidates(self, resolved_name: str, original_query: str) -> list[str]:
        raw_candidates: list[str] = []
        for text in [resolved_name, original_query, ALIASES.get(normalize_name(original_query), "")]:
            if not text:
                continue
            raw_candidates.append(text)
            raw_candidates.append(strip_parenthetical(text))
            raw_candidates.append(text.replace("&", "and"))

        slug_candidates: list[str] = []
        seen: set[str] = set()
        for text in raw_candidates:
            slug = slugify(text)
            if not slug:
                continue
            variants = [slug, slug.replace("-and-", "-"), re.sub(r"-(id|sea|asia|global)$", "", slug)]
            for candidate in variants:
                candidate = candidate.strip("-")
                if candidate and candidate not in seen:
                    seen.add(candidate)
                    slug_candidates.append(candidate)
        return slug_candidates

    def fetch_html(self, url: str) -> str:
        response = self.session.get(url, timeout=self.timeout)
        response.raise_for_status()
        return response.text

    def looks_like_product_page(self, html: str) -> bool:
        soup = BeautifulSoup(html, "html.parser")
        lines = html_to_lines(soup)
        line_set = {normalize_name(x) for x in lines}
        return ("pilih nominal yang ingin anda beli" in line_set or "pilih item" in line_set or any(x.startswith("region ") for x in line_set))

    def extract_categories_and_title(self, html: str) -> tuple[list[Category], str]:
        soup = BeautifulSoup(html, "html.parser")
        title = extract_title(soup)
        lines = html_to_lines(soup)
        section = extract_nominal_section(lines)
        categories = parse_categories(section)
        return categories, title


class AppState:
    def __init__(self):
        self.scraper = PriceListScraper()


def normalize(text: str) -> str:
    text = (text or "").lower().strip()
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def normalize_name(text: str) -> str:
    text = unicodedata.normalize("NFKD", text)
    text = text.encode("ascii", "ignore").decode("ascii")
    text = text.lower().strip().replace("&", " and ")
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def title_case(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip()).title()


def clean(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def slugify(text: str) -> str:
    text = unicodedata.normalize("NFKD", text)
    text = text.encode("ascii", "ignore").decode("ascii")
    text = text.lower().replace("&", " and ")
    text = re.sub(r"[^a-z0-9]+", "-", text)
    return text.strip("-")


def strip_parenthetical(text: str) -> str:
    return clean(re.sub(r"\([^)]*\)", " ", text))


def remove_header(text: str, title: str) -> str:
    lines = text.splitlines()
    header_norm = normalize_name(title)
    out = []
    skipped_header = False
    for line in lines:
        if not skipped_header and normalize_name(line) == header_norm:
            skipped_header = True
            continue
        if not skipped_header and not line.strip():
            continue
        out.append(line)
    while out and not out[0].strip():
        out.pop(0)
    return "\n".join(out).strip()


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
    for key in ("fields", "required_fields", "input_fields", "inputs", "data_fields"):
        value = raw.get(key)
        parsed = parse_fields_from_any(value)
        if parsed:
            return parsed
    return ["User ID"]


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
            products.append(Product(
                code=code,
                name=name,
                game=game,
                category=category_name,
                price_silver=price_silver,
                price_gold=price_gold,
                raw=svc,
            ))

    dedup: dict[str, Product] = {}
    for p in products:
        dedup[f"{p.code}|{p.name}|{p.category}"] = p
    return list(dedup.values())


def extract_title(soup: BeautifulSoup) -> str:
    bad_titles = {"testimoni", "masukkan data akun anda", "pilih nominal yang ingin anda beli", "pilih metode pembayaran", "daeng diamond store", "daftar harga"}
    for tag in soup.find_all(["h1", "h2", "h3"]):
        text = clean(tag.get_text(" ", strip=True))
        if text and normalize_name(text) not in bad_titles:
            return text
    if soup.title and soup.title.string:
        title = clean(soup.title.string)
        title = re.split(r"\||-", title)[0].strip()
        if title:
            return title
    return "DAENG DIAMOND STORE"


def html_to_lines(soup: BeautifulSoup) -> list[str]:
    for tag in soup(["script", "style", "noscript", "svg", "iframe"]):
        tag.decompose()
    text = soup.get_text("\n", strip=True)
    raw_lines = [clean(line) for line in text.splitlines()]
    lines = []
    for line in raw_lines:
        if not line:
            continue
        if line.lower().startswith("http://") or line.lower().startswith("https://"):
            continue
        if normalize_name(line) in COMMON_NOISE:
            continue
        if lines and line == lines[-1]:
            continue
        lines.append(line)
    return lines


def extract_nominal_section(lines: list[str]) -> list[str]:
    start = 0
    for i, line in enumerate(lines):
        lower = normalize_name(line)
        if lower in {"pilih nominal yang ingin anda beli", "pilih item"}:
            start = i + 1
            break
    section = lines[start:]
    end = len(section)
    for i, line in enumerate(section):
        if normalize_name(line) in SECTION_END_MARKERS:
            end = i
            break
    return section[:end]


def looks_inactive(text: str) -> bool:
    n = normalize_name(text)
    if not n:
        return False
    if any(phrase in n for phrase in INACTIVE_PHRASES):
        return True
    return any(re.search(pat, n) for pat in OFF_PATTERNS)


def parse_categories(lines: list[str]) -> list[Category]:
    starts: list[int] = []
    for i in range(len(lines) - 1):
        current = clean(lines[i])
        nxt = normalize_name(lines[i + 1])
        if nxt == "select a item list" and not is_price_line(current):
            starts.append(i)

    categories: list[Category] = []
    for idx, start in enumerate(starts):
        name = clean(lines[start])
        if looks_inactive(name):
            continue
        block_end = starts[idx + 1] if idx + 1 < len(starts) else len(lines)
        block = lines[start + 1:block_end]
        items = parse_items(block)
        if items:
            categories.append(Category(name=name, items=items))
    return categories


def parse_items(lines: Iterable[str]) -> list[tuple[str, str]]:
    seq = [clean(x) for x in lines if clean(x)]
    items: list[tuple[str, str]] = []
    i = 0
    while i < len(seq):
        line = seq[i]
        lower = normalize_name(line)
        if lower in IGNORE_PRODUCT_LINES or lower in SECTION_END_MARKERS or line.isdigit():
            i += 1
            continue
        if is_price_line(line):
            i += 1
            continue
        if lower.startswith("region "):
            i += 1
            continue

        name = line
        j = i + 1
        window = []
        prices = []

        while j < len(seq):
            nxt = seq[j]
            nxt_lower = normalize_name(nxt)
            if nxt_lower in SECTION_END_MARKERS:
                break
            if (not is_price_line(nxt) and nxt_lower not in IGNORE_PRODUCT_LINES and not looks_inactive(nxt) and not nxt_lower.startswith("region ") and len(prices) > 0):
                break
            window.append(nxt)
            p = extract_price(nxt)
            if p:
                prices.append(p)
            j += 1
            if len(window) >= 8:
                break

        block_text = " | ".join([name] + window)
        if looks_inactive(block_text):
            i = max(j, i + 1)
            continue

        if prices:
            if not looks_inactive(name) and not looks_inactive(" | ".join(window)):
                items.append((name, prices[0]))
            i = max(j, i + 1)
        else:
            i += 1
    return items


def extract_price(text: str) -> str | None:
    match = re.search(r"Rp\s*[\d.]+(?:,\d+)?", text)
    if not match:
        return None
    price = re.sub(r"\s+", " ", match.group(0)).strip()
    return price.replace("Rp", "Rp ").replace("  ", " ").strip()


def is_price_line(text: str) -> bool:
    return extract_price(text) is not None


def format_text(title: str, categories: list[Category], format_mode: str = "reseller") -> str:
    mode = normalize_name(format_mode) or "reseller"
    if mode == "broadcast":
        lines = [f"LIST HARGA {title.upper()}", ""]
        for idx, category in enumerate(categories):
            if idx > 0:
                lines.append("")
            lines.append(category.name.upper())
            for item_name, price in category.items:
                lines.append(f"{item_name} - {price}")
    elif mode == "mentah":
        lines = [title.upper(), "=" * 40, ""]
        for idx, category in enumerate(categories):
            if idx > 0:
                lines.append("")
            lines.append("-" * 40)
            lines.append(category.name.upper())
            lines.append("-" * 40)
            for item_name, price in category.items:
                lines.append(f"{item_name} - {price}")
    else:
        lines = [title.upper(), ""]
        for idx, category in enumerate(categories):
            if idx > 0:
                lines.append("")
            lines.append(category.name.upper())
            for item_name, price in category.items:
                lines.append(f"{item_name} - {price}")
    return "\n".join(lines).strip()


def format_check_result(result: dict[str, Any]) -> str:
    lines = ["Hasil Cek Status", ""]
    lines.append(f"Invoice: {result.get('invoice', '-')}")
    lines.append(f"Produk: {result.get('product', '-')}")
    lines.append(f"Status: {result.get('order_status', '-')}")
    return "\n".join(lines)


def format_info_result(result: dict[str, Any]) -> str:
    name = result.get("name") or result.get("username") or result.get("user") or result.get("email") or "-"
    balance = result.get("balance") or result.get("saldo") or result.get("deposit") or result.get("credit") or "-"
    return f"Info Akun\n\nNama: {name}\nSaldo: {balance}"


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




PENDING_ORDERS_FILE = "/root/daengbot/pending_orders.json"
NOTIFIED_ORDERS_FILE = "/root/daengbot/notified_orders.json"

def save_pending_order(invoice: str, draft: Draft) -> None:
    invoice = str(invoice or "").strip()
    if not invoice or invoice == "-":
        return

    target_parts = []
    for k, v in (draft.values or {}).items():
        if v:
            target_parts.append(f"{k}: {v}")
    target = " | ".join(target_parts)

    row = {
        "invoice": invoice,
        "product": draft.product_name or "",
        "game": draft.game or "",
        "target": target,
        "created_at": datetime.utcnow().isoformat(),
        "last_status": "pending",
    }

    data = []
    try:
        if os.path.exists(PENDING_ORDERS_FILE):
            with open(PENDING_ORDERS_FILE, "r", encoding="utf-8") as f:
                loaded = json.load(f)
                if isinstance(loaded, list):
                    data = loaded
    except Exception:
        data = []

    # update kalau invoice sudah ada
    found = False
    for i, item in enumerate(data):
        if str(item.get("invoice", "")).strip() == invoice:
            data[i] = row
            found = True
            break
    if not found:
        data.append(row)

    with open(PENDING_ORDERS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def reset_draft(context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data["draft"] = Draft()
    context.user_data["awaiting"] = None


def build_data_array(draft: Draft) -> list[dict[str, str]]:
    return [{field: draft.values.get(field, "")} for field in draft.fields]


def get_state(context: ContextTypes.DEFAULT_TYPE) -> AppState:
    state = context.bot_data.get("state")
    if isinstance(state, AppState):
        return state
    state = AppState()
    context.bot_data["state"] = state
    return state


def get_list_format(context: ContextTypes.DEFAULT_TYPE) -> str:
    return context.user_data.get("list_format_mode", "reseller")


def set_list_format(context: ContextTypes.DEFAULT_TYPE, mode: str) -> None:
    context.user_data["list_format_mode"] = mode


def set_last_list_query(context: ContextTypes.DEFAULT_TYPE, query: str) -> None:
    context.user_data["last_list_query"] = query


def get_last_list_query(context: ContextTypes.DEFAULT_TYPE) -> Optional[str]:
    val = context.user_data.get("last_list_query")
    return val if isinstance(val, str) and val.strip() else None


def menu_main() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Order", callback_data="menu_games")],
        [InlineKeyboardButton("List Harga", callback_data="menu_list_home")],
        [InlineKeyboardButton("Cek Status", callback_data="menu_check")],
        [InlineKeyboardButton("Info Akun", callback_data="menu_info")],
        [InlineKeyboardButton("Refresh Produk API", callback_data="menu_refresh")],
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


def list_menu(context: ContextTypes.DEFAULT_TYPE) -> InlineKeyboardMarkup:
    current = get_list_format(context)
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Generate List", callback_data="menu_list_generate")],
        [InlineKeyboardButton("Cari Game Otomatis", callback_data="menu_list_search")],
        [InlineKeyboardButton(f"Format: {current.title()}", callback_data="menu_list_format")],
        [InlineKeyboardButton("Refresh Harga Terakhir", callback_data="menu_list_refresh_last")],
        nav_row(),
    ])


def list_games_keyboard(games: list[str], page: int) -> InlineKeyboardMarkup:
    start = page * LIST_GAME_PER_PAGE
    page_items = games[start:start + LIST_GAME_PER_PAGE]
    rows = [[InlineKeyboardButton(game[:55], callback_data=f"listgame|{game}")] for game in page_items]
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("<< Prev", callback_data=f"listgames_page|{page-1}"))
    if start + LIST_GAME_PER_PAGE < len(games):
        nav.append(InlineKeyboardButton("Next >>", callback_data=f"listgames_page|{page+1}"))
    if nav:
        rows.append(nav)
    rows.append(nav_row("menu_list_home"))
    return InlineKeyboardMarkup(rows)


def list_format_keyboard(current: str) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(("• " if current == "mentah" else "") + "Mentah", callback_data="listfmt|mentah")],
        [InlineKeyboardButton(("• " if current == "reseller" else "") + "Reseller", callback_data="listfmt|reseller")],
        [InlineKeyboardButton(("• " if current == "broadcast" else "") + "Broadcast", callback_data="listfmt|broadcast")],
        nav_row("menu_list_home"),
    ]
    return InlineKeyboardMarkup(rows)


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


def add_part_headers(chunks: list[str], base_title: str) -> list[str]:
    if len(chunks) <= 1:
        return chunks
    title = clean(base_title).upper() or "LIST HARGA"
    out = []
    total = len(chunks)
    for i, chunk in enumerate(chunks, 1):
        out.append(f"{title} ({i}/{total})\n\n{chunk}")
    return out


def split_long_text(text: str, max_len: int = 3500) -> list[str]:
    text = text.strip()
    if len(text) <= max_len:
        return [text]
    chunks = []
    current = ""
    for part in text.split("\n\n"):
        part = part.strip()
        if not part:
            continue
        candidate = part if not current else current + "\n\n" + part
        if len(candidate) <= max_len:
            current = candidate
            continue
        if current:
            chunks.append(current)
        if len(part) <= max_len:
            current = part
            continue
        # hard split overly long section by lines
        lines = part.splitlines()
        current = ""
        for line in lines:
            candidate = line if not current else current + "\n" + line
            if len(candidate) <= max_len:
                current = candidate
            else:
                if current:
                    chunks.append(current)
                current = line
    if current:
        chunks.append(current)
    return chunks


async def send_list_text(update: Update, context: ContextTypes.DEFAULT_TYPE, query: str) -> None:
    state = get_state(context)
    mode = get_list_format(context)
    title, text = state.scraper.generate(query, format_mode=mode)
    set_last_list_query(context, query)
    chunks = split_long_text(text, 3500)
    chunks = add_part_headers(chunks, title)
    if update.callback_query:
        msg = update.callback_query.message
        for chunk in chunks:
            await msg.reply_text(chunk)
    else:
        msg = update.effective_message
        for chunk in chunks:
            await msg.reply_text(chunk)


async def show_main(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str = "Bot Daeng\n\nPilih menu di bawah.") -> None:
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
    if "list_format_mode" not in context.user_data:
        set_list_format(context, "reseller")
    await show_main(update, context)




def _draft_target_text(draft: Draft) -> str:
    parts = []
    for k, v in (draft.values or {}).items():
        if v:
            parts.append(f"{k}: {v}")
    return " | ".join(parts)

def _normalize_order_status(value: Any) -> str:
    return str(value or "").strip().lower()

def _is_final_order_status(value: Any) -> bool:
    s = _normalize_order_status(value)
    return s in {
        "success", "sukses", "completed", "done", "berhasil",
        "failed", "gagal", "error", "cancel", "canceled", "cancelled", "dibatalkan"
    }

def _pretty_order_status(value: Any) -> str:
    s = _normalize_order_status(value)
    if s in {"success", "sukses", "completed", "done", "berhasil"}:
        return "SUCCESS"
    if s in {"failed", "gagal", "error", "cancel", "canceled", "cancelled", "dibatalkan"}:
        return "GAGAL"
    return str(value or "-").upper()

def _format_check_target(value: Any, draft: Draft) -> str:
    if isinstance(value, list):
        parts = []
        for item in value:
            if isinstance(item, dict):
                for k, v in item.items():
                    parts.append(f"{k}: {v}")
            else:
                parts.append(str(item))
        text = " | ".join(parts)
        return text or _draft_target_text(draft)
    if isinstance(value, dict):
        text = " | ".join(f"{k}: {v}" for k, v in value.items())
        return text or _draft_target_text(draft)
    if value not in (None, "", [], {}):
        return str(value)
    return _draft_target_text(draft)

def format_auto_check_result(result: dict[str, Any], draft: Draft) -> str:
    status = result.get("order_status") or result.get("status") or "-"
    product = result.get("product") or result.get("service") or draft.product_name or "-"
    invoice = result.get("invoice") or result.get("invoice_id") or "-"
    trx_id = result.get("trxid") or result.get("trx_id") or result.get("id") or ""
    message = result.get("message") or result.get("msg") or ""
    sn = result.get("sn") or result.get("serial_number") or result.get("serial") or ""
    target = _format_check_target(result.get("data"), draft)

    icon = "✅" if _normalize_order_status(status) in {"success", "sukses", "completed", "done", "berhasil"} else "❌" if _normalize_order_status(status) in {"failed", "gagal", "error", "cancel", "canceled", "cancelled", "dibatalkan"} else "📦"

    lines = [
        f"{icon} HASIL CEK STATUS ORDER",
        "━━━━━━━━━━━━━━",
        f"Produk  : {product}",
        f"Tujuan  : {target or '-'}",
        f"Invoice : {invoice}",
        f"Status  : {_pretty_order_status(status)}",
    ]

    if trx_id:
        lines.append(f"Trx ID  : {trx_id}")
    if sn:
        lines.append(f"SN      : {sn}")
    elif message:
        lines.append(f"Catatan : {message}")

    return "\n".join(lines)

async def auto_check_invoice_status(invoice: str, draft: Draft, attempts: int = 5, delay_seconds: int = 4) -> Optional[dict[str, Any]]:
    invoice = str(invoice or "").strip()
    if not invoice:
        return None

    await asyncio.sleep(delay_seconds)

    for attempt in range(attempts):
        try:
            result = api_post(CHECK_ENDPOINT, {"invoice": invoice})
            status = result.get("order_status") or result.get("status") or ""
            logger.info("Auto-check invoice %s attempt %s => %s", invoice, attempt + 1, status)
            if _is_final_order_status(status):
                return result
        except Exception as e:
            logger.warning("Auto-check invoice %s gagal pada attempt %s: %s", invoice, attempt + 1, e)

        if attempt < attempts - 1:
            await asyncio.sleep(delay_seconds)

    return None


async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    user = query.from_user
    if not is_allowed(user.id):
        await query.edit_message_text("Akses ditolak.")
        return

    data = query.data or ""
    try:
        if data == "menu_home":
            reset_draft(context)
            await show_main(update, context)
            return

        if data == "menu_refresh":
            products = refresh_products_cache(context)
            games = get_games(products)
            await query.edit_message_text(
                f"Refresh berhasil.\n\nTotal game: {len(games)}\nTotal produk: {len(products)}",
                reply_markup=menu_main(),
            )
            return

        if data == "menu_games":
            products = get_products_cache(context)
            games = get_games(products)
            await query.edit_message_text("Pilih Game", reply_markup=games_keyboard(games, 0))
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
            await query.edit_message_text(f"{game}\n\nPilih produk:", reply_markup=products_keyboard(products, game, page))
            return

        if data == "menu_search":
            context.user_data["awaiting"] = "order_search_keyword"
            await query.edit_message_text("Ketik nama game atau produk order.", reply_markup=InlineKeyboardMarkup([nav_row()]))
            return

        if data.startswith("search_page|"):
            page = int(data.split("|", 1)[1])
            results = context.user_data.get("order_search_results", [])
            if not isinstance(results, list):
                results = []
            await query.edit_message_text("Hasil Pencarian", reply_markup=products_keyboard(results, "Search", page, search_mode=True))
            return

        if data == "menu_check":
            context.user_data["awaiting"] = "check_invoice"
            await query.edit_message_text("Masukkan invoice untuk cek status.", reply_markup=InlineKeyboardMarkup([nav_row()]))
            return

        if data == "menu_info":
            result = api_post(INFO_ENDPOINT, {})
            text = format_info_result(result if isinstance(result, dict) else {})
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
            text = f"✅ ORDER BERHASIL DIKIRIM\n━━━━━━━━━━━━━━\nInvoice : {invoice}\nPesan   : {message}"
            save_pending_order(invoice, draft)
            reset_draft(context)
            await query.edit_message_text(text[:4000], reply_markup=menu_main())
            return

        if data == "cancel_order":
            reset_draft(context)
            await query.edit_message_text("Order dibatalkan.", reply_markup=menu_main())
            return

        if data == "menu_list_home":
            await query.edit_message_text("List Harga Daeng\n\nPilih menu di bawah.", reply_markup=list_menu(context))
            return

        if data == "menu_list_generate":
            games = get_state(context).scraper.fetch_product_names(force_refresh=True)
            await query.edit_message_text("Pilih Game untuk List Harga", reply_markup=list_games_keyboard(games, 0))
            return

        if data.startswith("listgames_page|"):
            page = int(data.split("|", 1)[1])
            games = get_state(context).scraper.fetch_product_names()
            await query.edit_message_text("Pilih Game untuk List Harga", reply_markup=list_games_keyboard(games, page))
            return

        if data.startswith("listgame|"):
            game = data.split("|", 1)[1]
            await query.edit_message_text(
                f"Generate list untuk:\n{game}",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("Generate Sekarang", callback_data=f"listgen|{game}")],
                    nav_row("menu_list_generate"),
                ]),
            )
            return

        if data.startswith("listgen|"):
            game = data.split("|", 1)[1]
            await query.edit_message_text(f"Mengambil harga terbaru untuk {game}...")
            await send_list_text(update, context, game)
            await query.message.reply_text("Selesai.", reply_markup=list_menu(context))
            return

        if data == "menu_list_search":
            context.user_data["awaiting"] = "list_search_query"
            await query.edit_message_text(
                "Ketik nama game apa saja.\nContoh: mobile legends, mlbb, free fire, roblox, hok.\n\nBot akan cari otomatis ke daftar game live dari web Daeng. Kalau hasil sangat panjang, bot akan pecah jadi beberapa pesan.",
                reply_markup=InlineKeyboardMarkup([nav_row("menu_list_home")]),
            )
            return

        if data == "menu_list_format":
            await query.edit_message_text("Pilih format output", reply_markup=list_format_keyboard(get_list_format(context)))
            return

        if data.startswith("listfmt|"):
            mode = data.split("|", 1)[1]
            set_list_format(context, mode)
            await query.edit_message_text(f"Format diubah ke: {mode.title()}", reply_markup=list_menu(context))
            return

        if data == "menu_list_refresh_last":
            last = get_last_list_query(context)
            if not last:
                await query.edit_message_text("Belum ada game terakhir yang di-generate.", reply_markup=list_menu(context))
                return
            await query.edit_message_text(f"Mengambil harga terbaru untuk {last}...")
            await send_list_text(update, context, last)
            await query.message.reply_text("Selesai.", reply_markup=list_menu(context))
            return

    except Exception as exc:
        await query.message.reply_text(f"Gagal:\n{exc}", reply_markup=menu_main())


async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user or not is_allowed(user.id):
        await update.effective_message.reply_text("Akses ditolak.")
        return

    text = (update.effective_message.text or "").strip()
    awaiting = context.user_data.get("awaiting")

    try:
        if awaiting == "order_search_keyword":
            context.user_data["awaiting"] = None
            q = normalize(text)
            products = get_products_cache(context)
            results = [p for p in products if q in normalize(p.game) or q in normalize(p.name) or q in normalize(p.category) or q in normalize(p.code)]
            context.user_data["order_search_results"] = sort_products_for_menu(results)
            if not results:
                await update.effective_message.reply_text("Produk tidak ditemukan.", reply_markup=menu_main())
                return
            await update.effective_message.reply_text(f"Ditemukan {len(results)} produk.", reply_markup=products_keyboard(results, "Search", 0, search_mode=True))
            return

        if awaiting == "check_invoice":
            context.user_data["awaiting"] = None
            result = api_post(CHECK_ENDPOINT, {"invoice": text})
            out = format_check_result(result if isinstance(result, dict) else {})
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
            await update.effective_message.reply_text(format_order_preview(draft), reply_markup=confirm_keyboard())
            return

        if awaiting == "list_search_query":
            context.user_data["awaiting"] = None
            await update.effective_message.reply_text(f"Mengambil harga terbaru untuk {text}...")
            await send_list_text(update, context, text)
            await update.effective_message.reply_text("Selesai.", reply_markup=list_menu(context))
            return

    except Exception as exc:
        await update.effective_message.reply_text(f"Gagal:\n{exc}", reply_markup=menu_main())
        return

    await update.effective_message.reply_text("Pilih menu di bawah.", reply_markup=menu_main())


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
