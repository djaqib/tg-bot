import os
import logging
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters

logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ["BOT_TOKEN"]
WEBHOOK_SECRET = os.environ["WEBHOOK_SECRET"]  # set this yourself, e.g. a random 32-char string
RENDER_EXTERNAL_URL = os.environ["RENDER_EXTERNAL_URL"]  # auto-set by Render
PORT = int(os.environ.get("PORT", 10000))

# In-memory store. Fine for a quick personal bot, but read the note at the bottom
# of the chat reply about why this won't survive restarts/sleep on Render free.
user_files: dict[int, list[str]] = {}


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("👋 Bot is alive!")


async def handle_files(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    file_id = None

    if update.message.photo:
        file_id = update.message.photo[-1].file_id
    elif update.message.document:
        file_id = update.message.document.file_id

    if not file_id:
        return

    user_files.setdefault(user_id, []).append(file_id)
    await update.message.reply_text("✅ Saved")


async def done(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    files = user_files.get(user_id, [])

    if not files:
        await update.message.reply_text("No files found ❌")
        return

    for f in files:
        await context.bot.send_document(chat_id=update.effective_chat.id, document=f)

    await update.message.reply_text("📤 Done")
    user_files.pop(user_id, None)  # clear after sending, otherwise /done re-sends old files forever


def main():
    application = Application.builder().token(BOT_TOKEN).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("done", done))
    application.add_handler(MessageHandler(filters.PHOTO | filters.Document.ALL, handle_files))

    application.run_webhook(
        listen="0.0.0.0",
        port=PORT,
        secret_token=WEBHOOK_SECRET,
        webhook_url=f"{RENDER_EXTERNAL_URL}/webhook",
        url_path="webhook",
    )


if __name__ == "__main__":
    main()
