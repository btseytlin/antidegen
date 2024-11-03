import asyncio
import html
import http.server
import json
import logging
import os
import socketserver
import traceback
from collections import Counter
from threading import Thread

import google.generativeai as genai
from telegram import Update
from telegram.constants import ParseMode
from telegram.error import TelegramError
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

logging.basicConfig(level=logging.INFO)

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]

ADMIN_ID = int(os.environ["ADMIN_ID"])
TARGET_GROUP_ID = int(os.environ["TARGET_GROUP_ID"])
WHITELIST_IDS = [int(ADMIN_ID), int(TARGET_GROUP_ID)] + [
    int(id) for id in os.environ["WHITELIST_IDS"].split(",")
]


genai.configure(api_key=GEMINI_API_KEY)


system_instruction = """
You are an anti-spam moderator bot for a Telegram channel comments. You will be shown comment info from Telegram API as a json/dict. Classify the comment as spam or not spam.

Categories of spam:
-   Links aiming to sell something.
-   Bait messages luring a user to check the spammer account profile.
-   Comment posted after publication of a post impossibly quickly for a human. The difference between post date and comment date is stored in the "comment_delay_seconds" field.
-   Content unrelated to the post the comment is replying, "reply_to_message" field contains a sample of the post if present.
-   Porn, prostitution, gambling, crypto/NFT, get rich quick schemes and such.
Anything else is not spam. When in doubt, classify as not spam. We don't want to ruin the experience for legitimate commenters.

Answer format: 
1. If spam: `{"why": "explanation", "spam": true}`. "why" should explain your reason in 4 words or less.
2. If not spam: `{"spam": false}`
Output as plain string, no formatting. For example:{"why": "bait link", "spam": true}
""".strip()


prompt_template = """
Comment: {comment_dict}\n
Answer:"""


async def start(update: Update, context):
    await update.message.reply_text("Bot is running and monitoring comments.")


def retry(func, max_retries=3):
    def wrapper(*args, **kwargs):
        for i in range(max_retries):
            try:
                return func(*args, **kwargs)
            except Exception as e:
                logging.error(f"Retrying {func.__name__} failed: {e}")
                if i == max_retries - 1:
                    raise e

    return wrapper


def call_model_stack(prompt, stack=["gemini-1.5-pro", "gemini-1.5-flash-002"]):
    for i, model_name in enumerate(stack):
        try:
            model = genai.GenerativeModel(
                model_name,
                system_instruction=system_instruction,
                generation_config=genai.GenerationConfig(
                    max_output_tokens=30,
                ),
            )
            response = model.generate_content(prompt)
            return response
        except Exception as e:
            logging.error(f"Failed to call {model_name}: {e}")
            if i == len(stack) - 1:
                logging.error(f"All models failed: {e}")
                raise e
            continue


@retry
async def check_spam(user_dict, comment_dict):
    comment_dict = dict(comment_dict)

    prompt = prompt_template.format(
        comment_dict=json.dumps(comment_dict, ensure_ascii=False)
    )
    response = call_model_stack(prompt)
    result = response.text.strip().lower()
    result = json.loads(result, strict=False)
    return prompt, result


def truncate(text, max_length):
    if len(text) > max_length:
        return text[:max_length] + "..."
    return text


async def get_comment_info(
    update: Update, context, treat_forward_origin_as_sender=False
):
    user_dict = None
    if treat_forward_origin_as_sender:
        if hasattr(update.message, "forward_origin") and update.message.forward_origin:
            if hasattr(update.message.forward_origin, "sender_user"):
                user_dict = update.message.forward_origin.sender_user.to_dict()

    if user_dict is None:
        if hasattr(update.message, "sender_chat") and update.message.sender_chat:
            user_dict = update.message.sender_chat.to_dict()
        else:
            user = update.message.from_user
            user_dict = user.to_dict()

    assert user_dict is not None, f"Failed to get user dict for {update.message}"
    comment_dict = update.message.to_dict()

    delete_fields = [
        "link_preview_options",
        "chat",
        "group_chat_created",
        "channel_chat_created",
        "delete_chat_photo",
        "supergroup_chat_created",
        "message_id",
        "forward_origin",
        "forward_date",
        "forward_from",
        "forward_from_chat",
        "from",
        "entities",
        "message_thread_id",
    ]
    for field in delete_fields:
        comment_dict.pop(field, None)

    comment_dict["from_user"] = dict(user_dict)
    comment_dict["from_user"].pop("is_bot", None)
    comment_dict["from_user"].pop("id", None)

    if comment_dict.get("reply_to_message"):
        if "caption" in comment_dict["reply_to_message"]:
            comment_dict["reply_to_message"]["caption"] = truncate(
                comment_dict["reply_to_message"]["caption"], 500
            )
        if "text" in comment_dict["reply_to_message"]:
            if "caption" in comment_dict["reply_to_message"]:
                del comment_dict["reply_to_message"]["caption"]
            comment_dict["reply_to_message"]["text"] = truncate(
                comment_dict["reply_to_message"]["text"], 500
            )

        drop_keys = []
        for k in comment_dict["reply_to_message"]:
            if k not in ["caption", "date"]:
                drop_keys.append(k)
        logging.info(f"Dropping keys: {drop_keys}")
        for k in drop_keys:
            del comment_dict["reply_to_message"][k]

    if comment_dict.get("reply_to_message") and comment_dict.get("date"):
        comment_delay = comment_dict["date"] - comment_dict["reply_to_message"]["date"]
        comment_dict["comment_delay_seconds"] = comment_delay
    if "text" in comment_dict:
        comment_dict["text"] = truncate(comment_dict["text"], 1000)
    return user_dict, comment_dict


async def send_to(
    context,
    chat_id,
    content: list[str | dict],
):
    message = []

    for item in content:
        if isinstance(item, str):
            message.append(item)
        else:
            message.append(
                f"<pre>{html.escape(json.dumps(item, ensure_ascii=False, indent=2))}</pre>"
            )

    message = "\n".join(message)
    return await context.bot.send_message(
        chat_id=chat_id, text=message, parse_mode=ParseMode.HTML
    )


async def handle_comment(update: Update, context):
    if update.effective_chat.id != int(TARGET_GROUP_ID):
        return

    user_dict, comment_dict = await get_comment_info(update, context)
    user_id = user_dict["id"]

    logging.info(f"Comment {user_dict}, {comment_dict}")

    if user_id in WHITELIST_IDS:
        logging.info(f"User {user_dict} is whitelisted.")
        return

    if user_dict.get("is_premium") is True:
        logging.info(f"User {user_dict} is premium.")
        return

    prompt, result = await check_spam(user_dict, comment_dict)
    logging.info(f"Prompt:\n{prompt}\n\nResult:\n{result}")

    if result["spam"]:
        await send_to(
            context,
            ADMIN_ID,
            content=[
                "Spam detected",
                "User",
                user_dict,
                "Comment",
                comment_dict,
                "Result",
                result,
            ],
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

    user_dict, comment_dict = await get_comment_info(
        update, context, treat_forward_origin_as_sender=True
    )
    user_id = user_dict["id"]

    if user_id in WHITELIST_IDS:
        await update.message.reply_text("User is whitelisted.")

    prompt, result = await check_spam(user_dict, comment_dict)

    await send_to(
        context,
        update.effective_chat.id,
        content=[
            "User",
            user_dict,
            "Comment",
            comment_dict,
            "Prompt",
            "---",
            prompt,
            "---",
            "Result",
            result,
        ],
    )


async def error_handler(update: object, context) -> None:
    """Log the error and send a telegram message to notify the developer."""
    logging.error(msg="Exception while handling an update:", exc_info=context.error)

    tb_list = traceback.format_exception(
        None, context.error, context.error.__traceback__
    )
    tb_string = "".join(tb_list)

    update_dict = update.to_dict() if isinstance(update, Update) else {}
    if "message" in update_dict:
        if "text" in update_dict["message"]:
            update_dict["message"]["text"] = truncate(
                update_dict["message"]["text"], 200
            )

    update_dict = update_dict or str(update)

    await send_to(
        context,
        ADMIN_ID,
        content=[
            "Exception",
            "Update",
            update_dict,
            "Traceback",
            f"<pre>{html.escape(tb_string[-300:])}</pre>",
        ],
    )

    if isinstance(context.error, TelegramError):
        logging.error(f"Telegram error received, killing process.")
        exit(1)


class HealthCheckHandler(http.server.SimpleHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain")
        self.end_headers()
        self.wfile.write(b"OK")


def run_health_check_server():
    with socketserver.TCPServer(("", 8080), HealthCheckHandler) as httpd:
        print("Health check server running on port 8080")
        httpd.serve_forever()


def main():
    # Start health check server in a separate thread
    health_check_thread = Thread(target=run_health_check_server, daemon=True)
    health_check_thread.start()

    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(MessageHandler(filters.ChatType.GROUPS, handle_comment))
    application.add_handler(
        MessageHandler(
            filters.ChatType.PRIVATE,
            handle_private_message,
        )
    )
    application.add_error_handler(error_handler)
    logging.info("Starting bot")
    application.run_polling()


if __name__ == "__main__":
    main()
