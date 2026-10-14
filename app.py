import os
import sqlite3
import logging
import threading
import secrets
from datetime import datetime, timezone
from functools import wraps

from dotenv import load_dotenv
from flask import Flask, request, abort
import telebot
from telebot import types

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("zentro")

TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_ID_RAW = os.getenv("ADMIN_ID", "").strip()
CHANNEL = os.getenv("CHANNEL_USERNAME", "@Zentro_VPNx").strip()
CHANNEL_URL = os.getenv("CHANNEL_URL", "https://t.me/Zentro_VPNx").strip()
PAYMENT_CARD = os.getenv("PAYMENT_CARD", "").strip()
PAYMENT_NAME = os.getenv("PAYMENT_NAME", "").strip()
SUPPORT_USERNAME = os.getenv("SUPPORT_USERNAME", "ASD_Zentro").strip().lstrip("@")
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "").strip().rstrip("/")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "").strip()
DB_PATH = os.getenv("DB_PATH", "zentro.db").strip()

if not TOKEN:
    raise RuntimeError("BOT_TOKEN is missing in .env")
if not WEBHOOK_URL:
    log.warning("WEBHOOK_URL is empty; webhook will not be configured automatically.")
if not WEBHOOK_SECRET:
    raise RuntimeError("WEBHOOK_SECRET is missing in .env")

try:
    ADMIN_ID = int(ADMIN_ID_RAW) if ADMIN_ID_RAW else 0
except ValueError:
    ADMIN_ID = 0

bot = telebot.TeleBot(TOKEN, threaded=False)
app = Flask(__name__)
db_lock = threading.RLock()

def db():
    con = sqlite3.connect(DB_PATH, timeout=20, check_same_thread=False)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    return con

def init_db():
    with db_lock, db() as con:
        con.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            first_name TEXT,
            joined_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS products (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            price INTEGER NOT NULL CHECK(price >= 0),
            duration TEXT NOT NULL,
            volume TEXT NOT NULL,
            description TEXT NOT NULL DEFAULT '',
            active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS configs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            product_id INTEGER NOT NULL REFERENCES products(id),
            config_text TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'available',
            order_id INTEGER,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            product_id INTEGER NOT NULL REFERENCES products(id),
            product_name TEXT NOT NULL,
            price INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'awaiting_payment',
            receipt_file_id TEXT,
            assigned_config_id INTEGER,
            admin_note TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS processed_receipts (
            file_unique_id TEXT PRIMARY KEY,
            order_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        """)
        con.commit()

def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")

def is_admin(user_id):
    return ADMIN_ID != 0 and int(user_id) == ADMIN_ID

def admin_only(func):
    @wraps(func)
    def wrapper(message, *args, **kwargs):
        if not is_admin(message.from_user.id):
            bot.reply_to(message, "⛔ این بخش فقط برای مدیر ربات است.")
            return
        return func(message, *args, **kwargs)
    return wrapper

def record_user(user):
    with db_lock, db() as con:
        con.execute(
            "INSERT INTO users(user_id, username, first_name, joined_at) VALUES(?,?,?,?) "
            "ON CONFLICT(user_id) DO UPDATE SET username=excluded.username, first_name=excluded.first_name",
            (user.id, user.username or "", user.first_name or "", now())
        )
        con.commit()

def joined(user_id):
    try:
        member = bot.get_chat_member(CHANNEL, user_id)
        return member.status in ("creator", "administrator", "member") or (
            member.status == "restricted" and getattr(member, "is_member", False)
        )
    except Exception as exc:
        log.warning("Membership check failed: %s", exc)
        return False

def join_keyboard():
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("📢 عضویت در کانال", url=CHANNEL_URL))
    kb.add(types.InlineKeyboardButton("✅ بررسی عضویت", callback_data="check_join"))
    return kb

def main_keyboard():
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.row("🛒 خرید کانفیگ", "📦 سفارش‌های من")
    kb.row("🎫 پشتیبانی", "📋 راهنمای خرید")
    kb.row("📢 کانال رسمی", "ℹ️ درباره ما")
    return kb

def require_join_message(chat_id):
    bot.send_message(
        chat_id,
        "🔒 برای استفاده از ربات Zentro VPN، ابتدا عضو کانال رسمی شوید.\n\n"
        "پس از عضویت، روی «بررسی عضویت» بزنید.",
        reply_markup=join_keyboard()
    )

def user_allowed(message):
    if is_admin(message.from_user.id):
        return True
    if not joined(message.from_user.id):
        require_join_message(message.chat.id)
        return False
    return True

def get_product(product_id):
    with db_lock, db() as con:
        return con.execute("SELECT * FROM products WHERE id=? AND active=1", (product_id,)).fetchone()

def products_keyboard():
    kb = types.InlineKeyboardMarkup()
    with db_lock, db() as con:
        rows = con.execute("SELECT * FROM products WHERE active=1 ORDER BY id DESC").fetchall()
        for p in rows:
            stock = con.execute(
                "SELECT COUNT(*) n FROM configs WHERE product_id=? AND status='available'",
                (p["id"],)
            ).fetchone()["n"]
            label = f"{p['name']} — {p['price']:,} تومان"
            if stock <= 0:
                label += " (ناموجود)"
            kb.add(types.InlineKeyboardButton(label, callback_data=f"product:{p['id']}"))
    return kb, len(rows)

def format_order(row):
    return (
        f"🧾 سفارش #{row['id']}\n"
        f"📦 محصول: {row['product_name']}\n"
        f"💰 مبلغ: {row['price']:,} تومان\n"
        f"📌 وضعیت: {row['status']}\n"
        f"🕒 ثبت: {row['created_at']}"
    )

def safe_send(chat_id, text, **kwargs):
    try:
        return bot.send_message(chat_id, text, **kwargs)
    except Exception:
        log.exception("Could not send message to %s", chat_id)
        return None

@bot.message_handler(commands=["myid", "start"])
def start(message):
    record_user(message.from_user)
    if message.text and message.text.split()[0].endswith("myid"):
        bot.reply_to(message, f"شناسه عددی تلگرام شما: `{message.from_user.id}`", parse_mode="Markdown")
        return
    if not user_allowed(message):
        return
    bot.send_message(
        message.chat.id,
        "سلام 👋 به Zentro VPN خوش اومدی.\n\n"
        "از منوی زیر محصول موردنظرت رو انتخاب کن. پرداخت‌ها پس از بررسی مدیر تأیید می‌شن.",
        reply_markup=main_keyboard()
    )

@bot.callback_query_handler(func=lambda c: c.data == "check_join")
def check_join_callback(call):
    if joined(call.from_user.id):
        bot.answer_callback_query(call.id, "عضویت تأیید شد ✅")
        try:
            bot.edit_message_text("✅ عضویت شما تأیید شد. حالا می‌تونید از ربات استفاده کنید.",
                                  call.message.chat.id, call.message.message_id)
        except Exception:
            pass
        bot.send_message(call.message.chat.id, "منوی اصلی Zentro VPN:", reply_markup=main_keyboard())
    else:
        bot.answer_callback_query(call.id, "هنوز عضو کانال نیستید یا بررسی عضویت ممکن نیست.", show_alert=True)

@bot.message_handler(func=lambda m: m.text == "📢 کانال رسمی")
def channel_button(message):
    if user_allowed(message):
        bot.send_message(message.chat.id, f"کانال رسمی: {CHANNEL_URL}")

@bot.message_handler(func=lambda m: m.text == "ℹ️ درباره ما")
def about(message):
    if user_allowed(message):
        bot.send_message(message.chat.id,
            "🚀 Zentro VPN\nخرید اشتراک و دریافت کانفیگ پس از تأیید پرداخت.\n"
            "قبل از خرید، مدت اعتبار، حجم و شرایط محصول را بررسی کنید.")

@bot.message_handler(func=lambda m: m.text == "📋 راهنمای خرید")
def help_buy(message):
    if user_allowed(message):
        bot.send_message(message.chat.id,
            "📋 راهنمای خرید\n"
            "۱) از «خرید کانفیگ» محصول را انتخاب کنید.\n"
            "۲) مبلغ نمایش‌داده‌شده را پرداخت کنید.\n"
            "۳) تصویر رسید را در همین گفت‌وگو بفرستید.\n"
            "۴) پس از بررسی و تأیید واقعی واریز توسط مدیر، کانفیگ ارسال می‌شود.\n\n"
            "⚠️ عکس رسید به‌تنهایی اثبات واریز نیست. هرگز رمز پویا یا اطلاعات ورود بانکی را ارسال نکنید.")

@bot.message_handler(func=lambda m: m.text == "🛒 خرید کانفیگ")
def buy(message):
    if not user_allowed(message):
        return
    kb, count = products_keyboard()
    if count == 0:
        bot.send_message(message.chat.id, "فعلاً محصولی برای فروش ثبت نشده است.")
    else:
        bot.send_message(message.chat.id, "یکی از محصولات را انتخاب کنید:", reply_markup=kb)

@bot.callback_query_handler(func=lambda c: c.data.startswith("product:"))
def product_callback(call):
    if not is_admin(call.from_user.id) and not joined(call.from_user.id):
        bot.answer_callback_query(call.id, "ابتدا باید عضو کانال شوید.", show_alert=True)
        require_join_message(call.message.chat.id)
        return
    try:
        pid = int(call.data.split(":")[1])
    except Exception:
        bot.answer_callback_query(call.id, "محصول نامعتبر است.")
        return
    p = get_product(pid)
    if not p:
        bot.answer_callback_query(call.id, "این محصول فعال نیست.")
        return
    with db_lock, db() as con:
        stock = con.execute("SELECT COUNT(*) n FROM configs WHERE product_id=? AND status='available'", (pid,)).fetchone()["n"]
    text = (f"📦 {p['name']}\n💰 قیمت: {p['price']:,} تومان\n"
            f"⏳ اعتبار: {p['duration']}\n📊 حجم: {p['volume']}\n")
    if p["description"]:
        text += f"\nℹ️ {p['description']}\n"
    text += f"\n📦 موجودی: {stock}"
    kb = types.InlineKeyboardMarkup()
    if stock > 0:
        kb.add(types.InlineKeyboardButton("🧾 ثبت سفارش", callback_data=f"order:{pid}"))
    else:
        kb.add(types.InlineKeyboardButton("ناموجود", callback_data="noop"))
    bot.answer_callback_query(call.id)
    bot.send_message(call.message.chat.id, text, reply_markup=kb)

@bot.callback_query_handler(func=lambda c: c.data == "noop")
def noop(call):
    bot.answer_callback_query(call.id, "این محصول فعلاً موجود نیست.")

@bot.callback_query_handler(func=lambda c: c.data.startswith("order:"))
def create_order(call):
    if not is_admin(call.from_user.id) and not joined(call.from_user.id):
        bot.answer_callback_query(call.id, "ابتدا عضو کانال شوید.", show_alert=True)
        require_join_message(call.message.chat.id)
        return
    try:
        pid = int(call.data.split(":")[1])
    except Exception:
        bot.answer_callback_query(call.id, "سفارش نامعتبر است.")
        return
    p = get_product(pid)
    if not p:
        bot.answer_callback_query(call.id, "محصول در دسترس نیست.", show_alert=True)
        return
    with db_lock, db() as con:
        stock = con.execute("SELECT COUNT(*) n FROM configs WHERE product_id=? AND status='available'", (pid,)).fetchone()["n"]
        if stock < 1:
            con.commit()
            bot.answer_callback_query(call.id, "موجودی تمام شده است.", show_alert=True)
            return
        cur = con.execute(
            "INSERT INTO orders(user_id, product_id, product_name, price, status, created_at, updated_at) VALUES(?,?,?,?,?,?,?)",
            (call.from_user.id, pid, p["name"], p["price"], "awaiting_receipt", now(), now())
        )
        oid = cur.lastrowid
        con.commit()
    bot.answer_callback_query(call.id, f"سفارش #{oid} ثبت شد.")
    if PAYMENT_CARD:
        payment = f"💳 اطلاعات پرداخت\n\nشماره کارت: {PAYMENT_CARD}\nنام صاحب کارت: {PAYMENT_NAME or '—'}\nمبلغ دقیق: {p['price']:,} تومان"
    else:
        payment = "⚠️ اطلاعات پرداخت هنوز توسط مدیر تنظیم نشده است. لطفاً با پشتیبانی تماس بگیرید."
    payment += (
        f"\n\nشناسه سفارش: #{oid}\n"
        "پس از پرداخت، تصویر رسید را همین‌جا به‌صورت عکس ارسال کنید.\n"
        "رسید فقط پس از بررسی مدیر تأیید می‌شود. اطلاعات محرمانه بانکی را ارسال نکنید."
    )
    bot.send_message(call.message.chat.id, payment)

@bot.message_handler(func=lambda m: m.text == "📦 سفارش‌های من")
def my_orders(message):
    if not user_allowed(message):
        return
    with db_lock, db() as con:
        rows = con.execute("SELECT * FROM orders WHERE user_id=? ORDER BY id DESC LIMIT 10", (message.from_user.id,)).fetchall()
    if not rows:
        bot.send_message(message.chat.id, "هنوز سفارشی ثبت نکرده‌اید.")
        return
    bot.send_message(message.chat.id, "\n\n".join(format_order(r) for r in rows))

@bot.message_handler(content_types=["photo"])
def receipt_photo(message):
    if not user_allowed(message):
        return
    with db_lock, db() as con:
        row = con.execute(
            "SELECT * FROM orders WHERE user_id=? AND status='awaiting_receipt' ORDER BY id DESC LIMIT 1",
            (message.from_user.id,)
        ).fetchone()
        if not row:
            bot.reply_to(message, "سفارش در انتظار رسید پیدا نشد. ابتدا از بخش خرید کانفیگ سفارش ثبت کنید.")
            return
        file_id = message.photo[-1].file_id
        unique_id = message.photo[-1].file_unique_id
        dup = con.execute("SELECT order_id FROM processed_receipts WHERE file_unique_id=?", (unique_id,)).fetchone()
        if dup:
            bot.reply_to(message, "این تصویر قبلاً برای یک سفارش ثبت شده است. اگر اشتباه شده، با پشتیبانی تماس بگیرید.")
            return
        con.execute("UPDATE orders SET receipt_file_id=?, status='pending_review', updated_at=? WHERE id=?",
                    (file_id, now(), row["id"]))
        con.execute("INSERT INTO processed_receipts(file_unique_id, order_id, user_id, created_at) VALUES(?,?,?,?)",
                    (unique_id, row["id"], message.from_user.id, now()))
        con.commit()
    bot.reply_to(message, f"✅ رسید سفارش #{row['id']} دریافت شد و برای بررسی مدیر ارسال می‌شود. لطفاً تا اعلام نتیجه صبر کنید.")
    if ADMIN_ID:
        user = message.from_user
        username = f"@{user.username}" if user.username else "(بدون نام کاربری)"
        caption = (
            f"🧾 رسید جدید — سفارش #{row['id']}\n"
            f"👤 خریدار: {user.first_name or ''} {username}\n"
            f"🆔 شناسه عددی: {user.id}\n"
            f"📦 محصول: {row['product_name']}\n"
            f"💰 مبلغ مورد انتظار: {row['price']:,} تومان\n"
            f"🕒 زمان رسید: {now()}\n\n"
            "قبل از تأیید، واریز واقعی و مبلغ را از حساب بانکی بررسی کنید."
        )
        kb = types.InlineKeyboardMarkup()
        kb.row(
            types.InlineKeyboardButton("✅ تأیید و تحویل", callback_data=f"approve:{row['id']}"),
            types.InlineKeyboardButton("❌ رد رسید", callback_data=f"reject:{row['id']}")
        )
        try:
            bot.send_photo(ADMIN_ID, file_id, caption=caption, reply_markup=kb)
        except Exception:
            log.exception("Could not forward receipt to admin")
            bot.send_message(ADMIN_ID, caption + "\n⚠️ ارسال عکس ناموفق بود؛ رسید را از گفت‌وگوی ربات بررسی کنید.")
    else:
        log.error("ADMIN_ID not set; receipt cannot be sent to admin.")

@bot.callback_query_handler(func=lambda c: c.data.startswith("approve:"))
def approve_order(call):
    if not is_admin(call.from_user.id):
        bot.answer_callback_query(call.id, "دسترسی ندارید.", show_alert=True)
        return
    oid = int(call.data.split(":")[1])
    # Atomically select and reserve one available config, and mark the order paid.
    with db_lock, db() as con:
        con.execute("BEGIN IMMEDIATE")
        order = con.execute("SELECT * FROM orders WHERE id=?", (oid,)).fetchone()
        if not order or order["status"] != "pending_review":
            con.rollback()
            bot.answer_callback_query(call.id, "این سفارش در انتظار بررسی نیست.", show_alert=True)
            return
        cfg = con.execute(
            "SELECT * FROM configs WHERE product_id=? AND status='available' ORDER BY id LIMIT 1",
            (order["product_id"],)
        ).fetchone()
        if not cfg:
            con.execute("UPDATE orders SET status='paid_no_stock', admin_note=?, updated_at=? WHERE id=?",
                        ("پرداخت تأیید شد ولی موجودی کانفیگ تمام است؛ رسیدگی دستی لازم است.", now(), oid))
            con.commit()
            bot.answer_callback_query(call.id, "پرداخت تأیید شد اما موجودی تمام است.", show_alert=True)
            safe_send(order["user_id"], f"✅ پرداخت سفارش #{oid} تأیید شد، اما موجودی برای تحویل خودکار موجود نیست. مدیر برای تحویل یا بازپرداخت با شما هماهنگ می‌کند.")
            return
        con.execute("UPDATE configs SET status='delivered', order_id=? WHERE id=? AND status='available'", (oid, cfg["id"]))
        con.execute("UPDATE orders SET status='delivered', assigned_config_id=?, updated_at=? WHERE id=?",
                    (cfg["id"], now(), oid))
        con.commit()
    bot.answer_callback_query(call.id, "پرداخت تأیید و کانفیگ تخصیص داده شد.")
    try:
        bot.send_message(order["user_id"],
            f"✅ پرداخت سفارش #{oid} تأیید شد.\n\n"
            f"📦 محصول: {order['product_name']}\n"
            f"🔑 کانفیگ شما:\n\n<code>{cfg['config_text']}</code>\n\n"
            "این پیام را نگه دارید. اگر اتصال برقرار نشد، از بخش پشتیبانی پیام دهید.",
            parse_mode="HTML")
    except Exception:
        log.exception("Delivery message failed for order %s", oid)
        with db_lock, db() as con:
            con.execute("UPDATE orders SET status='delivery_failed', updated_at=? WHERE id=?", (now(), oid))
            con.commit()
        safe_send(ADMIN_ID, f"⚠️ سفارش #{oid} پرداختش تأیید و کانفیگ تخصیص داده شد، اما ارسال به مشتری ناموفق بود. از پنل سفارش پیگیری کنید.")
    try:
        bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
    except Exception:
        pass

@bot.callback_query_handler(func=lambda c: c.data.startswith("reject:"))
def reject_order(call):
    if not is_admin(call.from_user.id):
        bot.answer_callback_query(call.id, "دسترسی ندارید.", show_alert=True)
        return
    oid = int(call.data.split(":")[1])
    with db_lock, db() as con:
        row = con.execute("SELECT * FROM orders WHERE id=?", (oid,)).fetchone()
        if not row or row["status"] != "pending_review":
            con.rollback()
            bot.answer_callback_query(call.id, "این سفارش در انتظار بررسی نیست.", show_alert=True)
            return
        con.execute("UPDATE orders SET status='rejected', updated_at=? WHERE id=?", (now(), oid))
        con.commit()
    bot.answer_callback_query(call.id, "رسید رد شد.")
    safe_send(row["user_id"],
        f"❌ رسید سفارش #{oid} تأیید نشد.\nاگر فکر می‌کنید اشتباهی رخ داده، با پشتیبانی @{SUPPORT_USERNAME} تماس بگیرید. در صورت امکان، دلیل رد را از مدیر بپرسید.")
    try:
        bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
    except Exception:
        pass

@bot.message_handler(func=lambda m: m.text == "🎫 پشتیبانی")
def support(message):
    if not user_allowed(message):
        return
    bot.send_message(message.chat.id, f"برای پشتیبانی به @{SUPPORT_USERNAME} پیام دهید.\nدر پیام خود شناسه سفارش را هم بفرستید.")

@bot.message_handler(commands=["admin"])
def admin_menu(message):
    if not is_admin(message.from_user.id):
        bot.reply_to(message, "⛔ این دستور مخصوص مدیر است.")
        return
    bot.reply_to(message,
        "👑 پنل مدیریت Zentro VPN\n\n"
        "/stats — آمار کلی\n"
        "/products — فهرست محصولات\n"
        "/addproduct نام|قیمت تومان|مدت|حجم|توضیحات — افزودن محصول\n"
        "/addconfig شناسه_محصول|متن_کانفیگ — افزودن کانفیگ به موجودی\n"
        "/offproduct شناسه_محصول — غیرفعال کردن محصول\n"
        "/orders — سفارش‌های اخیر\n"
        "/broadcast متن — پیام همگانی\n"
        "/myid — شناسه عددی شما\n\n"
        "قیمت را فقط به عدد صحیح تومان وارد کنید. هر کانفیگ را در یک دستور جداگانه اضافه کنید.")

@bot.message_handler(commands=["stats"])
@admin_only
def stats(message):
    with db_lock, db() as con:
        users = con.execute("SELECT COUNT(*) n FROM users").fetchone()["n"]
        products = con.execute("SELECT COUNT(*) n FROM products WHERE active=1").fetchone()["n"]
        orders = con.execute("SELECT COUNT(*) n FROM orders").fetchone()["n"]
        delivered = con.execute("SELECT COUNT(*) n FROM orders WHERE status='delivered'").fetchone()["n"]
        paid = con.execute("SELECT COALESCE(SUM(price),0) s FROM orders WHERE status IN ('delivered','paid_no_stock','delivery_failed')").fetchone()["s"]
        pending = con.execute("SELECT COUNT(*) n FROM orders WHERE status='pending_review'").fetchone()["n"]
        stock = con.execute("SELECT COUNT(*) n FROM configs WHERE status='available'").fetchone()["n"]
    bot.reply_to(message,
        f"📊 آمار ربات\n👥 کاربران: {users}\n🛍 محصولات فعال: {products}\n"
        f"🧾 کل سفارش‌ها: {orders}\n⏳ رسیدهای منتظر بررسی: {pending}\n"
        f"📦 کانفیگ موجود: {stock}\n✅ تحویل‌شده: {delivered}\n"
        f"💰 مجموع مبالغ پرداخت تأییدشده: {paid:,} تومان\n\n"
        "این گزارش جایگزین گزارش بانکی نیست.")

@bot.message_handler(commands=["products"])
@admin_only
def list_products(message):
    with db_lock, db() as con:
        rows = con.execute("SELECT * FROM products ORDER BY id DESC LIMIT 50").fetchall()
        if not rows:
            bot.reply_to(message, "هنوز محصولی ثبت نشده است.")
            return
        lines = []
        for p in rows:
            stock = con.execute("SELECT COUNT(*) n FROM configs WHERE product_id=? AND status='available'", (p["id"],)).fetchone()["n"]
            lines.append(f"#{p['id']} | {p['name']} | {p['price']:,} تومان | موجودی: {stock} | {'فعال' if p['active'] else 'غیرفعال'}")
    bot.reply_to(message, "\n".join(lines))

@bot.message_handler(commands=["addproduct"])
@admin_only
def add_product(message):
    raw = message.text.partition(" ")[2].strip()
    parts = [x.strip() for x in raw.split("|")]
    if len(parts) < 4:
        bot.reply_to(message, "فرمت:\n/addproduct نام|قیمت تومان|مدت|حجم|توضیحات اختیاری")
        return
    try:
        name = parts[0]
        price = int(parts[1].replace(",", ""))
        duration, volume = parts[2], parts[3]
        description = parts[4] if len(parts) > 4 else ""
        if not name or price < 0 or not duration or not volume:
            raise ValueError()
    except ValueError:
        bot.reply_to(message, "اطلاعات محصول درست نیست. قیمت باید عدد صحیح تومان باشد.")
        return
    with db_lock, db() as con:
        cur = con.execute("INSERT INTO products(name,price,duration,volume,description,active,created_at) VALUES(?,?,?,?,?,1,?)",
                          (name, price, duration, volume, description, now()))
        con.commit()
        pid = cur.lastrowid
    bot.reply_to(message, f"✅ محصول ثبت شد.\nشناسه محصول: {pid}\nحالا کانفیگ‌ها را با دستور زیر اضافه کن:\n/addconfig {pid}|متن کانفیگ")

@bot.message_handler(commands=["addconfig"])
@admin_only
def add_config(message):
    raw = message.text.partition(" ")[2]
    if "|" not in raw:
        bot.reply_to(message, "فرمت:\n/addconfig شناسه_محصول|متن_کانفیگ")
        return
    pid_raw, config_text = raw.split("|", 1)
    config_text = config_text.strip()
    try:
        pid = int(pid_raw.strip())
    except ValueError:
        bot.reply_to(message, "شناسه محصول باید عدد باشد.")
        return
    if not config_text or len(config_text) > 3500:
        bot.reply_to(message, "متن کانفیگ خالی است یا بیش از حد طولانی است.")
        return
    with db_lock, db() as con:
        p = con.execute("SELECT id FROM products WHERE id=?", (pid,)).fetchone()
        if not p:
            bot.reply_to(message, "محصولی با این شناسه پیدا نشد.")
            return
        con.execute("INSERT INTO configs(product_id,config_text,status,created_at) VALUES(?,?, 'available',?)",
                    (pid, config_text, now()))
        con.commit()
        count = con.execute("SELECT COUNT(*) n FROM configs WHERE product_id=? AND status='available'", (pid,)).fetchone()["n"]
    bot.reply_to(message, f"✅ کانفیگ به موجودی محصول #{pid} اضافه شد. موجودی فعلی: {count}")

@bot.message_handler(commands=["offproduct"])
@admin_only
def off_product(message):
    try:
        pid = int(message.text.split(maxsplit=1)[1])
    except Exception:
        bot.reply_to(message, "فرمت: /offproduct شناسه_محصول")
        return
    with db_lock, db() as con:
        cur = con.execute("UPDATE products SET active=0 WHERE id=?", (pid,))
        con.commit()
    bot.reply_to(message, "✅ محصول غیرفعال شد." if cur.rowcount else "محصول پیدا نشد.")

@bot.message_handler(commands=["orders"])
@admin_only
def recent_orders(message):
    with db_lock, db() as con:
        rows = con.execute("SELECT * FROM orders ORDER BY id DESC LIMIT 15").fetchall()
    if not rows:
        bot.reply_to(message, "سفارشی ثبت نشده است.")
        return
    bot.reply_to(message, "\n\n".join(format_order(r) for r in rows))

@bot.message_handler(commands=["broadcast"])
@admin_only
def broadcast(message):
    body = message.text.partition(" ")[2].strip()
    if not body:
        bot.reply_to(message, "فرمت: /broadcast متن پیام")
        return
    with db_lock, db() as con:
        ids = [r["user_id"] for r in con.execute("SELECT user_id FROM users").fetchall()]
    sent = failed = 0
    for uid in ids:
        try:
            bot.send_message(uid, body)
            sent += 1
        except Exception:
            failed += 1
    bot.reply_to(message, f"📢 پایان ارسال همگانی\nموفق: {sent}\nناموفق: {failed}")

@bot.message_handler(func=lambda m: True, content_types=["text"])
def fallback(message):
    if not user_allowed(message):
        return
    bot.send_message(message.chat.id, "از دکمه‌های منوی اصلی استفاده کنید یا /start را بزنید.", reply_markup=main_keyboard())

@app.get("/")
def health():
    return "Zentro VPN bot is running.", 200

@app.post("/webhook")
def webhook():
    provided = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
    if not secrets.compare_digest(provided, WEBHOOK_SECRET):
        abort(403)
    if request.headers.get("content-type", "").startswith("application/json"):
        update = telebot.types.Update.de_json(request.get_data().decode("utf-8"))
        bot.process_new_updates([update])
        return "ok", 200
    abort(415)

def configure_webhook():
    if WEBHOOK_URL:
        try:
            bot.set_webhook(url=f"{WEBHOOK_URL}/webhook", secret_token=WEBHOOK_SECRET, drop_pending_updates=False)
            log.info("Webhook configured.")
        except Exception:
            log.exception("Could not configure webhook; check WEBHOOK_URL and Telegram connectivity.")

init_db()
configure_webhook()
