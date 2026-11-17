from functools import wraps
from telegram import Update
from telegram.ext import ContextTypes

ADMIN_IDS = [8271828643]  # Replace with your actual ID

def admin_only(func):
    @wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        user_id = update.effective_user.id
        if user_id not in ADMIN_IDS:
            await update.message.reply_text("⛔ This command is restricted to admins.")
            return
        return await func(update, context, *args, **kwargs)
    return wrapper
