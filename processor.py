import asyncio
import logging
import os
import shutil
import tempfile
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from converter import convert_gif_to_webm_sync, is_valid_gif

logger = logging.getLogger(__name__)

MAX_ZIP_SIZE = 300 * 1024 * 1024  # 300MB uncompressed
MAX_GIFS = 120  # Telegram sticker pack limit
MAX_FILE_SIZE = 20 * 1024 * 1024  # single file limit in zip


class ProcessingError(Exception):
    pass


class ZipBombError(ProcessingError):
    pass


class NoGifsFoundError(ProcessingError):
    pass


class TooManyGifsError(ProcessingError):
    pass


def _safe_extract(zip_path: str, dest_dir: str) -> list[str]:
    """Extract ZIP safely, returning list of extracted file paths."""
    extracted = []

    with zipfile.ZipFile(zip_path, "r") as zf:
        total_size = 0
        files = [info for info in zf.infolist() if not info.is_dir()]

        for info in files:
            total_size += info.file_size
            if total_size > MAX_ZIP_SIZE:
                raise ZipBombError("ZIP content exceeds 300MB limit")

            if info.file_size > MAX_FILE_SIZE:
                continue

            name_lower = info.filename.lower()
            if not name_lower.endswith(".gif"):
                continue

            dest = zf.extract(info, dest_dir)
            extracted.append(dest)

    return extracted


async def process_zip(
    zip_path: str,
    work_dir: str,
    progress_callback=None,
    workers: int = 4,
) -> list[str]:
    """Process a ZIP file containing GIFs.

    Args:
        zip_path: Path to the uploaded ZIP file
        work_dir: Working directory for extraction and conversion
        progress_callback: Optional async callable(current, total, status_text)
        workers: Number of parallel conversion threads

    Returns:
        List of paths to converted WebM files, in order
    """
    zip_path = str(zip_path)
    extract_dir = os.path.join(work_dir, "extracted")
    convert_dir = os.path.join(work_dir, "converted")
    os.makedirs(extract_dir, exist_ok=True)
    os.makedirs(convert_dir, exist_ok=True)

    # Step 1: Extract
    if progress_callback:
        await progress_callback(0, 1, "Extracting ZIP...")

    try:
        extracted = _safe_extract(zip_path, extract_dir)
    except ZipBombError:
        raise
    except zipfile.BadZipFile:
        raise ProcessingError("Invalid ZIP file")

    if not extracted:
        raise NoGifsFoundError("No GIF files found in the ZIP")

    if len(extracted) > MAX_GIFS:
        raise TooManyGifsError(
            f"Found {len(extracted)} GIFs, maximum is {MAX_GIFS}"
        )

    # Step 2: Validate GIF headers
    if progress_callback:
        await progress_callback(0, len(extracted), "Validating files...")

    valid_gifs = []
    for fp in extracted:
        if is_valid_gif(fp):
            valid_gifs.append(fp)
        else:
            os.remove(fp)

    if not valid_gifs:
        raise NoGifsFoundError("No valid GIF files found")

    # Step 3: Convert to WebM
    total = len(valid_gifs)
    webm_files = []

    loop = asyncio.get_running_loop()
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for i, gif_path in enumerate(valid_gifs):
            name = Path(gif_path).stem
            webm_path = os.path.join(convert_dir, f"{name}.webm")

            if progress_callback:
                await progress_callback(
                    i + 1, total, f"Converting {i+1}/{total}: {name}"
                )

            success = await loop.run_in_executor(
                pool, convert_gif_to_webm_sync, gif_path, webm_path
            )

            if success and os.path.getsize(webm_path) > 0:
                webm_files.append(webm_path)
            else:
                logger.warning("Failed to convert %s", gif_path)

    if not webm_files:
        raise ProcessingError("All GIFs failed to convert")

    return sorted(webm_files)


def cleanup_work_dir(work_dir: str) -> None:
    if os.path.isdir(work_dir):
        shutil.rmtree(work_dir, ignore_errors=True)
