import asyncio
import hashlib
import logging
import os
import random

import asyncpg
from dotenv import load_dotenv
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup,
    InputMediaPhoto, InputMediaVideo, InputMediaAnimation, InputMediaDocument,
)
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters,
)

load_dotenv()
logging.basicConfig(format="%(asctime)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

# ---------------- config ----------------
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
ADMIN_IDS = {int(x) for x in os.getenv("ADMIN_IDS", "").replace(" ", "").split(",") if x.isdigit()}

if not BOT_TOKEN:
    raise SystemExit("❌ BOT_TOKEN missing in .env")
if not ADMIN_IDS:
    raise SystemExit("❌ ADMIN_IDS missing or empty in .env")
if not DATABASE_URL:
    raise SystemExit("❌ DATABASE_URL missing in .env")

BUFFER_TIMEOUT = 180
MAX_ALBUM = 10
BATCH_DELAY = (4, 7)
REJECT_NOTICE_COOLDOWN = 10

KINDS = ("photo", "video", "animation", "document")
SINGLE_SEND = {
    "photo": "send_photo", "video": "send_video",
    "animation": "send_animation", "document": "send_document",
}
MEDIA_CLS = {
    "photo": InputMediaPhoto, "video": InputMediaVideo,
    "animation": InputMediaAnimation, "document": InputMediaDocument,
}

DB_POOL: asyncpg.Pool | None = None
buffers = {}          # {uid: {"photo": [...], ..., "task": Task}}
last_notice = {}      # {uid: monotonic time}

# ---------------- database ----------------

async def init_db():
    global DB_POOL
    DB_POOL = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=5)
    async with DB_POOL.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS user_settings (
                user_id   BIGINT PRIMARY KEY,
                dedup     BOOLEAN NOT NULL DEFAULT TRUE,
                photos    BOOLEAN NOT NULL DEFAULT FALSE,
                documents BOOLEAN NOT NULL DEFAULT FALSE
            );
            CREATE TABLE IF NOT EXISTS seen (
                user_id    BIGINT NOT NULL,
                hash       TEXT   NOT NULL,
                kind       TEXT   NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                PRIMARY KEY (user_id, hash)
            );
            CREATE TABLE IF NOT EXISTS stats (
                user_id      BIGINT NOT NULL,
                kind         TEXT   NOT NULL,
                sent         BIGINT NOT NULL DEFAULT 0,
                dup_rejected BIGINT NOT NULL DEFAULT 0,
                PRIMARY KEY (user_id, kind)
            );
        """)
    logger.info("DB schema ready")


async def close_db():
    if DB_POOL:
        await DB_POOL.close()
        logger.info("DB pool closed")


async def get_prefs(uid: int) -> dict:
    row = await DB_POOL.fetchrow(
        "SELECT dedup, photos, documents FROM user_settings WHERE user_id = $1", uid)
    if row is None:
        await DB_POOL.execute(
            "INSERT INTO user_settings (user_id) VALUES ($1) ON CONFLICT DO NOTHING", uid)
        return {"dedup": True, "photos": False, "documents": False}
    return {"dedup": row["dedup"], "photos": row["photos"], "documents": row["documents"]}


async def set_pref(uid: int, key: str, value: bool):
    if key not in ("dedup", "photos", "documents"):
        raise ValueError(key)
    await DB_POOL.execute(
        f"UPDATE user_settings SET {key} = $1 WHERE user_id = $2", value, uid)


async def is_duplicate(uid: int, h: str, kind: str) -> bool:
    """Atomically insert; returns True if the row already existed."""
    inserted = await DB_POOL.fetchval(
        "INSERT INTO seen (user_id, hash, kind) VALUES ($1, $2, $3) "
        "ON CONFLICT (user_id, hash) DO NOTHING RETURNING 1",
        uid, h, kind)
    return inserted is None


async def reset_dedup(uid: int):
    await DB_POOL.execute("DELETE FROM seen WHERE user_id = $1", uid)


async def inc_stat(uid: int, kind: str, field: str, amount: int = 1):
    if field not in ("sent", "dup_rejected"):
        raise ValueError(field)
    await DB_POOL.execute(
        f"INSERT INTO stats (user_id, kind, {field}) VALUES ($1, $2, $3) "
        f"ON CONFLICT (user_id, kind) DO UPDATE "
        f"SET {field} = stats.{field} + EXCLUDED.{field}",
        uid, kind, amount)


async def get_stats(uid: int) -> dict:
    rows = await DB_POOL.fetch(
        "SELECT kind, sent, dup_rejected FROM stats WHERE user_id = $1", uid)
    return {r["kind"]: {"sent": r["sent"], "dup_rejected": r["dup_rejected"]} for r in rows}

# ---------------- helpers ----------------

def is_admin(uid: int) -> bool:
    return uid in ADMIN_IDS


async def delete_safe(msg):
    try:
        await msg.delete()
    except Exception as e:
        logger.warning("could not delete message: %s", e)


async def send_clean(context, chat_id: int, kind: str, file_ids: list[str]):
    if len(file_ids) == 1:
        await getattr(context.bot, SINGLE_SEND[kind])(chat_id=chat_id, **{kind: file_ids[0]})
    else:
        media = [MEDIA_CLS[kind](media=fid) for fid in file_ids]
        await context.bot.send_media_group(chat_id=chat_id, media=media)


def get_buffer(uid: int) -> dict:
    buf = buffers.get(uid)
    if buf is None:
        buf = {k: [] for k in KINDS}
        buf["task"] = None
        buffers[uid] = buf
    return buf


def restart_timer(uid: int, chat_id: int, context):
    buf = buffers[uid]
    if buf["task"]:
        buf["task"].cancel()
    buf["task"] = asyncio.create_task(timeout_flush(uid, chat_id, context))


async def timeout_flush(uid: int, chat_id: int, context):
    try:
        await asyncio.sleep(BUFFER_TIMEOUT)
    except asyncio.CancelledError:
        return
    await flush_all(uid, chat_id, context)


async def flush_ready(uid: int, chat_id: int, context):
    buf = buffers.get(uid)
    if not buf:
        return
    first = True
    for kind in KINDS:
        while len(buf[kind]) >= MAX_ALBUM:
            chunk = buf[kind][:MAX_ALBUM]
            del buf[kind][:MAX_ALBUM]
            if not first:
                await asyncio.sleep(random.uniform(*BATCH_DELAY))
            first = False
            try:
                await send_clean(context, chat_id, kind, chunk)
                await inc_stat(uid, kind, "sent", len(chunk))
                logger.info("sent %s x%d", kind, len(chunk))
            except Exception as e:
                logger.error("send failed: %s", e)


async def flush_all(uid: int, chat_id: int, context):
    buf = buffers.pop(uid, None)
    if not buf:
        return
    first = True
    for kind in KINDS:
        ids = buf[kind]
        for i in range(0, len(ids), MAX_ALBUM):
            chunk = ids[i:i + MAX_ALBUM]
            if not first:
                await asyncio.sleep(random.uniform(*BATCH_DELAY))
            first = False
            try:
                await send_clean(context, chat_id, kind, chunk)
                await inc_stat(uid, kind, "sent", len(chunk))
                logger.info("flushed %s x%d", kind, len(chunk))
            except Exception as e:
                logger.error("send failed: %s", e)

# ---------------- handlers ----------------

async def handle_media(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    uid = update.effective_user.id
    chat_id = update.effective_chat.id
    p = await get_prefs(uid)

    await delete_safe(msg)

    if msg.photo:
        kind, obj = "photo", msg.photo[-1]
    elif msg.animation:
        kind, obj = "animation", msg.animation
    elif msg.video:
        kind, obj = "video", msg.video
    elif msg.document:
        kind, obj = "document", msg.document
    else:
        return

    blocked = (
        (kind in ("photo", "animation") and not p["photos"])
        or (kind == "document" and not p["documents"])
    )
    if blocked:
        now = asyncio.get_event_loop().time()
        if now - last_notice.get(uid, 0) > REJECT_NOTICE_COOLDOWN:
            last_notice[uid] = now
            label = "Photos/GIFs" if kind != "document" else "Documents"
            await context.bot.send_message(
                chat_id, f"📎 {label} are disabled. Enable them in /settings.")
        return

    if p["dedup"]:
        h = hashlib.sha256(f"{kind}:{obj.file_unique_id}".encode()).hexdigest()
        if await is_duplicate(uid, h, kind):
            await inc_stat(uid, kind, "dup_rejected")
            logger.info("duplicate skipped (%s)", kind)
            return

    buf = get_buffer(uid)
    buf[kind].append(obj.file_id)
    restart_timer(uid, chat_id, context)
    await flush_ready(uid, chat_id, context)


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    uid = update.effective_user.id
    chat_id = update.effective_chat.id
    p = await get_prefs(uid)

    text = (msg.text or "").strip()
    await delete_safe(msg)
    if not text:
        return

    if p["dedup"]:
        h = hashlib.sha256(("text:" + text).encode()).hexdigest()
        if await is_duplicate(uid, h, "text"):
            await inc_stat(uid, "text", "dup_rejected")
            return

    await context.bot.send_message(chat_id, text)
    await inc_stat(uid, "text", "sent")

# ---------------- settings UI ----------------

def settings_markup(p: dict) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(
            f"Dedup: {'✅ ON' if p['dedup'] else '❌ OFF'}", callback_data="toggle_dedup")],
        [InlineKeyboardButton(
            f"Photos: {'✅ ON' if p['photos'] else '❌ OFF'}", callback_data="toggle_photos")],
        [InlineKeyboardButton(
            f"Documents: {'✅ ON' if p['documents'] else '❌ OFF'}", callback_data="toggle_documents")],
        [InlineKeyboardButton("🧹 Reset dedup memory", callback_data="reset_dedup")],
    ])


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 *Clean Media Forwarder*\n\n"
        "Forward me photos, videos, GIFs, documents or text. I delete your "
        "original, strip captions and sender info, and send back a clean copy.\n\n"
        "• Media grouped into albums of 10 (each type separately)\n"
        "• Partial batches sent after 3 minutes\n"
        "• Text sent back immediately\n\n"
        "/settings — toggle options\n"
        "/stats — your counters\n"
        "/reset\\_dedup — wipe dedup memory",
        parse_mode="Markdown")


async def settings_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    p = await get_prefs(uid)
    await update.message.reply_text("⚙️ Settings:", reply_markup=settings_markup(p))


async def settings_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not is_admin(q.from_user.id):
        await q.answer("⛔ Unauthorized.", show_alert=True)
        return
    uid = q.from_user.id

    if q.data == "reset_dedup":
        await reset_dedup(uid)
        await q.answer("🧹 Dedup memory cleared.", show_alert=True)
        p = await get_prefs(uid)
        await q.edit_message_reply_markup(reply_markup=settings_markup(p))
        return

    await q.answer()
    p = await get_prefs(uid)
    if q.data == "toggle_dedup":
        await set_pref(uid, "dedup", not p["dedup"])
    elif q.data == "toggle_photos":
        await set_pref(uid, "photos", not p["photos"])
    elif q.data == "toggle_documents":
        await set_pref(uid, "documents", not p["documents"])
    p = await get_prefs(uid)
    await q.edit_message_reply_markup(reply_markup=settings_markup(p))


async def reset_dedup_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    await reset_dedup(uid)
    await update.message.reply_text("🧹 Dedup memory cleared.")


async def stats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    stats = await get_stats(uid)

    labels = [
        ("photo", "📷 Photos"),
        ("video", "🎬 Videos"),
        ("animation", "🎞 GIFs"),
        ("document", "📎 Documents"),
        ("text", "📝 Text"),
    ]

    lines = ["📊 *Your stats*\n"]
    total_sent = total_dup = 0
    for kind, label in labels:
        s = stats.get(kind, {"sent": 0, "dup_rejected": 0})
        total_sent += s["sent"]
        total_dup += s["dup_rejected"]
        lines.append(f"{label}: *{s['sent']}* sent · {s['dup_rejected']} dup")

    lines.append(f"\n*Total:* {total_sent} sent · {total_dup} dup rejected")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def log_unauthorized(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    logger.warning("ignored non-admin %s (@%s)", u.id, u.username)

# ---------------- lifecycle ----------------

async def post_init(app: Application):
    await init_db()


async def post_shutdown(app: Application):
    await close_db()


def main():
    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )

    admin = filters.User(ADMIN_IDS)

    app.add_handler(CommandHandler("start", start, filters=admin))
    app.add_handler(CommandHandler("settings", settings_cmd, filters=admin))
    app.add_handler(CommandHandler("stats", stats_cmd, filters=admin))
    app.add_handler(CommandHandler("reset_dedup", reset_dedup_cmd, filters=admin))
    app.add_handler(CallbackQueryHandler(
        settings_button,
        pattern="^(toggle_(dedup|photos|documents)|reset_dedup)$"))
    app.add_handler(MessageHandler(
        admin & (filters.PHOTO | filters.VIDEO | filters.ANIMATION | filters.Document.ALL),
        handle_media))
    app.add_handler(MessageHandler(admin & filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(MessageHandler(filters.ALL & ~admin, log_unauthorized))

    logger.info("Bot running. Admins: %s", ADMIN_IDS)
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
