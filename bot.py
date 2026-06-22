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

# Shared pool: every account that talks to this bot adds to and reads from the
# same collection. Not split by user_id anymore.
shared_files: list[str] = []

# Dedup tracking for the shared pool. Telegram videos carry a "file_unique_id"
# that's stable across forwards/resends of the exact same file (unlike file_id,
# which can change). That's what we dedupe on.
seen_unique_ids: set[str] = set()

HELP_TEXT = (
    "🎬 *Video Collector Bot*\n\n"
    "Send or forward videos to me and I'll collect them into one shared pool\\. "
    "Anyone who messages this bot can add videos to or retrieve from the same collection\\. "
    "Photos and other file types are ignored\\. Duplicate videos "
    "\\(already sent before\\) are skipped automatically\\.\n\n"
    "*Commands:*\n"
    "/start \\- Show welcome message\n"
    "/help \\- Show this help message\n"
    "/done \\- Send back all collected videos and clear the pool\n"
    "/clear \\- Wipe the pool without sending\n"
    "/status \\- Show how many videos are currently saved"
)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("👋 Send me videos and I'll collect them. Use /help to see all commands.")


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(HELP_TEXT, parse_mode="MarkdownV2")


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"📦 {len(shared_files)} video(s) currently saved.")


async def handle_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    video = update.message.video
    unique_id = video.file_unique_id

    if unique_id in seen_unique_ids:
        await update.message.reply_text("⚠️ Already got that one, skipping duplicate.")
        return

    seen_unique_ids.add(unique_id)
    shared_files.append(video.file_id)
    await update.message.reply_text("✅ Saved")


async def handle_non_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Photos, regular documents, audio, etc. land here and are silently ignored.
    # If you'd rather the bot speak up, uncomment the line below.
    # await update.message.reply_text("🚫 Only videos are accepted.")
    return


async def done(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not shared_files:
        await update.message.reply_text("No videos found ❌")
        return

    for f in shared_files:
        await context.bot.send_video(chat_id=update.effective_chat.id, video=f)

    await update.message.reply_text(f"📤 Done — sent {len(shared_files)} video(s)")
    shared_files.clear()
    seen_unique_ids.clear()  # reset dedup tracking too, fresh batch next time


async def clear(update: Update, context: ContextTypes.DEFAULT_TYPE):
    shared_files.clear()
    seen_unique_ids.clear()
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
