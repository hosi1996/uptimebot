import asyncio
import html
import logging
import os
import re
import time
from pathlib import Path

from dotenv import load_dotenv
from telegram import BotCommand, Update
from telegram import InlineKeyboardButton as Btn
from telegram import InlineKeyboardMarkup as Markup
from telegram.constants import ParseMode
from telegram.error import BadRequest
from telegram.ext import (
    Application,
    ApplicationHandlerStop,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    TypeHandler,
    filters,
)

from . import checks
from .db import DB

ROOT = Path(__file__).resolve().parent.parent
INTERVALS = [1, 2, 5, 10, 15, 30, 60]
DEFAULT_INTERVAL = 5
LOCATIONS = {"out": "🌍 خارج", "iran": "🇮🇷 ایران", "both": "🌍🇮🇷 هر دو"}
NEXT_LOCATION = {"out": "iran", "iran": "both", "both": "out"}
TICK_SECONDS = 15
MAX_PARALLEL = 10
DOMAIN_RE = re.compile(r"^(?=.{4,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+(xn--[a-z0-9-]+|[a-z]{2,63})$")

log = logging.getLogger("uptimebot")


def esc(text) -> str:
    return html.escape(str(text))


def parse_domain(text: str):
    t = re.sub(r"^[a-z]+://", "", text.strip().lower())
    t = re.split(r"[/?#:]", t, maxsplit=1)[0].rstrip(".")
    try:
        t = t.encode("idna").decode()
    except UnicodeError:
        return None
    return t if DOMAIN_RE.match(t) else None


def site_checks(site) -> list:
    return [n for n in site["checks"].split(",") if n in checks.CHECKS]


# ---------- views: each returns (text, markup) ----------

def menu_view():
    return "🤖 <b>ربات مانیتورینگ دامنه</b>\nیک گزینه را انتخاب کنید:", Markup(
        [[Btn("➕ افزودن دامنه", callback_data="add")], [Btn("📋 دامنه‌ها", callback_data="list")]]
    )


def list_view(db, chat_id):
    rows = [
        [Btn(f"{'⏸' if not s['enabled'] else '🟢' if s['last_ok'] else '🔴'} {s['domain']} · {s['interval_min']}m",
             callback_data=f"s:{s['id']}")]
        for s in db.list(chat_id)
    ]
    rows.append([Btn("➕ افزودن دامنه", callback_data="add")])
    rows.append([Btn("🔙 منو", callback_data="menu")])
    return ("📋 <b>دامنه‌ها</b>" if len(rows) > 2 else "هنوز دامنه‌ای اضافه نکرده‌اید."), Markup(rows)


def site_view(site, iran, note=""):
    sid = site["id"]
    text = (
        f"{note}🌐 <b>{esc(site['domain'])}</b>\n"
        f"{'🟢 فعال' if site['enabled'] else '⏸ متوقف'}\n"
        f"⏱ هر {site['interval_min']} دقیقه\n"
        f"🔔 {'فقط مشکلات' if site['only_problems'] else 'همیشه گزارش'}\n"
        f"🔎 چک‌ها: {len(site_checks(site))} از {len(checks.CHECKS)}"
    )
    location_row = [[Btn(f"📍 محل چک: {LOCATIONS[site['location']]}", callback_data=f"loc:{sid}")]] if iran else []
    if iran:
        text += f"\n📍 محل چک: {LOCATIONS[site['location']]}"
    return text, Markup([
        [Btn("▶️ بررسی الان", callback_data=f"run:{sid}")],
        [Btn("⏱ بازه", callback_data=f"iv:{sid}"), Btn("🔎 چک‌ها", callback_data=f"ck:{sid}")],
        [Btn("▶️ فعال‌سازی" if not site["enabled"] else "⏸ توقف", callback_data=f"en:{sid}"),
         Btn("🔔 همیشه" if site["only_problems"] else "🔕 فقط مشکلات", callback_data=f"op:{sid}")],
        *location_row,
        [Btn("🗑 حذف", callback_data=f"del:{sid}")],
        [Btn("🔙 لیست", callback_data="list")],
    ])


def interval_view(site):
    sid = site["id"]
    buttons = [Btn(("✅ " if m == site["interval_min"] else "") + f"{m}m", callback_data=f"setiv:{sid}:{m}")
               for m in INTERVALS]
    return "⏱ هر چند دقیقه بررسی شود؟", Markup([buttons[:4], buttons[4:], [Btn("🔙 برگشت", callback_data=f"s:{sid}")]])


def checks_view(site):
    sid = site["id"]
    active = set(site_checks(site))
    rows = [[Btn(("✅ " if n in active else "⬜ ") + label, callback_data=f"tg:{sid}:{n}")]
            for n, label in checks.CHECKS.items()]
    rows.append([Btn("🔙 برگشت", callback_data=f"s:{sid}")])
    return f"🔎 چک‌های <b>{esc(site['domain'])}</b> را انتخاب کنید:", Markup(rows)


def delete_view(site):
    sid = site["id"]
    return f"❗ <b>{esc(site['domain'])}</b> حذف شود؟", Markup(
        [[Btn("✅ بله، حذف", callback_data=f"delok:{sid}"), Btn("❌ نه", callback_data=f"s:{sid}")]]
    )


def format_report(domain, rows, full=False) -> str:
    """rows: list of (label, Result)."""
    bad = [r for _, r in rows if r.ok is False]
    if bad:
        lines = [f"🔴 <b>{esc(domain)}</b> — {len(bad)} مشکل"]
    else:
        lines = [f"🟢 <b>{esc(domain)}</b> — همه چیز اوکیه ✅"]
    for label, r in rows:
        if full or r.ok is False:
            icon = "✅" if r.ok is True else "❌" if r.ok is False else "➖"
            lines.append(f"{icon} {label}: {esc(r.detail)}")
    if not full:
        lines.append(f"({sum(1 for _, r in rows if r.ok is True)} چک سالم)")
    return "\n".join(lines)


async def collect(site, iran):
    """Run the site's checks from the selected location(s); returns [(label, Result)]."""
    names = site_checks(site)
    loc = site["location"] if iran else "out"
    local = [n for n in names if loc != "iran" or n in checks.LOCATION_FREE]
    remote = [n for n in names if loc != "out" and n not in checks.LOCATION_FREE]
    tag_out = " 🌍" if loc == "both" else ""

    async def remote_rows():
        try:
            res = await checks.run_remote(*iran, site["domain"], remote)
        except Exception as e:
            return [("چک‌کننده ایران 🇮🇷", checks.Result(False, str(e) or type(e).__name__))]
        return [(f"{checks.CHECKS[n]} 🇮🇷", r) for n, r in res.items()]

    local_res, remote_res = await asyncio.gather(
        checks.run_checks(site["domain"], local) if local else asyncio.sleep(0, {}),
        remote_rows() if remote else asyncio.sleep(0, []),
    )
    rows = [(checks.CHECKS[n] + ("" if n in checks.LOCATION_FREE else tag_out), r) for n, r in local_res.items()]
    return rows + remote_res


async def render(q, view):
    text, markup = view
    try:
        await q.edit_message_text(text, reply_markup=markup, parse_mode=ParseMode.HTML)
    except BadRequest as e:
        if "not modified" not in str(e):
            raise


# ---------- handlers ----------

async def guard(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user and user.id in ctx.bot_data["admins"]:
        return
    if update.message and user:
        await update.message.reply_text(f"⛔ دسترسی ندارید. آیدی شما: {user.id}")
    raise ApplicationHandlerStop


async def cmd_menu(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data.pop("awaiting", None)
    text, markup = menu_view()
    await update.message.reply_text(text, reply_markup=markup, parse_mode=ParseMode.HTML)


async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if ctx.user_data.get("awaiting") != "domain":
        return await cmd_menu(update, ctx)
    db = ctx.bot_data["db"]
    chat_id = update.effective_chat.id
    domain = parse_domain(update.message.text)
    if not domain:
        await update.message.reply_text("❌ دامنه معتبر نیست. دوباره بفرستید (مثال: example.com)")
        return
    ctx.user_data.pop("awaiting")
    site_id = db.add(chat_id, domain, DEFAULT_INTERVAL, checks.DEFAULT_CHECKS)
    if site_id:
        site, note = db.get(site_id, chat_id), "✅ اضافه شد؛ بازه و چک‌ها را تنظیم کنید.\n\n"
    else:
        site, note = db.find(chat_id, domain), "این دامنه از قبل وجود دارد.\n\n"
    text, markup = site_view(site, ctx.bot_data["iran"], note)
    await update.message.reply_text(text, reply_markup=markup, parse_mode=ParseMode.HTML)


async def on_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    db = ctx.bot_data["db"]
    chat_id = q.message.chat_id
    action, *args = q.data.split(":")
    ctx.user_data.pop("awaiting", None)

    if action == "menu":
        return await render(q, menu_view())
    if action == "list":
        return await render(q, list_view(db, chat_id))
    if action == "add":
        ctx.user_data["awaiting"] = "domain"
        return await render(q, ("➕ دامنه را بفرستید (مثال: example.com)",
                                Markup([[Btn("🔙 انصراف", callback_data="menu")]])))

    site = db.get(int(args[0]), chat_id)
    if not site:
        return await render(q, list_view(db, chat_id))
    sid = site["id"]

    if action == "run":
        if not site_checks(site):
            return await q.message.reply_text("هیچ چکی انتخاب نشده.")
        msg = await q.message.reply_text("⏳ در حال بررسی…")
        rows = await collect(site, ctx.bot_data["iran"])
        return await msg.edit_text(format_report(site["domain"], rows, full=True), parse_mode=ParseMode.HTML)
    if action == "iv":
        return await render(q, interval_view(site))
    if action == "ck":
        return await render(q, checks_view(site))
    if action == "del":
        return await render(q, delete_view(site))
    if action == "delok":
        db.delete(sid)
        return await render(q, list_view(db, chat_id))

    if action == "setiv" and int(args[1]) in INTERVALS:
        db.update(sid, interval_min=int(args[1]), last_run=time.time())
    elif action == "tg" and args[1] in checks.CHECKS:
        active = set(site_checks(site)) ^ {args[1]}
        db.update(sid, checks=",".join(n for n in checks.CHECKS if n in active))
        return await render(q, checks_view(db.get(sid, chat_id)))
    elif action == "en":
        db.update(sid, enabled=1 - site["enabled"], last_run=time.time())
    elif action == "op":
        db.update(sid, only_problems=1 - site["only_problems"])
    elif action == "loc" and ctx.bot_data["iran"]:
        db.update(sid, location=NEXT_LOCATION[site["location"]])
    await render(q, site_view(db.get(sid, chat_id), ctx.bot_data["iran"]))


# ---------- scheduler ----------

async def process(app: Application, site, sem: asyncio.Semaphore):
    if not site_checks(site):
        return
    async with sem:
        rows = await collect(site, app.bot_data["iran"])
    ok = all(r.ok is not False for _, r in rows)
    recovered = ok and not site["last_ok"]
    app.bot_data["db"].update(site["id"], last_ok=int(ok))
    if ok and site["only_problems"] and not recovered:
        return
    text = format_report(site["domain"], rows)
    if recovered:
        text = "🎉 <b>بازیابی شد</b>\n" + text
    try:
        await app.bot.send_message(site["chat_id"], text, parse_mode=ParseMode.HTML)
    except Exception:
        log.exception("failed to send report for %s", site["domain"])


async def tick(ctx: ContextTypes.DEFAULT_TYPE):
    db = ctx.bot_data["db"]
    now = time.time()
    due = db.due(now)
    for site in due:
        db.update(site["id"], last_run=now)
    sem = asyncio.Semaphore(MAX_PARALLEL)
    await asyncio.gather(*(process(ctx.application, s, sem) for s in due))


async def post_init(app: Application):
    await app.bot.set_my_commands([BotCommand("menu", "منوی اصلی")])


def main():
    load_dotenv(ROOT / ".env")
    logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO)
    logging.getLogger("httpx").setLevel(logging.WARNING)

    token = os.environ.get("BOT_TOKEN", "").strip()
    admins = {int(x) for x in re.split(r"[,\s]+", os.environ.get("ADMIN_IDS", "")) if x}
    if not token or not admins:
        raise SystemExit("BOT_TOKEN and ADMIN_IDS must be set in .env")

    app = Application.builder().token(token).post_init(post_init).build()
    app.bot_data["db"] = DB(os.environ.get("DB_PATH") or ROOT / "data" / "uptimebot.db")
    app.bot_data["admins"] = admins
    iran_url, iran_token = os.environ.get("IRAN_CHECK_URL", "").strip(), os.environ.get("IRAN_CHECK_TOKEN", "").strip()
    app.bot_data["iran"] = (iran_url, iran_token) if iran_url and iran_token else None

    app.add_handler(TypeHandler(Update, guard), group=-1)
    app.add_handler(CommandHandler(["start", "menu"], cmd_menu))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.job_queue.run_repeating(tick, interval=TICK_SECONDS, first=5)
    app.run_polling(allowed_updates=["message", "callback_query"])
