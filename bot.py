"""Новостной Telegram-бот: RSS, перевод, дайджесты, панель настроек.

Основные команды: /start /help /news /day /week /digest /status /stats
Источники: /sources /addsource /delsource /testsource
Фильтры: /keywords /interval /autopilot /translate /settings
Избранное: /save /saved /unsave
"""
import asyncio
import html
import json
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path

import feedparser
from deep_translator import GoogleTranslator, MyMemoryTranslator
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (Application, CallbackQueryHandler, CommandHandler,
                          ContextTypes)

BOT_TOKEN = os.environ["BOT_TOKEN"]
MAX_PER_FEED = 3
TRANSLATE_EMAIL = os.environ.get("TRANSLATE_EMAIL", "")
DEFAULT_FEEDS = {
    "BBC World": "https://feeds.bbci.co.uk/news/world/rss.xml",
    "BBC Русская служба": "https://feeds.bbci.co.uk/russian/rss.xml",
    "DW Русский": "https://rss.dw.com/rdf/rss-ru-all",
    "Al Jazeera": "https://www.aljazeera.com/xml/rss/all.xml",
    "The Guardian World": "https://www.theguardian.com/world/rss",
}
DEFAULT_SETTINGS = {
    "sources": DEFAULT_FEEDS,
    "keywords": [],
    "interval_min": int(os.environ.get("INTERVAL_MIN", "30")),
    "autopilot": True,
    "translate": True,
}

DATA_DIR = Path(os.environ.get("DATA_DIR", "."))
DATA_DIR.mkdir(parents=True, exist_ok=True)
SEEN_FILE = DATA_DIR / "seen.json"
CHAT_ID_FILE = DATA_DIR / "chat_id.json"
SETTINGS_FILE = DATA_DIR / "settings.json"
SAVED_FILE = DATA_DIR / "saved.json"
STATS_FILE = DATA_DIR / "stats.json"
START_TIME = datetime.now(timezone.utc)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("newsbot")


# ---------------- Состояние ----------------
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


def load_seen() -> set[str]:
    return set(load_json(SEEN_FILE, []))


def save_seen(seen: set[str]) -> None:
    save_json(SEEN_FILE, list(seen)[-3000:])


def get_chat_id() -> int:
    env_id = int(os.environ.get("CHAT_ID", "0") or 0)
    if env_id:
        return env_id
    try:
        return int(load_json(CHAT_ID_FILE, 0))
    except (ValueError, TypeError):
        return 0


def set_chat_id(chat_id: int) -> None:
    save_json(CHAT_ID_FILE, chat_id)


def record_stat(key: str, value: int = 1) -> None:
    stats = load_json(STATS_FILE, {})
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    day = stats.setdefault(today, {})
    day[key] = day.get(key, 0) + value
    if len(stats) > 60:
        for old in sorted(stats)[:-60]:
            stats.pop(old, None)
    save_json(STATS_FILE, stats)


def today_stats() -> dict:
    return load_json(STATS_FILE, {}).get(datetime.now(timezone.utc).strftime("%Y-%m-%d"), {})


def clean(text: str) -> str:
    text = re.sub(r"<[^>]+>", " ", text or "")
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


def is_owner(update: Update) -> bool:
    owner = get_chat_id()
    return not owner or update.effective_chat.id == owner


# ---------------- Перевод ----------------
def needs_translation(text: str) -> bool:
    if not text:
        return False
    cyrillic = len(re.findall(r"[А-Яа-яЁё]", text))
    latin = len(re.findall(r"[A-Za-z]", text))
    return cyrillic <= latin


@lru_cache(maxsize=2000)
def translate_cached(text: str) -> str:
    if not needs_translation(text):
        return text
    for make in (
        lambda: GoogleTranslator(source="auto", target="ru"),
        lambda: MyMemoryTranslator(source="english", target="russian", email=TRANSLATE_EMAIL),
    ):
        try:
            result = make().translate(text)
            if result:
                return result
        except Exception as e:
            log.debug("Переводчик недоступен: %s", type(e).__name__)
    return text


async def translate_to_ru(text: str) -> str:
    if not settings()["translate"] or not text:
        return text
    return await asyncio.to_thread(translate_cached, text)


# ---------------- Ленты ----------------
def extract_media(entry) -> tuple[str | None, str | None]:
    for enc in entry.get("enclosures", []):
        t = enc.get("type", "")
        if t.startswith("video"):
            return "video", enc.get("href")
        if t.startswith("image"):
            return "photo", enc.get("href")
    for m in entry.get("media_content", []):
        if m.get("medium") == "video" or m.get("type", "").startswith("video"):
            return "video", m.get("url")
        if m.get("url"):
            return "photo", m["url"]
    for m in entry.get("media_thumbnail", []):
        if m.get("url"):
            return "photo", m["url"]
    img = re.search(r'<img[^>]+src="([^"]+)"', entry.get("summary", ""))
    if img:
        return "photo", img.group(1)
    return None, None


def title_key(title: str) -> str:
    return " ".join(re.findall(r"[a-zа-яё0-9]+", title.lower()))[:200]


def similar_to_recent(title: str, seen: set[str]) -> bool:
    key = title_key(title)
    if not key:
        return False
    words = set(key.split())
    if len(words) < 4:
        return False
    for old in list(seen)[-400:]:
        old_words = set(str(old).split())
        if len(words & old_words) / max(len(words | old_words), 1) >= 0.72:
            return True
    return False


async def build_caption(source: str, entry) -> tuple[str, str]:
    title = clean(entry.get("title", ""))
    summary = clean(entry.get("summary", ""))
    if len(summary) > 500:
        summary = summary[:500].rsplit(" ", 1)[0] + "…"
    title = html.escape(await translate_to_ru(title))
    summary = html.escape(await translate_to_ru(summary))
    link = html.escape(entry.get("link", ""), quote=True)
    parts = [f"🌍 <b>{title}</b>"]
    if summary and summary != title:
        parts.append(summary)
    parts.append(f'<i>{html.escape(source)}</i> · <a href="{link}">читать</a>')
    return "\n\n".join(parts)[:1024], clean(entry.get("title", ""))


async def send_item(bot, chat_id: int, source: str, entry, context=None) -> None:
    caption, raw_title = await build_caption(source, entry)
    kind, url = extract_media(entry)
    markup = None
    if context is not None:
        token = str(abs(hash(entry.get("link") or raw_title)))[:10]
        context.bot_data.setdefault("news_items", {})[token] = {
            "title": raw_title, "source": source,
            "link": entry.get("link", ""),
            "date": datetime.now(timezone.utc).strftime("%d.%m %H:%M"),
        }
        markup = InlineKeyboardMarkup([[InlineKeyboardButton("⭐ Сохранить", callback_data=f"save|{token}")]])
    try:
        if kind == "video" and url:
            await bot.send_video(chat_id, url, caption=caption, parse_mode="HTML", reply_markup=markup)
            return
        if kind == "photo" and url:
            await bot.send_photo(chat_id, url, caption=caption, parse_mode="HTML", reply_markup=markup)
            return
    except Exception as e:
        log.warning("Медиа недоступно (%s), отправляю текстом", e)
    await bot.send_message(chat_id, caption, parse_mode="HTML",
                           disable_web_page_preview=False, reply_markup=markup)


async def collect_news(bot, chat_id: int, seed_only: bool = False, context=None) -> int:
    s = settings()
    seen = load_seen()
    sent = 0
    for source, url in s["sources"].items():
        try:
            feed = await asyncio.to_thread(feedparser.parse, url)
        except Exception as e:
            log.error("Ошибка загрузки %s: %s", source, e)
            record_stat("feed_errors")
            continue
        fresh = []
        for entry in feed.entries:
            uid = entry.get("id") or entry.get("link")
            if not uid or uid in seen:
                continue
            seen.add(uid)
            title = entry.get("title", "")
            keys = s["keywords"]
            if keys and not any(k.lower() in title.lower() for k in keys):
                continue
            if similar_to_recent(title, seen):
                continue
            seen.add(title_key(title))
            fresh.append(entry)
        if seed_only:
            continue
        for entry in reversed(fresh[:MAX_PER_FEED]):
            await send_item(bot, chat_id, source, entry, context)
            sent += 1
            record_stat("sent")
            await asyncio.sleep(1.5)
    save_seen(seen)
    if sent:
        save_json(DATA_DIR / "last_check.json", {"at": datetime.now(timezone.utc).isoformat(), "sent": sent})
    return sent


# ---------------- Дайджесты ----------------
def entry_date(entry) -> datetime | None:
    for key in ("published_parsed", "updated_parsed"):
        tp = entry.get(key)
        if tp:
            try:
                return datetime(*tp[:6], tzinfo=timezone.utc)
            except (TypeError, ValueError):
                continue
    return None


async def collect_digest(hours: int) -> list[tuple[datetime, str, str, str]]:
    s = settings()
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    items: list[tuple[datetime, str, str, str]] = []
    used: set[str] = set()
    for source, url in s["sources"].items():
        try:
            feed = await asyncio.to_thread(feedparser.parse, url)
        except Exception as e:
            log.error("Ошибка загрузки %s: %s", source, e)
            continue
        for entry in feed.entries:
            dt = entry_date(entry)
            if dt is None or dt < cutoff:
                continue
            title = entry.get("title", "")
            if s["keywords"] and not any(k.lower() in title.lower() for k in s["keywords"]):
                continue
            key = title_key(title)
            if not key or key in used:
                continue
            used.add(key)
            items.append((dt, source, clean(title), entry.get("link", "")))
    items = [(dt, src, await translate_to_ru(title), link) for dt, src, title, link in items]
    items.sort(reverse=True)
    return items


def build_digest_messages(items, label: str, max_len: int = 3900) -> list[str]:
    if not items:
        return []
    header = f"📰 <b>Новости за {label}</b>\n\n"
    messages, current = [], header
    for dt, source, title, link in items:
        line = f"• "
        if link:
            line += f'<a href="{html.escape(link, quote=True)}">{html.escape(title)}</a>'
        else:
            line += html.escape(title)
        line += f" — <i>{html.escape(source)}</i> ({dt.strftime('%d.%m %H:%M')})\n"
        if len(current) + len(line) > max_len:
            messages.append(current)
            current = line
        else:
            current += line
    if current.strip():
        messages.append(current)
    return messages


async def send_digest(update, context, hours: int, label: str) -> None:
    if not is_owner(update):
        return
    await update.message.reply_text(f"Собираю новости за {label}…")
    items = await collect_digest(hours)
    messages = build_digest_messages(items, label)
    if not messages:
        await update.message.reply_text(f"За {label} новостей не найдено.")
        return
    for message in messages:
        await update.message.reply_text(message, parse_mode="HTML", disable_web_page_preview=True)
        await asyncio.sleep(0.5)


# ---------------- Команды ----------------
async def cmd_start(update, context):
    chat_id = update.effective_chat.id
    set_chat_id(chat_id)
    if not SEEN_FILE.exists():
        await collect_news(context.bot, chat_id, seed_only=True)
    await update.message.reply_text(
        "📰 Новостной бот готов.\n"
        f"Интервал: {settings()['interval_min']} мин.\n\n"
        "Основное: /news /day /week /digest\n"
        "Управление: /settings /sources /keywords /interval /autopilot\n"
        "Полный список: /help"
    )


async def cmd_help(update, context):
    if not is_owner(update):
        return
    await update.message.reply_text(
        "📋 <b>Команды</b>\n\n"
        "/news — свежие новости сейчас\n"
        "/day — за сутки · /week — за неделю · /digest — то же, что /week\n"
        "/status — состояние бота\n"
        "/stats — статистика за сегодня\n"
        "/settings — панель настроек\n"
        "/sources — список лент\n"
        "/addsource URL — добавить ленту\n"
        "/delsource N — удалить ленту\n"
        "/testsource URL — проверить ленту\n"
        "/keywords слово1,слово2 — фильтр (пусто = сброс)\n"
        "/interval 30 — интервал в минутах (5–1440)\n"
        "/autopilot on|off — пауза рассылки\n"
        "/translate on|off — перевод на русский\n"
        "/save — сохранить последнюю новость\n"
        "/saved — избранное · /unsave N — удалить из избранного\n"
        "/ping — проверка связи",
        parse_mode="HTML",
    )


async def cmd_news(update, context):
    if not is_owner(update):
        return
    await update.message.reply_text("Ищу свежие новости…")
    n = await collect_news(context.bot, update.effective_chat.id, context=context)
    if n == 0:
        await update.message.reply_text("Новых важных новостей пока нет.")


async def cmd_day(update, context):
    await send_digest(update, context, 24, "прошедший день")


async def cmd_week(update, context):
    await send_digest(update, context, 7 * 24, "прошлую неделю")


async def cmd_digest(update, context):
    await send_digest(update, context, 7 * 24, "прошлую неделю")


async def cmd_status(update, context):
    if not is_owner(update):
        return
    s = settings()
    uptime = datetime.now(timezone.utc) - START_TIME
    last = load_json(DATA_DIR / "last_check.json", {})
    stats = today_stats()
    text = (
        "🟢 <b>Статус</b>\n\n"
        f"Источников: {len(s['sources'])}\n"
        f"Интервал: {s['interval_min']} мин.\n"
        f"Авторассылка: {'включена' if s['autopilot'] else 'на паузе'}\n"
        f"Перевод: {'включён' if s['translate'] else 'выключен'}\n"
        f"Фильтр-слова: {', '.join(s['keywords']) or 'нет'}\n"
        f"Отправлено сегодня: {stats.get('sent', 0)}\n"
        f"Ошибок лент сегодня: {stats.get('feed_errors', 0)}\n"
        f"Последняя проверка: {html.escape(last.get('at', 'ещё не было'))[:19]}\n"
        f"В сети: {str(uptime).split('.')[0]}\n"
        f"В избранном: {len(load_json(SAVED_FILE, []))}"
    )
    await update.message.reply_text(text, parse_mode="HTML")


async def cmd_stats(update, context):
    if not is_owner(update):
        return
    stats = load_json(STATS_FILE, {})
    lines = []
    for day in sorted(stats, reverse=True)[:7]:
        d = stats[day]
        lines.append(f"{day}: 📤 {d.get('sent', 0)} · ⚠️ {d.get('feed_errors', 0)}")
    await update.message.reply_text("📊 <b>Статистика</b>\n\n" + "\n".join(lines) or "Нет данных", parse_mode="HTML")


async def cmd_sources(update, context):
    if not is_owner(update):
        return
    s = settings()
    lines = [f"{i}. <b>{html.escape(name)}</b> — {html.escape(url)}"
             for i, (name, url) in enumerate(s["sources"].items(), 1)]
    await update.message.reply_text("📡 <b>Ленты</b>\n\n" + "\n".join(lines), parse_mode="HTML",
                                    disable_web_page_preview=True)


async def cmd_addsource(update, context):
    if not is_owner(update):
        return
    args = context.args or []
    if not args:
        await update.message.reply_text("Формат: /addsource https://site/rss.xml \"Название\"")
        return
    url = args[0].strip()
    name = " ".join(args[1:]).strip("\"' ") or f"Лента {len(settings()['sources']) + 1}"
    s = settings()
    s["sources"][name] = url
    save_json(SETTINGS_FILE, s)
    await update.message.reply_text(f"✅ Добавлено: {name}")


async def cmd_delsource(update, context):
    if not is_owner(update):
        return
    args = context.args or []
    if not args or not args[0].isdigit():
        await update.message.reply_text("Формат: /delsource 3 (номер из /sources)")
        return
    s = settings()
    names = list(s["sources"])
    idx = int(args[0]) - 1
    if not 0 <= idx < len(names):
        await update.message.reply_text("Нет такого номера.")
        return
    removed = names.pop(idx)
    s["sources"] = {n: s["sources"][n] for n in names}
    save_json(SETTINGS_FILE, s)
    await update.message.reply_text(f"🗑 Удалено: {removed}")


async def cmd_testsource(update, context):
    if not is_owner(update):
        return
    args = context.args or []
    if not args:
        await update.message.reply_text("Формат: /testsource https://site/rss.xml")
        return
    url = args[0]
    try:
        feed = await asyncio.to_thread(feedparser.parse, url)
        count = len(feed.entries)
        title = feed.feed.get("title", "без названия")
        await update.message.reply_text(f"✅ Лента доступна: {title} · записей: {count}")
    except Exception as e:
        await update.message.reply_text(f"❌ Ошибка: {e}")


async def cmd_keywords(update, context):
    if not is_owner(update):
        return
    args = context.args or []
    s = settings()
    if not args:
        await update.message.reply_text("Сейчас фильтр: " + (", ".join(s["keywords"]) or "нет") +
                                        "\nЗадать: /keywords война,выборы\nСбросить: /keywords clear")
        return
    raw = " ".join(args).strip()
    s["keywords"] = [] if raw.lower() == "clear" else [k.strip() for k in raw.split(",") if k.strip()]
    save_json(SETTINGS_FILE, s)
    await update.message.reply_text("✅ Фильтр: " + (", ".join(s["keywords"]) or "сброшен"))


async def reschedule(context) -> None:
    job = context.bot_data.get("news_job")
    if job:
        job.schedule_interval(settings()["interval_min"] * 60)


async def cmd_interval(update, context):
    if not is_owner(update):
        return
    args = context.args or []
    if not args or not args[0].isdigit():
        await update.message.reply_text("Формат: /interval 30 (от 5 до 1440 минут)")
        return
    value = int(args[0])
    if not 5 <= value <= 1440:
        await update.message.reply_text("Укажите от 5 до 1440 минут.")
        return
    s = settings()
    s["interval_min"] = value
    save_json(SETTINGS_FILE, s)
    await reschedule(context)
    await update.message.reply_text(f"✅ Интервал: {value} мин.")


async def cmd_autopilot(update, context):
    if not is_owner(update):
        return
    args = context.args or []
    s = settings()
    if args and args[0].lower() in ("on", "off"):
        s["autopilot"] = args[0].lower() == "on"
    else:
        s["autopilot"] = not s["autopilot"]
    save_json(SETTINGS_FILE, s)
    await update.message.reply_text(f"Авторассылка: {'включена' if s['autopilot'] else 'на паузе'}")


async def cmd_translate(update, context):
    if not is_owner(update):
        return
    args = context.args or []
    s = settings()
    if args and args[0].lower() in ("on", "off"):
        s["translate"] = args[0].lower() == "on"
    else:
        s["translate"] = not s["translate"]
    save_json(SETTINGS_FILE, s)
    await update.message.reply_text(f"Перевод: {'включён' if s['translate'] else 'выключен'}")


async def cmd_settings(update, context):
    if not is_owner(update):
        return
    s = settings()
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton(f"{'⏸ Пауза' if s['autopilot'] else '▶️ Включить'}", callback_data="set|autopilot")],
        [InlineKeyboardButton(f"{'🇷🇺 Перевод вкл' if s['translate'] else 'Перевод выкл'}", callback_data="set|translate")],
        [InlineKeyboardButton("⏱ 15 мин", callback_data="set|int15"),
         InlineKeyboardButton("⏱ 30 мин", callback_data="set|int30"),
         InlineKeyboardButton("⏱ 60 мин", callback_data="set|int60")],
        [InlineKeyboardButton("📡 Ленты", callback_data="set|sources"),
         InlineKeyboardButton("📊 Статус", callback_data="set|status")],
    ])
    await update.message.reply_text("⚙️ <b>Панель настроек</b>", parse_mode="HTML", reply_markup=keyboard)


async def on_button(update, context):
    query = update.callback_query
    await query.answer()
    data = query.data or ""
    if data.startswith("save|"):
        token = data.split("|", 1)[1]
        item = context.bot_data.get("news_items", {}).get(token)
        if not item:
            await query.answer("Новость уже недоступна", show_alert=True)
            return
        saved = load_json(SAVED_FILE, [])
        if not any(x.get("link") == item["link"] for x in saved):
            saved.append(item)
            save_json(SAVED_FILE, saved)
            record_stat("saved")
        await query.answer("⭐ Сохранено")
        await query.edit_message_reply_markup(reply_markup=None)
        return
    if data.startswith("set|"):
        action = data.split("|", 1)[1]
        s = settings()
        if action == "autopilot":
            s["autopilot"] = not s["autopilot"]
        elif action == "translate":
            s["translate"] = not s["translate"]
        elif action.startswith("int"):
            s["interval_min"] = int(action[3:])
            await reschedule(context)
        save_json(SETTINGS_FILE, s)
        if action == "sources":
            lines = [f"{i}. {n}" for i, (n, _) in enumerate(s["sources"].items(), 1)]
            await query.edit_message_text("Ленты:\n" + "\n".join(lines))
            return
        if action == "status":
            await query.edit_message_text(
                f"Авто: {'вкл' if s['autopilot'] else 'пауза'} · Перевод: {'вкл' if s['translate'] else 'выкл'}\n"
                f"Интервал: {s['interval_min']} мин · Лент: {len(s['sources'])}")
            return
        await query.edit_message_text(
            f"✅ Авто: {'вкл' if s['autopilot'] else 'пауза'} · Перевод: {'вкл' if s['translate'] else 'выкл'}\n"
            f"Интервал: {s['interval_min']} мин")


async def cmd_saved(update, context):
    if not is_owner(update):
        return
    saved = load_json(SAVED_FILE, [])
    if not saved:
        await update.message.reply_text("Избранное пусто. Нажмите ⭐ под новостью, чтобы сохранить.")
        return
    lines = []
    for i, item in enumerate(saved[:20], 1):
        link = html.escape(item.get("link", ""), quote=True)
        title = html.escape(item.get("title", "")[:90])
        lines.append(f'{i}. <a href="{link}">{title}</a> — <i>{html.escape(item.get("source", ""))}</i>')
    await update.message.reply_text("⭐ <b>Избранное</b>\n\n" + "\n".join(lines), parse_mode="HTML",
                                    disable_web_page_preview=True)


async def cmd_unsave(update, context):
    if not is_owner(update):
        return
    args = context.args or []
    if not args or not args[0].isdigit():
        await update.message.reply_text("Формат: /unsave 2 (номер из /saved)")
        return
    saved = load_json(SAVED_FILE, [])
    idx = int(args[0]) - 1
    if not 0 <= idx < len(saved):
        await update.message.reply_text("Нет такого номера.")
        return
    saved.pop(idx)
    save_json(SAVED_FILE, saved)
    await update.message.reply_text("🗑 Удалено из избранного.")


async def cmd_ping(update, context):
    latency = int((datetime.now(timezone.utc) - START_TIME).total_seconds())
    await update.message.reply_text(f"🏓 pong · в сети {latency} с")


async def scheduled(context):
    s = settings()
    if not s["autopilot"]:
        return
    chat_id = get_chat_id()
    if chat_id:
        await collect_news(context.bot, chat_id, context=context)


def main() -> None:
    app = Application.builder().token(BOT_TOKEN).build()
    for cmd, handler in [
        ("start", cmd_start), ("help", cmd_help), ("news", cmd_news),
        ("day", cmd_day), ("week", cmd_week), ("digest", cmd_digest),
        ("status", cmd_status), ("stats", cmd_stats),
        ("sources", cmd_sources), ("addsource", cmd_addsource),
        ("delsource", cmd_delsource), ("testsource", cmd_testsource),
        ("keywords", cmd_keywords), ("interval", cmd_interval),
        ("autopilot", cmd_autopilot), ("translate", cmd_translate),
        ("settings", cmd_settings), ("saved", cmd_saved),
        ("unsave", cmd_unsave), ("ping", cmd_ping),
    ]:
        app.add_handler(CommandHandler(cmd, handler))
    app.add_handler(CallbackQueryHandler(on_button))
    job = app.job_queue.run_repeating(
        scheduled, interval=settings()["interval_min"] * 60, first=5
    )
    app.bot_data["news_job"] = job
    app.run_polling(bootstrap_retries=10, timeout=30)


if __name__ == "__main__":
    main()
