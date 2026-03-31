import asyncio
import glob
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Request, HTTPException, BackgroundTasks
from fastapi.responses import JSONResponse

from bot.conversation import load_conversation, save_conversation
from bot.flow import process_message, send_followup_if_needed, send_reminder_if_needed, send_auto_cancel_if_needed
from services.evolution import extract_phone, is_group_message, is_bot_sent_message

COLOMBIA_TZ = ZoneInfo("America/Bogota")
TAKEOVER_WINDOW_MINUTES = 5

# Follow-up scheduler interval (seconds) — check every 4 hours
FOLLOWUP_CHECK_INTERVAL = 4 * 60 * 60

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

# Command Yesica types to hand back control to the bot
RESUME_BOT_COMMAND = "!bot"


# ---------------------------------------------------------------------------
# Background scheduler — 24h follow-ups
# ---------------------------------------------------------------------------

async def _followup_scheduler():
    """Background loop: checks inactive conversations every 4h and sends follow-ups."""
    await asyncio.sleep(60)
    while True:
        try:
            conversations_dir = "data/conversations"
            sent = 0
            checked = 0
            for filepath in glob.glob(os.path.join(conversations_dir, "*.json")):
                phone = os.path.basename(filepath).replace(".json", "")
                checked += 1
                try:
                    if await send_followup_if_needed(phone):
                        sent += 1
                except Exception as e:
                    logger.error(f"Follow-up error for {phone}: {e}")
            if sent > 0:
                logger.info(f"Follow-up scheduler: {checked} checked, {sent} sent")
        except Exception as e:
            logger.error(f"Follow-up scheduler error: {e}")
        await asyncio.sleep(FOLLOWUP_CHECK_INTERVAL)


# Reminder check interval — every 15 minutes
REMINDER_CHECK_INTERVAL = 15 * 60


async def _reminder_scheduler():
    """Background loop: checks for upcoming appointments every 15min and sends reminders.
    Also handles auto-cancellation of unconfirmed appointments."""
    await asyncio.sleep(120)  # Wait 2min after startup
    while True:
        try:
            conversations_dir = "data/conversations"
            sent = 0
            cancelled = 0
            for filepath in glob.glob(os.path.join(conversations_dir, "*.json")):
                phone = os.path.basename(filepath).replace(".json", "")
                try:
                    if await send_reminder_if_needed(phone):
                        sent += 1
                except Exception as e:
                    logger.error(f"Reminder error for {phone}: {e}")
                try:
                    if await send_auto_cancel_if_needed(phone):
                        cancelled += 1
                except Exception as e:
                    logger.error(f"Auto-cancel error for {phone}: {e}")
            if sent > 0 or cancelled > 0:
                logger.info(f"Reminder scheduler: {sent} reminders sent, {cancelled} auto-cancelled")
        except Exception as e:
            logger.error(f"Reminder scheduler error: {e}")
        await asyncio.sleep(REMINDER_CHECK_INTERVAL)


@asynccontextmanager
async def lifespan(app: FastAPI):
    os.makedirs("data/conversations", exist_ok=True)
    os.makedirs("data/learning", exist_ok=True)
    os.makedirs("credentials", exist_ok=True)
    logger.info("Estetica Real Bot (Valen v4) arrancado correctamente")
    # Start background schedulers
    followup_task = asyncio.create_task(_followup_scheduler())
    reminder_task = asyncio.create_task(_reminder_scheduler())
    yield
    followup_task.cancel()
    reminder_task.cancel()
    logger.info("Bot detenido")


app = FastAPI(title="Estetica Real WhatsApp Bot", lifespan=lifespan)


@app.get("/health")
async def health():
    from services.calendar import _get_credentials
    cal_ok = _get_credentials() is not None
    return {"status": "ok", "bot": "Estetica Real — Valen", "calendar": "connected" if cal_ok else "error"}


@app.post("/check-followups")
async def check_followups():
    """Manual trigger for follow-up checks (also runs automatically every 4h)."""
    conversations_dir = "data/conversations"
    sent = 0
    checked = 0

    for filepath in glob.glob(os.path.join(conversations_dir, "*.json")):
        phone = os.path.basename(filepath).replace(".json", "")
        checked += 1
        try:
            if await send_followup_if_needed(phone):
                sent += 1
        except Exception as e:
            logger.error(f"Follow-up error for {phone}: {e}")

    return {"status": "ok", "checked": checked, "followups_sent": sent}


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

        # Yesica typed/sent this manually — handle human takeover
        text_content = (
            message_obj.get("conversation")
            or message_obj.get("extendedTextMessage", {}).get("text", "")
        ).strip()

        # Check if Yésica sent an audio
        audio_base64 = None
        audio_key_id = None
        if "audioMessage" in message_obj or "pttMessage" in message_obj:
            audio_obj = message_obj.get("audioMessage") or message_obj.get("pttMessage", {})
            audio_base64 = audio_obj.get("base64")
            audio_key_id = key.get("id") if not audio_base64 else None

        background_tasks.add_task(
            _handle_yesica_intervention, phone, text_content,
            audio_base64=audio_base64, audio_key_id=audio_key_id,
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

async def _handle_yesica_intervention(
    phone: str,
    text: str,
    audio_base64: str | None = None,
    audio_key_id: str | None = None,
) -> None:
    """
    Called when Yesica manually types/sends a message from the bot's WhatsApp.
    - Sets a 5-minute takeover window (bot stays silent).
    - Each new message from Yesica resets the 5-minute window.
    - If Yesica types !bot, immediately re-enables the bot.
    - Saves Yesica's messages for learning (style adaptation).
    """
    from bot.learning import save_yesica_message

    conv = load_conversation(phone)

    if text.lower() == RESUME_BOT_COMMAND:
        conv.human_takeover = False
        conv.human_takeover_until = None
        save_conversation(conv)
        logger.info(f"Bot re-enabled immediately for {phone} by Yesica (!bot)")
        return

    until = datetime.now(COLOMBIA_TZ) + timedelta(minutes=TAKEOVER_WINDOW_MINUTES)
    conv.human_takeover = True
    conv.human_takeover_until = until.isoformat()

    # Handle Yésica's audio messages — transcribe and learn from them
    if audio_base64 or audio_key_id:
        try:
            from services import ai as ai_service, evolution as evo_service
            base64_data = audio_base64
            if not base64_data and audio_key_id:
                base64_data = await evo_service.get_media_base64(audio_key_id, phone=phone)
            if base64_data:
                transcription = await ai_service.transcribe_audio(base64_data)
                if transcription:
                    logger.info(f"Yésica audio transcription for {phone}: '{transcription[:100]}'")
                    conv.add_message("assistant", transcription)
                    save_yesica_message(phone, transcription, is_audio=True)
        except Exception as e:
            logger.error(f"Error transcribing Yésica's audio for {phone}: {e}")
    elif text:
        conv.add_message("assistant", text)
        save_yesica_message(phone, text, is_audio=False)

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
