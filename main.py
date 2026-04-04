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
from bot.flow import process_message, send_followup_if_needed, send_reminder_if_needed, send_auto_cancel_if_needed, _get_phone_lock
from config import get_settings
from services.evolution import extract_phone, is_group_message, is_bot_sent_message

COLOMBIA_TZ = ZoneInfo("America/Bogota")
TAKEOVER_WINDOW_MINUTES = 5

# Follow-up scheduler interval (seconds) — check every 4 hours
FOLLOWUP_CHECK_INTERVAL = 4 * 60 * 60

# Webhook deduplication — Evolution API can retry webhooks on slow responses
from collections import deque
_processed_message_ids: deque[str] = deque(maxlen=2000)

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
            conversations_dir = get_settings().conversations_dir
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
            conversations_dir = get_settings().conversations_dir
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
    settings = get_settings()
    os.makedirs(settings.conversations_dir, exist_ok=True)
    os.makedirs(settings.credentials_dir, exist_ok=True)
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
    from services.calendar import _get_credentials, _get_service
    creds = _get_credentials()
    cal_status = "error"
    cal_detail = ""
    if creds is None:
        cal_detail = "credentials missing or expired (no refresh_token?)"
    elif not creds.valid:
        cal_detail = "credentials invalid after refresh attempt"
    else:
        service = _get_service()
        if service:
            cal_status = "connected"
        else:
            cal_detail = "service build failed"
    return {
        "status": "ok",
        "bot": "Estetica Real — Valen",
        "calendar": cal_status,
        "calendar_detail": cal_detail or None,
    }


@app.get("/diagnose-calendar")
async def diagnose_calendar():
    """Full calendar diagnostic: credentials, read events, create+delete test event."""
    from services.calendar import (
        _get_credentials, _get_service, get_available_slots,
        verify_slot_available, COLOMBIA_TZ,
    )
    from datetime import datetime, timedelta

    results = {"steps": [], "errors": []}

    def step_ok(name, detail=""):
        results["steps"].append({"step": name, "status": "ok", "detail": detail})

    def step_fail(name, detail=""):
        results["steps"].append({"step": name, "status": "FAIL", "detail": detail})
        results["errors"].append(f"{name}: {detail}")

    # 1. Credentials
    creds = _get_credentials()
    if creds and creds.valid:
        has_refresh = bool(creds.refresh_token)
        step_ok("credentials", f"valid=True, has_refresh_token={has_refresh}, expiry={creds.expiry}")
    else:
        step_fail("credentials", "None or invalid")
        results["overall"] = "FAIL"
        return results

    # 2. Service
    service = _get_service()
    if service:
        step_ok("service", "built successfully")
    else:
        step_fail("service", "build returned None")
        results["overall"] = "FAIL"
        return results

    # 3. Read events
    settings = get_settings()
    cal_id = settings.google_calendar_id
    now = datetime.now(COLOMBIA_TZ)
    try:
        events_result = service.events().list(
            calendarId=cal_id,
            timeMin=now.isoformat(),
            timeMax=(now + timedelta(days=7)).isoformat(),
            singleEvents=True, orderBy="startTime", maxResults=10,
        ).execute()
        events = events_result.get("items", [])
        event_summaries = [
            f"{e.get('start',{}).get('dateTime','?')} — {e.get('summary','?')}"
            for e in events
        ]
        step_ok("read_events", f"{len(events)} events in next 7 days: {event_summaries}")
    except Exception as e:
        step_fail("read_events", str(e))

    # 4. Write test: create + delete
    try:
        test_time = now + timedelta(days=14, hours=3)
        test_time = test_time.replace(minute=0, second=0, microsecond=0)
        test_event = {
            "summary": "TEST diagnose-calendar (auto-delete)",
            "start": {"dateTime": test_time.isoformat(), "timeZone": "America/Bogota"},
            "end": {"dateTime": (test_time + timedelta(minutes=30)).isoformat(), "timeZone": "America/Bogota"},
        }
        created = service.events().insert(calendarId=cal_id, body=test_event).execute()
        event_id = created.get("id")
        step_ok("create_event", f"id={event_id}, start={created.get('start',{}).get('dateTime')}")

        service.events().delete(calendarId=cal_id, eventId=event_id).execute()
        step_ok("delete_event", f"deleted {event_id}")
    except Exception as e:
        step_fail("write_test", str(e))

    # 5. Async pipeline: available slots
    try:
        slots = await get_available_slots(days_ahead=7)
        step_ok("available_slots", f"{len(slots)} slots in next 7 days")
        if slots:
            result = await verify_slot_available(slots[0])
            step_ok("verify_slot", f"slot={slots[0].isoformat()}, available={result}")
    except Exception as e:
        step_fail("booking_pipeline", str(e))

    results["overall"] = "FAIL" if results["errors"] else "ALL_OK"
    return results


@app.post("/check-followups")
async def check_followups():
    """Manual trigger for follow-up checks (also runs automatically every 4h)."""
    conversations_dir = get_settings().conversations_dir
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

    # Dedup — Evolution API may retry webhook delivery for the same message
    if message_id and message_id in _processed_message_ids:
        return JSONResponse({"status": "ignored", "reason": "duplicate"})
    if message_id:
        _processed_message_ids.append(message_id)

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

    lock = await _get_phone_lock(phone)
    async with lock:
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
                    base64_data = await evo_service.get_media_base64(audio_key_id, phone=phone, from_me=True)
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
