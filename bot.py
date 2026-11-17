import os
import logging
import asyncio
import random
import time
from helpers import admin_only
from telegram import Update, InputMediaVideo, BotCommand
from dotenv import load_dotenv  # 1. Import it first

# 2. Now call it to load the .env file
load_dotenv()

TOKEN = os.getenv("BOT_TOKEN", "").strip()
DEFAULT_ADMINS = {8976017144, 8271828643}
ADMINS = {int(x.strip()) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()} or DEFAULT_ADMINS
DEFAULT_TIMEOUT = max(10, int(os.getenv("BATCH_TIMEOUT", "60")))
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("video-vault")
last_video = {}
locks = {}
progress = {}

SCHEMA = """
CREATE TABLE IF NOT EXISTS videos (
 id BIGSERIAL PRIMARY KEY, owner_id BIGINT NOT NULL, chat_id BIGINT NOT NULL,
 file_id TEXT NOT NULL, unique_id TEXT, file_name TEXT, caption TEXT,
 file_size BIGINT, duration INTEGER, status TEXT NOT NULL DEFAULT 'queued',
 received_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), sent_at TIMESTAMPTZ, last_error TEXT
);
CREATE INDEX IF NOT EXISTS videos_lookup ON videos(owner_id, unique_id);
CREATE INDEX IF NOT EXISTS videos_queue ON videos(owner_id, chat_id, status, received_at, id);
CREATE TABLE IF NOT EXISTS settings (
 owner_id BIGINT PRIMARY KEY, dedup BOOLEAN NOT NULL DEFAULT TRUE,
 auto_mode BOOLEAN NOT NULL DEFAULT TRUE, batch_size INTEGER NOT NULL DEFAULT 10,
 timeout_sec INTEGER NOT NULL DEFAULT 60, progress_every INTEGER NOT NULL DEFAULT 50
);
CREATE TABLE IF NOT EXISTS events (
 id BIGSERIAL PRIMARY KEY, owner_id BIGINT NOT NULL, kind TEXT NOT NULL, detail TEXT,
 created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
"""

def admin(fn):
    @wraps(fn)
    async def wrapped(update: Update, context: ContextTypes.DEFAULT_TYPE):
        u = update.effective_user
        if not u or u.id not in ADMINS:
            q = update.callback_query
            if q:
                await q.answer("Private bot: access denied.", show_alert=True)
            elif update.effective_message:
                await update.effective_message.reply_text("This bot is private. Access denied.")
            return
        return await fn(update, context)
    return wrapped

async def pool(context):
    p = context.application.bot_data.get("pool")
    if not p: raise RuntimeError("PostgreSQL pool is unavailable")
    return p


# -----------------------------
# Commands (Admins only)
# -----------------------------
@admin_only
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"Bot ready.\nYour Telegram ID is: {update.effective_user.id}"
    )


@admin_only
async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "Available commands:\n"
        "/start – Show your Telegram ID\n"
        "/help – Show this help menu\n"
        "/settings – Show bot settings\n"
        "/admin_commands – Show admin commands\n"
        "/toggle_photo_mode – Toggle photo approval mode\n"
        "/approve – Approve pending photo\n"
        "/reject – Reject pending photo\n"
        "/flush – Flush remaining videos\n"
    )
    await update.message.reply_text(text)


@admin_only
async def settings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "Settings:\n"
        f"Photo approval mode: {'ON' if photo_approval_mode else 'OFF'}\n"
        f"Batch timeout: {FLUSH_TIMEOUT} seconds\n"
        "Album size: 10 videos\n"
    )
    await update.message.reply_text(text)


@admin_only
async def admin_commands(update: Update, context: ContextTypes.DEFAULT_TYPE):
    commands = [
        "/toggle_photo_mode – Enable/disable photo approval mode",
        "/approve – Approve pending photo",
        "/reject – Reject pending photo",
        "/flush – Manually flush remaining videos",
        "/admin_commands – Show admin commands"
    ]
    text = "Admin commands:\n\n" + "\n".join(commands)
    await update.message.reply_text(text)


@admin_only
async def toggle_photo_mode(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global photo_approval_mode
    photo_approval_mode = not photo_approval_mode
    status = "ON" if photo_approval_mode else "OFF"
    await update.message.reply_text(f"Photo approval mode is now {status}")


@admin_only
async def approve(update: Update, context: ContextTypes.DEFAULT_TYPE):
    file_id = context.user_data.get("pending_photo")
    if not file_id:
        await update.message.reply_text("No pending photo.")
        return

    await update.message.reply_photo(file_id)
    context.user_data["pending_photo"] = None


@admin_only
async def reject(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["pending_photo"] = None
    await update.message.reply_text("Photo rejected.")


@admin_only
async def flush(update: Update, context: ContextTypes.DEFAULT_TYPE):
    album = context.user_data.get("album", [])
    if not album:
        await update.message.reply_text("No pending videos.")
        return

    await send_album(update, context)
    await update.message.reply_text("Flushed remaining videos.")


# -----------------------------
# Photo Handler (Admins only)
# -----------------------------
@admin_only
async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global photo_approval_mode

    file_id = update.message.photo[-1].file_id

    if not photo_approval_mode:
        await update.message.reply_photo(file_id)
        return

    context.user_data["pending_photo"] = file_id
    await update.message.reply_text("Photo received. Use /approve or /reject.")


# -----------------------------
# Video Handler (Admins only)
# -----------------------------
@admin_only
async def handle_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global last_video_time, batch_count

    message = update.message

    try:
        await message.delete()
    except:
        pass

    if not message.video:
        return

    file_id = message.video.file_id

    if file_id in video_cache:
        return

    video_cache.add(file_id)

    last_video_time = time.time()
    batch_count += 1

    await update.message.reply_text(f"Received {batch_count} videos…")

    if "album" not in context.user_data:
        context.user_data["album"] = []

    context.user_data["album"].append(file_id)

    if len(context.user_data["album"]) >= 10:
        await send_album(update, context)


# -----------------------------
# Album Sending
# -----------------------------
async def send_album(update: Update, context: ContextTypes.DEFAULT_TYPE):
    album = context.user_data.get("album", [])
    if not album:
        return

    delay = random.uniform(2, 3)
    await asyncio.sleep(delay)

    media_group = [InputMediaVideo(media=fid) for fid in album]

    await update.message.reply_media_group(media_group)
    context.user_data["album"] = []


async def send_album_to_chat(app, chat_id, album):
    media_group = [InputMediaVideo(media=fid) for fid in album]
    await app.bot.send_media_group(chat_id, media_group)


# -----------------------------
# Batch Watcher (JobQueue)
# -----------------------------
async def batch_watcher(app):
    global last_video_time

    now = time.time()

    for chat_id, data in list(app.chat_data.items()):
        album = data.get("album", [])
        if album and now - last_video_time >= FLUSH_TIMEOUT:
            await app.bot.send_message(chat_id, "Batch ended. Sending remaining videos…")
            await send_album_to_chat(app, chat_id, album)
            data["album"] = []


# -----------------------------
# post_init (Fix for async startup)
# -----------------------------
async def post_init(app):
    commands = [
        BotCommand("start", "Show your Telegram ID"),
        BotCommand("help", "Show help menu"),
        BotCommand("settings", "Show bot settings"),
        BotCommand("admin_commands", "Show admin commands"),
        BotCommand("toggle_photo_mode", "Toggle photo approval mode"),
        BotCommand("approve", "Approve pending photo"),
        BotCommand("reject", "Reject pending photo"),
        BotCommand("flush", "Flush remaining videos"),
    ]

    await app.bot.set_my_commands(commands)

    app.job_queue.run_repeating(
        lambda ctx: asyncio.create_task(batch_watcher(app)),
        interval=5,
        first=5
    )

    logger.info("Post-init tasks completed.")


# -----------------------------
# Main (Railway-safe)
# -----------------------------
def main():
    BOT_TOKEN = os.getenv("BOT_TOKEN")
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN environment variable is missing!")

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    # Commands
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("settings", settings))
    app.add_handler(CommandHandler("admin_commands", admin_commands))
    app.add_handler(CommandHandler("toggle_photo_mode", toggle_photo_mode))
    app.add_handler(CommandHandler("approve", approve))
    app.add_handler(CommandHandler("reject", reject))
    app.add_handler(CommandHandler("flush", flush))

    # Media handlers
    app.add_handler(MessageHandler(filters.VIDEO, handle_video))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))

    # Run async startup tasks AFTER loop starts
    app.post_init = post_init

    logger.info("Bot is now polling...")
    app.run_polling()


if __name__ == "__main__":
    main()
