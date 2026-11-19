"""Одноразовый запуск бота для GitHub Actions.

За одну итерацию:
1. читает команды /start, /news, /day, /week из Telegram;
2. отправляет свежие новости;
3. сохраняет состояние (offset, seen.json, chat_id.json) в папку state —
   workflow коммитит её обратно в репозиторий.
"""
import asyncio
import json
import os
from pathlib import Path

from telegram import Bot

import bot as newsbot

STATE_DIR = Path(os.environ.get("DATA_DIR", "state"))
OFFSET_FILE = STATE_DIR / "offset.json"


def load_offset() -> int:
    if OFFSET_FILE.exists():
        try:
            return int(json.loads(OFFSET_FILE.read_text("utf-8")))
        except (ValueError, TypeError, json.JSONDecodeError):
            pass
    return 0


def save_offset(offset: int) -> None:
    OFFSET_FILE.write_text(json.dumps(offset), "utf-8")


async def send_text(tg: Bot, chat_id: int, text: str) -> None:
    await tg.send_message(chat_id, text, parse_mode="HTML",
                          disable_web_page_preview=True)


async def handle_command(tg: Bot, chat_id: int, text: str) -> None:
    command = text.split()[0].split("@")[0].lower()
    if command == "/start":
        newsbot.set_chat_id(chat_id)
        if not newsbot.SEEN_FILE.exists():
            await newsbot.collect_news(tg, chat_id, seed_only=True)
        await send_text(tg, chat_id,
                        f"✅ Готово! Chat id {chat_id} сохранён.\n"
                        "/news — свежие новости\n"
                        "/day — за прошедший день\n"
                        "/week — за прошлую неделю")
    elif command == "/news":
        await send_text(tg, chat_id, "Ищу свежие новости…")
        n = await newsbot.collect_news(tg, chat_id)
        if n == 0:
            await send_text(tg, chat_id, "Новых важных новостей пока нет.")
    elif command in ("/day", "/week"):
        hours, label = (24, "прошедший день") if command == "/day" else (7 * 24, "прошлую неделю")
        await send_text(tg, chat_id, f"Собираю новости за {label}…")
        items = await newsbot.collect_digest(hours)
        messages = newsbot.build_digest_messages(items, label)
        if not messages:
            await send_text(tg, chat_id, f"За {label} новостей не найдено.")
        else:
            for message in messages:
                await send_text(tg, chat_id, message)
                await asyncio.sleep(0.4)


async def main() -> None:
    tg = Bot(newsbot.BOT_TOKEN)
    owner = newsbot.get_chat_id()
    offset = load_offset()

    # 1. Команды от владельца
    try:
        updates = await tg.get_updates(offset=offset, timeout=0)
        for upd in updates:
            offset = max(offset, upd.update_id + 1)
            msg = upd.message or (upd.edited_message if hasattr(upd, "edited_message") else None)
            if not msg or not msg.text:
                continue
            if owner and msg.chat.id != owner:
                continue
            try:
                await handle_command(tg, msg.chat.id, msg.text)
            except Exception as e:
                newsbot.log.error("Ошибка команды: %s", e)
    except Exception as e:
        newsbot.log.error("getUpdates: %s", e)
    save_offset(offset)

    # 2. Плановая рассылка новых новостей
    if owner:
        try:
            await newsbot.collect_news(tg, owner)
        except Exception as e:
            newsbot.log.error("Рассылка: %s", e)


if __name__ == "__main__":
    asyncio.run(main())
