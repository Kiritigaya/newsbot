"""Telegram-бот напоминаний.

Форматы:
  14.11.26 17:30 "Запись к стоматологу"
  через 10 минут "Позвонить"
  завтра в 9:00 "Совещание"
  каждый день в 8:30 "Зарядка"

Команды: /start /help /records /list /next /find /edit /delete /done
         /clear /tz /count /snooze
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

PATTERN = re.compile(r'^\s*(\d{1,2})\.(\d{1,2})\.(\d{2}|\d{4})\s+(\d{1,2}):(\d{2})\s+"(.+?)"\s*$')


def load_json(path, default):
    if path.exists():
        try:
            return json.loads(path.read_text("utf-8"))
        except (ValueError, json.JSONDecodeError):
            pass
    return json.loads(json.dumps(default))


def save_json(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), "utf-8")


def get_tz_offset() -> int:
    return int(load_json(SETTINGS_FILE, {}).get("tz_offset", os.environ.get("TZ_OFFSET", "7")))


def tz() -> timezone:
    return timezone(timedelta(hours=get_tz_offset()))


def now_local() -> datetime:
    return datetime.now(tz())


def fmt_dt(dt: datetime) -> str:
    return dt.strftime("%d.%m.%y %H:%M")


def mark(r: dict) -> str:
    p = "🔴 " if r.get("priority") == "high" else "🔵 " if r.get("priority") == "low" else ""
    tags = " ".join("#" + t for t in r.get("tags", []))
    return p + (tags + " " if tags else "")


def strip_quotes(text: str) -> str:
    return text.strip().strip('"«»""').strip()


def parse_input(text: str) -> tuple[datetime, str, str | None] | None:
    """Возвращает (время, текст, повтор) или None."""
    text = (text or "").strip()

    # 1. Классический формат с датой
    m = PATTERN.match(text)
    if m:
        day, month, year_s, hour, minute, body = m.groups()
        year = int(year_s)
        if len(year_s) == 2:
            year += 2000 if year < 70 else 1900
        try:
            return datetime(year, int(month), int(day), int(hour), int(minute), tzinfo=tz()), body.strip(), None
        except ValueError:
            return None

    low = text.lower()

    # 2. Повторяющиеся
    repeat = None
    rest = text
    for word, rule in (("каждый день", "daily"), ("ежедневно", "daily"), ("every day", "daily"),
                       ("каждую неделю", "weekly"), ("еженедельно", "weekly"), ("every week", "weekly"),
                       ("каждый месяц", "monthly"), ("ежемесячно", "monthly"), ("every month", "monthly")):
        if word in low:
            repeat = rule
            rest = text[low.index(word) + len(word):].strip()
            break

    # 3. Относительные ("через N минут/часов", "in N minutes")
    rel = re.search(r'(?:через|in)\s+(\d+)\s*(минут|мин|часов?|час|часа|ден|дня|дней|недел|week|day|hour|minute)s?', low)
    if rel and not repeat:
        amount = int(rel.group(1))
        unit = rel.group(2)
        if unit.startswith(("мин", "minute")):
            delta = timedelta(minutes=amount)
        elif unit.startswith(("час", "hour")):
            delta = timedelta(hours=amount)
        elif unit.startswith(("недел", "week")):
            delta = timedelta(weeks=amount)
        else:
            delta = timedelta(days=amount)
        body = strip_quotes(text[rel.end():])
        if not body:
            return None
        return now_local() + delta, body, None

    # 4. Завтра / сегодня / повтор с временем
    m = re.search(r'(завтра|сегодня|tomorrow|today)?\s*(?:в\s*|at\s*)?(\d{1,2}):(\d{2})', low)
    if m:
        hour, minute = int(m.group(2)), int(m.group(3))
        word = m.group(1) or ""
        body = strip_quotes(text[m.end():])
        if not body:
            body = strip_quotes(text[:m.start()] or text)
        if not body:
            return None
        base = now_local()
        if word in ("завтра", "tomorrow"):
            base += timedelta(days=1)
        dt = base.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if dt <= base and not repeat:
            dt += timedelta(days=1)
        if repeat == "weekly":
            dt += timedelta(weeks=1)
        if repeat == "monthly":
            dt = add_month(dt)
        return dt, body, repeat

    return None


def add_month(dt: datetime) -> datetime:
    year = dt.year + (dt.month // 12)
    month = dt.month % 12 + 1
    day = min(dt.day, [31, 29 if year % 4 == 0 and (year % 100 != 0 or year % 400 == 0) else 28,
                       31, 30, 31, 30, 31, 31, 30, 31, 30, 31][month - 1])
    return dt.replace(year=year, month=month, day=day)


def get_owner() -> int:
    data = load_json(OWNER_FILE, {})
    return int(data.get("owner", os.environ.get("CHAT_ID", "0") or 0))


def own_reminders(chat_id: int) -> list[dict]:
    return [r for r in load_json(REM_FILE, []) if r["chat_id"] == chat_id]


async def add_reminder(chat_id: int, text: str) -> str:
    parsed = parse_input(text)
    if not parsed:
        return ('Не понял формат. Примеры:\n14.11.26 17:30 "Текст"\n'
                'через 10 минут "Позвонить"\nзавтра в 9:00 "Совещание"\nкаждый день в 8:30 "Зарядка"')
    dt, body, repeat = parsed
    if dt <= now_local():
        return "Это время уже прошло — укажите будущее."
    reminders = load_json(REM_FILE, [])
    rid = max([r["id"] for r in reminders], default=0) + 1
    reminders.append({"id": rid, "chat_id": chat_id, "time": dt.isoformat(),
                      "text": body, "repeat": repeat or "",
                      "priority": "", "tags": []})
    reminders.sort(key=lambda r: r["time"])
    save_json(REM_FILE, reminders)
    rep = {"daily": " · каждый день", "weekly": " · каждую неделю", "monthly": " · каждый месяц"}.get(repeat, "")
    return f'✅ Напоминание #{rid} на {fmt_dt(dt)}{rep}: "{body}"'


async def cmd_start(update, context):
    save_json(OWNER_FILE, {"owner": update.effective_chat.id})
    await update.message.reply_text(
        "⏰ Бот напоминаний готов.\n\n"
        '14.11.26 17:30 "Запись к стоматологу"\n'
        'через 10 минут "Позвонить"\n'
        'завтра в 9:00 "Совещание"\n'
        'каждый день в 8:30 "Зарядка"\n\n'
        "/records — панель · /help — все команды"
    )


async def cmd_help(update, context):
    if not is_owner(update):
        return
    await update.message.reply_text(
        "📋 <b>Команды</b>\n\n"
        "/records — панель записей (удаление/редактирование)\n"
        "/list — то же, списком\n"
        "/next — ближайшее напоминание\n"
        "/find текст — поиск\n"
        '/edit 3 15.11.26 10:00 "Новый текст"\n'
        "/delete 3 — удалить\n"
        "/done 3 — отметить выполненным\n"
        "/snooze 3 10 — отложить на 10 минут\n"
        "/clear — удалить все (с подтверждением)\n"
        "/count — количество записей\n"
        "/repeat 3 daily|weekly|monthly — повтор\n"
        "/priority 3 high|low — приоритет 🔴/🔵\n"
        "/tag 3 работа — тег · /tagged работа — поиск по тегу\n"
        "/tz 7 — часовой пояс\n\n"
        "Форматы ввода:\n"
        '14.11.26 17:30 "Текст"\n'
        'через 10 минут "Текст"\n'
        'завтра в 9:00 "Текст"\n'
        'каждый день в 8:30 "Текст"',
        parse_mode="HTML")


def is_owner(update) -> bool:
    owner = get_owner()
    return not owner or update.effective_chat.id == owner


async def records_page(update, context, page: int = 0):
    if not is_owner(update):
        return
    all_items = own_reminders(update.effective_chat.id)
    if not all_items:
        await update.message.reply_text("Активных записей нет.")
        return
    per_page = 6
    pages = (len(all_items) + per_page - 1) // per_page
    page = max(0, min(page, pages - 1))
    chunk = all_items[page * per_page:(page + 1) * per_page]
    rows = []
    for r in chunk:
        dt = datetime.fromisoformat(r["time"])
        rep = {"daily": " 🔁", "weekly": " 🔁", "monthly": " 🔁"}.get(r.get("repeat"), "")
        rows.append(f'#{r["id"]} · {mark(r)}{fmt_dt(dt)}{rep} — {html.escape(r["text"])}')
    keyboard = []
    for r in chunk:
        keyboard.append([
            InlineKeyboardButton(f"✏️ {r['id']}", callback_data=f"edit|{r['id']}"),
            InlineKeyboardButton(f"🗑 {r['id']}", callback_data=f"del|{r['id']}"),
        ])
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀️", callback_data=f"page|{page - 1}"))
    nav.append(InlineKeyboardButton(f"{page + 1}/{pages}", callback_data="page|0"))
    if page < pages - 1:
        nav.append(InlineKeyboardButton("▶️", callback_data=f"page|{page + 1}"))
    keyboard.append(nav)
    text = f"📋 <b>Мои записи</b> ({len(all_items)})\n\n" + "\n".join(rows)
    await update.message.reply_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))


async def cmd_records(update, context):
    await records_page(update, context, 0)


async def cmd_list(update, context):
    await records_page(update, context, 0)


async def cmd_next(update, context):
    if not is_owner(update):
        return
    items = own_reminders(update.effective_chat.id)
    if not items:
        await update.message.reply_text("Записей нет.")
        return
    item = min(items, key=lambda r: r["time"])
    dt = datetime.fromisoformat(item["time"])
    left = dt - now_local()
    mins = int(left.total_seconds() // 60)
    await update.message.reply_text(
        f"⏭ Ближайшее: #{item['id']} · {fmt_dt(dt)} (через {mins} мин)\n{item['text']}")


async def cmd_count(update, context):
    if not is_owner(update):
        return
    await update.message.reply_text(f"Всего записей: {len(own_reminders(update.effective_chat.id))}")


async def cmd_find(update, context):
    if not is_owner(update):
        return
    args = context.args or []
    if not args:
        await update.message.reply_text("Формат: /find текст")
        return
    query = " ".join(args).lower()
    found = [r for r in own_reminders(update.effective_chat.id) if query in r["text"].lower()]
    if not found:
        await update.message.reply_text("Ничего не найдено.")
        return
    lines = []
    for r in found[:15]:
        lines.append(f'#{r["id"]} · {mark(r)}{fmt_dt(datetime.fromisoformat(r["time"]))} — {html.escape(r["text"])}')
    await update.message.reply_text("🔍 " + "\n".join(lines), parse_mode="HTML")


async def change_item(update, context, rid: int, new_text: str):
    reminders = load_json(REM_FILE, [])
    item = next((r for r in reminders if r["id"] == rid), None)
    if not item:
        await update.message.reply_text("Запись не найдена.")
        return
    parsed = parse_input(new_text)
    if not parsed:
        await update.message.reply_text('Нужен формат: 15.11.26 10:00 "Новый текст"')
        return
    dt, body, repeat = parsed
    item["time"] = dt.isoformat()
    item["text"] = body
    item["repeat"] = repeat or ""
    save_json(REM_FILE, reminders)
    await update.message.reply_text(f"✏️ #{rid} обновлено: {fmt_dt(dt)} — {body}")


async def cmd_edit(update, context):
    if not is_owner(update):
        return
    args = context.args or []
    if len(args) < 3:
        await update.message.reply_text('Формат: /edit 3 15.11.26 10:00 "Новый текст"')
        return
    try:
        rid = int(args[0])
    except ValueError:
        await update.message.reply_text("Неверный номер.")
        return
    await change_item(update, context, rid, " ".join(args[1:]))


async def delete_item(update, context, rid: int):
    reminders = load_json(REM_FILE, [])
    new = [r for r in reminders if r["id"] != rid]
    if len(new) == len(reminders):
        await update.message.reply_text("Запись не найдена.")
        return
    save_json(REM_FILE, new)
    await update.message.reply_text(f"🗑 Запись #{rid} удалена.")


async def cmd_delete(update, context):
    if not is_owner(update):
        return
    args = context.args or []
    if not args or not args[0].isdigit():
        await update.message.reply_text("Формат: /delete 3")
        return
    await delete_item(update, context, int(args[0]))


async def cmd_done(update, context):
    if not is_owner(update):
        return
    args = context.args or []
    if not args or not args[0].isdigit():
        await update.message.reply_text("Формат: /done 3")
        return
    await delete_item(update, context, int(args[0]))


async def cmd_snooze(update, context):
    if not is_owner(update):
        return
    args = context.args or []
    if len(args) < 2 or not args[0].isdigit() or not args[1].isdigit():
        await update.message.reply_text("Формат: /snooze 3 10 (минуты)")
        return
    rid, minutes = int(args[0]), int(args[1])
    reminders = load_json(REM_FILE, [])
    item = next((r for r in reminders if r["id"] == rid), None)
    if not item:
        await update.message.reply_text("Запись не найдена.")
        return
    item["time"] = (now_local() + timedelta(minutes=minutes)).isoformat()
    save_json(REM_FILE, reminders)
    await update.message.reply_text(f"⏳ #{rid} отложено на {minutes} мин.")


async def cmd_clear(update, context):
    if not is_owner(update):
        return
    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("✅ Да, удалить всё", callback_data="clear|yes")]])
    await update.message.reply_text("Удалить все записи?", reply_markup=keyboard)


async def cmd_tz(update, context):
    if not is_owner(update):
        return
    args = context.args or []
    if not args:
        await update.message.reply_text(f"Сейчас UTC+{get_tz_offset()}. Изменить: /tz 7")
        return
    try:
        value = int(args[0])
        if not -12 <= value <= 14:
            raise ValueError
    except ValueError:
        await update.message.reply_text("Укажите число от -12 до 14.")
        return
    save_json(SETTINGS_FILE, {"tz_offset": value})
    await update.message.reply_text(f"✅ Часовой пояс UTC+{value}")


async def on_message(update, context):
    if not is_owner(update):
        return
    text = update.message.text or ""
    if text.startswith("/clear"):
        await cmd_clear(update, context)
        return
    if (context.user_data or {}).get("edit"):
        rid = context.user_data.pop("edit")
        await change_item(update, context, rid, text)
        return
    await update.message.reply_text(await add_reminder(update.effective_chat.id, text))


async def on_button(update, context):
    query = update.callback_query
    await query.answer()
    data = query.data or ""
    if data.startswith("page|"):
        page = int(data.split("|", 1)[1])
        all_items = own_reminders(query.message.chat_id)
        if not all_items:
            return
        per_page = 6
        pages = (len(all_items) + per_page - 1) // per_page
        page = max(0, min(page, pages - 1))
        chunk = all_items[page * per_page:(page + 1) * per_page]
        rows = [f'#{r["id"]} · {mark(r)}{fmt_dt(datetime.fromisoformat(r["time"]))} — {html.escape(r["text"])}' for r in chunk]
        keyboard = []
        for r in chunk:
            keyboard.append([
                InlineKeyboardButton(f"✏️ {r['id']}", callback_data=f"edit|{r['id']}"),
                InlineKeyboardButton(f"🗑 {r['id']}", callback_data=f"del|{r['id']}"),
            ])
        nav = []
        if page > 0:
            nav.append(InlineKeyboardButton("◀️", callback_data=f"page|{page - 1}"))
        nav.append(InlineKeyboardButton(f"{page + 1}/{pages}", callback_data="page|0"))
        if page < pages - 1:
            nav.append(InlineKeyboardButton("▶️", callback_data=f"page|{page + 1}"))
        keyboard.append(nav)
        await query.edit_message_text(f"📋 <b>Мои записи</b>\n\n" + "\n".join(rows), parse_mode="HTML",
                                      reply_markup=InlineKeyboardMarkup(keyboard))
        return
    if data.startswith("del|"):
        rid = int(data.split("|", 1)[1])
        reminders = load_json(REM_FILE, [])
        save_json(REM_FILE, [r for r in reminders if r["id"] != rid])
        await query.edit_message_text(f"🗑 Запись #{rid} удалена.")
        return
    if data.startswith("edit|"):
        rid = int(data.split("|", 1)[1])
        context.user_data["edit"] = rid
        await query.edit_message_text(f'✏️ Введи новые дату/время и текст для #{rid}:\n15.11.26 10:00 "Новый текст"')
        return
    if data.startswith("snooze|"):
        _, rid_s, minutes_s = data.split("|")
        rid, minutes = int(rid_s), int(minutes_s)
        reminders = load_json(REM_FILE, [])
        item = next((r for r in reminders if r["id"] == rid), None)
        if item:
            item["time"] = (now_local() + timedelta(minutes=minutes)).isoformat()
            save_json(REM_FILE, reminders)
        await query.edit_message_text(f"⏳ Отложено на {minutes} мин.")
        return
    if data.startswith("done|"):
        rid = int(data.split("|", 1)[1])
        reminders = load_json(REM_FILE, [])
        save_json(REM_FILE, [r for r in reminders if r["id"] != rid])
        await query.edit_message_text("✅ Готово, напоминание закрыто.")
        return
    if data.startswith("clear|"):
        reminders = load_json(REM_FILE, [])
        save_json(REM_FILE, [r for r in reminders if r["chat_id"] != query.message.chat_id])
        await query.edit_message_text("🗑 Все записи удалены.")


def next_time(item: dict, repeat: str) -> datetime:
    dt = datetime.fromisoformat(item["time"])
    if repeat == "daily":
        return dt + timedelta(days=1)
    if repeat == "weekly":
        return dt + timedelta(weeks=1)
    if repeat == "monthly":
        return add_month(dt)
    return dt


async def check_due(context):
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
    for r in due:
        repeat = r.get("repeat", "")
        if repeat:
            r["time"] = next_time(r, repeat).isoformat()
            remaining.append(r)
        markup = InlineKeyboardMarkup([[
            InlineKeyboardButton("⏳ +5 мин", callback_data=f"snooze|{r['id']}|5"),
            InlineKeyboardButton("⏳ +1 час", callback_data=f"snooze|{r['id']}|60"),
            InlineKeyboardButton("✅ Готово", callback_data=f"done|{r['id']}"),
        ]])
        try:
            await context.bot.send_message(
                r["chat_id"], f'⏰ <b>Напоминание!</b>\n\n{html.escape(r["text"])}',
                parse_mode="HTML", reply_markup=markup)
        except Exception as e:
            log.error("Не отправил напоминание: %s", e)
    save_json(REM_FILE, remaining)


async def cmd_repeat(update, context):
    if not is_owner(update):
        return
    args = context.args or []
    if len(args) < 2 or args[1] not in ("daily", "weekly", "monthly", "off"):
        await update.message.reply_text("Формат: /repeat 3 daily|weekly|monthly|off")
        return
    rid = int(args[0]) if args[0].isdigit() else -1
    reminders = load_json(REM_FILE, [])
    item = next((r for r in reminders if r["id"] == rid), None)
    if not item:
        await update.message.reply_text("Запись не найдена.")
        return
    rule = "" if args[1] == "off" else args[1]
    item["repeat"] = rule
    if rule:
        dt = next_time(item, rule)
        if dt <= now_local():
            dt = next_time({"time": now_local().isoformat()}, rule)
        item["time"] = dt.isoformat()
    save_json(REM_FILE, reminders)
    await update.message.reply_text(f"🔁 Запись #{rid}: повтор {rule or 'выключен'}")


async def cmd_priority(update, context):
    if not is_owner(update):
        return
    args = context.args or []
    if len(args) < 2 or args[1] not in ("high", "low", "none"):
        await update.message.reply_text("Формат: /priority 3 high|low|none")
        return
    rid = int(args[0]) if args[0].isdigit() else -1
    reminders = load_json(REM_FILE, [])
    item = next((r for r in reminders if r["id"] == rid), None)
    if not item:
        await update.message.reply_text("Запись не найдена.")
        return
    item["priority"] = "" if args[1] == "none" else args[1]
    save_json(REM_FILE, reminders)
    await update.message.reply_text(f"✅ Приоритет #{rid}: {args[1]}")


async def cmd_tag(update, context):
    if not is_owner(update):
        return
    args = context.args or []
    if len(args) < 2:
        await update.message.reply_text("Формат: /tag 3 работа")
        return
    rid = int(args[0]) if args[0].isdigit() else -1
    tag = args[1].lstrip("#").lower()
    reminders = load_json(REM_FILE, [])
    item = next((r for r in reminders if r["id"] == rid), None)
    if not item:
        await update.message.reply_text("Запись не найдена.")
        return
    tags = item.setdefault("tags", [])
    if tag not in tags:
        tags.append(tag)
    save_json(REM_FILE, reminders)
    await update.message.reply_text(f"🏷 #{rid}: #{tag}")


async def cmd_tagged(update, context):
    if not is_owner(update):
        return
    args = context.args or []
    if not args:
        await update.message.reply_text("Формат: /tagged работа")
        return
    tag = args[0].lstrip("#").lower()
    found = [r for r in own_reminders(update.effective_chat.id) if tag in r.get("tags", [])]
    if not found:
        await update.message.reply_text("По этому тегу ничего нет.")
        return
    lines = [f'#{r["id"]} · {mark(r)}{fmt_dt(datetime.fromisoformat(r["time"]))} — {html.escape(r["text"])}'
             for r in found[:15]]
    await update.message.reply_text(f"🏷 <b>#{html.escape(tag)}</b>\n\n" + "\n".join(lines), parse_mode="HTML")


def main():
    app = Application.builder().token(TOKEN).build()
    for cmd, handler in [
        ("start", cmd_start), ("help", cmd_help), ("records", cmd_records),
        ("list", cmd_list), ("next", cmd_next), ("find", cmd_find),
        ("edit", cmd_edit), ("delete", cmd_delete), ("done", cmd_done),
        ("snooze", cmd_snooze), ("clear", cmd_clear), ("tz", cmd_tz),
        ("count", cmd_count), ("repeat", cmd_repeat),
        ("priority", cmd_priority), ("tag", cmd_tag),
        ("tagged", cmd_tagged),
    ]:
        app.add_handler(CommandHandler(cmd, handler))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message))
    app.job_queue.run_repeating(check_due, interval=CHECK_SEC, first=5)
    app.run_polling(bootstrap_retries=10, timeout=30)


if __name__ == "__main__":
    main()
