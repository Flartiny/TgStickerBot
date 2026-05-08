import asyncio
import os
from pathlib import Path

MAX_DIMENSION = 512
MAX_DURATION = 3
MAX_FILE_SIZE = 256 * 1024
TARGET_FPS = 30

STICKER_FORMAT_MAGIC = {
    b"GIF87a": True,
    b"GIF89a": True,
}


def is_valid_gif(filepath: str) -> bool:
    with open(filepath, "rb") as f:
        header = f.read(6)
    return header in STICKER_FORMAT_MAGIC


def get_gif_frame_count(filepath: str) -> int:
    """Count frames by reading the GIF stream. Returns 0 if unknown."""
    count = 0
    with open(filepath, "rb") as f:
        header = f.read(6)
        if header not in (b"GIF87a", b"GIF89a"):
            return 0
        # skip logical screen descriptor (7 bytes)
        f.seek(13)
        # read global color table if present
        packed = header[4] if len(header) > 4 else 0
        if packed & 0x80:
            color_table_size = 3 * (2 << (packed & 0x07))
            f.seek(13 + color_table_size)
        while True:
            block = f.read(1)
            if not block:
                break
            if block == b"\x2c":
                count += 1
                f.seek(9, 1)
                packed_byte = f.read(1)
                if packed_byte:
                    f.seek(1, 1)
            elif block == b"\x21":
                ext = f.read(1)
                if ext == b"\xf9":
                    count += 1
                    f.seek(5, 1)
                elif ext == b"\xfe":
                    while True:
                        sub_block = f.read(1)
                        if sub_block == b"\x00":
                            break
                        size = sub_block[0]
                        f.seek(size, 1)
                elif ext == b"\xff":
                    while True:
                        sub_block = f.read(1)
                        if sub_block == b"\x00":
                            break
                        size = sub_block[0]
                        f.seek(size, 1)
                elif ext == b"\x01":
                    f.seek(13, 1)
                    while True:
                        sub_block = f.read(1)
                        if sub_block == b"\x00":
                            break
                        size = sub_block[0]
                        f.seek(size, 1)
            elif block == b"\x3b":
                break
    return count


def _build_ffmpeg_cmd(input_path: str, output_path: str, bitrate: int, scale: int) -> list[str]:
    return [
        "ffmpeg", "-y",
        "-i", input_path,
        "-vf", f"scale={scale}:-1:flags=lanczos,fps={TARGET_FPS}",
        "-c:v", "libvpx-vp9",
        "-pix_fmt", "yuva420p",
        "-b:v", f"{bitrate}k",
        "-t", str(MAX_DURATION),
        "-an",
        "-cpu-used", "2",
        "-row-mt", "1",
        output_path,
    ]


async def convert_gif_to_webm(input_path: str, output_path: str) -> bool:
    """Convert a GIF to Telegram-compatible WebM (VP9). Returns True on success."""

    input_path = str(input_path)
    output_path = str(output_path)

    bitrate = 200
    scale = MAX_DIMENSION

    while True:
        cmd = _build_ffmpeg_cmd(input_path, output_path, bitrate, scale)
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()

        if proc.returncode != 0:
            return False

        if not os.path.exists(output_path) or os.path.getsize(output_path) == 0:
            return False

        file_size = os.path.getsize(output_path)
        if file_size <= MAX_FILE_SIZE:
            return True

        if bitrate > 50:
            bitrate = max(50, bitrate - 50)
        elif scale > 256:
            scale = max(256, scale - 64)
            bitrate = 200
        else:
            # already at minimum, accept whatever we got
            return True


def convert_gif_to_webm_sync(input_path: str, output_path: str) -> bool:
    """Synchronous wrapper for use in thread pool."""
    import subprocess

    input_path = str(input_path)
    output_path = str(output_path)

    bitrate = 200
    scale = MAX_DIMENSION

    while True:
        cmd = _build_ffmpeg_cmd(input_path, output_path, bitrate, scale)
        result = subprocess.run(cmd, capture_output=True)

        if result.returncode != 0:
            return False

        if not os.path.exists(output_path) or os.path.getsize(output_path) == 0:
            return False

        file_size = os.path.getsize(output_path)
        if file_size <= MAX_FILE_SIZE:
            return True

        if bitrate > 50:
            bitrate = max(50, bitrate - 50)
        elif scale > 256:
            scale = max(256, scale - 64)
            bitrate = 200
        else:
            return True
