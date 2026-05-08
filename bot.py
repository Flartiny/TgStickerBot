import asyncio
import logging
import os
import re
import tempfile
import time

from telegram import Update, InputSticker, BotCommand
from telegram.constants import ChatAction, StickerFormat
from telegram.ext import (
    Application,
    CommandHandler,
    ConversationHandler,
    MessageHandler,
    filters,
    ContextTypes,
)

from processor import (
    ProcessingError,
    NoGifsFoundError,
    TooManyGifsError,
    ZipBombError,
    process_zip,
    cleanup_work_dir,
)

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

DEFAULT_EMOJI = "\u2b50"
PROGRESS_UPDATE_INTERVAL = 1.5

WAIT_ZIP_ADD = 0
WAIT_NAME, WAIT_TITLE, WAIT_ZIP_NEW = 1, 2, 3

HELP_TEXT = (
    "\U0001f9e9 Send me a <b>ZIP file</b> containing GIF images, "
    "and I'll turn them into a Telegram animated sticker pack.\n\n"
    "<b>Quick create (auto name):</b>\n"
    "  Send a ZIP — caption becomes the pack title\n\n"
    "<b>Interactive create (custom name):</b>\n"
    "  /newpack — I'll guide you step by step\n\n"
    "<b>Add to an existing pack:</b>\n"
    "  /add <code>pack_name</code> or /add <code>pack_url</code>\n"
    "  Then send the ZIP\n\n"
    "<b>Limits:</b>\n"
    "- Max 120 GIFs per pack\n"
    "- ZIP uncompressed size limit: 300MB\n"
    "- GIFs auto-cropped to 3 seconds, resized to 512px"
)

SUPPORTED_MIME = {
    "application/zip",
    "application/x-zip-compressed",
    "application/x-zip",
    "multipart/x-zip",
}


def _sanitize_name(raw: str) -> str:
    name = raw.strip().lower()
    name = re.sub(r"[^a-z0-9_]", "_", name)
    name = re.sub(r"_+", "_", name)
    name = name.strip("_")
    if not name:
        name = "stickers"
    return name[:32]


def _parse_pack_ref(text: str, bot_username: str) -> str | None:
    text = text.strip()
    if not text:
        return None
    if "/" in text:
        m = re.search(r"addstickers/([a-zA-Z0-9_]+)", text)
        if m:
            return m.group(1)
        return None
    if f"_by_{bot_username}" in text:
        return text
    return f"{text}_by_{bot_username}"



def _progress_bar(current: int, total: int, width: int = 12) -> str:
    filled = int(width * current / total) if total else 0
    empty = width - filled
    return f"[{'█' * filled}{'░' * empty}]"


def _load_stickers(webm_paths: list[str]) -> list[InputSticker]:
    stickers = []
    for p in webm_paths:
        with open(p, "rb") as f:
            data = f.read()
        stickers.append(InputSticker(data, [DEFAULT_EMOJI], StickerFormat.VIDEO))
    return stickers


async def _create_sticker_pack(
    context: ContextTypes.DEFAULT_TYPE,
    user_id: int,
    pack_name: str,
    pack_title: str,
    input_stickers: list[InputSticker],
) -> None:
    await context.bot.create_new_sticker_set(
        user_id=user_id,
        name=pack_name,
        title=pack_title[:64],
        stickers=[input_stickers[0]],
    )
    for sticker in input_stickers[1:]:
        await context.bot.add_sticker_to_set(
            user_id=user_id,
            name=pack_name,
            sticker=sticker,
        )
        await asyncio.sleep(0.1)


async def _add_to_existing_pack(
    context: ContextTypes.DEFAULT_TYPE,
    user_id: int,
    pack_name: str,
    input_stickers: list[InputSticker],
) -> None:
    for sticker in input_stickers:
        await context.bot.add_sticker_to_set(
            user_id=user_id,
            name=pack_name,
            sticker=sticker,
        )
        await asyncio.sleep(0.1)


async def _build_full_pack_name(context: ContextTypes.DEFAULT_TYPE, name_base: str) -> str:
    me = await context.bot.get_me()
    bot_username = me.username
    full = f"{name_base}_by_{bot_username}"
    if len(full) > 64:
        name_base = name_base[: 64 - len(f"_by_{bot_username}") - 1]
        full = f"{name_base}_by_{bot_username}"
    return full


async def _run_processing_pipeline(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    status_msg,
    work_dir: str,
    zip_path: str,
    pack_name_base: str,
    pack_title: str,
    existing_pack_name: str | None,
) -> None:
    last_update = [0.0]

    async def on_progress(current: int, total: int, status: str) -> None:
        now = time.monotonic()
        if now - last_update[0] < PROGRESS_UPDATE_INTERVAL and current < total:
            return
        last_update[0] = now
        bar = _progress_bar(current, total)
        text = f"{status}\n{bar} {current}/{total}"
        try:
            await status_msg.edit_text(text)
        except Exception:
            pass

    webm_files = await process_zip(zip_path, work_dir, on_progress)
    input_stickers = _load_stickers(webm_files)

    if not input_stickers:
        raise ProcessingError("No stickers were generated")

    action_text = (
        f"Adding {len(webm_files)} stickers to existing pack..."
        if existing_pack_name
        else f"Creating sticker pack for {len(webm_files)} stickers..."
    )
    await status_msg.edit_text(action_text)
    await update.message.chat.send_action(ChatAction.TYPING)

    if existing_pack_name:
        await _add_to_existing_pack(
            context, update.effective_user.id, existing_pack_name, input_stickers
        )
        pack_link = f"https://t.me/addstickers/{existing_pack_name}"
        await status_msg.edit_text(
            f"\u2705 {len(webm_files)} stickers added to "
            f"<a href='{pack_link}'>existing pack</a>",
            disable_web_page_preview=False,
        )
    else:
        full_pack_name = await _build_full_pack_name(context, pack_name_base)
        await _create_sticker_pack(
            context, update.effective_user.id, full_pack_name, pack_title, input_stickers
        )
        pack_link = f"https://t.me/addstickers/{full_pack_name}"
        await status_msg.edit_text(
            f"\u2705 Sticker pack <b>{pack_title}</b> created!\n\n"
            f"{len(webm_files)} stickers added.\n"
            f"\U0001f517 <a href='{pack_link}'>Add stickers</a>",
            disable_web_page_preview=False,
        )


# ---- Error wrapper for ZIP processing ----


async def _process_zip_safely(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    status_msg,
    pack_name_base: str,
    pack_title: str,
    existing_pack_name: str | None,
) -> None:
    doc = update.message.document
    work_dir = tempfile.mkdtemp(prefix="tgstickers_")
    try:
        tg_file = await context.bot.get_file(doc.file_id)
        zip_path = os.path.join(work_dir, "input.zip")
        await tg_file.download_to_drive(zip_path)

        await _run_processing_pipeline(
            update, context, status_msg, work_dir, zip_path,
            pack_name_base, pack_title, existing_pack_name,
        )
    except NoGifsFoundError:
        await status_msg.edit_text("No GIF files found in the ZIP archive.")
    except TooManyGifsError as e:
        await status_msg.edit_text(str(e))
    except ZipBombError:
        await status_msg.edit_text("ZIP file too large when extracted (limit: 300MB).")
    except ProcessingError as e:
        await status_msg.edit_text(f"Processing failed: {e}")
    except Exception as e:
        logger.exception("Unexpected error")
        await status_msg.edit_text(f"Something went wrong.\nError: {e}\nPlease try again.")
    finally:
        cleanup_work_dir(work_dir)


def _is_zip(doc) -> bool:
    mime = doc.mime_type or ""
    fname = (doc.file_name or "").lower()
    return mime in SUPPORTED_MIME or fname.endswith(".zip")


def _zip_too_large(doc) -> bool:
    return bool(doc.file_size and doc.file_size > 20 * 1024 * 1024)


# ---- Conversation: /newpack ----


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_html(HELP_TEXT)


async def newpack_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text(
        "Let's create a new sticker pack.\n\n"
        "Step 1/3: What should the <b>link ending</b> be?\n\n"
        "This goes in the URL: t.me/addstickers/<b>???</b>_by_bot\n"
        "Only English letters, digits, and underscores.\n"
        "Example: <code>cat_memes</code>"
    )
    return WAIT_NAME


async def newpack_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    raw = update.message.text.strip() if update.message.text else ""
    name_base = _sanitize_name(raw)

    full = await _build_full_pack_name(context, name_base)
    context.user_data["new_name_base"] = name_base

    await update.message.reply_text(
        f"Link will be: <code>t.me/addstickers/{full}</code>\n\n"
        f"Step 2/3: Now give it a <b>display title</b>.\n"
        f"Example: <code>Funny Cat Memes</code>"
    )
    return WAIT_TITLE


async def newpack_title(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    title = update.message.text.strip() if update.message.text else "Stickers"
    context.user_data["new_title"] = title

    name_base = context.user_data.get("new_name_base", "")
    full = await _build_full_pack_name(context, name_base)

    await update.message.reply_text(
        f"\U0001f4ce Summary:\n"
        f"  Link: <code>t.me/addstickers/{full}</code>\n"
        f"  Title: <b>{title}</b>\n\n"
        f"Step 3/3: Send me the <b>ZIP file</b> with GIFs now."
    )
    return WAIT_ZIP_NEW


async def newpack_zip(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    doc = update.message.document
    if not doc:
        await update.message.reply_text("Please send a ZIP file, or /cancel to abort.")
        return WAIT_ZIP_NEW

    if not _is_zip(doc):
        await update.message.reply_text("That doesn't look like a ZIP. Send a ZIP or /cancel.")
        return WAIT_ZIP_NEW

    if _zip_too_large(doc):
        await update.message.reply_text("File too large (20MB limit).")
        return WAIT_ZIP_NEW

    name_base = context.user_data.get("new_name_base")
    title = context.user_data.get("new_title")
    if not name_base:
        await update.message.reply_text("Session expired. Use /newpack again.")
        return ConversationHandler.END

    status_msg = await update.message.reply_text("Downloading ZIP file...")
    await update.message.chat.send_action(ChatAction.TYPING)

    await _process_zip_safely(
        update, context, status_msg, name_base, title, existing_pack_name=None,
    )
    return ConversationHandler.END


# ---- Conversation: /add ----


async def add_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    me = await context.bot.get_me()
    raw = update.message.text[len("/add"):].strip()
    pack_name = _parse_pack_ref(raw, me.username)

    if not pack_name:
        await update.message.reply_text(
            "Usage: /add <code>pack_name</code> or /add <code>pack_url</code>\n\n"
            "Example: /add my_stickers"
        )
        return ConversationHandler.END

    context.user_data["add_pack_name"] = pack_name
    await update.message.reply_text(
        f"\U0001f4ce Will add stickers to <b>{pack_name}</b>.\n"
        "Now send me a ZIP file with GIFs."
    )
    return WAIT_ZIP_ADD


async def add_receive_zip(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    doc = update.message.document
    if not doc:
        await update.message.reply_text("Please send a ZIP file, or /cancel to abort.")
        return WAIT_ZIP_ADD

    if not _is_zip(doc):
        await update.message.reply_text("That doesn't look like a ZIP. Send a ZIP or /cancel.")
        return WAIT_ZIP_ADD

    if _zip_too_large(doc):
        await update.message.reply_text("File too large (20MB limit).")
        return WAIT_ZIP_ADD

    pack_name = context.user_data.get("add_pack_name")
    if not pack_name:
        await update.message.reply_text("Session expired. Use /add again.")
        return ConversationHandler.END

    status_msg = await update.message.reply_text("Downloading ZIP file...")
    await update.message.chat.send_action(ChatAction.TYPING)

    await _process_zip_safely(
        update, context, status_msg,
        pack_name_base="", pack_title="", existing_pack_name=pack_name,
    )
    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("Cancelled.")
    return ConversationHandler.END


# ---- Quick-create: send ZIP directly ----


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    doc = update.message.document
    if not doc:
        return

    if not _is_zip(doc):
        await update.message.reply_text(
            "Please send a ZIP file containing GIF images. Use /start for instructions."
        )
        return

    if _zip_too_large(doc):
        await update.message.reply_text(
            "File is too large. Telegram limits bot downloads to 20MB."
        )
        return

    caption = update.message.caption or ""
    pack_title = caption.strip() if caption.strip() else f"Stickers {int(time.time())}"
    pack_name_base = _sanitize_name(caption) if caption.strip() else _sanitize_name(pack_title)

    status_msg = await update.message.reply_text("Downloading ZIP file...")
    await update.message.chat.send_action(ChatAction.TYPING)

    await _process_zip_safely(
        update, context, status_msg, pack_name_base, pack_title, existing_pack_name=None,
    )


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("Update %s caused error: %s", update, context.error)


async def register_commands(app: Application) -> None:
    commands = [
        BotCommand("start", "Show help and instructions"),
        BotCommand("newpack", "Create a new sticker pack with custom name"),
        BotCommand("add", "Add stickers to an existing pack"),
        BotCommand("cancel", "Cancel current operation"),
    ]
    await app.bot.set_my_commands(commands)


def main() -> None:
    token = os.environ.get("BOT_TOKEN")
    if not token:
        raise RuntimeError("BOT_TOKEN environment variable is not set")

    app = Application.builder().token(token).post_init(register_commands).build()

    new_conv = ConversationHandler(
        entry_points=[CommandHandler("newpack", newpack_start)],
        states={
            WAIT_NAME: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, newpack_name),
                CommandHandler("cancel", cancel),
            ],
            WAIT_TITLE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, newpack_title),
                CommandHandler("cancel", cancel),
            ],
            WAIT_ZIP_NEW: [
                MessageHandler(filters.Document.ALL, newpack_zip),
                CommandHandler("cancel", cancel),
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    add_conv = ConversationHandler(
        entry_points=[CommandHandler("add", add_command)],
        states={
            WAIT_ZIP_ADD: [
                MessageHandler(filters.Document.ALL, add_receive_zip),
                CommandHandler("cancel", cancel),
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", start))
    app.add_handler(new_conv)
    app.add_handler(add_conv)
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    app.add_error_handler(error_handler)

    logger.info("Bot started")
    app.run_polling()


if __name__ == "__main__":
    main()
