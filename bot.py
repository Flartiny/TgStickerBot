import asyncio
import logging
import os
import re
import tempfile
import time

from telegram import Update, InputSticker
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

WAIT_ZIP = 0

HELP_TEXT = (
    "\U0001f9e9 Send me a <b>ZIP file</b> containing GIF images, "
    "and I'll turn them into a Telegram animated sticker pack.\n\n"
    "<b>Creating a new pack:</b>\n"
    "  Send a ZIP — add a caption for the pack title\n\n"
    "<b>Adding to an existing pack:</b>\n"
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
    """Parse sticker pack reference from user input.

    Accepts:
      - Full name:  mypack_by_MyBot
      - Short name: mypack  (appends _by_<bot_username>)
      - URL:        https://t.me/addstickers/mypack_by_MyBot

    Returns the full pack name or None if unparseable.
    """
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

    me = await context.bot.get_me()
    bot_username = me.username

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
        full_pack_name = f"{pack_name_base}_by_{bot_username}"
        if len(full_pack_name) > 64:
            pack_name_base = pack_name_base[: 64 - len(f"_by_{bot_username}") - 1]
            full_pack_name = f"{pack_name_base}_by_{bot_username}"

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


# ---- Command handlers ----


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_html(HELP_TEXT)


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
    return WAIT_ZIP


async def add_receive_zip(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    doc = update.message.document
    if not doc:
        await update.message.reply_text("Please send a ZIP file, or /cancel to abort.")
        return WAIT_ZIP

    mime = doc.mime_type or ""
    fname = (doc.file_name or "").lower()
    if not (mime in SUPPORTED_MIME or fname.endswith(".zip")):
        await update.message.reply_text("That doesn't look like a ZIP. Send a ZIP or /cancel.")
        return WAIT_ZIP

    if doc.file_size and doc.file_size > 20 * 1024 * 1024:
        await update.message.reply_text("File too large (20MB limit).")
        return WAIT_ZIP

    pack_name = context.user_data.get("add_pack_name")
    if not pack_name:
        await update.message.reply_text("Session expired. Use /add again.")
        return ConversationHandler.END

    status_msg = await update.message.reply_text("Downloading ZIP file...")
    await update.message.chat.send_action(ChatAction.TYPING)

    work_dir = tempfile.mkdtemp(prefix="tgstickers_")
    zip_path = None

    try:
        tg_file = await context.bot.get_file(doc.file_id)
        zip_path = os.path.join(work_dir, "input.zip")
        await tg_file.download_to_drive(zip_path)

        await _run_processing_pipeline(
            update, context, status_msg, work_dir, zip_path,
            pack_name_base="", pack_title="", existing_pack_name=pack_name,
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
        await status_msg.edit_text(
            f"Something went wrong.\nError: {e}\nPlease try again."
        )
    finally:
        cleanup_work_dir(work_dir)

    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("Cancelled.")
    return ConversationHandler.END


# ---- Document handler for new sticker packs ----


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    doc = update.message.document
    if not doc:
        return

    mime = doc.mime_type or ""
    fname = (doc.file_name or "").lower()

    is_zip = mime in SUPPORTED_MIME or fname.endswith(".zip")

    if not is_zip:
        await update.message.reply_text(
            "Please send a ZIP file containing GIF images. Use /start for instructions."
        )
        return

    if doc.file_size and doc.file_size > 20 * 1024 * 1024:
        await update.message.reply_text(
            "File is too large. Telegram limits bot downloads to 20MB."
        )
        return

    caption = update.message.caption or ""
    pack_title = caption.strip() if caption.strip() else f"Stickers {int(time.time())}"
    pack_name_base = _sanitize_name(caption) if caption.strip() else _sanitize_name(pack_title)

    status_msg = await update.message.reply_text("Downloading ZIP file...")
    await update.message.chat.send_action(ChatAction.TYPING)

    work_dir = tempfile.mkdtemp(prefix="tgstickers_")

    try:
        tg_file = await context.bot.get_file(doc.file_id)
        zip_path = os.path.join(work_dir, "input.zip")
        await tg_file.download_to_drive(zip_path)

        await _run_processing_pipeline(
            update, context, status_msg, work_dir, zip_path,
            pack_name_base, pack_title, existing_pack_name=None,
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
        await status_msg.edit_text(
            f"Something went wrong while creating the sticker pack.\n\n"
            f"Error: {e}\n\n"
            f"Please try again or check that the GIFs are valid."
        )
    finally:
        cleanup_work_dir(work_dir)


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("Update %s caused error: %s", update, context.error)


def main() -> None:
    token = os.environ.get("BOT_TOKEN")
    if not token:
        raise RuntimeError("BOT_TOKEN environment variable is not set")

    app = Application.builder().token(token).build()

    add_conv = ConversationHandler(
        entry_points=[CommandHandler("add", add_command)],
        states={
            WAIT_ZIP: [
                MessageHandler(filters.Document.ALL, add_receive_zip),
                CommandHandler("cancel", cancel),
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", start))
    app.add_handler(add_conv)
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    app.add_error_handler(error_handler)

    logger.info("Bot started")
    app.run_polling()


if __name__ == "__main__":
    main()
