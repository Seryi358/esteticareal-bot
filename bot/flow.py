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
DEBOUNCE_SECONDS = 3.0

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

    if conv.phase == "collecting_data":
        await _try_collect_data_and_schedule(conv)
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
        "awaiting_slot_selection", "collecting_data",
        "appointment_confirmed", "escalated_to_yesica",
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
# Evening/Weekend escalation to Yésica
# ---------------------------------------------------------------------------

async def _escalate_to_yesica_evening(conv: ConversationState, user_text: str) -> None:
    """User needs evening/weekend hours. Escalate to Yésica."""
    settings = get_settings()

    conv.phase = "escalated_to_yesica"
    conv.inject_system_event(
        "EVENING_ESCALATION: El usuario necesita un horario fuera del rango normal "
        "(después de 5pm o fin de semana). Dile que lo conectás directamente con "
        "Yésica para coordinar ese horario especial. Sé natural y positiva."
    )

    reply = await _generate_reply(conv)
    await _send_and_record(conv, reply)

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
    if not conv.calendar_slots_json:
        await _fetch_and_inject_slots(conv)
        reply = await _generate_reply(conv)
        await _send_and_record(conv, reply)
        return

    available_slots = json.loads(conv.calendar_slots_json)
    now_str = datetime.now(COLOMBIA_TZ).strftime("%A %d/%m/%Y %I:%M %p")

    # Let GPT understand what slot the user wants
    selected_iso = await ai.parse_slot_selection(text, available_slots, now_str)

    if selected_iso:
        selected_slot = datetime.fromisoformat(selected_iso).replace(tzinfo=COLOMBIA_TZ)
        formatted_dt = _format_appointment_datetime(selected_slot)
        conv.appointment_datetime = selected_slot.isoformat()
        logger.info(f"[{conv.phone}] GPT parsed slot: {formatted_dt}")

        has_name = conv.collected_name or conv.user_display_name
        if has_name:
            conv.phase = "collecting_data"
            await _try_collect_data_and_schedule(conv)
            return
        else:
            conv.phase = "collecting_data"
            conv.inject_system_event(
                f"El usuario eligió el horario: {formatted_dt}. "
                f"Necesitas su nombre completo para confirmar la cita. "
                f"Pídelo de forma natural."
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
            conv.inject_system_event(
                f"APPOINTMENT_CONFIRMED: Cita registrada para {formatted_dt}. "
                f"Da detalles: fecha/hora, direccion (Cra 49b #26b-50, Unidad Ciudad Central, "
                f"Apto 1618, Torre 2, Bello), Estacion Madera del Metro, llegar antes, "
                f"cancelar con 24h."
            )
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

    event = await calendar.create_appointment(
        slot,
        conv.collected_name or conv.user_display_name or "Cliente",
        conv.collected_phone or conv.phone,
        conv.collected_email or "",
    )
    formatted_dt = _format_appointment_datetime(slot)
    conv.phase = "appointment_confirmed"

    if event:
        conv.inject_system_event(
            f"APPOINTMENT_CONFIRMED: Cita creada exitosamente. "
            f"Fecha y hora: {formatted_dt}. "
            f"Da todos los detalles: fecha/hora, direccion completa "
            f"(Cra 49b #26b-50, Unidad Ciudad Central, Apto 1618, Torre 2, Bello), "
            f"como llegar en Metro (a pasos de la Estacion Madera), "
            f"llegar 5-10 min antes, cancelar con 24h de anticipacion."
        )
        asyncio.create_task(_notify_yesica_appointment(conv, formatted_dt))
        # Save success pattern for learning
        asyncio.create_task(
            asyncio.to_thread(save_success_pattern, conv.phone, conv.messages)
        )
    else:
        # Calendar API failed but we still confirm to user
        logger.warning(f"[{conv.phone}] Calendar API returned None — confirming anyway")
        conv.inject_system_event(
            f"APPOINTMENT_CONFIRMED: Cita registrada para {formatted_dt}. "
            f"Da detalles: fecha/hora, direccion, Metro, llegar antes."
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
        formatted = calendar.format_slots_for_whatsapp(slots)
        conv.inject_system_event(
            f"CALENDAR_SLOTS: Yesica tiene disponible {formatted}. "
            f"INSTRUCCION: Ofrece PRIMERO solo UN horario (el mas proximo, ej: 'mañana en la mañana'). "
            f"Si el usuario dice que no puede, ofrece la otra franja del MISMO dia (ej: la tarde). "
            f"Solo si no puede en ninguna franja de ese dia, ofrece otro dia. "
            f"Se proactivo: 'Mañana tiene disponible en la mañana, te sirve o prefieres en la tarde?' "
            f"NO hagas lista. Conversacional."
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
    """Split reply by [MSG], send each part as a separate WhatsApp message."""
    # Safety net: ensure response never dies before appointment is confirmed
    reply = _ensure_conversation_alive(reply, conv.phase)

    parts = [p.strip() for p in reply.split("[MSG]") if p.strip()]
    if not parts:
        return

    full_reply = " ".join(parts)
    conv.add_message("assistant", full_reply)

    for i, part in enumerate(parts):
        await evolution.send_typing_presence(conv.phone)
        delay = min(1.0 + len(part) * 0.015, 3.5)
        await asyncio.sleep(delay)
        await evolution.send_text_message(conv.phone, part)


def _ensure_conversation_alive(reply: str, phase: str) -> str:
    """Safety net: before appointment is confirmed, ensure reply has a question."""
    if not reply or not reply.strip():
        return "¿En qué te puedo ayudar?"

    # After appointment confirmed or escalated — don't force questions
    if phase in ("appointment_confirmed", "escalated_to_yesica"):
        return reply

    # Before appointment: if no question mark, GPT forgot — add one
    if "?" not in reply:
        return reply + " ¿Qué te parece?"

    return reply


# ---------------------------------------------------------------------------
# 24h Follow-up system
# ---------------------------------------------------------------------------

async def send_followup_if_needed(phone: str) -> bool:
    """Check if this conversation needs a 24h follow-up and send it."""
    conv = load_conversation(phone)

    if conv.follow_up_sent:
        return False
    if conv.phase in ("appointment_confirmed", "collecting_data", "escalated_to_yesica"):
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
        f"para valoraciones. Recordá que este mes la valoración no tiene costo. "
        f"¿Qué día te queda más fácil?"
    )

    await evolution.send_text_message(phone, followup_msg)
    conv.follow_up_sent = True
    conv.add_message("assistant", followup_msg)
    save_conversation(conv)
    logger.info(f"[{phone}] Sent 24h follow-up message")
    return True


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
            f"Podés responder con un párrafo corto. UN solo mensaje."
        )
    else:
        mirror_instruction = (
            f"El usuario escribió un mensaje largo ({user_word_count} palabras). "
            f"Podés responder con un poco más de detalle pero no te excedas."
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
    message = (
        f"✅ *Nueva valoración agendada*\n\n"
        f"*Paciente:* {name}\n"
        f"*Teléfono:* +{conv.collected_phone or conv.phone}\n"
        f"*WhatsApp:* +{conv.phone}\n"
        f"*Servicio:* {conv.service_interest or 'Levantamiento de glúteos'}\n"
        f"*Valoración:* Gratuita (promoción del mes)\n"
        f"*Fecha:* {appointment_dt}\n\n"
        f"Quedó registrado en tu Google Calendar 📅"
    )
    await evolution.send_text_message(settings.yesica_phone, message)


# ---------------------------------------------------------------------------
# Slot extraction from natural language
# ---------------------------------------------------------------------------

    # _extract_slot_from_text removed — GPT handles slot parsing via ai.parse_slot_selection



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
    time_str = dt.strftime("%I:%M %p").lstrip("0")
    return f"{day_name} {dt.day} de {month_name} a las {time_str}"
