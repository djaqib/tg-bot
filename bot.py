import os
import logging
import asyncio
import random
from telegram import Update, InputMediaVideo
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler,
    ContextTypes, filters
)

# --- Logging Setup ---
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[
        logging.FileHandler("bot.log"),
        logging.StreamHandler()
    ]
)

logger = logging.getLogger(__name__)

# In-memory storage
video_cache = set()
photo_approval_mode = False


# --- Commands ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.info("User %s started the bot", update.effective_user.id)
    await update.message.reply_text("Bot ready. Forward videos and I'll clean them.")


async def toggle_photo_mode(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global photo_approval_mode
    photo_approval_mode = not photo_approval_mode
    status = "ON" if photo_approval_mode else "OFF"
    logger.info("Photo approval mode toggled to %s", status)
    await update.message.reply_text(f"Photo approval mode is now {status}")


# --- Core Logic ---

async def handle_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message

    # Auto-delete forwarded message
    try:
        await message.delete()
        logger.info("Deleted forwarded message from chat %s", update.effective_chat.id)
    except Exception as e:
        logger.error("Failed to delete message: %s", e)

    if not message.video:
        return

    file_id = message.video.file_id
    logger.info("Received video: %s", file_id)

    # Dedup check
    if file_id in video_cache:
        logger.info("Duplicate video ignored: %s", file_id)
        await update.message.reply_text("Duplicate video ignored.")
        return

    video_cache.add(file_id)
    logger.info("Video added to cache: %s", file_id)

    # Album batching
    if "album" not in context.user_data:
        context.user_data["album"] = []

    context.user_data["album"].append(file_id)
    logger.info("Album size now %d", len(context.user_data["album"]))

    if len(context.user_data["album"]) >= 10:
        await send_album(update, context)
    else:
        await update.message.reply_video(video=file_id)
        logger.info("Sent clean single video: %s", file_id)


async def send_album(update: Update, context: ContextTypes.DEFAULT_TYPE):
    album = context.user_data.get("album", [])
    if not album:
        return

    logger.info("Preparing to send album of %d videos", len(album))

    # Delay to avoid rate limits
    delay = random.uniform(2, 3)
    logger.info("Sleeping for %.2f seconds before sending album", delay)
    await asyncio.sleep(delay)

    media_group = [InputMediaVideo(media=file_id) for file_id in album]

    try:
        await update.message.reply_media_group(media_group)
        logger.info("Album sent successfully")
    except Exception as e:
        logger.error("Failed to send album: %s", e)

    context.user_data["album"] = []


# --- Photo Handling ---

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global photo_approval_mode

    file_id = update.message.photo[-1].file_id
    logger.info("Received photo: %s", file_id)

    if not photo_approval_mode:
        await update.message.reply_photo(file_id)
        logger.info("Auto-sent clean photo: %s", file_id)
        return

    context.user_data["pending_photo"] = file_id
    logger.info("Photo pending approval: %s", file_id)

    await update.message.reply_text("Photo received. Approve or reject?")


async def approve(update: Update, context: ContextTypes.DEFAULT_TYPE):
    file_id = context.user_data.get("pending_photo
