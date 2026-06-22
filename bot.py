import os

from flask import Flask, request
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

BOT_TOKEN = os.getenv("BOT_TOKEN")
PORT = int(os.environ.get("PORT", 10000))

app_flask = Flask(__name__)

user_files = {}

telegram_app = Application.builder().token(BOT_TOKEN).build()


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        " Send me photos/files and I'll store them."
    )


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

    await update.message.reply_text(" Saved")


async def done(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    files = user_files.get(user_id, [])

    if not files:
        await update.message.reply_text("No files found ")
        return

    for f in files:
        await context.bot.send_document(
            chat_id=update.effective_chat.id,
            document=f
        )

    await update.message.reply_text(" Done sending all files")


telegram_app.add_handler(CommandHandler("start", start))
telegram_app.add_handler(CommandHandler("done", done))

telegram_app.add_handler(
    MessageHandler(
        filters.PHOTO | filters.Document.ALL,
        handle_files
    )
)


@app_flask.route("/", methods=["GET"])
def home():
    return "Bot is running!"


if __name__ == "__main__":
    app_flask.run(host="0.0.0.0", port=PORT)