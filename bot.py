import asyncio
import logging
import os
from collections import Counter

import google.generativeai as genai
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters

logging.basicConfig(level=logging.INFO)

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]

ADMIN_ID = int(os.environ["ADMIN_ID"])
TARGET_GROUP_ID = int(os.environ["TARGET_GROUP_ID"])
WHITELIST_IDS = [int(ADMIN_ID), int(TARGET_GROUP_ID)] + [
    int(id) for id in os.environ["WHITELIST_IDS"].split(",")
]


genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel(
    "gemini-1.5-flash-002",
    system_instruction="""You are a moderator bot for a Telegram channel comments. 
You will be shown a user comment. Answer with "spam" or "no_spam". Just this string and nothing else.
""",
    generation_config=genai.GenerationConfig(
        max_output_tokens=20,
    ),
)

prompt_template = """
Username: {username}
Full Name: {user_name}
Comment: "{comment}"\n
Is this spam?"""


async def start(update: Update, context):
    await update.message.reply_text("Bot is running and monitoring comments.")


async def check_spam(user_name, username, comment):
    prompt = prompt_template.format(
        user_name=user_name[:100],
        username=username[:100],
        comment=comment[:500],
    )
    response = model.generate_content(prompt)
    result = response.text.strip().lower()
    return prompt, result


async def get_comment_info(update: Update, context):
    logging.info(update)
    logging.info(update.message)
    logging.info(update.message.from_user)

    if hasattr(update.message, "sender_chat") and update.message.sender_chat:
        user_id = update.message.sender_chat.id
        user_name = update.message.sender_chat.title
        username = update.message.sender_chat.username
    else:
        user = update.message.from_user
        user_id = user.id
        user_name = user.full_name
        username = f"@{user.username}" if user.username else "<No username>"
    comment = update.message.text

    return user_id, user_name, username, comment


async def handle_comment(update: Update, context):

    if update.effective_chat.id != int(TARGET_GROUP_ID):
        return

    user = update.message.from_user

    user_id, user_name, username, comment = await get_comment_info(update, context)

    logging.info(f"Comment from {user_name} @{username} {user_id}")

    if user_id in WHITELIST_IDS:
        logging.info(f"User {user_name} @{username} is whitelisted.")
        return

    prompt, result = await check_spam(user_name, username, comment)
    logging.info(f"Prompt:\n{prompt}\n\nResult:\n{result}")

    if result == "spam":
        await context.bot.send_message(
            chat_id=ADMIN_ID,
            text=f"Spam detected from {user_name} @{username}",
        )
        await context.bot.forward_message(
            chat_id=ADMIN_ID,
            from_chat_id=update.effective_chat.id,
            message_id=update.message.message_id,
        )
        # await update.message.delete()
        # await context.bot.ban_chat_member(update.message.chat_id, user.id)


async def handle_private_message(update: Update, context):
    if update.effective_chat.id != int(ADMIN_ID):
        return

    user_id, user_name, username, comment = await get_comment_info(update, context)

    if user_id in WHITELIST_IDS:
        await update.message.reply_text("User is whitelisted.")

    prompt, result = await check_spam(user_name, username, comment)
    await update.message.reply_text(f"Prompt:\n{prompt}\n\nResult:\n{result}")


def main():
    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(MessageHandler(filters.ChatType.GROUPS, handle_comment))
    application.add_handler(
        MessageHandler(
            filters.ChatType.PRIVATE,
            handle_private_message,
        )
    )
    logging.info("Starting bot")
    application.run_polling()


if __name__ == "__main__":
    main()
    main()
