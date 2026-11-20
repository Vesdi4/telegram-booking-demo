"""
North Cut Barbers: Telegram booking bot with a Mini App.

Features
- Booking happens in a Telegram Mini App (app-style UI inside Telegram)
- Live availability: taken slots are greyed out per barber
- No double bookings (enforced in the database)
- Owner gets each booking with Confirm / Decline buttons; the customer is told the result
- Automatic reminder 2 hours before the appointment (survives restarts)
- Customers can view and cancel their bookings (/my)
- Owner schedule view (/today, /tomorrow)

Env vars: BOT_TOKEN, OWNER_CHAT_ID, WEBAPP_URL
    pip install "python-telegram-bot[job-queue]>=21,<22"
"""
import json
import logging
import os
import sqlite3
from datetime import date, datetime, timedelta
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from telegram import (
    InlineKeyboardButton as Btn, InlineKeyboardMarkup, KeyboardButton,
    ReplyKeyboardMarkup, Update, WebAppInfo,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)

TZ = ZoneInfo("Europe/Tallinn")
OWNER = int(os.environ.get("OWNER_CHAT_ID", "0"))
WEBAPP_URL = os.environ["WEBAPP_URL"]
SHOP = "North Cut Barbers"
ADDRESS = ("Telliskivi 60", 59.4400, 24.7280)

SERVICES = {
    "cut": ("Classic haircut", 45, 25, "✂️"),
    "fade": ("Skin fade", 50, 30, "💈"),
    "beard": ("Beard trim", 30, 18, "🧔"),
    "combo": ("Cut + beard", 75, 40, "👑"),
}
PROS = {"mark": "Mark", "liis": "Liis", "artur": "Artur"}
TIMES = ["10:00", "10:30", "11:00", "11:30", "12:00", "12:30", "13:00", "14:00", "14:30",
         "15:00", "15:30", "16:00", "16:30", "17:00", "17:30", "18:00", "18:30", "19:00"]
STATUS = {"pending": "⏳ Waiting for confirmation", "confirmed": "✅ Confirmed",
          "declined": "❌ Declined", "cancelled": "🚫 Cancelled"}

DB = sqlite3.connect(os.path.join(os.path.dirname(os.path.abspath(__file__)), "bookings2.db"),
                     check_same_thread=False)
DB.row_factory = sqlite3.Row
DB.execute("""CREATE TABLE IF NOT EXISTS bookings(
    id INTEGER PRIMARY KEY, user_id INTEGER, name TEXT, phone TEXT, note TEXT,
    service TEXT, pro TEXT, day TEXT, time TEXT, status TEXT DEFAULT 'pending',
    created TEXT DEFAULT CURRENT_TIMESTAMP)""")
# one active booking per barber per slot
DB.execute("""CREATE UNIQUE INDEX IF NOT EXISTS slot_active ON bookings(pro, day, time)
              WHERE status IN ('pending','confirmed')""")
DB.commit()


def starts_at(b) -> datetime:
    return datetime.fromisoformat(f"{b['day']}T{b['time']}").replace(tzinfo=TZ)


def nice_day(day: str) -> str:
    d = date.fromisoformat(day)
    if d == date.today():
        return "Today"
    if d == date.today() + timedelta(days=1):
        return "Tomorrow"
    return d.strftime("%a %d %b")


def card(b, title=None) -> str:
    name, mins, price, icon = SERVICES[b["service"]]
    lines = [f"<b>{title}</b>\n" if title else "",
             f"{icon} <b>{name}</b> with {PROS[b['pro']]}",
             f"📅 {nice_day(b['day'])}, {b['time']} · {mins} min",
             f"💶 €{price}",
             f"📍 {ADDRESS[0]}, Tallinn",
             f"\n{STATUS[b['status']]}  ·  booking #{b['id']}"]
    return "\n".join(l for l in lines if l)


def webapp_url() -> str:
    rows = DB.execute("SELECT pro, day, time FROM bookings WHERE status IN ('pending','confirmed') AND day>=?",
                      (date.today().isoformat(),)).fetchall()
    taken = ",".join(f"{r['pro']}|{r['day']}|{r['time']}" for r in rows)
    return f"{WEBAPP_URL}?{urlencode({'taken': taken})}" if taken else WEBAPP_URL


def main_keyboard():
    return ReplyKeyboardMarkup(
        [[KeyboardButton("✂️ Book an appointment", web_app=WebAppInfo(webapp_url()))],
         [KeyboardButton("📋 My bookings"), KeyboardButton("📍 Location")]],
        resize_keyboard=True,
    )


# ---------- customer ----------
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    first = update.effective_user.first_name or "there"
    await update.message.reply_text(
        f"Hey {first} 👋\n\nWelcome to <b>{SHOP}</b>, Tallinn's sharpest cuts.\n\n"
        "Tap <b>Book an appointment</b> below to pick a service, barber and time. Takes about 20 seconds.",
        parse_mode=ParseMode.HTML, reply_markup=main_keyboard())


async def location(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await ctx.bot.send_venue(update.effective_chat.id, ADDRESS[1], ADDRESS[2], SHOP, f"{ADDRESS[0]}, Tallinn")
    await update.message.reply_text("Open Mon–Sat 10:00–20:00. Closed Sundays.", reply_markup=main_keyboard())


async def web_app_data(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    try:
        d = json.loads(update.effective_message.web_app_data.data)
        assert d["service"] in SERVICES and d["pro"] in PROS and d["time"] in TIMES
        when = datetime.fromisoformat(f"{d['date']}T{d['time']}").replace(tzinfo=TZ)
        assert when > datetime.now(TZ) and when.weekday() != 6
        name, phone = str(d["name"]).strip()[:60], str(d["phone"]).strip()[:30]
        assert name and phone
    except Exception:
        await update.effective_message.reply_text("Something looked off with that booking. Please try again.",
                                                  reply_markup=main_keyboard())
        return
    try:
        cur = DB.execute(
            "INSERT INTO bookings(user_id,name,phone,note,service,pro,day,time) VALUES(?,?,?,?,?,?,?,?)",
            (update.effective_user.id, name, phone, str(d.get("note", ""))[:200],
             d["service"], d["pro"], d["date"], d["time"]))
        DB.commit()
    except sqlite3.IntegrityError:
        await update.effective_message.reply_text(
            "😕 Someone grabbed that slot a moment ago. Please pick another time.", reply_markup=main_keyboard())
        return
    b = DB.execute("SELECT * FROM bookings WHERE id=?", (cur.lastrowid,)).fetchone()
    await update.effective_message.reply_text(
        card(b, "Booking received 🎉") + "\n\nWe'll confirm shortly and remind you 2 hours before.",
        parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup([[Btn("Cancel booking", callback_data=f"cx:{b['id']}")]]))
    await update.effective_message.reply_text("Anything else?", reply_markup=main_keyboard())
    schedule_reminder(ctx.application, b)
    if OWNER:
        extra = f"\n👤 {b['name']} · {b['phone']}" + (f"\n💬 {b['note']}" if b["note"] else "")
        await ctx.bot.send_message(
            OWNER, card(b, "🔔 New booking") + extra, parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([[Btn("✅ Confirm", callback_data=f"ok:{b['id']}"),
                                                Btn("❌ Decline", callback_data=f"no:{b['id']}")]]))


async def my_bookings(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    rows = DB.execute(
        "SELECT * FROM bookings WHERE user_id=? AND status IN ('pending','confirmed') AND day>=? ORDER BY day,time",
        (update.effective_user.id, date.today().isoformat())).fetchall()
    if not rows:
        await update.message.reply_text("You have no upcoming bookings. Tap ✂️ Book an appointment to make one.",
                                        reply_markup=main_keyboard())
        return
    for b in rows:
        await update.message.reply_text(
            card(b), parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([[Btn("Cancel booking", callback_data=f"cx:{b['id']}")]]))


async def on_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    action, bid = q.data.split(":")
    b = DB.execute("SELECT * FROM bookings WHERE id=?", (int(bid),)).fetchone()
    if not b:
        await q.answer("Booking not found")
        return

    if action == "cx":
        if q.from_user.id != b["user_id"]:
            await q.answer("Not your booking")
            return
        if b["status"] not in ("pending", "confirmed"):
            await q.answer("Already " + b["status"])
            return
        if starts_at(b) - datetime.now(TZ) < timedelta(hours=2):
            await q.answer("Less than 2 hours left. Please call us to cancel.", show_alert=True)
            return
        set_status(b["id"], "cancelled")
        b = DB.execute("SELECT * FROM bookings WHERE id=?", (b["id"],)).fetchone()
        await q.answer("Cancelled")
        await q.edit_message_text(card(b), parse_mode=ParseMode.HTML)
        if OWNER:
            await ctx.bot.send_message(OWNER, card(b, "🚫 Customer cancelled"), parse_mode=ParseMode.HTML)
        return

    if q.from_user.id != OWNER:
        await q.answer("Owner only")
        return
    if b["status"] != "pending":
        await q.answer("Already " + b["status"])
        return
    new = "confirmed" if action == "ok" else "declined"
    set_status(b["id"], new)
    b = DB.execute("SELECT * FROM bookings WHERE id=?", (b["id"],)).fetchone()
    await q.answer(new.capitalize())
    await q.edit_message_text(card(b, "Booking " + new) + f"\n👤 {b['name']} · {b['phone']}", parse_mode=ParseMode.HTML)
    msg = ("Your booking is confirmed. See you there! ✂️" if new == "confirmed"
           else "Sorry, we can't take this booking. Please choose another time.")
    await ctx.bot.send_message(b["user_id"], card(b) + "\n\n" + msg, parse_mode=ParseMode.HTML)


def set_status(bid: int, status: str):
    DB.execute("UPDATE bookings SET status=? WHERE id=?", (status, bid))
    DB.commit()


# ---------- reminders ----------
def schedule_reminder(app: Application, b):
    when = starts_at(b) - timedelta(hours=2)
    if when > datetime.now(TZ):
        app.job_queue.run_once(send_reminder, when, data=b["id"], name=f"rem{b['id']}")


async def send_reminder(ctx: ContextTypes.DEFAULT_TYPE):
    b = DB.execute("SELECT * FROM bookings WHERE id=?", (ctx.job.data,)).fetchone()
    if b and b["status"] in ("pending", "confirmed"):
        await ctx.bot.send_message(b["user_id"], card(b, "⏰ Reminder: see you in 2 hours"), parse_mode=ParseMode.HTML)


async def on_startup(app: Application):
    for b in DB.execute("SELECT * FROM bookings WHERE status IN ('pending','confirmed') AND day>=?",
                        (date.today().isoformat(),)).fetchall():
        schedule_reminder(app, b)


# ---------- owner ----------
async def schedule(update: Update, ctx: ContextTypes.DEFAULT_TYPE, offset: int):
    if update.effective_user.id != OWNER:
        return
    day = (date.today() + timedelta(days=offset)).isoformat()
    rows = DB.execute("SELECT * FROM bookings WHERE day=? AND status IN ('pending','confirmed') ORDER BY time",
                      (day,)).fetchall()
    if not rows:
        await update.message.reply_text(f"{nice_day(day)}: no bookings yet.")
        return
    total = sum(SERVICES[b["service"]][2] for b in rows)
    lines = [f"<b>{nice_day(day)}: {len(rows)} bookings · €{total}</b>\n"]
    for b in rows:
        mark = "✅" if b["status"] == "confirmed" else "⏳"
        lines.append(f"{mark} <b>{b['time']}</b> {PROS[b['pro']]} · {SERVICES[b['service']][0]} · {b['name']} {b['phone']}")
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


async def today(update, ctx): await schedule(update, ctx, 0)
async def tomorrow(update, ctx): await schedule(update, ctx, 1)
async def myid(update, ctx): await update.message.reply_text(f"Your chat id: {update.effective_chat.id}")


def main():
    app = Application.builder().token(os.environ["BOT_TOKEN"]).post_init(on_startup).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("book", start))
    app.add_handler(CommandHandler("my", my_bookings))
    app.add_handler(CommandHandler("today", today))
    app.add_handler(CommandHandler("tomorrow", tomorrow))
    app.add_handler(CommandHandler("myid", myid))
    app.add_handler(MessageHandler(filters.StatusUpdate.WEB_APP_DATA, web_app_data))
    app.add_handler(MessageHandler(filters.Regex("^📋 My bookings$"), my_bookings))
    app.add_handler(MessageHandler(filters.Regex("^📍 Location$"), location))
    app.add_handler(CallbackQueryHandler(on_button))
    app.run_polling()


if __name__ == "__main__":
    main()
