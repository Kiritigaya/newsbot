"""Telegram-бот мониторинга GitHub Actions.

Команды: /start /help /status /check /watch /interval /history /silence
"""
import asyncio
import html
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

import requests
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

TOKEN = os.environ["MONITOR_BOT_TOKEN"]
GH_PAT = os.environ.get("GH_PAT", "")
REPO = os.environ.get("MONITOR_REPO", "Kiritigaya/newsbot")
WORKFLOW = os.environ.get("MONITOR_WORKFLOW", "news.yml")
DATA_DIR = Path(os.environ.get("DATA_DIR", "monitor_data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
OWNER_FILE = DATA_DIR / "owner.json"
SETTINGS_FILE = DATA_DIR / "settings.json"
STATE_FILE = DATA_DIR / "state.json"
CHECK_MIN = int(os.environ.get("MONITOR_CHECK_MIN", "15"))
STALE_MIN = int(os.environ.get("MONITOR_STALE_MIN", "90"))
API = f"https://api.github.com/repos/{REPO}/actions/workflows/{WORKFLOW}/runs"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("monitorbot")

DEFAULT_SETTINGS = {"watch": True, "interval_min": CHECK_MIN, "silent": False}

BOTS = {
    name: os.environ.get(key, "")
    for name, key in {
        "новости+погода": "BOT_TOKEN",
        "напоминания": "REMINDER_BOT_TOKEN",
        "скидки": "DEALS_BOT_TOKEN",
        "деньги": "EXPENSE_BOT_TOKEN",
        "мониторинг": "MONITOR_BOT_TOKEN",
    }.items()
    if os.environ.get(key, "")
}


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
    return int(load_json(OWNER_FILE, {}).get("owner", os.environ.get("CHAT_ID", "0") or 0))


def is_owner(update) -> bool:
    owner = get_owner()
    return not owner or update.effective_chat.id == owner


def fetch_runs(per_page: int = 5) -> list[dict] | None:
    if not GH_PAT:
        return None
    try:
        r = requests.get(API, params={"per_page": per_page}, timeout=20,
                         headers={"Authorization": f"token {GH_PAT}",
                                  "User-Agent": "monitor-bot",
                                  "Accept": "application/vnd.github+json"})
        r.raise_for_status()
        return r.json().get("workflow_runs", [])
    except Exception as e:
        log.error("GitHub API: %s", e)
        return None


def run_line(run: dict) -> str:
    conclusion = run.get("conclusion") or run.get("status", "…")
    emoji = {"success": "✅", "failure": "❌", "cancelled": "⏹", "in_progress": "🔄"}.get(conclusion, "⚪")
    created = (run.get("created_at") or "")[:19].replace("T", " ")
    return f'{emoji} #{run["id"]} · {conclusion} · {created}'


def run_url(run: dict) -> str:
    return run.get("html_url", "")


async def cmd_start(update, context):
    save_json(OWNER_FILE, {"owner": update.effective_chat.id})
    await update.message.reply_text(
        "🔍 GitHub-мониторинг готов.\n"
        f"Слежу за `{html.escape(REPO)}` / `{html.escape(WORKFLOW)}`.\n\n"
        "/status — состояние\n/check — последние запуски\n"
        "/watch — пауза/возобновление\n/interval 15 — частота\n"
        "/history — история тревог\n/help — все команды",
        parse_mode="HTML")


async def cmd_help(update, context):
    if not is_owner(update):
        return
    await update.message.reply_text(
        "📋 <b>Команды</b>\n\n"
        "/status — настройки и последний запуск\n"
        "/check — последние 5 запусков со ссылками\n"
        "/watch — включить/выключить слежение\n"
        "/interval 15 — интервал проверки в минутах\n"
        "/silence — тихий режим\n"
        "/history — последние тревоги\n"
        "/ping — проверить все боты и задержку\n"
        "/uptime — статистика доступности",
        parse_mode="HTML")


async def cmd_status(update, context):
    if not is_owner(update):
        return
    s = settings()
    state = load_json(STATE_FILE, {})
    runs = await asyncio.to_thread(fetch_runs, 1)
    last = run_line(runs[0]) if runs else "недоступно"
    await update.message.reply_text(
        f"🔍 <b>Мониторинг</b>\n\nРепозиторий: {html.escape(REPO)}\n"
        f"Workflow: {html.escape(WORKFLOW)}\n"
        f"Слежение: {'вкл' if s['watch'] else 'пауза'}\n"
        f"Интервал: {s['interval_min']} мин.\n"
        f"Уведомления: {'вкл' if not s['silent'] else 'тихий режим'}\n"
        f"Последний запуск: {html.escape(last)}\n"
        f"Последняя тревога: {html.escape(str(state.get('last_alert', 'нет'))[:40])}",
        parse_mode="HTML")


async def cmd_check(update, context):
    if not is_owner(update):
        return
    runs = await asyncio.to_thread(fetch_runs, 5)
    if not runs:
        await update.message.reply_text("GitHub API недоступен или токен не задан.")
        return
    lines = []
    for r in runs:
        line = run_line(r)
        url = run_url(r)
        lines.append(f'{line}\n<a href="{html.escape(url, quote=True)}">логи</a>' if url else line)
    await update.message.reply_text("🏃 <b>Последние запуски</b>\n\n" + "\n\n".join(lines),
                                    parse_mode="HTML", disable_web_page_preview=True)


async def cmd_watch(update, context):
    if not is_owner(update):
        return
    s = settings()
    s["watch"] = not s["watch"]
    save_json(SETTINGS_FILE, s)
    await update.message.reply_text(f"Слежение: {'включено' if s['watch'] else 'на паузе'}")


async def cmd_interval(update, context):
    if not is_owner(update):
        return
    args = context.args or []
    if not args or not args[0].isdigit():
        await update.message.reply_text("Формат: /interval 15 (5–1440)")
        return
    value = int(args[0])
    if not 5 <= value <= 1440:
        await update.message.reply_text("Укажите от 5 до 1440 минут.")
        return
    s = settings()
    s["interval_min"] = value
    save_json(SETTINGS_FILE, s)
    job = context.bot_data.get("monitor_job")
    if job:
        job.schedule_interval(value * 60)
    await update.message.reply_text(f"✅ Интервал: {value} мин.")


async def cmd_silence(update, context):
    if not is_owner(update):
        return
    s = settings()
    s["silent"] = not s["silent"]
    save_json(SETTINGS_FILE, s)
    await update.message.reply_text(f"Уведомления: {'включены' if not s['silent'] else 'тихий режим'}")


async def cmd_history(update, context):
    if not is_owner(update):
        return
    history = load_json(STATE_FILE, {}).get("history", [])
    if not history:
        await update.message.reply_text("Тревог пока не было.")
        return
    lines = [f'{h.get("time", "")[:19]} — {h.get("text", "")[:80]}' for h in history[-10:]]
    await update.message.reply_text("📜 <b>История тревог</b>\n\n" + "\n".join(lines), parse_mode="HTML")


def ping_bot(name: str, token: str) -> tuple[str, int | None]:
    import time
    start = time.monotonic()
    try:
        r = requests.get(f"https://api.telegram.org/bot{token}/getMe", timeout=10)
        r.raise_for_status()
        return name, int((time.monotonic() - start) * 1000)
    except Exception:
        return name, None


async def cmd_ping(update, context):
    if not is_owner(update):
        return
    results = [await asyncio.to_thread(ping_bot, n, t) for n, t in BOTS.items()]
    lines = [f'{"🟢" if ms is not None else "🔴"} {n}: {ms if ms is not None else "нет ответа"} мс'
             for n, ms in results]
    await update.message.reply_text("🏓 <b>Проверка ботов</b>\n\n" + "\n".join(lines), parse_mode="HTML")


async def cmd_uptime(update, context):
    if not is_owner(update):
        return
    state = load_json(STATE_FILE, {})
    stats = state.get("bot_checks", {})
    if not stats:
        await update.message.reply_text("Статистики пока нет — подождите несколько проверок.")
        return
    lines = []
    for name, data in stats.items():
        total = data.get("ok", 0) + data.get("fail", 0)
        if total:
            lines.append(f"{name}: {int(data['ok'] / total * 100)}% успешных ({total} проверок)")
    await update.message.reply_text("📈 <b>Аптайм ботов</b>\n\n" + "\n".join(lines), parse_mode="HTML")


async def alert(context, text: str, url: str = ""):
    if settings()["silent"]:
        return
    full = text + (f'\n<a href="{html.escape(url, quote=True)}">открыть в GitHub</a>' if url else "")
    try:
        await context.bot.send_message(get_owner(), full, parse_mode="HTML", disable_web_page_preview=True)
    except Exception as e:
        log.error("Не отправил тревогу: %s", e)
    state = load_json(STATE_FILE, {})
    state["last_alert"] = datetime.now(timezone.utc).isoformat()
    state.setdefault("history", []).append({"time": state["last_alert"], "text": text})
    state["history"] = state["history"][-50:]
    save_json(STATE_FILE, state)


async def check(context):
    s = settings()
    if not s["watch"]:
        return
    runs = await asyncio.to_thread(fetch_runs, 5)
    if not runs:
        return
    state = load_json(STATE_FILE, {})
    known = state.get("known_runs", [])
    for run in runs:
        rid = run["id"]
        if rid in known:
            continue
        known.append(rid)
        if run.get("conclusion") == "failure":
            await alert(context, f'❌ <b>Workflow упал!</b>\n{run_line(run)}', run_url(run))

    active = any(r.get("status") == "in_progress" for r in runs)
    latest = runs[0]
    created = datetime.fromisoformat(latest["created_at"].replace("Z", "+00:00"))
    age = (datetime.now(timezone.utc) - created).total_seconds() / 60
    if not active and age > STALE_MIN and not s["silent"]:
        last_stale = state.get("last_stale", "")
        if last_stale[:10] != datetime.now(timezone.utc).strftime("%Y-%m-%d"):
            await alert(context,
                        f'⚠️ <b>Боты могут быть остановлены</b>\n'
                        f'Последний запуск был {int(age)} мин назад, активных нет.',
                        run_url(latest))
            state["last_stale"] = datetime.now(timezone.utc).isoformat()

    # Проверка живости ботов
    bot_checks = state.setdefault("bot_checks", {})
    for name, token in BOTS.items():
        _, ms = await asyncio.to_thread(ping_bot, name, token)
        entry = bot_checks.setdefault(name, {"ok": 0, "fail": 0, "streak": 0})
        if ms is None:
            entry["fail"] += 1
            entry["streak"] += 1
            if entry["streak"] == 2:
                await alert(context, f'🔴 <b>Бот не отвечает:</b> {name}\nДве проверки подряд без ответа.')
        else:
            entry["ok"] += 1
            entry["streak"] = 0

    state["known_runs"] = known[-200:]
    state["last_check"] = datetime.now(timezone.utc).isoformat()
    save_json(STATE_FILE, state)


def main():
    app = Application.builder().token(TOKEN).build()
    for cmd, handler in [
        ("start", cmd_start), ("help", cmd_help), ("status", cmd_status),
        ("check", cmd_check), ("watch", cmd_watch),
        ("interval", cmd_interval), ("silence", cmd_silence),
        ("history", cmd_history), ("ping", cmd_ping),
        ("uptime", cmd_uptime),
    ]:
        app.add_handler(CommandHandler(cmd, handler))
    job = app.job_queue.run_repeating(check, interval=settings()["interval_min"] * 60, first=10)
    app.bot_data["monitor_job"] = job
    app.run_polling(bootstrap_retries=10, timeout=30)


if __name__ == "__main__":
    main()
