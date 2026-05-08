import asyncio
import logging
import os
import re
import tempfile
import time
from pathlib import Path

from telegram import Update, InputSticker
from telegram.constants import ChatAction, StickerFormat
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

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

HELP_TEXT = (
    "\U0001f9e9 Send me a <b>ZIP file</b> containing GIF images, "
    "and I'll turn them into a Telegram animated sticker pack.\n\n"
    "<b>Instructions:</b>\n"
    "1. Prepare a ZIP with your GIF files (max 120)\n"
    "2. Send the ZIP to me — add a caption for the pack title\n"
    "3. I'll process the GIFs and create the sticker pack\n"
    "4. You'll receive a link to add the pack\n\n"
    "<b>Limits:</b>\n"
    "- Max 120 GIFs per pack\n"
    "- ZIP uncompressed size limit: 300MB\n"
    "- GIFs are auto-cropped to 3 seconds\n"
    "- Stickers resize to 512px on the long side"
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


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_html(HELP_TEXT)


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
    zip_path = None

    try:
        # Download
        tg_file = await context.bot.get_file(doc.file_id)
        zip_path = os.path.join(work_dir, "input.zip")
        await tg_file.download_to_drive(zip_path)

        # Progress callback
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

        # Process
        webm_files = await process_zip(zip_path, work_dir, on_progress)

        await status_msg.edit_text(f"Creating sticker pack for {len(webm_files)} stickers...")
        await update.message.chat.send_action(ChatAction.TYPING)

        # Get bot username for pack name
        me = await context.bot.get_me()
        bot_username = me.username
        full_pack_name = f"{pack_name_base}_by_{bot_username}"

        # Ensure name is not too long (Telegram limit ~64 chars for full name)
        if len(full_pack_name) > 64:
            pack_name_base = pack_name_base[:64 - len(f"_by_{bot_username}") - 1]
            full_pack_name = f"{pack_name_base}_by_{bot_username}"

        # Create sticker set — first sticker
        input_stickers = []
        for webm_path in webm_files:
            with open(webm_path, "rb") as f:
                sticker_bytes = f.read()
            input_stickers.append(
                InputSticker(sticker_bytes, [DEFAULT_EMOJI], StickerFormat.VIDEO)
            )

        if not input_stickers:
            raise ProcessingError("No stickers were generated")

        await context.bot.create_new_sticker_set(
            user_id=update.effective_user.id,
            name=full_pack_name,
            title=pack_title[:64],
            stickers=[input_stickers[0]],
            sticker_format=StickerFormat.VIDEO,
        )

        # Add remaining stickers
        for sticker in input_stickers[1:]:
            await context.bot.add_sticker_to_set(
                user_id=update.effective_user.id,
                name=full_pack_name,
                sticker=sticker,
            )
            await asyncio.sleep(0.1)

        pack_link = f"https://t.me/addstickers/{full_pack_name}"
        await status_msg.edit_text(
            f"\u2705 Sticker pack <b>{pack_title}</b> created!\n\n"
            f"{len(webm_files)} stickers added.\n"
            f"\U0001f517 <a href='{pack_link}'>Add stickers</a>",
            disable_web_page_preview=False,
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
        if work_dir:
            cleanup_work_dir(work_dir)


def _progress_bar(current: int, total: int, width: int = 12) -> str:
    filled = int(width * current / total) if total else 0
    empty = width - filled
    return f"[{'█' * filled}{'░' * empty}]"


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("Update %s caused error: %s", update, context.error)


def main() -> None:
    token = os.environ.get("BOT_TOKEN")
    if not token:
        raise RuntimeError("BOT_TOKEN environment variable is not set")

    app = Application.builder().token(token).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", start))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    app.add_error_handler(error_handler)

    logger.info("Bot started")
    app.run_polling()


if __name__ == "__main__":
    main()
