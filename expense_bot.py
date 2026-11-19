"""Telegram-бот учёта расходов и доходов.

Команды: /start /help /spend /income /balance /today /month
         /categories /budget /delete /export /stats
"""
import csv
import html
import io
import json
import logging
import os
import re
from datetime import datetime, time, timedelta, timezone
from pathlib import Path

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (Application, CallbackQueryHandler, CommandHandler,
                          ContextTypes, MessageHandler, filters)

TOKEN = os.environ["EXPENSE_BOT_TOKEN"]
DATA_DIR = Path(os.environ.get("DATA_DIR", "expense_data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
EXPENSES_FILE = DATA_DIR / "expenses.json"
OWNER_FILE = DATA_DIR / "owner.json"
SETTINGS_FILE = DATA_DIR / "settings.json"
TZ_OFFSET = int(os.environ.get("TZ_OFFSET", "7"))
TZ = timezone(timedelta(hours=TZ_OFFSET))

CATEGORIES = ["еда", "транспорт", "развлечения", "дом", "здоровье", "покупки", "другое"]
EMOJI = {"еда": "🍔", "транспорт": "🚌", "развлечения": "🎬", "дом": "🏠",
         "здоровье": "💊", "покупки": "🛍", "другое": "📦", "доход": "💵"}

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("expensebot")


def load_json(path, default):
    if path.exists():
        try:
            return json.loads(path.read_text("utf-8"))
        except (ValueError, json.JSONDecodeError):
            pass
    return json.loads(json.dumps(default))


def save_json(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), "utf-8")


def get_owner() -> int:
    return int(load_json(OWNER_FILE, {}).get("owner", os.environ.get("CHAT_ID", "0") or 0))


def is_owner(update) -> bool:
    owner = get_owner()
    return not owner or update.effective_chat.id == owner


def today() -> str:
    return datetime.now(TZ).strftime("%Y-%m-%d")


def month() -> str:
    return datetime.now(TZ).strftime("%Y-%m")


def fmt_money(value: int) -> str:
    return f"{value:,}".replace(",", " ")


async def cmd_start(update, context):
    save_json(OWNER_FILE, {"owner": update.effective_chat.id})
    await update.message.reply_text(
        "💰 Бот учёта расходов и доходов готов.\n\n"
        "/spend 500 кофе — расход\n"
        "/income 50000 зарплата — доход\n"
        "/balance — остаток за месяц\n"
        "/today — за сегодня\n"
        "/month — за месяц\n"
        "/budget 30000 — лимит расходов\n"
        "/help — все команды"
    )


async def cmd_help(update, context):
    if not is_owner(update):
        return
    await update.message.reply_text(
        "📋 <b>Команды</b>\n\n"
        "/spend 500 кофе — расход (сумма, категория, комментарий)\n"
        "/income 50000 зарплата — доход\n"
        "/add — добавить расход через кнопки\n"
        "/balance — доходы минус расходы за месяц\n"
        "/today — записи за сегодня\n"
        "/month — итоги за месяц по категориям\n"
        "/stats — среднее в день\n"
        "/categories — список категорий\n"
        "/budget 30000 — месячный лимит расходов (0 = сбросить)\n"
        "/delete 3 — удалить запись из /today\n"
        "/undo — удалить последнюю запись\n"
        "/tagged тег — сумма по тегу\n"
        "/compare — этот месяц против прошлого\n"
        "/export — выгрузить CSV файл\n\n"
        "Можно писать просто текстом: «сколько на еду в этом месяце?»",
        parse_mode="HTML")


def add_expense(chat_id: int, amount: int, category: str, comment: str,
                kind: str = "expense", tags: list[str] | None = None) -> int:
    expenses = load_json(EXPENSES_FILE, [])
    rid = max([e["id"] for e in expenses], default=0) + 1
    expenses.append({
        "id": rid, "chat_id": chat_id, "amount": amount,
        "category": category, "comment": comment, "kind": kind,
        "tags": tags or [],
        "date": datetime.now(TZ).isoformat(),
    })
    save_json(EXPENSES_FILE, expenses)
    return rid


def extract_tags(text: str) -> tuple[str, list[str]]:
    tags = re.findall(r"#([А-Яа-яA-Za-z0-9_-]+)", text)
    clean_text = re.sub(r"#[А-Яа-яA-Za-z0-9_-]+", "", text).strip()
    return clean_text, tags


async def cmd_spend(update, context):
    if not is_owner(update):
        return
    args = context.args or []
    if not args:
        await update.message.reply_text("Формат: /spend 500 кофе")
        return
    try:
        amount = int(args[0].replace(" ", "").replace("₽", ""))
    except ValueError:
        await update.message.reply_text("Первым аргументом должна быть сумма числом.")
        return
    if amount <= 0:
        await update.message.reply_text("Сумма должна быть положительной.")
        return
    rest, tags = extract_tags(" ".join(args[1:]).strip())
    category = "другое"
    comment = rest
    low = rest.lower()
    for c in CATEGORIES:
        if c in low:
            category = c
            comment = rest.replace(c, "").strip()
            break
    rid = add_expense(update.effective_chat.id, amount, category, comment, tags=tags)
    text = (f"✅ Добавлено #{rid}: {fmt_money(amount)} ₽ · {EMOJI[category]} {category}"
            + (f" · {html.escape(comment)}" if comment else "")
            + (" · " + " ".join("#" + html.escape(t) for t in tags) if tags else ""))
    await update.message.reply_text(text, parse_mode="HTML")

    # Алерт по бюджету
    s = load_json(SETTINGS_FILE, {})
    budget = s.get("budget", 0)
    if budget:
        spent = sum(e["amount"] for e in list_for(month()) if e.get("kind") != "income")
        if spent >= budget:
            await update.message.reply_text(
                f"🚨 <b>Лимит превышен!</b> Расходы за месяц: {fmt_money(spent)} ₽ при лимите {fmt_money(budget)} ₽",
                parse_mode="HTML")
        elif spent >= budget * 0.8:
            await update.message.reply_text(
                f"⚠️ Израсходовано {int(spent / budget * 100)}% лимита: {fmt_money(spent)} ₽ из {fmt_money(budget)} ₽",
                parse_mode="HTML")


async def cmd_income(update, context):
    if not is_owner(update):
        return
    args = context.args or []
    if not args:
        await update.message.reply_text("Формат: /income 50000 зарплата")
        return
    try:
        amount = int(args[0].replace(" ", "").replace("₽", ""))
    except ValueError:
        await update.message.reply_text("Первым аргументом должна быть сумма числом.")
        return
    if amount <= 0:
        await update.message.reply_text("Сумма должна быть положительной.")
        return
    source = " ".join(args[1:]).strip() or "доход"
    rid = add_expense(update.effective_chat.id, amount, "доход", source, kind="income")
    await update.message.reply_text(
        f"✅ Доход #{rid}: <b>+{fmt_money(amount)} ₽</b> · {html.escape(source)}",
        parse_mode="HTML")


async def cmd_balance(update, context):
    if not is_owner(update):
        return
    items = list_for(month())
    income = sum(e["amount"] for e in items if e.get("kind") == "income")
    expense = sum(e["amount"] for e in items if e.get("kind") != "income")
    await update.message.reply_text(
        f"💰 <b>Баланс за {month()}</b>\n\n"
        f"💵 Доходы: <b>{fmt_money(income)} ₽</b>\n"
        f"💸 Расходы: <b>{fmt_money(expense)} ₽</b>\n"
        f"🧮 Остаток: <b>{fmt_money(income - expense)} ₽</b>",
        parse_mode="HTML")


async def cmd_add(update, context):
    if not is_owner(update):
        return
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton(f"{EMOJI[c]} {c}", callback_data=f"cat|{c}") for c in CATEGORIES[:3]],
        [InlineKeyboardButton(f"{EMOJI[c]} {c}", callback_data=f"cat|{c}") for c in CATEGORIES[3:6]],
        [InlineKeyboardButton(f"{EMOJI['другое']} другое", callback_data="cat|другое")],
    ])
    await update.message.reply_text("Выберите категорию:", reply_markup=keyboard)


async def on_button(update, context):
    query = update.callback_query
    await query.answer()
    data = query.data or ""
    if data.startswith("cat|"):
        category = data.split("|", 1)[1]
        context.user_data["pending_category"] = category
        await query.edit_message_text(f"Категория: {EMOJI[category]} {category}\nВведите сумму числом.")


async def on_message(update, context):
    if not is_owner(update):
        return
    category = (context.user_data or {}).get("pending_category")
    if category:
        text = (update.message.text or "").strip()
        try:
            amount = int(text.replace(" ", "").replace("₽", ""))
        except ValueError:
            await update.message.reply_text("Нужно число, например 500")
            return
        context.user_data.pop("pending_category", None)
        rid = add_expense(update.effective_chat.id, amount, category, "")
        await update.message.reply_text(f"✅ Добавлено #{rid}: {fmt_money(amount)} ₽ · {category}")
        return
    if await natural_query(update, context):
        return
    await update.message.reply_text("Добавить расход: /spend 500 кофе · /add · /income 50000")


def list_for(period: str) -> list[dict]:
    expenses = [e for e in load_json(EXPENSES_FILE, []) if e["date"][:len(period)] == period]
    return expenses


async def cmd_today(update, context):
    if not is_owner(update):
        return
    items = [e for e in list_for(today()) if e["date"][:10] == today()]
    if not items:
        await update.message.reply_text("Сегодня расходов нет.")
        return
    lines = []
    for e in items[-15:]:
        sign = "+" if e.get("kind") == "income" else "−"
        lines.append(f'#{e["id"]} · {sign}{fmt_money(e["amount"])} ₽ · {EMOJI.get(e["category"], "📦")} {e["category"]}'
                     + (f" · {html.escape(e['comment'])}" if e.get("comment") else ""))
    income = sum(e["amount"] for e in items if e.get("kind") == "income")
    outcome = sum(e["amount"] for e in items if e.get("kind") != "income")
    await update.message.reply_text(
        "📅 <b>Сегодня</b>\n\n" + "\n".join(lines)
        + f"\n\nДоходы: <b>+{fmt_money(income)} ₽</b> · Расходы: <b>−{fmt_money(outcome)} ₽</b>",
        parse_mode="HTML")


async def cmd_month(update, context):
    if not is_owner(update):
        return
    items = list_for(month())
    if not items:
        await update.message.reply_text("В этом месяце записей нет.")
        return
    totals = {}
    for e in items:
        if e.get("kind") == "income":
            continue
        totals[e["category"]] = totals.get(e["category"], 0) + e["amount"]
    lines = [f"{EMOJI.get(c, '📦')} {c}: <b>{fmt_money(v)} ₽</b>"
             for c, v in sorted(totals.items(), key=lambda x: -x[1])]
    total = sum(totals.values())
    income = sum(e["amount"] for e in items if e.get("kind") == "income")
    budget = load_json(SETTINGS_FILE, {}).get("budget", 0)
    text = f"📆 <b>Месяц {month()}</b>\n\n"
    if lines:
        text += "\n".join(lines) + f"\n\n<b>Расходы: {fmt_money(total)} ₽</b>\n"
    if income:
        text += f"<b>Доходы: {fmt_money(income)} ₽</b>\n<b>Остаток: {fmt_money(income - total)} ₽</b>\n"
    if budget:
        text += f"\nЛимит: {fmt_money(budget)} ₽ · осталось {fmt_money(max(budget - total, 0))} ₽"
    await update.message.reply_text(text, parse_mode="HTML")


async def cmd_stats(update, context):
    if not is_owner(update):
        return
    items = list_for(month())
    if not items:
        await update.message.reply_text("Нет данных за месяц.")
        return
    days = len({e["date"][:10] for e in items})
    income = sum(e["amount"] for e in items if e.get("kind") == "income")
    expense = sum(e["amount"] for e in items if e.get("kind") != "income")
    await update.message.reply_text(
        f"📊 <b>Статистика за {month()}</b>\n\n"
        f"Записей: {len(items)}\n"
        f"Дней с записями: {days}\n"
        f"Доходы: {fmt_money(income)} ₽\n"
        f"Расходы: {fmt_money(expense)} ₽\n"
        f"Средние расходы в день: {fmt_money(expense // max(days, 1))} ₽", parse_mode="HTML")


async def cmd_categories(update, context):
    if not is_owner(update):
        return
    await update.message.reply_text("Категории:\n" + "\n".join(f"{EMOJI[c]} {c}" for c in CATEGORIES))


async def cmd_budget(update, context):
    if not is_owner(update):
        return
    args = context.args or []
    if not args:
        settings = load_json(SETTINGS_FILE, {})
        await update.message.reply_text(f"Лимит: {fmt_money(settings.get('budget', 0))} ₽")
        return
    try:
        value = int(args[0].replace(" ", "").replace("₽", ""))
    except ValueError:
        await update.message.reply_text("Укажите число.")
        return
    settings = load_json(SETTINGS_FILE, {})
    settings["budget"] = value
    save_json(SETTINGS_FILE, settings)
    await update.message.reply_text(f"✅ Лимит на месяц: {fmt_money(value)} ₽" if value else "✅ Лимит сброшен")


async def cmd_delete(update, context):
    if not is_owner(update):
        return
    args = context.args or []
    if not args or not args[0].isdigit():
        await update.message.reply_text("Формат: /delete 3 (номер из /today)")
        return
    rid = int(args[0])
    expenses = load_json(EXPENSES_FILE, [])
    new = [e for e in expenses if e["id"] != rid]
    if len(new) == len(expenses):
        await update.message.reply_text("Запись не найдена.")
        return
    save_json(EXPENSES_FILE, new)
    await update.message.reply_text(f"🗑 Запись #{rid} удалена.")


async def cmd_undo(update, context):
    if not is_owner(update):
        return
    expenses = load_json(EXPENSES_FILE, [])
    mine = [e for e in expenses if e["chat_id"] == update.effective_chat.id]
    if not mine:
        await update.message.reply_text("Нечего отменять.")
        return
    last = max(mine, key=lambda e: e["id"])
    save_json(EXPENSES_FILE, [e for e in expenses if e["id"] != last["id"]])
    await update.message.reply_text(f"↩️ Отменено: #{last['id']} · {fmt_money(last['amount'])} ₽")


async def cmd_tagged(update, context):
    if not is_owner(update):
        return
    args = context.args or []
    if not args:
        await update.message.reply_text("Формат: /tagged тег (без #)")
        return
    tag = args[0].lstrip("#").lower()
    items = [e for e in load_json(EXPENSES_FILE, []) if tag in [t.lower() for t in e.get("tags", [])]]
    if not items:
        await update.message.reply_text("По этому тегу ничего нет.")
        return
    total = sum(e["amount"] for e in items)
    lines = [f'#{e["id"]} · {fmt_money(e["amount"])} ₽ · {html.escape(e["comment"])}' for e in items[-10:]]
    await update.message.reply_text(f"🏷 <b>#{html.escape(tag)}</b> · {len(items)} записей\n\n"
                                    + "\n".join(lines) + f"\n\n<b>Итого: {fmt_money(total)} ₽</b>", parse_mode="HTML")


async def cmd_compare(update, context):
    if not is_owner(update):
        return
    def total_for(period: str) -> int:
        return sum(e["amount"] for e in list_for(period) if e.get("kind") != "income")
    now = datetime.now(TZ)
    prev = (now.replace(day=1) - timedelta(days=1)).strftime("%Y-%m")
    cur, old = total_for(month()), total_for(prev)
    diff = cur - old
    sign = "+" if diff > 0 else ""
    percent = int(diff / old * 100) if old else 0
    await update.message.reply_text(
        f"📊 <b>Сравнение месяцев</b>\n\n"
        f"{prev}: {fmt_money(old)} ₽\n"
        f"{month()}: {fmt_money(cur)} ₽\n"
        f"Изменение: {sign}{fmt_money(diff)} ₽ ({sign}{percent}%)", parse_mode="HTML")


async def natural_query(update, context):
    text = (update.message.text or "").lower().strip()
    if not any(w in text for w in ("сколько", "итог", "потратил", "расход", "доход")):
        return False
    category = next((c for c in CATEGORIES if c in text), None)
    period = month() if ("месяц" in text or "месяце" in text) else today()
    items = [e for e in list_for(period) if e["date"][:10] == today()] if period == today() else list_for(period)
    if category:
        items = [e for e in items if e["category"] == category]
    income = sum(e["amount"] for e in items if e.get("kind") == "income")
    expense = sum(e["amount"] for e in items if e.get("kind") != "income")
    label = category or "все категории"
    await update.message.reply_text(
        f"💬 {label} · {period}:\n💸 Расходы {fmt_money(expense)} ₽ · 💵 Доходы {fmt_money(income)} ₽")
    return True


async def evening_report(context):
    items = [e for e in load_json(EXPENSES_FILE, []) if e["date"][:10] == today()]
    expense = sum(e["amount"] for e in items if e.get("kind") != "income")
    month_total = sum(e["amount"] for e in list_for(month()) if e.get("kind") != "income")
    await context.bot.send_message(
        get_owner(),
        f"🌙 <b>Итоги дня</b>\n\nСегодня: {fmt_money(expense)} ₽\n"
        f"За месяц: {fmt_money(month_total)} ₽",
        parse_mode="HTML")


async def weekly_report(context):
    now = datetime.now(TZ)
    start = (now - timedelta(days=7)).strftime("%Y-%m-%d")
    items = [e for e in load_json(EXPENSES_FILE, []) if start <= e["date"][:10] <= today()]
    totals = {}
    for e in items:
        if e.get("kind") != "income":
            totals[e["category"]] = totals.get(e["category"], 0) + e["amount"]
    if not totals:
        return
    lines = [f"{EMOJI.get(c, '📦')} {c}: {fmt_money(v)} ₽" for c, v in sorted(totals.items(), key=lambda x: -x[1])]
    await context.bot.send_message(
        get_owner(),
        "📊 <b>Итоги недели</b>\n\n" + "\n".join(lines) + f"\n\nВсего: {fmt_money(sum(totals.values()))} ₽",
        parse_mode="HTML")


async def cmd_export(update, context):
    if not is_owner(update):
        return
    items = load_json(EXPENSES_FILE, [])
    if not items:
        await update.message.reply_text("Нечего выгружать.")
        return
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["id", "date", "kind", "amount", "category", "comment"])
    for e in items:
        writer.writerow([e["id"], e["date"], e.get("kind", "expense"),
                         e["amount"], e["category"], e.get("comment", "")])
    data = buf.getvalue().encode("utf-8-sig")
    await update.message.reply_document(document=io.BytesIO(data), filename=f"expenses_{month()}.csv")


def main():
    app = Application.builder().token(TOKEN).build()
    for cmd, handler in [
        ("start", cmd_start), ("help", cmd_help), ("spend", cmd_spend),
        ("income", cmd_income), ("balance", cmd_balance),
        ("add", cmd_add), ("today", cmd_today), ("month", cmd_month),
        ("stats", cmd_stats), ("categories", cmd_categories),
        ("budget", cmd_budget), ("delete", cmd_delete), ("export", cmd_export),
        ("undo", cmd_undo), ("tagged", cmd_tagged), ("compare", cmd_compare),
    ]:
        app.add_handler(CommandHandler(cmd, handler))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message))
    app.job_queue.run_daily(evening_report, time=time(hour=22, minute=0, tzinfo=TZ))
    app.job_queue.run_daily(weekly_report, time=time(hour=20, minute=0, tzinfo=TZ), days=(6,))
    app.run_polling(bootstrap_retries=10, timeout=30)


if __name__ == "__main__":
    main()
