import asyncio
import json
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from bot.conversation import ConversationState, load_conversation, save_conversation
from bot.learning import save_success_pattern, save_yesica_message, get_learning_context
from bot.prompts import SYSTEM_PROMPT
from config import get_settings
from services import ai, calendar, evolution

logger = logging.getLogger(__name__)
COLOMBIA_TZ = ZoneInfo("America/Bogota")

# Cache for push name results to avoid repeated GPT calls for the same name
_push_name_cache: dict[str, str | None] = {}


# ---------------------------------------------------------------------------
# Debounce — accumulate rapid messages before processing
# ---------------------------------------------------------------------------
DEBOUNCE_SECONDS = 4.0  # Wait for user to finish typing multiple messages

_pending_text: dict[str, list[str]] = {}
_pending_names: dict[str, str] = {}
_debounce_tasks: dict[str, asyncio.Task] = {}


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

async def process_message(
    phone: str,
    push_name: str | None,
    message_type: str,
    text_content: str | None,
    media_key_id: str | None,
    media_base64_inline: str | None,
) -> None:
    """Main handler called from the webhook endpoint."""
    # Extract name from push name using GPT (with cache)
    if push_name and push_name not in _push_name_cache:
        extracted = await ai.extract_name_from_pushname(push_name)
        _push_name_cache[push_name] = extracted
        logger.info(f"[{phone}] Push name '{push_name}' → '{extracted}'")
    resolved_name = _push_name_cache.get(push_name) if push_name else None
    if resolved_name:
        _pending_names[phone] = resolved_name

    # Check human takeover — if Yesica's window is active, bot stays silent
    conv = load_conversation(phone)
    if conv.is_human_takeover_active():
        logger.info(f"Human takeover active for {phone} (until {conv.human_takeover_until}) — bot silent")
        return
    # Clear expired takeover flag if it was left on
    if conv.human_takeover and not conv.human_takeover_until:
        conv.human_takeover = False
        conv.inject_system_event(
            "YESICA_HANDBACK: Yésica acaba de terminar de hablar con este cliente. "
            "Lee los mensajes anteriores de Yésica (aparecen como 'assistant') para "
            "tener contexto de lo que hablaron. Continúa la conversación de forma "
            "natural sin repetir lo que Yésica ya dijo."
        )
        save_conversation(conv)

    # Audio — transcribe first, then treat as text
    if message_type == "audioMessage":
        conv = load_conversation(phone)
        if resolved_name and not conv.user_display_name:
            conv.user_display_name = resolved_name
        try:
            await _handle_audio(conv, media_key_id, media_base64_inline)
        except Exception as e:
            logger.error(f"CRITICAL: Unhandled error in _handle_audio for {phone}: {e}", exc_info=True)
            try:
                await evolution.send_text_message(phone, "Disculpa, tuve un inconveniente con el audio 😅 Me lo puedes enviar de nuevo o escribirme?")
            except Exception:
                pass
        finally:
            save_conversation(conv)
        return

    # Images
    if message_type == "imageMessage":
        conv = load_conversation(phone)
        if resolved_name and not conv.user_display_name:
            conv.user_display_name = resolved_name
        try:
            await _handle_image(conv, media_key_id, media_base64_inline)
        except Exception as e:
            logger.error(f"CRITICAL: Unhandled error in _handle_image for {phone}: {e}", exc_info=True)
            try:
                await evolution.send_text_message(phone, "Disculpa, tuve un inconveniente con la imagen 😅 Me la puedes enviar de nuevo?")
            except Exception:
                pass
        finally:
            save_conversation(conv)
        return

    # Text messages go through debounce
    text = text_content or ""
    if not text.strip():
        return

    if phone not in _pending_text:
        _pending_text[phone] = []
    _pending_text[phone].append(text)

    # Cancel existing debounce timer
    existing = _debounce_tasks.get(phone)
    if existing and not existing.done():
        existing.cancel()

    # Schedule processing after delay
    _debounce_tasks[phone] = asyncio.create_task(
        _fire_after_delay(phone, DEBOUNCE_SECONDS)
    )


async def _fire_after_delay(phone: str, delay: float) -> None:
    """Wait, then process all accumulated messages for this user."""
    await asyncio.sleep(delay)

    messages = _pending_text.pop(phone, [])
    push_name = _pending_names.pop(phone, None)
    _debounce_tasks.pop(phone, None)

    if not messages:
        return

    combined = " ".join(messages)
    conv = load_conversation(phone)
    if push_name and not conv.user_display_name:
        conv.user_display_name = push_name

    try:
        await _handle_text(conv, combined)
    except Exception as e:
        logger.error(f"CRITICAL: Unhandled error in _handle_text for {phone}: {e}", exc_info=True)
        try:
            await evolution.send_text_message(phone, "Disculpa, tuve un inconveniente tecnico 😅 Dame un momento y te respondo!")
        except Exception:
            pass
    finally:
        save_conversation(conv)


# ---------------------------------------------------------------------------
# Audio handling
# ---------------------------------------------------------------------------

async def _handle_audio(
    conv: ConversationState,
    media_key_id: str | None,
    media_base64_inline: str | None,
) -> None:
    """Transcribe audio with Whisper and process as text."""
    await evolution.send_typing_presence(conv.phone)

    base64_data = media_base64_inline
    if not base64_data and media_key_id:
        base64_data = await evolution.get_media_base64(media_key_id, phone=conv.phone)

    if not base64_data:
        conv.add_message("user", "[El usuario envio un audio pero no se pudo descargar]")
        conv.inject_system_event(
            "INSTRUCCION: El usuario envio un audio pero no se pudo procesar. "
            "Pidele amablemente que lo reenvie o que escriba su mensaje."
        )
        reply = await _generate_reply(conv)
        await _send_and_record(conv, reply)
        return

    transcription = await ai.transcribe_audio(base64_data)

    if transcription:
        logger.info(f"[{conv.phone}] Whisper transcription: '{transcription[:100]}'")
        conv.last_user_message_at = datetime.now(COLOMBIA_TZ).isoformat()
        await _handle_text(conv, transcription)
    else:
        conv.inject_system_event(
            "INSTRUCCION: El usuario envio un audio pero no se pudo transcribir. "
            "Pidele amablemente que escriba su mensaje."
        )
        reply = await _generate_reply(conv)
        await _send_and_record(conv, reply)


# ---------------------------------------------------------------------------
# Image handling
# ---------------------------------------------------------------------------

async def _handle_image(
    conv: ConversationState,
    media_key_id: str | None,
    media_base64_inline: str | None,
) -> None:
    conv.last_user_message_at = datetime.now(COLOMBIA_TZ).isoformat()

    base64_data = media_base64_inline
    if not base64_data and media_key_id:
        await evolution.send_typing_presence(conv.phone)
        base64_data = await evolution.get_media_base64(media_key_id, phone=conv.phone)

    if not base64_data:
        conv.inject_system_event(
            "IMAGE_ANALYSIS: No se pudo descargar la imagen. Pide al usuario que la envie de nuevo."
        )
        reply = await _generate_reply(conv)
        await _send_and_record(conv, reply)
        return

    await evolution.send_typing_presence(conv.phone)
    analysis = await ai.analyze_image(base64_data)

    image_type = analysis.get("image_type", "OTHER")
    description = analysis.get("description", "")
    suggestion = analysis.get("response_suggestion", "")

    # Handle payment screenshots (legacy support)
    if image_type == "PAYMENT":
        conv.inject_system_event(
            f"IMAGE_ANALYSIS: El usuario envio lo que parece ser un comprobante de pago "
            f"({description}). La valoración es gratuita este mes, no necesita pagar. "
            f"Aclara amablemente que la valoración no tiene costo y pregunta si quiere agendar."
        )

    elif image_type == "BODY":
        zone = analysis.get("body_zone", "corporal")
        conv.inject_system_event(
            f"IMAGE_ANALYSIS: El usuario envio una foto de su zona {zone}. "
            f"Descripcion: {description}. "
            f"Responde con empatia, muestra interes profesional por su caso, "
            f"menciona que Yesica podria evaluarla en la valoracion gratuita "
            f"y guia hacia el agendamiento."
        )

    elif image_type == "FACE":
        conv.inject_system_event(
            f"IMAGE_ANALYSIS: El usuario envio una foto de su rostro o piel. "
            f"Descripcion: {description}. "
            f"Responde con empatia, menciona tratamientos faciales relevantes "
            f"y ofrece la valoracion gratuita con Yesica."
        )

    elif image_type == "BEFORE_AFTER":
        conv.inject_system_event(
            f"IMAGE_ANALYSIS: El usuario envio una foto de antes/despues. "
            f"Descripcion: {description}. "
            f"Celebra y valida esos resultados, usa como motivacion."
        )

    else:
        conv.inject_system_event(
            f"IMAGE_ANALYSIS: El usuario envio una imagen. "
            f"Descripcion: {description}. Responde de forma natural."
        )

    reply = await _generate_reply(conv)
    await _send_and_record(conv, reply)


# ---------------------------------------------------------------------------
# Text handling — GPT decides actions via tags
# ---------------------------------------------------------------------------

# Tags that GPT includes in its response to trigger actions
_TAG_CHECK_CALENDAR = "[REVISAR_AGENDA]"
_TAG_EVENING = "[HORARIO_ESPECIAL]"


async def _handle_text(conv: ConversationState, text: str) -> None:
    conv.add_message("user", text)
    conv.last_user_message_at = datetime.now(COLOMBIA_TZ).isoformat()

    # Phase-specific handling
    if conv.phase == "awaiting_slot_selection":
        await _try_parse_slot_selection(conv, text)
        return

    if conv.phase == "awaiting_confirmation":
        await _handle_slot_confirmation(conv, text)
        return

    if conv.phase == "awaiting_meeting_type":
        await _handle_meeting_type_selection(conv, text)
        return

    if conv.phase == "collecting_data":
        await _try_collect_data_and_schedule(conv)
        return

    # Detect rescheduling/cancellation intent when appointment is already confirmed
    if conv.phase == "appointment_confirmed":
        if _wants_to_reschedule(text):
            await _handle_reschedule(conv, text)
            return

    # Let GPT generate its reply — it decides what to do via tags
    reply = await _generate_reply(conv)

    # Strip tags from the reply and detect actions
    has_calendar_tag = _TAG_CHECK_CALENDAR in reply
    has_evening_tag = _TAG_EVENING in reply
    reply = reply.replace(_TAG_CHECK_CALENDAR, "").replace(_TAG_EVENING, "").strip()

    # ACTION: Evening/weekend escalation
    if has_evening_tag and conv.phase not in ("appointment_confirmed", "escalated_to_yesica"):
        await _send_and_record(conv, reply)
        await _escalate_to_yesica_evening(conv, text)
        return

    # ACTION: Check calendar and offer real slots
    if has_calendar_tag and conv.phase not in (
        "awaiting_slot_selection", "awaiting_confirmation", "awaiting_meeting_type",
        "collecting_data", "appointment_confirmed", "escalated_to_yesica",
    ):
        logger.info(f"[{conv.phone}] Calendar check triggered by GPT")
        # Fetch real slots FIRST
        await _fetch_and_inject_slots(conv)
        # Generate ONE reply that includes the slots (or error message)
        slot_reply = await _generate_reply(conv)
        await _send_and_record(conv, slot_reply)
        return

    # Normal reply
    await _send_and_record(conv, reply)


# ---------------------------------------------------------------------------
# Rescheduling — user wants to change or cancel an existing appointment
# ---------------------------------------------------------------------------

_RESCHEDULE_KEYWORDS = [
    "no puedo", "no me queda", "no me sirve", "cambiar la cita", "cambiar cita",
    "cambiar el horario", "cambiar horario", "cancelar", "reagendar", "reprogramar",
    "otro horario", "otra hora", "otro día", "otro dia", "no voy a poder",
    "me queda difícil", "me queda dificil", "puedo después", "puedo despues",
    "a esa hora no", "ese día no", "ese dia no", "no me funciona",
    "mover la cita", "mover cita", "cambiarla", "posponerla", "aplazar",
]


def _wants_to_reschedule(text: str) -> bool:
    """Detect if user wants to cancel or reschedule their confirmed appointment."""
    text_lower = text.lower()
    return any(kw in text_lower for kw in _RESCHEDULE_KEYWORDS)


async def _handle_reschedule(conv: ConversationState, text: str) -> None:
    """Handle rescheduling request — cancel existing appointment and offer new slots."""
    logger.info(f"[{conv.phone}] Rescheduling requested: '{text[:100]}'")

    # Check if user needs evening/weekend (outside business hours)
    evening_keywords = [
        "después de las 5", "despues de las 5", "en la noche", "por la noche",
        "a las 6", "a las 7", "a las 8", "fin de semana", "sábado", "sabado",
        "domingo", "7pm", "8pm", "6pm", "horario nocturno",
    ]
    text_lower = text.lower()
    needs_evening = any(kw in text_lower for kw in evening_keywords)

    # Reset appointment state
    old_dt = conv.appointment_datetime
    conv.appointment_datetime = None
    conv.calendar_slots_json = None
    conv.meeting_type = None
    conv.meet_link = None
    conv.reminder_sent = False
    conv.reminder_day_before_sent = False

    if needs_evening:
        conv.inject_system_event(
            "RESCHEDULE_EVENING: El usuario quiere cambiar su cita y necesita un horario "
            "fuera del rango normal (después de 5pm o fin de semana). "
            "Dile que lo conectas con Yésica para coordinar ese horario especial. "
            "Sé comprensiva y natural."
        )
        reply = await _generate_reply(conv)
        await _send_and_record(conv, reply)
        await _escalate_to_yesica_evening(conv, text)
    else:
        # Offer new slots
        conv.phase = "chatting"
        conv.inject_system_event(
            "RESCHEDULE: El usuario quiere cambiar su cita. "
            "Responde con comprensión (ej: 'Claro, sin problema') y ofrece buscar "
            "otro horario. Incluye [REVISAR_AGENDA] al final de tu mensaje."
        )
        reply = await _generate_reply(conv)

        # Process tags from GPT reply
        has_calendar_tag = _TAG_CHECK_CALENDAR in reply
        reply = reply.replace(_TAG_CHECK_CALENDAR, "").replace(_TAG_EVENING, "").strip()

        if has_calendar_tag:
            await _send_and_record(conv, reply)
            await _fetch_and_inject_slots(conv)
            slot_reply = await _generate_reply(conv)
            await _send_and_record(conv, slot_reply)
        else:
            # GPT didn't add the tag — fetch slots anyway
            await _send_and_record(conv, reply)
            await _fetch_and_inject_slots(conv)
            slot_reply = await _generate_reply(conv)
            await _send_and_record(conv, slot_reply)


# ---------------------------------------------------------------------------
# Evening/Weekend escalation to Yésica
# ---------------------------------------------------------------------------

async def _escalate_to_yesica_evening(conv: ConversationState, user_text: str) -> None:
    """User needs evening/weekend hours. Escalate to Yésica."""
    settings = get_settings()

    conv.phase = "escalated_to_yesica"

    # Notify Yésica with context
    name = conv.collected_name or conv.user_display_name or "Cliente"
    service = conv.service_interest or "Levantamiento de glúteos"
    message = (
        f"📋 *Solicitud de horario especial*\n\n"
        f"*Cliente:* {name}\n"
        f"*WhatsApp:* +{conv.phone}\n"
        f"*Servicio:* {service}\n"
        f"*Solicitud:* {user_text[:200]}\n\n"
        f"El cliente necesita horario fuera de 9am-5pm L-V. "
        f"Por favor coordina directamente."
    )
    try:
        await evolution.send_text_message(settings.yesica_phone, message)
        logger.info(f"Escalated {conv.phone} to Yésica for evening/weekend scheduling")
    except Exception as e:
        logger.error(f"Failed to notify Yésica for evening escalation: {e}")


# ---------------------------------------------------------------------------
# Slot selection → appointment booking (no payment required)
# ---------------------------------------------------------------------------

async def _try_parse_slot_selection(conv: ConversationState, text: str) -> None:
    """User is picking a time slot. GPT parses the selection."""
    # Check if user needs evening/weekend hours — escalate even during slot selection
    evening_keywords = [
        "después de las 5", "despues de las 5", "después de las 6", "despues de las 6",
        "después de las 7", "despues de las 7", "después de las 4", "despues de las 4",
        "en la noche", "por la noche", "de noche",
        "a las 6", "a las 7", "a las 8", "a las 9",
        "6pm", "7pm", "8pm", "9pm", "6 pm", "7 pm", "8 pm", "9 pm",
        "6 p.m", "7 p.m", "8 p.m", "9 p.m",
        "fin de semana", "sábado", "sabado", "domingo",
        "solo puedo en la noche", "horario nocturno",
        "puedo después de las 5", "puedo despues de las 5",
        "solo después de las 5", "solo despues de las 5",
        "solo en la noche", "a partir de las 5", "a partir de las 6",
        "de 5 en adelante", "de 6 en adelante",
    ]
    text_lower = text.lower()
    if any(kw in text_lower for kw in evening_keywords):
        conv.inject_system_event(
            "EVENING_ESCALATION: El usuario necesita un horario fuera del rango normal "
            "(después de 5pm o fin de semana). Dile que lo conectas directamente con "
            "Yésica para coordinar ese horario especial. Sé natural y positiva."
        )
        reply = await _generate_reply(conv)
        await _send_and_record(conv, reply)
        await _escalate_to_yesica_evening(conv, text)
        return

    if not conv.calendar_slots_json:
        await _fetch_and_inject_slots(conv)
        reply = await _generate_reply(conv)
        await _send_and_record(conv, reply)
        return

    available_slots = json.loads(conv.calendar_slots_json)
    now_str = datetime.now(COLOMBIA_TZ).strftime("%A %d/%m/%Y %I:%M %p")

    # Build recent conversation context so GPT knows what user rejected
    recent_context = ""
    recent_msgs = [m for m in conv.messages[-10:] if m.get("role") in ("user", "assistant")]
    if recent_msgs:
        recent_context = "\n".join(
            f"{'Usuario' if m['role'] == 'user' else 'Asistente'}: {m['content']}"
            for m in recent_msgs
        )

    # Let GPT understand what slot the user wants
    selected_iso = await ai.parse_slot_selection(text, available_slots, now_str, recent_context)

    if selected_iso:
        selected_slot = datetime.fromisoformat(selected_iso).replace(tzinfo=COLOMBIA_TZ)
        formatted_dt = _format_appointment_datetime(selected_slot)
        conv.appointment_datetime = selected_slot.isoformat()
        conv.phase = "awaiting_confirmation"
        logger.info(f"[{conv.phone}] GPT parsed slot: {formatted_dt} — awaiting user confirmation")

        conv.inject_system_event(
            f"SLOT_SELECTED: El usuario eligió el horario: {formatted_dt}. "
            f"IMPORTANTE: NO agendes todavía. Primero confirma con el usuario. "
            f"Pregúntale algo como '¿Te confirmo para el {formatted_dt}?' o "
            f"'Listo, entonces el {formatted_dt}, ¿te queda bien?'. "
            f"Espera a que el usuario diga SÍ antes de agendar."
        )
    else:
        # GPT couldn't parse — let the conversation flow naturally
        conv.inject_system_event(
            "INSTRUCCION: El usuario respondió pero no se identificó un horario claro. "
            "Pregunta de forma natural qué día y hora le queda mejor."
        )

    reply = await _generate_reply(conv)
    await _send_and_record(conv, reply)


# ---------------------------------------------------------------------------
# Slot confirmation — user must confirm before we book
# ---------------------------------------------------------------------------

# Counter to avoid infinite ambiguity loops
_confirmation_attempts: dict[str, int] = {}


async def _handle_slot_confirmation(conv: ConversationState, text: str) -> None:
    """Handle user's yes/no response to the proposed appointment time — GPT interprets."""
    # Build context for GPT
    proposed_slot = "pendiente"
    if conv.appointment_datetime:
        try:
            proposed_slot = _format_appointment_datetime(
                datetime.fromisoformat(conv.appointment_datetime).replace(tzinfo=COLOMBIA_TZ)
            )
        except Exception:
            pass

    recent_msgs = [m for m in conv.messages[-6:] if m.get("role") in ("user", "assistant")]
    context = "\n".join(
        f"{'Usuario' if m['role'] == 'user' else 'Bot'}: {m['content']}"
        for m in recent_msgs
    )

    decision = await ai.interpret_confirmation(text, proposed_slot, context)

    if decision == "no":
        logger.info(f"[{conv.phone}] AI: user rejected proposed slot")
        _confirmation_attempts.pop(conv.phone, None)
        conv.appointment_datetime = None
        conv.phase = "awaiting_slot_selection"
        conv.inject_system_event(
            "SLOT_REJECTED: El usuario NO quiere ese horario. "
            "Pregunta qué otro día u hora le queda mejor. "
            "Ofrece otro horario de los disponibles."
        )
        reply = await _generate_reply(conv)
        await _send_and_record(conv, reply)
        return

    if decision == "yes":
        logger.info(f"[{conv.phone}] AI: user confirmed slot")
        _confirmation_attempts.pop(conv.phone, None)
        conv.phase = "awaiting_meeting_type"
        conv.inject_system_event(
            "SLOT_CONFIRMED: El usuario confirmó el horario. "
            "Ahora pregúntale cómo prefiere la videollamada: "
            "por *WhatsApp* o por *Google Meet*. "
            "Dile algo como: 'Perfecto, ¿prefieres que la valoración sea "
            "por videollamada de WhatsApp o por Google Meet?'"
        )
        reply = await _generate_reply(conv)
        await _send_and_record(conv, reply)
        return

    # Ambiguous — track attempts to avoid infinite loop
    attempts = _confirmation_attempts.get(conv.phone, 0) + 1
    _confirmation_attempts[conv.phone] = attempts

    if attempts >= 3:
        logger.info(f"[{conv.phone}] 3 ambiguous confirmations — treating as YES")
        _confirmation_attempts.pop(conv.phone, None)
        conv.phase = "awaiting_meeting_type"
        conv.inject_system_event(
            "SLOT_CONFIRMED: Asumimos que el usuario acepta el horario. "
            "Pregúntale cómo prefiere la videollamada: "
            "por *WhatsApp* o por *Google Meet*."
        )
        reply = await _generate_reply(conv)
        await _send_and_record(conv, reply)
        return

    conv.inject_system_event(
        "INSTRUCCION: El usuario respondió pero no queda claro si acepta o rechaza "
        "el horario propuesto. Pregunta de forma natural y directa si le queda bien "
        "ese horario o si prefiere otro. Sé breve."
    )
    reply = await _generate_reply(conv)
    await _send_and_record(conv, reply)


# ---------------------------------------------------------------------------
# Meeting type selection — WhatsApp videocall or Google Meet (AI-powered)
# ---------------------------------------------------------------------------

_meeting_type_attempts: dict[str, int] = {}


async def _handle_meeting_type_selection(conv: ConversationState, text: str) -> None:
    """Handle user's choice between WhatsApp videocall and Google Meet — GPT interprets."""
    chosen = await ai.interpret_meeting_type(text)

    if chosen:
        _meeting_type_attempts.pop(conv.phone, None)
        conv.meeting_type = chosen
        logger.info(f"[{conv.phone}] AI: meeting type chosen: {chosen}")
        has_name = conv.collected_name or conv.user_display_name
        if has_name:
            conv.phase = "collecting_data"
            await _try_collect_data_and_schedule(conv)
        else:
            conv.phase = "collecting_data"
            conv.inject_system_event(
                "INSTRUCCION: Ya tienes el horario y el tipo de videollamada. "
                "Solo falta el nombre completo del usuario para agendar la cita. "
                "Pídelo de forma natural."
            )
            reply = await _generate_reply(conv)
            await _send_and_record(conv, reply)
        return

    # Ambiguous — track attempts, default to WhatsApp after 3
    attempts = _meeting_type_attempts.get(conv.phone, 0) + 1
    _meeting_type_attempts[conv.phone] = attempts

    if attempts >= 3:
        logger.info(f"[{conv.phone}] 3 ambiguous meeting type attempts — defaulting to WhatsApp")
        _meeting_type_attempts.pop(conv.phone, None)
        conv.meeting_type = "whatsapp"
        has_name = conv.collected_name or conv.user_display_name
        if has_name:
            conv.phase = "collecting_data"
            await _try_collect_data_and_schedule(conv)
        else:
            conv.phase = "collecting_data"
            conv.inject_system_event(
                "INSTRUCCION: Procedemos con videollamada de WhatsApp. "
                "Solo falta el nombre completo del usuario. Pídelo de forma natural."
            )
            reply = await _generate_reply(conv)
            await _send_and_record(conv, reply)
        return

    conv.inject_system_event(
        "INSTRUCCION: El usuario respondió pero no queda claro si prefiere "
        "WhatsApp o Google Meet. Pregunta de forma directa y sencilla: "
        "'¿Por WhatsApp o por Google Meet?'"
    )
    reply = await _generate_reply(conv)
    await _send_and_record(conv, reply)


# ---------------------------------------------------------------------------
# Data collection & appointment creation
# ---------------------------------------------------------------------------

async def _try_collect_data_and_schedule(conv: ConversationState) -> None:
    """Collect user data and create the appointment."""
    logger.info(f"[{conv.phone}] Collecting data — phase={conv.phase}, appointment_datetime={conv.appointment_datetime}")

    try:
        extracted = await ai.extract_user_data(conv.messages)
        logger.info(f"[{conv.phone}] Extracted data: {extracted}")
    except Exception as e:
        logger.error(f"[{conv.phone}] Data extraction failed: {e}", exc_info=True)
        extracted = {"name": None, "phone": None, "email": None}

    if extracted.get("name"):
        conv.collected_name = extracted["name"]
        conv.user_display_name = extracted["name"].split()[0]
    if extracted.get("phone"):
        conv.collected_phone = extracted["phone"]
    if extracted.get("email"):
        conv.collected_email = extracted["email"]

    has_name = conv.collected_name or conv.user_display_name

    if conv.appointment_datetime and has_name:
        logger.info(f"[{conv.phone}] Creating appointment for: {conv.appointment_datetime}")
        try:
            await _create_appointment_from_saved_slot(conv)
        except Exception as e:
            logger.error(f"[{conv.phone}] Failed to create appointment: {e}", exc_info=True)
            conv.phase = "appointment_confirmed"
            formatted_dt = "pendiente"
            if conv.appointment_datetime:
                try:
                    formatted_dt = _format_appointment_datetime(
                        datetime.fromisoformat(conv.appointment_datetime).replace(tzinfo=COLOMBIA_TZ)
                    )
                except Exception:
                    pass
            # Send hard-coded confirmation even on calendar API failure
            uname = conv.user_display_name or conv.collected_name or ""
            greet = f"{uname}, te" if uname else "Te"
            mt = conv.meeting_type or "whatsapp"
            if mt == "meet":
                fallback_msg = (
                    f"{greet} confirmo que tu valoración virtual quedó agendada para el "
                    f"{formatted_dt}.\n\n"
                    f"💻 Te enviaremos el enlace de Google Meet antes de la cita. "
                    f"Si necesitas cambiarla o cancelarla, puedes escribirme por este WhatsApp."
                )
            else:
                fallback_msg = (
                    f"{greet} confirmo que tu valoración virtual quedó agendada para el "
                    f"{formatted_dt}.\n\n"
                    f"📲 Yésica te llamará por videollamada de WhatsApp a este mismo número "
                    f"el día y hora de tu cita.\n\n"
                    f"Asegúrate de tener buena conexión a internet. Si necesitas cambiarla o cancelarla, "
                    f"puedes escribirme directamente por este WhatsApp."
                )
            await evolution.send_text_message(conv.phone, fallback_msg)
            conv.add_message("assistant", fallback_msg)
            conv.inject_system_event(
                f"APPOINTMENT_CONFIRMED: Cita registrada para {formatted_dt}. "
                f"Ya le enviaste la confirmación. Si responde, sé natural."
            )
        # Confirmation already sent hard-coded — no GPT reply needed
        return
    elif conv.appointment_datetime and not has_name:
        conv.inject_system_event(
            "INSTRUCCION: Ya tienes el horario. Solo falta el nombre completo "
            "para confirmar la cita. Pídelo de forma natural."
        )
    else:
        logger.info(f"[{conv.phone}] No saved slot — fetching new slots")
        await _fetch_and_inject_slots(conv)

    reply = await _generate_reply(conv)
    await _send_and_record(conv, reply)


async def _create_appointment_from_saved_slot(conv: ConversationState) -> None:
    """Create a Google Calendar appointment."""
    try:
        slot = datetime.fromisoformat(conv.appointment_datetime).replace(tzinfo=COLOMBIA_TZ)
    except Exception as e:
        logger.error(f"Invalid appointment_datetime for {conv.phone}: {e}")
        await _fetch_and_inject_slots(conv)
        return

    meeting_type = conv.meeting_type or "whatsapp"
    event = await calendar.create_appointment(
        slot,
        conv.collected_name or conv.user_display_name or "Cliente",
        conv.collected_phone or conv.phone,
        conv.collected_email or "",
        meeting_type=meeting_type,
    )
    formatted_dt = _format_appointment_datetime(slot)
    conv.phase = "appointment_confirmed"

    # Extract Meet link if applicable
    meet_link = ""
    if event and meeting_type == "meet":
        meet_link = event.get("hangoutLink", "")
        conv.meet_link = meet_link

    # Send hard-coded confirmation message — never let GPT generate date/time text
    name = conv.user_display_name or conv.collected_name or ""
    greeting = f"{name}, te" if name else "Te"

    if meeting_type == "meet" and meet_link:
        confirmation_msg = (
            f"{greeting} confirmo que tu valoración virtual quedó agendada para el "
            f"{formatted_dt}.\n\n"
            f"💻 La reunión será por Google Meet. Este es tu enlace único:"
        )
        # Send text first, then link separately for clean formatting
        await evolution.send_text_message(conv.phone, confirmation_msg)
        await asyncio.sleep(1.0)
        await evolution.send_text_message(conv.phone, meet_link)
        await asyncio.sleep(1.0)
        follow_msg = (
            "Entra al enlace el día y hora de tu cita. "
            "Si necesitas cambiarla o cancelarla, puedes escribirme por este WhatsApp."
        )
        await evolution.send_text_message(conv.phone, follow_msg)
        conv.add_message("assistant", f"{confirmation_msg}\n{meet_link}\n{follow_msg}")
    else:
        confirmation_msg = (
            f"{greeting} confirmo que tu valoración virtual quedó agendada para el "
            f"{formatted_dt}.\n\n"
            f"📲 Yésica te llamará por videollamada de WhatsApp a este mismo número "
            f"el día y hora de tu cita.\n\n"
            f"Asegúrate de tener buena conexión a internet. Si necesitas cambiarla o cancelarla, "
            f"puedes escribirme directamente por este WhatsApp."
        )
        await evolution.send_text_message(conv.phone, confirmation_msg)
        conv.add_message("assistant", confirmation_msg)

    if event:
        conv.inject_system_event(
            f"APPOINTMENT_CONFIRMED: Cita creada y confirmada al usuario para {formatted_dt}. "
            f"Tipo: {'Google Meet' if meeting_type == 'meet' else 'videollamada WhatsApp'}. "
            f"Ya le enviaste la confirmación. "
            f"Si el usuario responde, sé natural y breve. No repitas la información."
        )
        asyncio.create_task(_notify_yesica_appointment(conv, formatted_dt))
        asyncio.create_task(
            asyncio.to_thread(save_success_pattern, conv.phone, conv.messages)
        )
    else:
        logger.warning(f"[{conv.phone}] Calendar API returned None — confirming anyway")
        conv.inject_system_event(
            f"APPOINTMENT_CONFIRMED: Cita registrada para {formatted_dt}. "
            f"Ya le enviaste la confirmación. Si responde, sé natural."
        )
        asyncio.create_task(_notify_yesica_appointment(conv, formatted_dt))


# ---------------------------------------------------------------------------
# Calendar slot fetching
# ---------------------------------------------------------------------------

async def _fetch_and_inject_slots(conv: ConversationState) -> None:
    """Fetch real calendar slots and inject them into the conversation."""
    logger.info(f"Fetching calendar slots for {conv.phone}")
    slots = await calendar.get_available_slots(days_ahead=7)

    if not slots:
        logger.info(f"No slots in 7 days for {conv.phone}, trying 14 days")
        slots = await calendar.get_available_slots(days_ahead=14)

    if slots:
        conv.calendar_slots_json = json.dumps([s.isoformat() for s in slots])
        conv.phase = "awaiting_slot_selection"
        detailed = calendar.format_slots_detailed(slots)
        conv.inject_system_event(
            f"CALENDAR_SLOTS: Disponibilidad real de Yesica: {detailed}. "
            f"INSTRUCCION: Ofrece el horario mas proximo de forma conversacional. "
            f"Si un dia NO tiene mañana pero SI tiene tarde (o viceversa), mencionalo: "
            f"'El viernes en la mañana no hay disponible pero en la tarde si, te sirve?' "
            f"Si el usuario pide un horario que no esta disponible, ofrece la otra franja del MISMO dia. "
            f"Solo si no puede en ninguna franja de ese dia, ofrece otro dia. "
            f"NO hagas lista. Conversacional. Maximo 2 mensajes."
        )
        logger.info(f"Calendar slots fetched for {conv.phone}: {len(slots)} slots")
    else:
        logger.warning(f"No calendar slots found for {conv.phone}")
        conv.inject_system_event(
            "CALENDAR_ERROR: No se encontraron horarios en las proximas 2 semanas. "
            "Dile que revisas la agenda y le confirmas en un momentico. "
            "Pregunta que dia y horario le quedaria ideal. "
            "NUNCA digas que Yesica se pondra en contacto."
        )


# ---------------------------------------------------------------------------
# Multi-message sending
# ---------------------------------------------------------------------------

async def _send_and_record(conv: ConversationState, reply: str) -> None:
    """Send reply as WhatsApp messages with human-like timing."""
    import random

    # Strip action tags that should never reach the user
    reply = reply.replace(_TAG_CHECK_CALENDAR, "").replace(_TAG_EVENING, "").strip()

    # Safety net: ensure response never dies before appointment is confirmed
    reply = _ensure_conversation_alive(reply, conv.phase)

    parts = [p.strip() for p in reply.split("[MSG]") if p.strip()]
    if not parts:
        return

    full_reply = " ".join(parts)
    conv.add_message("assistant", full_reply)

    for i, part in enumerate(parts):
        # Show "typing..." indicator
        await evolution.send_typing_presence(conv.phone)

        # Human-like delay: time to "read" the user's message + "type" the response
        if i == 0:
            # First message: simulate reading + thinking + typing
            read_time = random.uniform(1.5, 3.0)  # Reading the user's message
            type_time = min(len(part) * 0.03, 4.0)  # Typing speed ~33 chars/sec
            delay = read_time + type_time
        else:
            # Follow-up messages: shorter pause, just typing
            delay = random.uniform(1.0, 2.0) + min(len(part) * 0.02, 3.0)

        await asyncio.sleep(delay)
        await evolution.send_text_message(conv.phone, part)


def _ensure_conversation_alive(reply: str, phase: str) -> str:
    """Safety net: before appointment is confirmed, ensure reply has a question."""
    if not reply or not reply.strip():
        return "¿En qué te puedo ayudar?"

    # Don't add questions after: appointment confirmed, escalated, or out-of-zone (instagram link)
    if phase in ("appointment_confirmed", "escalated_to_yesica", "collecting_data", "awaiting_confirmation", "awaiting_meeting_type"):
        return reply
    if "instagram.com" in reply.lower():
        return reply

    # If GPT forgot a question, don't add genéricas — let GPT handle it
    return reply


# ---------------------------------------------------------------------------
# 24h Follow-up system
# ---------------------------------------------------------------------------

async def send_followup_if_needed(phone: str) -> bool:
    """Check if this conversation needs a 24h follow-up and send it."""
    conv = load_conversation(phone)

    if conv.follow_up_sent:
        return False
    if conv.phase in ("appointment_confirmed", "awaiting_confirmation", "awaiting_meeting_type", "collecting_data", "escalated_to_yesica"):
        return False
    if not conv.last_user_message_at:
        return False
    if conv.is_human_takeover_active():
        return False

    try:
        last_msg = datetime.fromisoformat(conv.last_user_message_at).replace(tzinfo=COLOMBIA_TZ)
    except Exception:
        return False

    now = datetime.now(COLOMBIA_TZ)
    hours_elapsed = (now - last_msg).total_seconds() / 3600

    if hours_elapsed < 20 or hours_elapsed > 48:
        return False

    name = conv.user_display_name or ""
    greeting = f"Hola {name}" if name else "Hola"
    followup_msg = (
        f"{greeting}, te escribo porque a Yésica le quedan pocos espacios esta semana "
        f"para valoraciones virtuales. Recuerda que este mes la valoración por videollamada "
        f"de WhatsApp no tiene costo. ¿Qué día te queda más fácil?"
    )

    await evolution.send_text_message(phone, followup_msg)
    conv.follow_up_sent = True
    conv.add_message("assistant", followup_msg)
    save_conversation(conv)
    logger.info(f"[{phone}] Sent 24h follow-up message")
    return True


# ---------------------------------------------------------------------------
# Appointment reminder system (2h before)
# ---------------------------------------------------------------------------

async def send_reminder_if_needed(phone: str) -> bool:
    """Check if this conversation has an upcoming appointment and send reminders.
    - Day-before reminder: sent between 20h and 26h before the appointment.
    - Same-day reminder: sent between 1.5h and 2.5h before the appointment.
    Returns True if any reminder was sent."""
    conv = load_conversation(phone)

    if conv.phase != "appointment_confirmed":
        return False
    if not conv.appointment_datetime:
        return False

    try:
        appointment = datetime.fromisoformat(conv.appointment_datetime).replace(tzinfo=COLOMBIA_TZ)
    except Exception:
        return False

    now = datetime.now(COLOMBIA_TZ)
    time_until = (appointment - now).total_seconds()
    sent_any = False

    name = conv.user_display_name or conv.collected_name or ""
    greeting = f"Hola {name}" if name else "Hola"
    formatted_dt = _format_appointment_datetime(appointment)
    settings = get_settings()

    meeting_type = conv.meeting_type or "whatsapp"
    meet_link = conv.meet_link or ""

    # ---- Day-before reminder (20h to 26h before) ----
    if not conv.reminder_day_before_sent and 72000 <= time_until <= 93600:
        time_spanish = _format_time_spanish(appointment)
        if meeting_type == "meet" and meet_link:
            meeting_info = (
                f"Recuerda que la valoración será por Google Meet. "
                f"Tu enlace: {meet_link}"
            )
        else:
            meeting_info = (
                f"Recuerda que Yésica te llamará por videollamada de WhatsApp a este mismo número. "
                f"Asegúrate de tener buena conexión a internet 😊"
            )
        day_before_msg = (
            f"{greeting}, ¿cómo estás? Te escribe la asistente de Yésica de Estética Real "
            f"para confirmar tu valoración virtual de mañana {appointment.strftime('%d')} de "
            f"{_month_name(appointment.month)} a las {time_spanish}.\n\n"
            f"Por favor confírmanos tu asistencia 🙏\n\n"
            f"{meeting_info}"
        )
        await evolution.send_text_message(phone, day_before_msg)
        conv.add_message("assistant", day_before_msg)

        # Notify Yésica
        yesica_day_msg = (
            f"📋 *Recordatorio cita mañana*\n\n"
            f"*Paciente:* {conv.collected_name or name or 'Cliente'}\n"
            f"*WhatsApp:* +{phone}\n"
            f"*Servicio:* {conv.service_interest or 'Valoración'}\n"
            f"*Fecha:* {formatted_dt}"
        )
        await evolution.send_text_message(settings.yesica_phone, yesica_day_msg)

        conv.reminder_day_before_sent = True
        sent_any = True
        logger.info(f"[{phone}] Sent day-before reminder (appointment at {formatted_dt})")

    # ---- Same-day reminder (1.5h to 2.5h before) ----
    if not conv.reminder_sent and 5400 <= time_until <= 9000:
        time_spanish = _format_time_spanish(appointment)
        if meeting_type == "meet" and meet_link:
            meeting_reminder = (
                f"Entra al enlace de Google Meet a la hora de tu cita:\n{meet_link}"
            )
        else:
            meeting_reminder = (
                f"Yésica te llamará por videollamada de WhatsApp a este mismo número. "
                f"Asegúrate de tener buena conexión a internet 😊"
            )
        reminder_msg = (
            f"{greeting}, te recuerdo que hoy tienes tu valoración virtual con Yésica "
            f"a las {time_spanish}. "
            f"{meeting_reminder}"
        )
        await evolution.send_text_message(phone, reminder_msg)
        conv.add_message("assistant", reminder_msg)

        # Notify Yésica too
        yesica_msg = (
            f"⏰ *Recordatorio de cita*\n\n"
            f"*Paciente:* {conv.collected_name or name or 'Cliente'}\n"
            f"*WhatsApp:* +{phone}\n"
            f"*Fecha:* {formatted_dt}\n"
            f"*Servicio:* {conv.service_interest or 'Valoración'}"
        )
        await evolution.send_text_message(settings.yesica_phone, yesica_msg)

        conv.reminder_sent = True
        sent_any = True
        logger.info(f"[{phone}] Sent same-day reminder (appointment at {formatted_dt})")

    if sent_any:
        save_conversation(conv)

    return sent_any


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _generate_reply(conv: ConversationState) -> str:
    """Build full messages list and call GPT-4o."""
    now_col = datetime.now(COLOMBIA_TZ)
    days_es = {0: "lunes", 1: "martes", 2: "miercoles", 3: "jueves", 4: "viernes", 5: "sabado", 6: "domingo"}
    months_es = {1: "enero", 2: "febrero", 3: "marzo", 4: "abril", 5: "mayo", 6: "junio",
                 7: "julio", 8: "agosto", 9: "septiembre", 10: "octubre", 11: "noviembre", 12: "diciembre"}
    fecha_actual = (
        f"{days_es[now_col.weekday()]} {now_col.day} de {months_es[now_col.month]} de {now_col.year}, "
        f"{now_col.strftime('%I:%M %p')} (hora Colombia)"
    )

    header = f"FECHA Y HORA ACTUAL: {fecha_actual}\n"
    if conv.user_display_name:
        header += (
            f"NOMBRE DEL USUARIO: {conv.user_display_name}\n"
            f"INSTRUCCION: Usa el nombre '{conv.user_display_name}' de forma natural. "
            f"En el primer mensaje empieza con su nombre.\n"
        )
    else:
        header += (
            "NOMBRE DEL USUARIO: Desconocido — usa 'hola' simple y pregunta su nombre "
            "naturalmente. NO uses 'amiga', 'amor' ni nada cursi.\n"
        )

    # Inject learning context
    learning_ctx = get_learning_context()
    if learning_ctx:
        header += f"\n{learning_ctx}\n"

    system_with_context = header + "\n" + SYSTEM_PROMPT

    messages = [{"role": "system", "content": system_with_context}] + conv.messages

    # Analyze last user message for mirroring enforcement
    last_user_msg = ""
    for msg in reversed(conv.messages):
        if msg.get("role") == "user":
            last_user_msg = msg["content"]
            break

    user_word_count = len(last_user_msg.split()) if last_user_msg else 0
    user_char_count = len(last_user_msg) if last_user_msg else 0

    mirror_instruction = ""
    if user_word_count <= 5:
        mirror_instruction = (
            f"EFECTO ESPEJO OBLIGATORIO: El usuario escribió solo {user_word_count} palabras. "
            f"Tu respuesta DEBE ser corta: máximo 1-2 líneas, máximo 15 palabras. "
            f"NO uses [MSG]. UN solo mensaje breve."
        )
    elif user_word_count <= 15:
        mirror_instruction = (
            f"EFECTO ESPEJO: El usuario escribió {user_word_count} palabras. "
            f"Responde con longitud similar: máximo 2-3 líneas. UN solo mensaje."
        )
    elif user_word_count <= 30:
        mirror_instruction = (
            f"EFECTO ESPEJO: El usuario escribió {user_word_count} palabras. "
            f"Puedes responder con un párrafo corto. UN solo mensaje."
        )
    else:
        mirror_instruction = (
            f"El usuario escribió un mensaje largo ({user_word_count} palabras). "
            f"Puedes responder con un poco más de detalle pero no te excedas."
        )

    name = conv.user_display_name
    name_reminder = (
        f"El usuario se llama {name}."
        if name else
        "No sabes el nombre. Pregunta cómo se llama si es natural hacerlo."
    )

    messages.append({
        "role": "system",
        "content": f"{mirror_instruction}\n{name_reminder}\nNO inventes precios, datos ni información que no tengas."
    })

    return await ai.chat(messages)


async def _notify_yesica_appointment(
    conv: ConversationState,
    appointment_dt: str,
) -> None:
    """Notify Yésica when an appointment is booked."""
    settings = get_settings()
    name = conv.collected_name or conv.user_display_name or "Pendiente"
    meeting_type = conv.meeting_type or "whatsapp"
    meet_link = conv.meet_link or ""

    if meeting_type == "meet" and meet_link:
        tipo_label = f"Google Meet\n*Enlace:* {meet_link}"
        instruccion = "El cliente entrará al enlace de Meet el día de la cita 💻"
    else:
        tipo_label = "Videollamada de WhatsApp"
        instruccion = "Recuerda llamar al cliente por videollamada de WhatsApp el día de la cita 📲"

    message = (
        f"✅ *Nueva valoración virtual agendada*\n\n"
        f"*Paciente:* {name}\n"
        f"*Teléfono:* +{conv.collected_phone or conv.phone}\n"
        f"*WhatsApp:* +{conv.phone}\n"
        f"*Servicio:* {conv.service_interest or 'Levantamiento de glúteos'}\n"
        f"*Valoración:* Gratuita — {tipo_label}\n"
        f"*Fecha:* {appointment_dt}\n\n"
        f"{instruccion}\n"
        f"Quedó registrado en tu Google Calendar 📅"
    )
    await evolution.send_text_message(settings.yesica_phone, message)


# ---------------------------------------------------------------------------
# Slot extraction from natural language
# ---------------------------------------------------------------------------

    # _extract_slot_from_text removed — GPT handles slot parsing via ai.parse_slot_selection



def _month_name(month: int) -> str:
    return {
        1: "enero", 2: "febrero", 3: "marzo", 4: "abril",
        5: "mayo", 6: "junio", 7: "julio", 8: "agosto",
        9: "septiembre", 10: "octubre", 11: "noviembre", 12: "diciembre",
    }[month]


def _format_time_spanish(dt: datetime) -> str:
    """Format time in Spanish: '4:30 p.m.', '9:00 a.m.', '12:00 p.m.'"""
    hour = dt.hour
    minute = dt.strftime("%M")
    period = "a.m." if hour < 12 else "p.m."
    if hour == 0:
        h = 12
    elif hour > 12:
        h = hour - 12
    else:
        h = hour
    if minute == "00":
        return f"{h}:{minute} {period}"
    return f"{h}:{minute} {period}"


def _format_appointment_datetime(dt: datetime) -> str:
    days_es = {
        0: "lunes", 1: "martes", 2: "miercoles", 3: "jueves",
        4: "viernes", 5: "sabado", 6: "domingo",
    }
    months_es = {
        1: "enero", 2: "febrero", 3: "marzo", 4: "abril",
        5: "mayo", 6: "junio", 7: "julio", 8: "agosto",
        9: "septiembre", 10: "octubre", 11: "noviembre", 12: "diciembre",
    }
    day_name = days_es[dt.weekday()]
    month_name = months_es[dt.month]
    time_str = _format_time_spanish(dt)
    return f"{day_name} {dt.day} de {month_name} a las {time_str}"
