from functools import wraps
from telegram import Update
from telegram.ext import ContextTypes

# List of your Telegram user IDs (integers)
ADMIN_IDS = [123456789, 8271828643]

def admin_only(func):
    @wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        user_id = update.effective_user.id
        if user_id not in ADMIN_IDS:
            await update.message.reply_text("⛔ This command is restricted to bot administrators.")
            return
        return await func(update, context, *args, **kwargs)
    return wrapper

# How to use it:
# @admin_only
# async def secret_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
#     await update.message.reply_text("Welcome to the admin panel!")
