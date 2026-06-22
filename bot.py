import os
from flask import Flask, request
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters

BOT_TOKEN = os.getenv("BOT_TOKEN")

app = Flask(__name__)
telegram_app = Application.builder().token(BOT_TOKEN).build()

user_files = {}


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("👋 Bot is alive! Send me files.")


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
        await context.bot.send_document(
            chat_id=update.effective_chat.id,
            document=f
        )

    await update.message.reply_text("📤 Done sending all files")


telegram_app.add_handler(CommandHandler("start", start))
telegram_app.add_handler(CommandHandler("done", done))
telegram_app.add_handler(MessageHandler(filters.PHOTO | filters.Document.ALL, handle_files))


@app.route("/", methods=["GET"])
def home():
    return "Bot is running"


# ⭐ THIS IS THE MISSING PIECE
@app.route("/", methods=["POST"])
async def webhook():
    update = Update.de_json(request.get_json(), telegram_app.bot)
    await telegram_app.process_update(update)
    return "ok"