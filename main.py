import asyncio
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Request, HTTPException, BackgroundTasks
from fastapi.responses import JSONResponse

from bot.conversation import load_conversation, save_conversation
from bot.flow import process_message
from services.evolution import extract_phone, is_group_message, is_bot_sent_message

COLOMBIA_TZ = ZoneInfo("America/Bogota")
TAKEOVER_WINDOW_MINUTES = 10

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

# Command Yesica types to hand back control to the bot
RESUME_BOT_COMMAND = "!bot"


@asynccontextmanager
async def lifespan(app: FastAPI):
    os.makedirs("data/conversations", exist_ok=True)
    os.makedirs("credentials", exist_ok=True)
    logger.info("Estetica Real Bot arrancado correctamente")
    yield
    logger.info("Bot detenido")


app = FastAPI(title="Estetica Real WhatsApp Bot", lifespan=lifespan)


@app.get("/health")
async def health():
    from services.calendar import _get_credentials
    cal_ok = _get_credentials() is not None
    return {"status": "ok", "bot": "Estetica Real", "calendar": "connected" if cal_ok else "error"}


@app.post("/webhook")
async def webhook(request: Request, background_tasks: BackgroundTasks):
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    event = payload.get("event", "")
    if event not in ("messages.upsert", "MESSAGES_UPSERT"):
        return JSONResponse({"status": "ignored", "event": event})

    data = payload.get("data", {})
    key = data.get("key", {})
    remote_jid = key.get("remoteJid", "")
    from_me = key.get("fromMe", False)
    message_id = key.get("id", "")

    # Ignore group messages
    if is_group_message(remote_jid):
        return JSONResponse({"status": "ignored"})

    phone = extract_phone(remote_jid)
    if not phone:
        return JSONResponse({"status": "ignored", "reason": "no phone"})

    message_obj: dict = data.get("message", {})

    # -----------------------------------------------------------------------
    # fromMe=true: either the bot sent it, or Yesica typed it manually
    # -----------------------------------------------------------------------
    if from_me:
        if is_bot_sent_message(message_id):
            # This is a message our bot sent — ignore
            return JSONResponse({"status": "ignored", "reason": "own message"})

        # Yesica typed this manually — handle human takeover
        text_content = (
            message_obj.get("conversation")
            or message_obj.get("extendedTextMessage", {}).get("text", "")
        ).strip()

        background_tasks.add_task(
            _handle_yesica_intervention, phone, text_content
        )
        return JSONResponse({"status": "yesica_intervention"})

    # -----------------------------------------------------------------------
    # Incoming message from a client
    # -----------------------------------------------------------------------
    push_name: str | None = data.get("pushName") or data.get("notifyName") or None
    logger.info(f"Incoming message from {phone} | pushName={push_name!r} | type={list(message_obj.keys())}")
    message_type, text_content, media_key_id, media_base64_inline = _parse_message(
        key, message_obj
    )

    if message_type is None:
        logger.debug(f"Unsupported message type from {phone}: {list(message_obj.keys())}")
        return JSONResponse({"status": "ignored", "reason": "unsupported type"})

    background_tasks.add_task(
        process_message,
        phone=phone,
        push_name=push_name,
        message_type=message_type,
        text_content=text_content,
        media_key_id=media_key_id,
        media_base64_inline=media_base64_inline,
    )

    return JSONResponse({"status": "queued"})


# ---------------------------------------------------------------------------
# Yesica intervention handler
# ---------------------------------------------------------------------------

async def _handle_yesica_intervention(phone: str, text: str) -> None:
    """
    Called when Yesica manually types a message from the bot's WhatsApp.
    - Sets a 10-minute takeover window (bot stays silent).
    - Each new message from Yesica resets the 10-minute window.
    - If Yesica types !bot, immediately re-enables the bot.
    """
    conv = load_conversation(phone)

    if text.lower() == RESUME_BOT_COMMAND:
        conv.human_takeover = False
        conv.human_takeover_until = None
        save_conversation(conv)
        logger.info(f"Bot re-enabled immediately for {phone} by Yesica (!bot)")
    else:
        until = datetime.now(COLOMBIA_TZ) + timedelta(minutes=TAKEOVER_WINDOW_MINUTES)
        conv.human_takeover = True
        conv.human_takeover_until = until.isoformat()
        save_conversation(conv)
        logger.info(f"Human takeover for {phone} — bot silent until {until.strftime('%H:%M:%S')}")


# ---------------------------------------------------------------------------
# Message parsing
# ---------------------------------------------------------------------------

def _parse_message(
    key: dict,
    message_obj: dict,
) -> tuple[str | None, str | None, str | None, str | None]:
    """Returns (message_type, text_content, media_key_id, media_base64_inline)"""

    # Plain text
    if "conversation" in message_obj:
        return "conversation", message_obj["conversation"], None, None

    # Extended text (links, formatted)
    if "extendedTextMessage" in message_obj:
        text = message_obj["extendedTextMessage"].get("text", "")
        return "conversation", text, None, None

    # Image
    if "imageMessage" in message_obj:
        img = message_obj["imageMessage"]
        media_key_id = key.get("id")
        base64_inline = img.get("base64") or img.get("jpegThumbnail")
        if base64_inline and len(base64_inline) > 5000:
            return "imageMessage", img.get("caption"), None, base64_inline
        return "imageMessage", img.get("caption"), media_key_id, None

    # Audio — voice note (pttMessage) or regular audio (audioMessage)
    if "audioMessage" in message_obj or "pttMessage" in message_obj:
        media_key_id = key.get("id")
        audio_obj = message_obj.get("audioMessage") or message_obj.get("pttMessage", {})
        base64_inline = audio_obj.get("base64")
        return "audioMessage", None, media_key_id, base64_inline

    # Button / list reply
    if "buttonsResponseMessage" in message_obj:
        text = message_obj["buttonsResponseMessage"].get("selectedButtonId", "")
        return "conversation", text, None, None

    if "listResponseMessage" in message_obj:
        text = message_obj["listResponseMessage"].get("title", "")
        return "conversation", text, None, None

    return None, None, None, None


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=False)
