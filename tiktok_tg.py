import asyncio
import logging
import os
import re
import tempfile
from urllib.parse import urlparse

import aiohttp
from dotenv import load_dotenv
from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
# Default to tikwm public API; you can swap this out for any compatible JSON API.
# Expected to return JSON with data.hdplay or data.play as a direct MP4 URL.
TIKTOK_API_BASE = os.getenv("TIKTOK_API_BASE", "https://www.tikwm.com/api/")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("tiktok-bot")

# --- TikTok link detection ---

URL_RE = re.compile(r"https?://[^\s)]+", re.IGNORECASE)

_ALLOWED_TT_HOSTS = {
    "tiktok.com",
    "www.tiktok.com",
    "m.tiktok.com",
    "vm.tiktok.com",  # short links
    "vt.tiktok.com",  # short links
}

def _is_tiktok_link(u: str) -> bool:
    try:
        host = urlparse(u).netloc.lower()
    except Exception:
        return False
    if ":" in host:
        host = host.split(":")[0]
    return host in _ALLOWED_TT_HOSTS

def _extract_tiktok_url_from_text(text: str | None) -> str | None:
    if not text:
        return None
    for m in URL_RE.finditer(text):
        url = m.group(0)
        if _is_tiktok_link(url):
            return url
    return None

def _display_name_from_msg(msg) -> str:
    """
    Prefer @username; fallback to 'First Last'; then to user id.
    """
    u = msg.from_user
    if not u:
        return "unknown"
    if u.username:
        return f"@{u.username}"
    # Join first + last if available
    name = " ".join(p for p in [u.first_name, u.last_name] if p)
    return name or str(u.id)

# --- TikTok resolver using a simple JSON API ---

async def resolve_tiktok_direct_url(session: aiohttp.ClientSession, tiktok_url: str) -> str | None:
    try:
        params = {"url": tiktok_url, "hd": "1"}
        async with session.get(
            TIKTOK_API_BASE,
            params=params,
            timeout=aiohttp.ClientTimeout(total=30),
        ) as r:
            if r.status != 200:
                log.warning("API status %s", r.status)
                return None
            data = await r.json()
    except Exception as e:
        log.exception("Resolver error: %s", e)
        return None

    d = (data or {}).get("data") or {}
    for key in ("hdplay", "play", "wmplay"):
        v = d.get(key)
        if v:
            return v
    return None

async def download_to_tempfile(session: aiohttp.ClientSession, url: str) -> str:
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".mp4")
    tmp_path = tmp.name
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=120)) as r:
            r.raise_for_status()
            async for chunk in r.content.iter_chunked(128 * 1024):
                tmp.write(chunk)
        tmp.close()
        return tmp_path
    except Exception:
        tmp.close()
        try:
            os.remove(tmp_path)
        except Exception:
            pass
        raise

# --- Telegram handlers ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "Send me a TikTok link and I’ll fetch the video.\n"
        "I only respond to TikTok URLs (tiktok.com, vm/vt short links)."
    )
    await update.message.reply_text(text)

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if not msg or not msg.text:
        return

    tiktok_url = _extract_tiktok_url_from_text(msg.text)
    if not tiktok_url:
        return

    chat_id = msg.chat_id
    sender = _display_name_from_msg(msg)
    caption = f"Send By : {sender}"

    # If this is a group/supergroup, try to delete the original message
    if msg.chat and msg.chat.type in ("group", "supergroup"):
        try:
            await msg.delete()
        except Exception as e:
            log.warning("Couldn't delete message: %s", e)

    # IMPORTANT: after delete(), do NOT use reply_* helpers.
    try:
        await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.UPLOAD_VIDEO)
    except Exception:
        pass

    async with aiohttp.ClientSession() as session:
        direct_url = await resolve_tiktok_direct_url(session, tiktok_url)
        if not direct_url:
            await context.bot.send_message(chat_id=chat_id, text="Couldn’t resolve that TikTok link. Try a different one?")
            return

        # Let Telegram fetch via URL first
        try:
            await context.bot.send_video(chat_id=chat_id, video=direct_url, caption=caption)
            return
        except Exception as e:
            log.warning("Direct send failed, will try uploading: %s", e)

        # Fallback: download then upload
        path = None
        try:
            path = await download_to_tempfile(session, direct_url)
            with open(path, "rb") as f:
                await context.bot.send_video(chat_id=chat_id, video=f, caption=caption)
        finally:
            if path:
                try:
                    os.remove(path)
                except Exception:
                    pass

def main():
    if not BOT_TOKEN:
        raise SystemExit("BOT_TOKEN is not set. Put it in .env")

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    log.info("Bot is running…")
    app.run_polling(close_loop=False)

if __name__ == "__main__":
    main()
