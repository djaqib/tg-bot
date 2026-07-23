import os
import logging
import asyncio
import random
import time
from telegram import Update, InputMediaVideo
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler,
    ContextTypes, filters
)

# -----------------------------
# Logging
# -----------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[
        logging.FileHandler("bot.log"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# -----------------------------
# Admin ID (replace with yours)
# -----------------------------
ADMIN_ID = 123456789   # <-- CHANGE THIS TO YOUR TELEGRAM USER ID

# -----------------------------
# Global State
# -----------------------------
video_cache = set()
photo_approval_mode = False
batch_count = 0
last_video_time = 0
FLUSH_TIMEOUT = 60  # seconds


# -----------------------------
# Admin-only decorator
# -----------------------------
def admin_only(func):
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if update.effective_user.id != ADMIN_ID:
            await update.message.reply_text("Access denied.")
            return
        return await func(update, context)
    return wrapper


# -----------------------------
# Commands
# -----------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"Bot ready.\nYour Telegram ID is: {update.effective_user.id}"
    )


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
# Photo Handler
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
# Video Handler
# -----------------------------
@admin_only
async def handle_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global last_video_time, batch_count

    message = update.message

    # Auto-delete forwarded message
    try:
        await message.delete()
    except:
        pass

    if not message.video:
        return

    file_id = message.video.file_id

    # Dedup
    if file_id in video_cache:
        return

    video_cache.add(file_id)

    # Update timers
    last_video_time = time.time()
    batch_count += 1

    # Progress message
    await update.message.reply_text(f"Received {batch_count} videos…")

    # Album batching
    if "album" not in context.user_data:
        context.user_data["album"] = []

    context.user_data["album"].append(file_id)

    # Send album every 10 videos
    if len(context.user_data["album"]) >= 10:
        await send_album(update, context)


# -----------------------------
# Album Sending
# -----------------------------
async def send_album(update: Update, context: ContextTypes.DEFAULT_TYPE):
    album = context.user_data.get("album", [])
    if not album:
        return

    # Delay to avoid Telegram rate limits
    delay = random.uniform(2, 3)
    await asyncio.sleep(delay)

    media_group = [InputMediaVideo(media=fid) for fid in album]

    await update.message.reply_media_group(media_group)
    context.user_data["album"] = []


async def send_album_to_chat(app, chat_id, album):
    media_group = [InputMediaVideo(media=fid) for fid in album]
    await app.bot.send_media_group(chat_id, media_group)


# -----------------------------
# Auto-detect batch end watcher
# -----------------------------
async def batch_watcher(app):
    global last_video_time

    while True:
        await asyncio.sleep(5)
        now = time.time()

        for chat_id, data in list(app.chat_data.items()):
            album = data.get("album", [])
            if album and now - last_video_time >= FLUSH_TIMEOUT:
                try:
                    await app.bot.send_message(chat_id, "Batch ended. Sending remaining videos…")
                    await send_album_to_chat(app, chat_id, album)
                    data["album"] = []
                except Exception as e:
                    logger.error("Auto-batch flush failed: %s", e)


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
    app.add_handler(CommandHandler("toggle_photo_mode", toggle_photo_mode))
    app.add_handler(CommandHandler("approve", approve))
    app.add_handler(CommandHandler("reject", reject))
    app.add_handler(CommandHandler("flush", flush))

    # Media handlers
    app.add_handler(MessageHandler(filters.VIDEO, handle_video))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))

    # Start batch watcher
    app.job_queue.run_once(lambda ctx: asyncio.create_task(batch_watcher(app)), 1)

    logger.info("Bot is now polling...")
    app.run_polling()


if __name__ == "__main__":
    main()
