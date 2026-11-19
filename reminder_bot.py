"""Telegram-бот напоминаний.

Формат: 14.11.26 17:30 "Запись к стоматологу"
Команды: /start /list /records /edit /delete /tz
"""
import asyncio
import html
import json
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (Application, CallbackQueryHandler, CommandHandler,
                          ContextTypes, MessageHandler, filters)

TOKEN = os.environ["REMINDER_BOT_TOKEN"]
DATA_DIR = Path(os.environ.get("DATA_DIR", "reminder_data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
REM_FILE = DATA_DIR / "reminders.json"
OWNER_FILE = DATA_DIR / "owner.json"
SETTINGS_FILE = DATA_DIR / "settings.json"
CHECK_SEC = int(os.environ.get("REMINDER_CHECK_SEC", "20"))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("reminderbot")

PATTERN = re.compile(
    r'^\s*(\d{1,2})\.(\d{1,2})\.(\d{2}|\d{4})\s+(\d{1,2}):(\d{2})\s+"(.+?)"\s*$'
)


def load_json(path, default):
    if path.exists():
        try:
            return json.loads(path.read_text("utf-8"))
        except (ValueError, json.JSONDecodeError):
            pass
    return default


def save_json(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), "utf-8")


def get_tz_offset() -> int:
    settings = load_json(SETTINGS_FILE, {})
    return int(settings.get("tz_offset", os.environ.get("TZ_OFFSET", "7")))


def tz() -> timezone:
    return timezone(timedelta(hours=get_tz_offset()))


def now_local() -> datetime:
    return datetime.now(tz())


def parse_reminder(text: str) -> tuple[datetime, str] | None:
    m = PATTERN.match(text or "")
    if not m:
        return None
    day, month, year_s, hour, minute, body = m.groups()
    year = int(year_s)
    if len(year_s) == 2:
        year += 2000 if year < 70 else 1900
    try:
        dt = datetime(year, int(month), int(day), int(hour), int(minute), tzinfo=tz())
    except ValueError:
        return None
    return dt, body.strip()


def get_owner() -> int:
    data = load_json(OWNER_FILE, {})
    return int(data.get("owner", os.environ.get("CHAT_ID", "0") or 0))


def fmt_dt(dt: datetime) -> str:
    return dt.strftime("%d.%m.%y %H:%M")


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    owner = update.effective_chat.id
    save_json(OWNER_FILE, {"owner": owner})
    await update.message.reply_text(
        "⏰ Бот напоминаний готов.\n\n"
        'Просто напиши: 14.11.26 17:30 "Запись к стоматологу"\n\n'
        "Команды:\n"
        "/records — панель записей (удаление/редактирование)\n"
        "/list — список напоминаний\n"
        '/edit 3 15.11.26 10:00 "Новый текст"\n'
        "/delete 3 — удалить\n"
        "/tz 7 — часовой пояс (сейчас UTC+%s)" % get_tz_offset()
    )


async def add_reminder(update: Update, text: str, chat_id: int) -> str:
    parsed = parse_reminder(text)
    if not parsed:
        return ('Не понял формат. Нужен пример: 14.11.26 17:30 "Текст напоминания"')
    dt, body = parsed
    if dt <= now_local():
        return "Эта дата уже в прошлом — укажи будущую."
    reminders = load_json(REM_FILE, [])
    rid = max([r["id"] for r in reminders], default=0) + 1
    reminders.append({"id": rid, "chat_id": chat_id,
                      "time": dt.isoformat(), "text": body})
    reminders.sort(key=lambda r: r["time"])
    save_json(REM_FILE, reminders)
    return f'✅ Напоминание #{rid} установлено на {fmt_dt(dt)}: "{body}"'


async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    owner = get_owner()
    if owner and update.effective_chat.id != owner:
        return
    text = update.message.text or ""
    if "edit" in (context.user_data or {}):
        # ожидаем новое значение после нажатия ✏️
        rid = context.user_data.pop("edit")
        reminders = load_json(REM_FILE, [])
        item = next((r for r in reminders if r["id"] == rid), None)
        if not item:
            await update.message.reply_text("Запись не найдена.")
            return
        parsed = parse_reminder(text)
        if not parsed:
            await update.message.reply_text('Нужен формат: 14.11.26 17:30 "Новый текст"')
            return
        dt, body = parsed
        item["time"] = dt.isoformat()
        item["text"] = body
        save_json(REM_FILE, reminders)
        await update.message.reply_text(f"✏️ Запись #{rid} обновлена: {fmt_dt(dt)} — {body}")
        return
    await update.message.reply_text(await add_reminder(update, text, update.effective_chat.id))


async def cmd_records(update: Update, context: ContextTypes.DEFAULT_TYPE):
    owner = get_owner()
    if owner and update.effective_chat.id != owner:
        return
    reminders = [r for r in load_json(REM_FILE, []) if r["chat_id"] == update.effective_chat.id]
    if not reminders:
        await update.message.reply_text("Активных записей нет.")
        return
    rows = []
    for r in reminders[:10]:
        dt = datetime.fromisoformat(r["time"])
        rows.append(f'#{r["id"]} · {fmt_dt(dt)} — {html.escape(r["text"])}')
    keyboard = []
    for r in reminders[:10]:
        keyboard.append([
            InlineKeyboardButton(f"✏️ #{r['id']}", callback_data=f"edit|{r['id']}"),
            InlineKeyboardButton(f"🗑 #{r['id']}", callback_data=f"del|{r['id']}"),
        ])
    text = "📋 <b>Мои записи</b>\n\n" + "\n".join(rows)
    if len(reminders) > 10:
        text += f"\n…и ещё {len(reminders) - 10}"
    await update.message.reply_text(text, parse_mode="HTML",
                                    reply_markup=InlineKeyboardMarkup(keyboard))


async def cmd_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await cmd_records(update, context)


async def cmd_edit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args or []
    if len(args) < 3:
        await update.message.reply_text('Формат: /edit 3 15.11.26 10:00 "Новый текст"')
        return
    text = " ".join(args[1:])
    reminders = load_json(REM_FILE, [])
    try:
        rid = int(args[0])
    except ValueError:
        await update.message.reply_text("Неверный номер записи.")
        return
    item = next((r for r in reminders if r["id"] == rid), None)
    if not item:
        await update.message.reply_text("Запись не найдена.")
        return
    parsed = parse_reminder(text)
    if not parsed:
        await update.message.reply_text('Нужен формат: 15.11.26 10:00 "Новый текст"')
        return
    dt, body = parsed
    item["time"], item["text"] = dt.isoformat(), body
    save_json(REM_FILE, reminders)
    await update.message.reply_text(f"✏️ Запись #{rid} обновлена: {fmt_dt(dt)} — {body}")


async def cmd_delete(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args or []
    if not args:
        await update.message.reply_text("Формат: /delete 3")
        return
    try:
        rid = int(args[0])
    except ValueError:
        await update.message.reply_text("Неверный номер записи.")
        return
    reminders = load_json(REM_FILE, [])
    new = [r for r in reminders if r["id"] != rid]
    if len(new) == len(reminders):
        await update.message.reply_text("Запись не найдена.")
        return
    save_json(REM_FILE, new)
    await update.message.reply_text(f"🗑 Запись #{rid} удалена.")


async def cmd_tz(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args or []
    if not args:
        await update.message.reply_text(f"Сейчас UTC+{get_tz_offset()}. Изменить: /tz 7")
        return
    try:
        value = int(args[0])
        if not -12 <= value <= 14:
            raise ValueError
    except ValueError:
        await update.message.reply_text("Укажи число от -12 до 14, например /tz 7")
        return
    settings = load_json(SETTINGS_FILE, {})
    settings["tz_offset"] = value
    save_json(SETTINGS_FILE, settings)
    await update.message.reply_text(f"✅ Часовой пояс: UTC+{value}")


async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    action, rid_s = (query.data or "").split("|", 1)
    rid = int(rid_s)
    reminders = load_json(REM_FILE, [])
    if action == "del":
        new = [r for r in reminders if r["id"] != rid]
        save_json(REM_FILE, new)
        await query.edit_message_text(f"🗑 Запись #{rid} удалена.")
    elif action == "edit":
        context.user_data["edit"] = rid
        await query.edit_message_text(f'✏️ Введи новые дату, время и текст для #{rid}:\n14.11.26 17:30 "Новый текст"')


async def check_due(context: ContextTypes.DEFAULT_TYPE):
    reminders = load_json(REM_FILE, [])
    if not reminders:
        return
    current_time = now_local()
    due, remaining = [], []
    for r in reminders:
        try:
            dt = datetime.fromisoformat(r["time"])
        except ValueError:
            continue
        if dt <= current_time:
            due.append(r)
        else:
            remaining.append(r)
    if not due:
        return
    save_json(REM_FILE, remaining)
    for r in due:
        try:
            await context.bot.send_message(
                r["chat_id"],
                f'⏰ <b>Напоминание!</b>\n\n{html.escape(r["text"])}',
                parse_mode="HTML",
            )
        except Exception as e:
            log.error("Не отправил напоминание: %s", e)


def main():
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("records", cmd_records))
    app.add_handler(CommandHandler("list", cmd_list))
    app.add_handler(CommandHandler("edit", cmd_edit))
    app.add_handler(CommandHandler("delete", cmd_delete))
    app.add_handler(CommandHandler("tz", cmd_tz))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message))
    app.job_queue.run_repeating(check_due, interval=CHECK_SEC, first=5)
    app.run_polling(bootstrap_retries=10, timeout=30)


if __name__ == "__main__":
    main()
