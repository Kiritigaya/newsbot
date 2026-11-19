"""Telegram-бот выгодных предложений Android-смартфонов.

Панель: /panel. Команды: /help /check /stats /history /favorites
/alerts /alert /delalert /city /price /minprice /discount /sort
/brands /source /sources /delsource /testsource /on /off
"""
import asyncio
import html
import json
import logging
import os
import re
from datetime import datetime, timezone
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
HISTORY_FILE = DATA_DIR / "history.json"
FAV_FILE = DATA_DIR / "favorites.json"
ALERTS_FILE = DATA_DIR / "alerts.json"
STATS_FILE = DATA_DIR / "stats.json"
CHECK_MIN = int(os.environ.get("DEALS_CHECK_MIN", "60"))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("dealsbot")

BRANDS = ["Samsung", "Xiaomi", "Redmi", "POCO", "OnePlus", "Motorola",
          "Pixel", "realme", "Nothing", "Honor", "Tecno"]
PRICE_PRESETS = [15000, 30000, 50000, 100000, 0]

DEFAULT_SETTINGS = {
    "enabled": True,
    "brands": [],
    "max_price": 0,
    "min_price": 0,
    "min_discount": 0,
    "sort": "date",          # date | price | discount
    "city": os.environ.get("DEALS_CITY", "Красноярск"),
    "sources": [
        "https://www.androidauthority.com/feed/",
        "https://www.gsmarena.com/rss-news-reviews.php3",
    ],
}

DEAL_WORDS = re.compile(
    r"deal|discount|price cut|price drop|off\b|record low|sale|cheap|"
    r"скидк|дешев|акци|выгодн|снижени", re.I)
PHONE_WORDS = re.compile(
    r"phone|smartphone|galaxy|pixel|xiaomi|redmi|poco|oneplus|motorola|"
    r"nothing|realme|honor|tecno|android|смартфон|телефон", re.I)
PRICE_RE = re.compile(r"(?:[$€₽]\s?\d[\d\s.,]*|\d[\d\s.,]*\s?(?:руб|₽|$|€))", re.I)
DISCOUNT_RE = re.compile(r"(\d{1,2})\s?%|\bhalf price\b|50% off", re.I)


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


def is_owner(update) -> bool:
    owner = get_owner()
    return not owner or update.effective_chat.id == owner


def bump_stat(key: str, value: int = 1) -> None:
    stats = load_json(STATS_FILE, {})
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    d = stats.setdefault(day, {})
    d[key] = d.get(key, 0) + value
    save_json(STATS_FILE, stats)


def extract_price(text: str) -> int | None:
    m = PRICE_RE.search(text or "")
    if not m:
        return None
    digits = re.sub(r"[^\d]", "", m.group())
    return int(digits) if digits else None


def extract_discount(text: str) -> int | None:
    m = DISCOUNT_RE.search(text or "")
    if not m:
        return None
    if "half price" in text.lower():
        return 50
    try:
        return int(m.group(1))
    except (TypeError, ValueError, IndexError):
        return None


def is_deal(title: str, summary: str, s: dict) -> bool:
    text = f"{title} {summary}"
    if not PHONE_WORDS.search(text):
        return False
    if not DEAL_WORDS.search(text):
        return False
    if s["brands"] and not any(b.lower() in text.lower() for b in s["brands"]):
        return False
    price = extract_price(text)
    rub = "₽" in text or "руб" in text.lower()
    if price and rub:
        if s["max_price"] and price > s["max_price"]:
            return False
        if s["min_price"] and price < s["min_price"]:
            return False
    discount = extract_discount(text)
    if s["min_discount"] and discount and discount < s["min_discount"]:
        return False
    return True


def parse_source(url: str) -> list[dict]:
    items = []
    try:
        if "rss" in url.lower() or "feed" in url.lower() or url.endswith(".xml"):
            feed = feedparser.parse(url)
            for entry in feed.entries:
                items.append({"title": entry.get("title", ""),
                              "summary": entry.get("summary", ""),
                              "link": entry.get("link", ""),
                              "date": datetime.now(timezone.utc).isoformat()})
            return items
        r = requests.get(url, timeout=20,
                         headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36"})
        soup = BeautifulSoup(r.text, "html.parser")
        for tag in soup.find_all(["a", "article", "div"], limit=400):
            title = tag.get_text(" ", strip=True)
            link = tag.get("href", "")
            if title and 15 < len(title) < 200:
                items.append({"title": title, "summary": "", "link": link,
                              "date": datetime.now(timezone.utc).isoformat()})
    except Exception as e:
        log.warning("Источник %s недоступен: %s", url, e)
        bump_stat("source_errors")
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
            if not is_deal(item["title"], item["summary"], s):
                continue
            item["local"] = s["city"].lower() in (item["title"] + item["summary"]).lower()
            item["price"] = extract_price(item["title"] + " " + item["summary"])
            item["discount"] = extract_discount(item["title"] + " " + item["summary"])
            found.append(item)
    save_json(SEEN_FILE, list(seen)[-3000:])

    if s["sort"] == "price":
        found.sort(key=lambda x: x.get("price") or 10**12)
    elif s["sort"] == "discount":
        found.sort(key=lambda x: x.get("discount") or 0, reverse=True)

    # История
    history = load_json(HISTORY_FILE, [])
    known = {h.get("link") or h.get("title") for h in history}
    for item in found:
        key = item.get("link") or item["title"]
        if key not in known:
            history.append(item)
    save_json(HISTORY_FILE, history[-500:])
    bump_stat("found", len(found))

    # Ценавые алерты
    alerts = load_json(ALERTS_FILE, [])
    for item in found:
        text = (item["title"] + " " + item["summary"]).lower()
        for a in alerts:
            if a["model"].lower() in text and (not a.get("max_price") or
                                               (item.get("price") and item["price"] <= a["max_price"])):
                a.setdefault("hits", 0)
                a["hits"] += 1
                item["alert_model"] = a["model"]
    save_json(ALERTS_FILE, alerts)
    return found[:10]


def panel_keyboard(s: dict) -> InlineKeyboardMarkup:
    rows = []
    brand_row = [InlineKeyboardButton(f"{'✅' if not s['brands'] else '⬜'} Все", callback_data="brand|ALL")]
    for b in BRANDS:
        brand_row.append(InlineKeyboardButton(f"{'✅' if b in s['brands'] else '⬜'} {b}", callback_data=f"brand|{b}"))
    for i in range(0, len(brand_row), 3):
        rows.append(brand_row[i:i + 3])
    rows.append([
        InlineKeyboardButton("💰 Любая" if not s["max_price"] else f"💰 до {s['max_price'] // 1000}k", callback_data="panel|price"),
        InlineKeyboardButton(f"📉 скидка {s['min_discount']}%", callback_data="panel|discount"),
    ])
    rows.append([
        InlineKeyboardButton(f"🔀 {s['sort']}", callback_data="panel|sort"),
        InlineKeyboardButton(f"📍 {s['city'][:10]}", callback_data="panel|city"),
    ])
    rows.append([
        InlineKeyboardButton("🔍 Проверить", callback_data="check"),
        InlineKeyboardButton("⏸" if s["enabled"] else "▶️", callback_data="toggle"),
    ])
    rows.append([InlineKeyboardButton("📋 Источники", callback_data="sources")])
    return InlineKeyboardMarkup(rows)


def panel_text(s: dict) -> str:
    return (
        f"⚙️ <b>Настройки поиска</b>\n\n"
        f"Бренды: {html.escape(', '.join(s['brands']) or 'все')}\n"
        f"Цена: {s['min_price'] or 0} – {s['max_price'] or '∞'} ₽\n"
        f"Мин. скидка: {s['min_discount']}%\n"
        f"Город: {html.escape(s['city'])}\n"
        f"Сортировка: {s['sort']}\n"
        f"Источников: {len(s['sources'])}\n"
        f"Статус: {'включён' if s['enabled'] else 'выключен'}\n"
        f"Проверка каждые {CHECK_MIN} мин."
    )


async def send_panel(update, context):
    s = settings()
    await update.message.reply_text(panel_text(s), parse_mode="HTML", reply_markup=panel_keyboard(s))


async def cmd_start(update, context):
    save_json(OWNER_FILE, {"owner": update.effective_chat.id})
    await send_panel(update, context)
    await update.message.reply_text("Полный список команд: /help")


async def cmd_help(update, context):
    if not is_owner(update):
        return
    await update.message.reply_text(
        "📋 <b>Команды</b>\n\n"
        "/panel — панель настроек\n"
        "/check — проверить сейчас\n"
        "/stats — статистика\n"
        "/history — найденные предложения\n"
        "/favorites — избранное · /unsave N\n"
        "/alerts — ценовые алерты\n"
        '/alert "Galaxy S24" 50000 — следить за моделью\n'
        "/delalert N — удалить алерт\n"
        "/city Красноярск — город\n"
        "/price 50000 — макс. цена (0 = без лимита)\n"
        "/minprice 10000 — мин. цена\n"
        "/discount 20 — мин. скидка в %\n"
        "/sort date|price|discount\n"
        "/brands Samsung,Xiaomi · /brands all\n"
        "/source URL · /sources · /delsource N · /testsource URL\n"
        "/on /off — включить/выключить проверку",
        parse_mode="HTML")


async def format_deal(item: dict) -> str:
    title = html.escape(item["title"][:180])
    link = html.escape(item.get("link", ""), quote=True)
    parts = []
    if item.get("alert_model"):
        parts.append(f'🔔 <b>АЛЕРТ: {html.escape(item["alert_model"])}</b>')
    if item.get("local"):
        parts.append("📍 Красноярск")
    parts.append(f"📱 <b>{title}</b>")
    details = []
    if item.get("price"):
        details.append(f"💰 ~{item['price']} ₽")
    if item.get("discount"):
        details.append(f"📉 −{item['discount']}%")
    if details:
        parts.append(" · ".join(details))
    if link:
        parts.append(f'<a href="{link}">открыть</a>')
    return "\n".join(parts)


async def send_deals(context, chat_id: int, deals: list[dict]):
    if not deals:
        await context.bot.send_message(chat_id, "Пока выгодных предложений не нашёл.")
        return
    for item in deals:
        text = await format_deal(item)
        key = str(abs(hash(item.get("link") or item["title"])))[:10]
        context.bot_data.setdefault("deal_items", {})[key] = item
        markup = InlineKeyboardMarkup([[
            InlineKeyboardButton("⭐ В избранное", callback_data=f"fav|{key}"),
            InlineKeyboardButton("🔗 Ссылка", url=item.get("link") or "https://t.me"),
        ]])
        await context.bot.send_message(chat_id, text, parse_mode="HTML",
                                       disable_web_page_preview=False, reply_markup=markup)
        await asyncio.sleep(0.5)


async def do_check(context, chat_id: int):
    deals = await collect_deals()
    bump_stat("checks")
    await send_deals(context, chat_id, deals)


async def cmd_check(update, context):
    if not is_owner(update):
        return
    await update.message.reply_text("Ищу выгодные предложения…")
    await do_check(context, update.effective_chat.id)


async def cmd_stats(update, context):
    if not is_owner(update):
        return
    stats = load_json(STATS_FILE, {})
    lines = []
    for day in sorted(stats, reverse=True)[:7]:
        d = stats[day]
        lines.append(f"{day}: 🔍 {d.get('checks', 0)} · 📱 {d.get('found', 0)} · ⚠️ {d.get('source_errors', 0)}")
    await update.message.reply_text("📊 <b>Статистика</b>\n\n" + ("\n".join(lines) or "Нет данных"), parse_mode="HTML")


async def cmd_history(update, context):
    if not is_owner(update):
        return
    history = load_json(HISTORY_FILE, [])
    if not history:
        await update.message.reply_text("История пуста.")
        return
    lines = []
    for i, item in enumerate(reversed(history[-10:]), 1):
        link = html.escape(item.get("link", ""), quote=True)
        lines.append(f'{i}. <a href="{link}">{html.escape(item["title"][:80])}</a>')
    await update.message.reply_text("🕘 <b>История</b>\n\n" + "\n".join(lines), parse_mode="HTML",
                                    disable_web_page_preview=True)


async def cmd_favorites(update, context):
    if not is_owner(update):
        return
    fav = load_json(FAV_FILE, [])
    if not fav:
        await update.message.reply_text("Избранное пусто — нажмите ⭐ под предложением.")
        return
    lines = []
    for i, item in enumerate(fav[:20], 1):
        link = html.escape(item.get("link", ""), quote=True)
        lines.append(f'{i}. <a href="{link}">{html.escape(item["title"][:80])}</a>')
    await update.message.reply_text("⭐ <b>Избранное</b>\n\n" + "\n".join(lines), parse_mode="HTML",
                                    disable_web_page_preview=True)


async def cmd_unsave(update, context):
    if not is_owner(update):
        return
    args = context.args or []
    if not args or not args[0].isdigit():
        await update.message.reply_text("Формат: /unsave 2")
        return
    fav = load_json(FAV_FILE, [])
    idx = int(args[0]) - 1
    if not 0 <= idx < len(fav):
        await update.message.reply_text("Нет такого номера.")
        return
    fav.pop(idx)
    save_json(FAV_FILE, fav)
    await update.message.reply_text("🗑 Удалено из избранного.")


async def cmd_alerts(update, context):
    if not is_owner(update):
        return
    alerts = load_json(ALERTS_FILE, [])
    if not alerts:
        await update.message.reply_text('Алертов нет. Создать: /alert "Galaxy S24" 50000')
        return
    lines = [f'{i}. {html.escape(a["model"])} · до {a.get("max_price") or "∞"} ₽ · срабатываний: {a.get("hits", 0)}'
             for i, a in enumerate(alerts, 1)]
    await update.message.reply_text("🔔 <b>Алерты</b>\n\n" + "\n".join(lines), parse_mode="HTML")


async def cmd_alert(update, context):
    if not is_owner(update):
        return
    args = context.args or []
    if len(args) < 2:
        await update.message.reply_text('Формат: /alert "Galaxy S24" 50000')
        return
    model = " ".join(args[:-1]).strip('"«»')
    try:
        price = int(args[-1].replace("₽", "").replace(" ", ""))
    except ValueError:
        await update.message.reply_text("Последним аргументом должна быть цена.")
        return
    alerts = load_json(ALERTS_FILE, [])
    alerts.append({"model": model, "max_price": price, "hits": 0})
    save_json(ALERTS_FILE, alerts)
    await update.message.reply_text(f'🔔 Алерт добавлен: {model} до {price} ₽')


async def cmd_delalert(update, context):
    if not is_owner(update):
        return
    args = context.args or []
    if not args or not args[0].isdigit():
        await update.message.reply_text("Формат: /delalert 1")
        return
    alerts = load_json(ALERTS_FILE, [])
    idx = int(args[0]) - 1
    if not 0 <= idx < len(alerts):
        await update.message.reply_text("Нет такого алерта.")
        return
    alerts.pop(idx)
    save_json(ALERTS_FILE, alerts)
    await update.message.reply_text("🗑 Алерт удалён.")


async def cmd_city(update, context):
    if not is_owner(update):
        return
    args = context.args or []
    if not args:
        await update.message.reply_text("Формат: /city Красноярск")
        return
    s = settings()
    s["city"] = " ".join(args)
    save_json(SETTINGS_FILE, s)
    await update.message.reply_text(f"📍 Город: {s['city']}")


async def cmd_price(update, context):
    if not is_owner(update):
        return
    args = context.args or []
    if not args:
        await update.message.reply_text("Формат: /price 50000 (0 = без лимита)")
        return
    try:
        value = int(args[0].replace(" ", "").replace("₽", ""))
    except ValueError:
        await update.message.reply_text("Укажите число.")
        return
    s = settings()
    s["max_price"] = value
    save_json(SETTINGS_FILE, s)
    await update.message.reply_text(f"✅ Макс. цена: {value or 'без лимита'} ₽")


async def cmd_minprice(update, context):
    if not is_owner(update):
        return
    args = context.args or []
    if not args or not args[0].isdigit():
        await update.message.reply_text("Формат: /minprice 10000")
        return
    s = settings()
    s["min_price"] = int(args[0])
    save_json(SETTINGS_FILE, s)
    await update.message.reply_text(f"✅ Мин. цена: {s['min_price']} ₽")


async def cmd_discount(update, context):
    if not is_owner(update):
        return
    args = context.args or []
    if not args or not args[0].isdigit():
        await update.message.reply_text("Формат: /discount 20")
        return
    s = settings()
    s["min_discount"] = int(args[0])
    save_json(SETTINGS_FILE, s)
    await update.message.reply_text(f"✅ Мин. скидка: {s['min_discount']}%")


async def cmd_sort(update, context):
    if not is_owner(update):
        return
    args = context.args or []
    if not args or args[0] not in ("date", "price", "discount"):
        await update.message.reply_text("Формат: /sort date|price|discount")
        return
    s = settings()
    s["sort"] = args[0]
    save_json(SETTINGS_FILE, s)
    await update.message.reply_text(f"✅ Сортировка: {args[0]}")


async def cmd_brands(update, context):
    if not is_owner(update):
        return
    args = context.args or []
    if not args:
        await update.message.reply_text("Формат: /brands Samsung,Xiaomi или /brands all")
        return
    raw = " ".join(args).strip().lower()
    s = settings()
    if raw == "all":
        s["brands"] = []
    else:
        parts = [x.strip() for x in raw.split(",")]
        chosen = [b for b in BRANDS if b.lower() in parts]
        if not chosen:
            await update.message.reply_text("Не распознал бренды. Доступные: " + ", ".join(BRANDS))
            return
        s["brands"] = chosen
    save_json(SETTINGS_FILE, s)
    await update.message.reply_text("✅ Бренды обновлены.")


async def cmd_source(update, context):
    if not is_owner(update):
        return
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


async def cmd_sources(update, context):
    if not is_owner(update):
        return
    s = settings()
    text = "\n".join(f"{i}. {u}" for i, u in enumerate(s["sources"], 1)) or "нет"
    await update.message.reply_text("Источники:\n" + text[:3000])


async def cmd_delsource(update, context):
    if not is_owner(update):
        return
    args = context.args or []
    if not args or not args[0].isdigit():
        await update.message.reply_text("Формат: /delsource 1")
        return
    s = settings()
    idx = int(args[0]) - 1
    if not 0 <= idx < len(s["sources"]):
        await update.message.reply_text("Нет такого источника.")
        return
    removed = s["sources"].pop(idx)
    save_json(SETTINGS_FILE, s)
    await update.message.reply_text(f"🗑 Удалено: {removed}")


async def cmd_testsource(update, context):
    if not is_owner(update):
        return
    args = context.args or []
    if not args:
        await update.message.reply_text("Формат: /testsource https://...rss")
        return
    items = await asyncio.to_thread(parse_source, args[0])
    await update.message.reply_text(f"✅ Источник доступен, записей: {len(items)}" if items else "❌ Пусто или недоступно")


async def cmd_toggle(update, context, enabled: bool | None = None):
    if not is_owner(update):
        return
    s = settings()
    s["enabled"] = not s["enabled"] if enabled is None else enabled
    save_json(SETTINGS_FILE, s)
    await update.message.reply_text(f"Статус: {'включён' if s['enabled'] else 'выключен'}")


async def on_button(update, context):
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
    elif data.startswith("panel|"):
        action = data.split("|", 1)[1]
        if action == "price":
            s["max_price"] = 0 if s["max_price"] else 50000
        elif action == "discount":
            s["min_discount"] = (s["min_discount"] + 10) % 40
        elif action == "sort":
            order = ["date", "price", "discount"]
            s["sort"] = order[(order.index(s["sort"]) + 1) % 3]
        elif action == "city":
            await query.edit_message_text("Изменить город: /city Красноярск")
            return
    elif data == "toggle":
        s["enabled"] = not s["enabled"]
    elif data == "check":
        await query.edit_message_text("Ищу выгодные предложения…")
        await do_check(context, query.message.chat_id)
        return
    elif data == "sources":
        text = "\n".join(f"{i}. {u}" for i, u in enumerate(s["sources"], 1))
        await query.edit_message_text("Источники:\n" + text[:3000] + "\n\nДобавить: /source URL")
        return
    elif data.startswith("fav|"):
        key = data.split("|", 1)[1]
        item = context.bot_data.get("deal_items", {}).get(key)
        if not item:
            await query.answer("Предложение недоступно", show_alert=True)
            return
        fav = load_json(FAV_FILE, [])
        if not any(x.get("link") == item.get("link") for x in fav):
            fav.append(item)
            save_json(FAV_FILE, fav)
        await query.answer("⭐ Сохранено")
        await query.edit_message_reply_markup(reply_markup=None)
        return
    save_json(SETTINGS_FILE, s)
    await query.edit_message_text(panel_text(s), parse_mode="HTML", reply_markup=panel_keyboard(s))


async def on_message(update, context):
    if not is_owner(update):
        return
    await update.message.reply_text("Воспользуйтесь панелью: /panel · /help")


async def scheduled(context):
    if not settings()["enabled"]:
        return
    owner = get_owner()
    if owner:
        await do_check(context, owner)


def main():
    app = Application.builder().token(TOKEN).build()
    handlers = [
        ("start", cmd_start), ("help", cmd_help), ("panel", lambda u, c: send_panel(u, c)),
        ("check", cmd_check), ("stats", cmd_stats), ("history", cmd_history),
        ("favorites", cmd_favorites), ("unsave", cmd_unsave),
        ("alerts", cmd_alerts), ("alert", cmd_alert), ("delalert", cmd_delalert),
        ("city", cmd_city), ("price", cmd_price), ("minprice", cmd_minprice),
        ("discount", cmd_discount), ("sort", cmd_sort), ("brands", cmd_brands),
        ("source", cmd_source), ("sources", cmd_sources),
        ("delsource", cmd_delsource), ("testsource", cmd_testsource),
        ("on", lambda u, c: cmd_toggle(u, c, True)),
        ("off", lambda u, c: cmd_toggle(u, c, False)),
    ]
    for cmd, handler in handlers:
        app.add_handler(CommandHandler(cmd, handler))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message))
    app.job_queue.run_repeating(scheduled, interval=CHECK_MIN * 60, first=10)
    app.run_polling(bootstrap_retries=10, timeout=30)


if __name__ == "__main__":
    main()
