"""Telegram-бот выгодных предложений Android-смартфонов.

Панель: /panel — бренды, цена, источники, включение.
Команды: /check /brands /price /source /sources /on /off /status
"""
import asyncio
import html
import json
import logging
import os
import re
from pathlib import Path

import feedparser
import requests
from bs4 import BeautifulSoup
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (Application, CallbackQueryHandler, CommandHandler,
                          ContextTypes, MessageHandler, filters)

TOKEN = os.environ["DEALS_BOT_TOKEN"]
DATA_DIR = Path(os.environ.get("DATA_DIR", "deals_data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
SETTINGS_FILE = DATA_DIR / "settings.json"
SEEN_FILE = DATA_DIR / "seen.json"
OWNER_FILE = DATA_DIR / "owner.json"
CHECK_MIN = int(os.environ.get("DEALS_CHECK_MIN", "60"))
CITY = os.environ.get("DEALS_CITY", "Красноярск")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("dealsbot")

BRANDS = ["Samsung", "Xiaomi", "Redmi", "POCO", "OnePlus", "Motorola",
          "Pixel", "realme", "Nothing", "Honor", "Tecno"]
PRICE_PRESETS = [15000, 30000, 50000, 100000, 0]  # 0 = без лимита

DEFAULT_SETTINGS = {
    "enabled": True,
    "brands": [],          # пусто = все
    "max_price": 0,        # 0 = без лимита
    "sources": [
        "https://www.androidauthority.com/feed/",
        "https://www.gsmarena.com/rss-news-reviews.php3",
    ],
}

DEAL_WORDS = re.compile(
    r"deal|discount|price cut|price drop|off\b|record low|sale|cheap|"
    r"скидк|дешев|акци|выгодн|снижени", re.I
)
PHONE_WORDS = re.compile(
    r"phone|smartphone|galaxy|pixel|xiaomi|redmi|poco|oneplus|motorola|"
    r"nothing|realme|honor|tecno|android|смартфон|телефон", re.I
)
PRICE_RE = re.compile(r"(?:[$€₽]\s?\d[\d\s.,]*|\d[\d\s.,]*\s?(?:руб|₽|$|€))", re.I)


def load_json(path, default):
    if path.exists():
        try:
            return json.loads(path.read_text("utf-8"))
        except (ValueError, json.JSONDecodeError):
            pass
    return json.loads(json.dumps(default))


def save_json(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), "utf-8")


def settings() -> dict:
    s = load_json(SETTINGS_FILE, DEFAULT_SETTINGS)
    for key, value in DEFAULT_SETTINGS.items():
        s.setdefault(key, value)
    return s


def get_owner() -> int:
    data = load_json(OWNER_FILE, {})
    return int(data.get("owner", os.environ.get("CHAT_ID", "0") or 0))


def extract_price(text: str) -> int | None:
    m = PRICE_RE.search(text or "")
    if not m:
        return None
    digits = re.sub(r"[^\d]", "", m.group())
    return int(digits) if digits else None


def is_deal(title: str, summary: str, s: dict) -> bool:
    text = f"{title} {summary}"
    if not PHONE_WORDS.search(text):
        return False
    if not DEAL_WORDS.search(text):
        return False
    if s["brands"] and not any(b.lower() in text.lower() for b in s["brands"]):
        return False
    price = extract_price(text)
    if s["max_price"] and price and ("₽" in text or "руб" in text.lower()):
        if price > s["max_price"]:
            return False
    return True


def parse_source(url: str) -> list[dict]:
    """Возвращает список {'title','summary','link'} из RSS или HTML."""
    items = []
    try:
        if "rss" in url.lower() or "feed" in url.lower() or url.endswith(".xml"):
            feed = feedparser.parse(url)
            for entry in feed.entries:
                items.append({
                    "title": entry.get("title", ""),
                    "summary": entry.get("summary", ""),
                    "link": entry.get("link", ""),
                })
            return items
        r = requests.get(url, timeout=20,
                         headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36"})
        soup = BeautifulSoup(r.text, "html.parser")
        for tag in soup.find_all(["a", "article", "div"], limit=400):
            title = tag.get_text(" ", strip=True)
            link = tag.get("href", "")
            if title and 15 < len(title) < 200:
                items.append({"title": title, "summary": "", "link": link})
    except Exception as e:
        log.warning("Источник %s недоступен: %s", url, e)
    return items


async def collect_deals() -> list[dict]:
    s = settings()
    seen = set(load_json(SEEN_FILE, []))
    found = []
    for url in s["sources"]:
        items = await asyncio.to_thread(parse_source, url)
        for item in items:
            key = item["link"] or item["title"]
            if not key or key in seen:
                continue
            seen.add(key)
            if is_deal(item["title"], item["summary"], s):
                if CITY.lower() in (item["title"] + item["summary"]).lower():
                    item["local"] = True
                found.append(item)
    save_json(SEEN_FILE, list(seen)[-3000:])
    return found[:10]


def panel_keyboard(s: dict) -> InlineKeyboardMarkup:
    rows = []
    brand_row = [InlineKeyboardButton(
        f"{'✅' if not s['brands'] else '⬜'} Все", callback_data="brand|ALL")]
    for b in BRANDS:
        brand_row.append(InlineKeyboardButton(
            f"{'✅' if b in s['brands'] else '⬜'} {b}", callback_data=f"brand|{b}"))
    # разбиваем по 3 в ряд
    for i in range(0, len(brand_row), 3):
        rows.append(brand_row[i:i + 3])
    price_row = []
    for p in PRICE_PRESETS:
        label = "Любая" if p == 0 else f"{p // 1000}k₽"
        price_row.append(InlineKeyboardButton(
            f"{'✅' if s['max_price'] == p else '⬜'} {label}", callback_data=f"price|{p}"))
    rows.append(price_row)
    rows.append([
        InlineKeyboardButton("🔍 Проверить", callback_data="check"),
        InlineKeyboardButton(f"{'⏸ Выключить' if s['enabled'] else '▶️ Включить'}", callback_data="toggle"),
    ])
    rows.append([InlineKeyboardButton("📋 Источники", callback_data="sources")])
    return InlineKeyboardMarkup(rows)


def panel_text(s: dict) -> str:
    brands = ", ".join(s["brands"]) if s["brands"] else "все"
    price = f"{s['max_price']} ₽" if s["max_price"] else "без лимита"
    return (
        f"⚙️ <b>Настройки поиска · {html.escape(CITY)}</b>\n\n"
        f"Бренды: {html.escape(brands)}\n"
        f"Макс. цена: {price}\n"
        f"Источников: {len(s['sources'])}\n"
        f"Статус: {'включён' if s['enabled'] else 'выключен'}\n"
        f"Проверка каждые {CHECK_MIN} мин."
    )


async def send_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    s = settings()
    await update.message.reply_text(panel_text(s), parse_mode="HTML",
                                    reply_markup=panel_keyboard(s))


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    save_json(OWNER_FILE, {"owner": update.effective_chat.id})
    await send_panel(update, context)
    await update.message.reply_text(
        "Команды:\n/panel — настройки\n/check — проверить сейчас\n"
        "/brands Samsung,Xiaomi — выбрать бренды\n"
        "/price 30000 — макс. цена в ₽ (0 = без лимита)\n"
        "/source URL — добавить источник\n/sources — список источников\n"
        "/on /off — включить/выключить проверку"
    )


async def cmd_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await send_panel(update, context)


async def do_check(context, chat_id: int):
    deals = await collect_deals()
    if not deals:
        await context.bot.send_message(chat_id, "Пока выгодных предложений не нашёл.")
        return
    for d in deals:
        title = html.escape(d["title"][:180])
        link = html.escape(d["link"], quote=True) if d["link"] else ""
        mark = "📍 " if d.get("local") else ""
        text = f"{mark}📱 <b>{title}</b>"
        if link:
            text += f'\n<a href="{link}">открыть</a>'
        await context.bot.send_message(chat_id, text, parse_mode="HTML",
                                       disable_web_page_preview=False)
        await asyncio.sleep(0.5)


async def cmd_check(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Ищу выгодные предложения…")
    await do_check(context, update.effective_chat.id)


async def cmd_brands(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args or []
    if not args:
        await update.message.reply_text(
            "Формат: /brands Samsung,Xiaomi\nЧтобы сбросить: /brands all")
        return
    raw = " ".join(args).strip().lower()
    s = settings()
    if raw == "all":
        s["brands"] = []
    else:
        chosen = [b for b in BRANDS if b.lower() in [x.strip() for x in raw.split(",")]]
        if not chosen:
            await update.message.reply_text("Не распознал бренды. Доступные: " + ", ".join(BRANDS))
            return
        s["brands"] = chosen
    save_json(SETTINGS_FILE, s)
    await update.message.reply_text("✅ Бренды обновлены.")


async def cmd_price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args or []
    if not args:
        await update.message.reply_text("Формат: /price 30000 (0 = без лимита)")
        return
    try:
        value = int(args[0].replace(" ", "").replace("₽", ""))
    except ValueError:
        await update.message.reply_text("Укажи число, например /price 30000")
        return
    s = settings()
    s["max_price"] = value
    save_json(SETTINGS_FILE, s)
    await update.message.reply_text(f"✅ Макс. цена: {value or 'без лимита'} ₽")


async def cmd_source(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args or []
    if not args:
        await update.message.reply_text("Формат: /source https://...rss")
        return
    s = settings()
    url = args[0].strip()
    if url in s["sources"]:
        await update.message.reply_text("Такой источник уже есть.")
        return
    s["sources"].append(url)
    save_json(SETTINGS_FILE, s)
    await update.message.reply_text("✅ Источник добавлен.")


async def cmd_sources(update: Update, context: ContextTypes.DEFAULT_TYPE):
    s = settings()
    text = "\n".join(f"{i+1}. {u}" for i, u in enumerate(s["sources"])) or "нет"
    await update.message.reply_text("Источники:\n" + text[:3000])


async def cmd_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE, enabled: bool | None = None):
    s = settings()
    s["enabled"] = not s["enabled"] if enabled is None else enabled
    save_json(SETTINGS_FILE, s)
    await update.message.reply_text(f"Статус: {'включён' if s['enabled'] else 'выключен'}")


async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data or ""
    s = settings()
    if data.startswith("brand|"):
        value = data.split("|", 1)[1]
        if value == "ALL":
            s["brands"] = []
        elif value in s["brands"]:
            s["brands"].remove(value)
        else:
            s["brands"].append(value)
    elif data.startswith("price|"):
        s["max_price"] = int(data.split("|", 1)[1])
    elif data == "toggle":
        s["enabled"] = not s["enabled"]
    elif data == "check":
        await query.edit_message_text("Ищу выгодные предложения…")
        await do_check(context, query.message.chat_id)
        return
    elif data == "sources":
        await query.edit_message_text("Добавить источник: /source URL\nСписок: /sources")
        return
    save_json(SETTINGS_FILE, s)
    await query.edit_message_text(panel_text(s), parse_mode="HTML",
                                  reply_markup=panel_keyboard(s))


async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    owner = get_owner()
    if owner and update.effective_chat.id != owner:
        return
    await update.message.reply_text("Воспользуйся панелью: /panel")


async def scheduled(context: ContextTypes.DEFAULT_TYPE):
    if not settings()["enabled"]:
        return
    await do_check(context, get_owner())


def main():
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("panel", cmd_panel))
    app.add_handler(CommandHandler("check", cmd_check))
    app.add_handler(CommandHandler("brands", cmd_brands))
    app.add_handler(CommandHandler("price", cmd_price))
    app.add_handler(CommandHandler("source", cmd_source))
    app.add_handler(CommandHandler("sources", cmd_sources))
    app.add_handler(CommandHandler("on", lambda u, c: cmd_toggle(u, c, True)))
    app.add_handler(CommandHandler("off", lambda u, c: cmd_toggle(u, c, False)))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message))
    app.job_queue.run_repeating(scheduled, interval=CHECK_MIN * 60, first=10)
    app.run_polling(bootstrap_retries=10, timeout=30)


if __name__ == "__main__":
    main()
