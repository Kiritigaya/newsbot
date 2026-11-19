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
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

# ---------------- Настройки ----------------
BOT_TOKEN = os.environ["BOT_TOKEN"]                    # токен от @BotFather
INTERVAL_MIN = int(os.environ.get("INTERVAL_MIN", "30"))  # как часто проверять ленты
MAX_PER_FEED = 3                                       # макс. новостей с одной ленты за проверку
TRANSLATE_TO_RU = os.environ.get("TRANSLATE_TO_RU", "1") != "0"  # переводить ли в русский
# Увеличивает дневной лимит MyMemory ( до 50 000 символов). Укажите свой e-mail при желании.
TRANSLATE_EMAIL = os.environ.get("TRANSLATE_EMAIL", "")

FEEDS = {
    "BBC World": "https://feeds.bbci.co.uk/news/world/rss.xml",
    "BBC Русская служба": "https://feeds.bbci.co.uk/russian/rss.xml",
    "DW Русский": "https://rss.dw.com/rdf/rss-ru-all",
    "Al Jazeera": "https://www.aljazeera.com/xml/rss/all.xml",
    "The Guardian World": "https://www.theguardian.com/world/rss",
}

# Если список не пуст — отправляются только новости с этими словами в заголовке.
# Пример: ["война", "выборы", "война", "war", "election", "earthquake"]
KEYWORDS: list[str] = []

DATA_DIR = Path(os.environ.get("DATA_DIR", "."))
DATA_DIR.mkdir(parents=True, exist_ok=True)

SEEN_FILE = DATA_DIR / "seen.json"
CHAT_ID_FILE = DATA_DIR / "chat_id.json"  # создаётся автоматически по команде /start
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("newsbot")


# ---------------- Вспомогательные функции ----------------
def load_seen() -> set[str]:
    if SEEN_FILE.exists():
        return set(json.loads(SEEN_FILE.read_text("utf-8")))
    return set()


def save_seen(seen: set[str]) -> None:
    SEEN_FILE.write_text(json.dumps(list(seen)[-3000:]), "utf-8")


def get_chat_id() -> int:
    """Chat id владельца: переменная окружения CHAT_ID или сохранённый через /start."""
    env_id = int(os.environ.get("CHAT_ID", "0") or 0)
    if env_id:
        return env_id
    if CHAT_ID_FILE.exists():
        try:
            return int(json.loads(CHAT_ID_FILE.read_text("utf-8")))
        except (ValueError, TypeError, json.JSONDecodeError):
            log.warning("Не удалось прочитать %s", CHAT_ID_FILE)
    return 0


def set_chat_id(chat_id: int) -> None:
    CHAT_ID_FILE.write_text(json.dumps(chat_id), "utf-8")


def clean(text: str) -> str:
    text = re.sub(r"<[^>]+>", " ", text or "")
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


# ---------------- Перевод ----------------
def needs_translation(text: str) -> bool:
    """Грубое определение: если кириллицы больше, чем латиницы — уже русский."""
    if not text:
        return False
    cyrillic = len(re.findall(r"[А-Яа-яЁё]", text))
    latin = len(re.findall(r"[A-Za-z]", text))
    return cyrillic <= latin


@lru_cache(maxsize=2000)
def translate_to_ru_cached(text: str) -> str:
    """Перевод с кэшем; сначала Google, при ошибке — MyMemory."""
    if not TRANSLATE_TO_RU or not needs_translation(text):
        return text
    # Готовим переводчики один раз на вызов (создание дешёвое)
    engines = [
        lambda: GoogleTranslator(source="auto", target="ru"),
        lambda: MyMemoryTranslator(source="english", target="russian", email=TRANSLATE_EMAIL),
    ]
    for make in engines:
        try:
            result = make().translate(text)
            if result:
                return result
        except Exception as e:
            log.debug("Переводчик не сработал: %s", type(e).__name__)
    log.warning("Не удалось перевести: %s…", text[:50])
    return text


async def translate_to_ru(text: str) -> str:
    if not TRANSLATE_TO_RU or not needs_translation(text):
        return text
    return await asyncio.to_thread(translate_to_ru_cached, text)


def extract_media(entry) -> tuple[str | None, str | None]:
    """Возвращает ('video'|'photo', url) или (None, None)."""
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


async def build_caption(source: str, entry) -> str:
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
    return "\n\n".join(parts)[:1024]  # лимит подписи к медиа в Telegram


async def send_item(bot, chat_id: int, source: str, entry) -> None:
    caption = await build_caption(source, entry)
    kind, url = extract_media(entry)
    try:
        if kind == "video" and url:
            await bot.send_video(chat_id, url, caption=caption, parse_mode="HTML")
            return
        if kind == "photo" and url:
            await bot.send_photo(chat_id, url, caption=caption, parse_mode="HTML")
            return
    except Exception as e:  # медиа недоступно/слишком большое — шлём текстом
        log.warning("Медиа не отправилось (%s), отправляю текстом", e)
    await bot.send_message(chat_id, caption, parse_mode="HTML",
                           disable_web_page_preview=False)


# ---------------- Основная логика ----------------
async def collect_news(bot, chat_id: int, seed_only: bool = False) -> int:
    seen = load_seen()
    sent = 0
    for source, url in FEEDS.items():
        try:
            feed = await asyncio.to_thread(feedparser.parse, url)
        except Exception as e:
            log.error("Ошибка загрузки %s: %s", source, e)
            continue

        fresh = []
        for entry in feed.entries:
            uid = entry.get("id") or entry.get("link")
            if not uid or uid in seen:
                continue
            seen.add(uid)
            title = entry.get("title", "").lower()
            if KEYWORDS and not any(k.lower() in title for k in KEYWORDS):
                continue
            fresh.append(entry)

        if seed_only:
            continue
        for entry in reversed(fresh[:MAX_PER_FEED]):  # от старых к новым
            await send_item(bot, chat_id, source, entry)
            sent += 1
            await asyncio.sleep(1.5)  # чтобы не упереться в лимиты Telegram

    save_seen(seen)
    return sent


# ---------------- Дайджесты за период ----------------
def entry_date(entry) -> datetime | None:
    """Дата публикации записи (feedparser возвращает время в UTC)."""
    for key in ("published_parsed", "updated_parsed"):
        tp = entry.get(key)
        if tp:
            try:
                return datetime(*tp[:6], tzinfo=timezone.utc)
            except (TypeError, ValueError):
                continue
    return None


async def collect_digest(hours: int) -> list[tuple[datetime, str, str, str]]:
    """Все новости из лент за последние `hours` часов, от новых к старым."""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    items: list[tuple[datetime, str, str, str]] = []
    seen: set[str] = set()

    for source, url in FEEDS.items():
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
            if KEYWORDS and not any(k.lower() in title.lower() for k in KEYWORDS):
                continue
            key = (entry.get("id") or entry.get("link") or title).strip()
            if not key or key in seen:
                continue
            seen.add(key)
            items.append((dt, source, clean(title), entry.get("link", "")))

    # Переводим заголовки параллельно, чтобы не тянуть время
    items = [
        (dt, src, await translate_to_ru(title), link)
        for dt, src, title, link in items
    ]
    items.sort(reverse=True)
    return items


def build_digest_messages(items: list[tuple[datetime, str, str, str]],
                          label: str, max_len: int = 3900) -> list[str]:
    """Разбивает дайджест на сообщения с учётом лимита Telegram (4096 символов)."""
    if not items:
        return []
    header = f"📰 <b>Новости за {label}</b>\n\n"
    messages: list[str] = []
    current = header
    for dt, source, title, link in items:
        date_str = dt.strftime("%d.%m %H:%M")
        link_html = html.escape(link, quote=True) if link else ""
        title_html = html.escape(title)
        line = f"• "
        if link_html:
            line += f'<a href="{link_html}">{title_html}</a>'
        else:
            line += title_html
        line += f" — <i>{html.escape(source)}</i> ({date_str})\n"
        if len(current) + len(line) > max_len:
            messages.append(current)
            current = line
        else:
            current += line
    if current.strip():
        messages.append(current)
    return messages


async def send_digest(update: Update, context: ContextTypes.DEFAULT_TYPE,
                      hours: int, label: str) -> None:
    owner_id = get_chat_id()
    if owner_id and update.effective_chat.id != owner_id:
        return  # только владелец
    await update.message.reply_text(f"Собираю новости за {label}…")
    items = await collect_digest(hours)
    messages = build_digest_messages(items, label)
    if not messages:
        await update.message.reply_text(
            f"За {label} новостей не найдено. "
            "Возможно, ленты хранят только последние несколько часов."
        )
        return
    for message in messages:
        await update.message.reply_text(
            message, parse_mode="HTML", disable_web_page_preview=True
        )
        await asyncio.sleep(0.5)


async def cmd_day(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await send_digest(update, context, 24, "прошедший день")


async def cmd_week(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await send_digest(update, context, 7 * 24, "прошлую неделю")


async def scheduled(context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = get_chat_id()
    if not chat_id:
        return  # владелец ещё не написал /start
    await collect_news(context.bot, chat_id)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    set_chat_id(chat_id)
    # Запоминаем текущие новости один раз, чтобы не завалить чат старыми статьями
    if not SEEN_FILE.exists():
        await collect_news(context.bot, chat_id, seed_only=True)
    log.info("Владелец задан: chat id %s", chat_id)
    await update.message.reply_text(
        f"Готово! Ваш chat id: {chat_id} сохранён.\n"
        f"Новости будут приходить автоматически каждые {INTERVAL_MIN} мин.\n"
        "Команды:\n"
        "/news — свежие новости прямо сейчас\n"
        "/day — все новости за прошедший день\n"
        "/week — все новости за прошлую неделю"
    )


async def cmd_news(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    owner_id = get_chat_id()
    if owner_id and update.effective_chat.id != owner_id:
        return  # бот отвечает только владельцу
    await update.message.reply_text("Ищу свежие новости…")
    n = await collect_news(context.bot, update.effective_chat.id)
    if n == 0:
        await update.message.reply_text("Новых важных новостей пока нет.")


def main() -> None:
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("news", cmd_news))
    app.add_handler(CommandHandler("day", cmd_day))
    app.add_handler(CommandHandler("week", cmd_week))
    app.job_queue.run_repeating(scheduled, interval=INTERVAL_MIN * 60, first=5)
    if not get_chat_id():
        log.info("Владелец не задан — напишите боту /start, чтобы включить рассылку.")
    app.run_polling(bootstrap_retries=10, timeout=30)


if __name__ == "__main__":
    main()
