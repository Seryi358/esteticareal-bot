import asyncio
import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, HTTPException, BackgroundTasks
from fastapi.responses import JSONResponse

from bot.flow import process_message
from services.evolution import extract_phone, is_group_message

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    os.makedirs("data/conversations", exist_ok=True)
    os.makedirs("credentials", exist_ok=True)
    logger.info("Estetica Real Bot arrancado correctamente")
    yield
    logger.info("Bot detenido")


app = FastAPI(
    title="Estetica Real WhatsApp Bot",
    lifespan=lifespan,
)

# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------

@app.get("/health")
async def health():
    return {"status": "ok", "bot": "Estetica Real"}


# ---------------------------------------------------------------------------
# Webhook endpoint — Evolution API posts here on every message
# ---------------------------------------------------------------------------

@app.post("/webhook")
async def webhook(request: Request, background_tasks: BackgroundTasks):
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    event = payload.get("event", "")

    # We only care about incoming messages
    if event not in ("messages.upsert", "MESSAGES_UPSERT"):
        return JSONResponse({"status": "ignored", "event": event})

    data = payload.get("data", {})
    key = data.get("key", {})
    remote_jid = key.get("remoteJid", "")
    from_me = key.get("fromMe", False)

    # Ignore messages sent BY the bot itself and group messages
    if from_me or is_group_message(remote_jid):
        return JSONResponse({"status": "ignored"})

    phone = extract_phone(remote_jid)
    if not phone:
        return JSONResponse({"status": "ignored", "reason": "no phone"})

    push_name: str | None = data.get("pushName") or data.get("notifyName")
    message_obj: dict = data.get("message", {})

    # Determine message type and extract content
    message_type, text_content, media_key_id, media_base64_inline = _parse_message(
        key, message_obj
    )

    if message_type is None:
        logger.debug(f"Unsupported message type from {phone}: {list(message_obj.keys())}")
        return JSONResponse({"status": "ignored", "reason": "unsupported message type"})

    # Process asynchronously so we return 200 immediately to Evolution API
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
# Message parsing helpers
# ---------------------------------------------------------------------------

def _parse_message(
    key: dict,
    message_obj: dict,
) -> tuple[str | None, str | None, str | None, str | None]:
    """
    Returns (message_type, text_content, media_key_id, media_base64_inline)
    message_type: 'conversation' | 'imageMessage' | None
    """
    # Plain text
    if "conversation" in message_obj:
        return "conversation", message_obj["conversation"], None, None

    # Extended text (links, formatted)
    if "extendedTextMessage" in message_obj:
        text = message_obj["extendedTextMessage"].get("text", "")
        return "conversation", text, None, None

    # Image message
    if "imageMessage" in message_obj:
        img = message_obj["imageMessage"]
        media_key_id = key.get("id")
        # Some Evolution setups include base64 directly in webhook
        base64_inline = img.get("base64") or img.get("jpegThumbnail")
        # Prefer full base64 if present, else we'll download it
        if base64_inline and len(base64_inline) > 5000:
            return "imageMessage", img.get("caption"), None, base64_inline
        return "imageMessage", img.get("caption"), media_key_id, None

    # Button / list reply
    if "buttonsResponseMessage" in message_obj:
        text = message_obj["buttonsResponseMessage"].get("selectedButtonId", "")
        return "conversation", text, None, None

    if "listResponseMessage" in message_obj:
        text = message_obj["listResponseMessage"].get("title", "")
        return "conversation", text, None, None

    return None, None, None, None


# ---------------------------------------------------------------------------
# Run directly
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=False)
