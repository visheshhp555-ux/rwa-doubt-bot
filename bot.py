import os
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ChatMemberHandler,
    ContextTypes,
    filters,
)

BOT_TOKEN = os.getenv("BOT_TOKEN")

RULES = """
📜 *Group Rules*

1. Respect everyone.
2. No Spam.
3. No Abuse.
4. Study Related Messages Only.
5. No Promotion.
"""

WELCOME = """
🎉 Welcome {name} ❤️

📚 Welcome to our Study Group.

Please read the rules below.
"""

GOODBYE = """
👋 Goodbye {name}

Best of Luck ❤️
"""

BAD_WORDS = [
    "mc","bc","madarchod","bhenchod","gandu"
]


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🤖 Bot Online Successfully!")


async def rules(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(RULES, parse_mode="Markdown")


async def welcome(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.chat_member.new_chat_member.status == "member":
        user = update.chat_member.new_chat_member.user

        await context.bot.send_message(
            update.effective_chat.id,
            WELCOME.format(name=user.first_name)
        )

        await context.bot.send_message(
            update.effective_chat.id,
            RULES,
            parse_mode="Markdown"
        )


async def goodbye(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.chat_member.new_chat_member.status == "left":
        user = update.chat_member.old_chat_member.user

        await context.bot.send_message(
            update.effective_chat.id,
            GOODBYE.format(name=user.first_name)
        )


async def filter_bad_words(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message is None:
        return

    text = update.message.text.lower()

    for word in BAD_WORDS:
        if word in text:
            try:
                await update.message.delete()
            except:
                pass
            return


def main():
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("rules", rules))

    app.add_handler(
        ChatMemberHandler(
            welcome,
            ChatMemberHandler.CHAT_MEMBER
        )
    )

    app.add_handler(
        ChatMemberHandler(
            goodbye,
            ChatMemberHandler.CHAT_MEMBER
        )
    )

    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            filter_bad_words
        )
    )

    print("Bot Running...")

    app.run_polling()


if __name__ == "__main__":
    main()
