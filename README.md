# TgStickerBot

A Telegram bot that converts ZIP archives of GIF images into animated sticker packs.

Upload a ZIP with GIFs, and the bot processes them into Telegram animated stickers (WebM/VP9), automatically handling sizing, duration, and file size limits.

## How It Works

```
User → ZIP → Bot → Extract → Validate → ffmpeg → WebM → createNewStickerSet
```

1. You send a ZIP file containing GIF images
2. The bot extracts and validates the GIFs
3. FFmpeg transcodes each GIF to WebM (VP9 video, Telegram animated sticker format)
4. A new sticker pack is created, or the stickers are added to an existing one
5. You get a link to install the sticker pack

## Usage

### Quick create (auto-generated name)

Send a ZIP file directly — the caption (if any) becomes the pack title.

### Interactive create (custom name)

```
/newpack
```
A 3-step conversation guides you through setting the pack name (URL slug) and title.

### Add to existing pack

```
/add pack_name_or_url
```
Then send the ZIP file with GIFs to add them to the specified pack.

### Commands

| Command | Description |
|---------|-------------|
| `/start` | Show help and instructions |
| `/newpack` | Interactive sticker pack creation |
| `/add <name>` | Add stickers to an existing pack |
| `/cancel` | Cancel current operation |

## Limits

| Limit | Value |
|-------|-------|
| Max GIFs per pack | 120 (Telegram limit) |
| ZIP upload size | ≤20MB (Telegram Bot API limit) |
| ZIP decompressed size | ≤300MB |
| Sticker duration | ≤3 seconds (auto-cropped) |
| Sticker resolution | 512px on the long side |
| Sticker file size | ≤256KB per sticker |
| Sticker format | WebM (VP9, 30 FPS) |

## Deployment

### Prerequisites

- Docker & Docker Compose
- A Telegram bot token from [@BotFather](https://t.me/BotFather)

### Quick start

```bash
cp .env.example .env
```

Edit `.env` and set your `BOT_TOKEN`.

```bash
docker compose up -d
```

The bot starts polling immediately with no further configuration needed.

### Manual deployment (no Docker)

```bash
# Install system dependencies
apt install ffmpeg

# Install Python dependencies
pip install -r requirements.txt

# Run
BOT_TOKEN=your_token python bot.py
```

## Project Structure

```
├── bot.py          # Telegram bot handlers and conversation flows
├── converter.py     # GIF → WebM transcoding with adaptive quality control
├── processor.py     # ZIP extraction, validation, and processing orchestration
├── Dockerfile       # Docker image (python:3.11-slim + ffmpeg)
├── docker-compose.yml
├── requirements.txt
└── .env.example
```

## Technical Notes

- **GIF to WebM conversion**: Uses ffmpeg with libvpx-vp9 codec. A two-stage encoding strategy handles the 256KB Telegram limit by reducing bitrate first, then resolution if needed.
- **ZIP safety**: Protects against ZIP bombs by tracking total decompressed size (300MB cap). Only extracts `.gif` files.
- **GIF validation**: Verifies the header magic bytes (`GIF87a`/`GIF89a`) before processing.
- **Progress feedback**: Real-time progress updates during conversion, rate-limited to avoid flooding the Telegram API.
- **Sticker pack format**: Telegram recommends `<name>_by_<bot_username>` as the internal pack name; the bot generates links as `https://t.me/addstickers/<name>_by_<bot_username>`.
