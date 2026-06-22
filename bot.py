import os
import logging
from telegram import Update, BotCommand
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters

logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ["BOT_TOKEN"]
WEBHOOK_SECRET = os.environ["WEBHOOK_SECRET"]
RENDER_EXTERNAL_URL = os.environ["RENDER_EXTERNAL_URL"]
PORT = int(os.environ.get("PORT", 10000))

# Per-user storage: user_id -> list of file_ids (videos only)
user_files: dict[int, list[str]] = {}

# Per-user dedup tracking: user_id -> set of unique video identifiers already saved.
# Telegram videos carry a "file_unique_id" that's stable across forwards/resends of
# the exact same file (unlike file_id, which can change). That's what we dedupe on.
seen_unique_ids: dict[int, set[str]] = {}

HELP_TEXT = (
    "🎬 *Video Collector Bot*\n\n"
    "Send or forward videos to me and I'll collect them. "
    "Photos and other file types are ignored. Duplicate videos "
    "(already sent before) are skipped automatically.\n\n"
    "*Commands:*\n"
    "/start \\- Show welcome message\n"
    "/help \\- Show this help message\n"
    "/done \\- Send back all collected videos and clear the list\n"
    "/clear \\- Wipe collected videos without sending them\n"
    "/status \\- Show how many videos are currently saved"
)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("👋 Send me videos and I'll collect them. Use /help to see all commands.")


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(HELP_TEXT, parse_mode="MarkdownV2")


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    count = len(user_files.get(user_id, []))
    await update.message.reply_text(f"📦 {count} video(s) currently saved.")


async def handle_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    video = update.message.video

    unique_id = video.file_unique_id
    seen = seen_unique_ids.setdefault(user_id, set())

    if unique_id in seen:
        await update.message.reply_text("⚠️ Already got that one, skipping duplicate.")
        return

    seen.add(unique_id)
    user_files.setdefault(user_id, []).append(video.file_id)
    await update.message.reply_text("✅ Saved")


async def handle_non_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Photos, regular documents, audio, etc. land here and are silently ignored.
    # If you'd rather the bot speak up, uncomment the line below.
    # await update.message.reply_text("🚫 Only videos are accepted.")
    return


async def done(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    files = user_files.get(user_id, [])

    if not files:
        await update.message.reply_text("No videos found ❌")
        return

    for f in files:
        await context.bot.send_video(chat_id=update.effective_chat.id, video=f)

    await update.message.reply_text(f"📤 Done — sent {len(files)} video(s)")
    user_files.pop(user_id, None)
    seen_unique_ids.pop(user_id, None)  # reset dedup tracking too, fresh batch next time


async def clear(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user_files.pop(user_id, None)
    seen_unique_ids.pop(user_id, None)
    await update.message.reply_text("🗑️ Cleared")


async def post_init(application: Application):
    # Registers the command list so Telegram shows the "/" menu button with
    # descriptions in the chat UI (tap the menu icon next to the message box).
    await application.bot.set_my_commands([
        BotCommand("start", "Show welcome message"),
        BotCommand("help", "Show help and command list"),
        BotCommand("done", "Send back collected videos"),
        BotCommand("clear", "Wipe collected videos"),
        BotCommand("status", "Show how many videos are saved"),
    ])


def main():
    application = Application.builder().token(BOT_TOKEN).post_init(post_init).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("done", done))
    application.add_handler(CommandHandler("clear", clear))
    application.add_handler(CommandHandler("status", status))
    application.add_handler(MessageHandler(filters.VIDEO, handle_video))
    application.add_handler(MessageHandler(
        filters.PHOTO | filters.Document.ALL | filters.AUDIO | filters.VOICE,
        handle_non_video,
    ))

    application.run_webhook(
        listen="0.0.0.0",
        port=PORT,
        secret_token=WEBHOOK_SECRET,
        webhook_url=f"{RENDER_EXTERNAL_URL}/webhook",
        url_path="webhook",
    )


if __name__ == "__main__":
    main()
