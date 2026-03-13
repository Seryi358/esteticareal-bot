import asyncio
import json
import logging
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from bot.conversation import ConversationState, load_conversation, save_conversation
from bot.prompts import SYSTEM_PROMPT
from config import get_settings
from services import ai, calendar, evolution

logger = logging.getLogger(__name__)
COLOMBIA_TZ = ZoneInfo("America/Bogota")

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
    if push_name:
        _pending_names[phone] = push_name

    # Check human takeover — if Yesica's window is active, bot stays silent
    conv = load_conversation(phone)
    if conv.is_human_takeover_active():
        logger.info(f"Human takeover active for {phone} (until {conv.human_takeover_until}) — bot silent")
        return
    # Clear expired takeover flag if it was left on
    if conv.human_takeover and not conv.human_takeover_until:
        conv.human_takeover = False
        save_conversation(conv)

    # Audio — transcribe first, then treat as text (no debounce needed for audio)
    if message_type == "audioMessage":
        conv = load_conversation(phone)
        if push_name and not conv.user_display_name:
            conv.user_display_name = push_name
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

    # Images are processed immediately (no debounce)
    if message_type == "imageMessage":
        conv = load_conversation(phone)
        if push_name and not conv.user_display_name:
            conv.user_display_name = push_name
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

    # Combine multiple messages into one coherent input
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

    logger.info(f"[{conv.phone}] Audio: inline={'yes' if media_base64_inline else 'no'}, key={media_key_id}, downloaded={'yes' if base64_data else 'NO'}")

    if not base64_data:
        conv.add_message("user", "[El usuario envio un audio pero no se pudo descargar]")
        conv.inject_system_event(
            "INSTRUCCION: El usuario envio un audio pero no se pudo procesar. "
            "Pidele amablemente que lo reenvie o que escriba su mensaje."
        )
        reply = await _generate_reply(conv)
        await _send_and_record(conv, reply)
        return

    logger.info(f"[{conv.phone}] Sending {len(base64_data)} chars to Whisper for transcription")
    transcription = await ai.transcribe_audio(base64_data)

    if transcription:
        logger.info(f"[{conv.phone}] Whisper transcription: '{transcription[:100]}'")
        await _handle_text(conv, transcription)
    else:
        logger.warning(f"[{conv.phone}] Whisper returned empty transcription")
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
    # Get base64 of the image
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

    # Analyze the image with GPT-4o Vision
    await evolution.send_typing_presence(conv.phone)
    analysis = await ai.analyze_image(base64_data)

    image_type = analysis.get("image_type", "OTHER")
    description = analysis.get("description", "")
    suggestion = analysis.get("response_suggestion", "")

    # Handle payment screenshots
    if image_type == "PAYMENT" and conv.phase == "awaiting_screenshot":
        is_valid = (
            analysis.get("payment_appears_authentic")
            and analysis.get("payment_recipient_matches")
        )

        if is_valid:
            conv.phase = "collecting_data"
            conv.payment_verified = True
            conv.inject_system_event(
                f"PAYMENT_VERIFIED: Comprobante verificado exitosamente. "
                f"Monto detectado: {analysis.get('payment_amount', '$25.000')}. "
                f"Ahora pide el nombre completo y celular del usuario para confirmar la cita."
            )
            if not conv.notification_sent:
                asyncio.create_task(_notify_yesica(conv))
                conv.notification_sent = True

        elif not analysis.get("payment_appears_authentic"):
            conv.inject_system_event(
                "PAYMENT_INVALID: El comprobante parece no ser autentico. "
                "Con mucho tacto pide que contacte a Yesica al 3006278237."
            )
        else:
            conv.inject_system_event(
                f"PAYMENT_UNCLEAR: No se pudo verificar el comprobante. "
                f"Razon: {description}. Pide que lo reenvie con mejor calidad, "
                f"asegurandose de que se vea el numero destino, monto y fecha."
            )

    elif image_type == "PAYMENT" and conv.phase != "awaiting_screenshot":
        conv.inject_system_event(
            f"IMAGE_ANALYSIS: El usuario envio lo que parece ser un comprobante de pago "
            f"({description}), pero todavia no habiamos llegado al paso del pago. "
            f"Reacciona de forma natural: si ya mencionaste el Nequi, verifica el pago; "
            f"si no, explica el proceso amablemente."
        )

    elif image_type == "BODY":
        zone = analysis.get("body_zone", "corporal")
        conv.inject_system_event(
            f"IMAGE_ANALYSIS: El usuario envio una foto de su zona {zone}. "
            f"Descripcion: {description}. "
            f"Sugerencia: {suggestion}. "
            f"Responde con empatia genuina, muestra interes profesional por su caso, "
            f"menciona que Yesica podria hacer una valoracion personalizada de esa zona "
            f"y conecta con los tratamientos relevantes para esa zona corporal."
        )

    elif image_type == "FACE":
        conv.inject_system_event(
            f"IMAGE_ANALYSIS: El usuario envio una foto de su rostro o piel. "
            f"Descripcion: {description}. "
            f"Sugerencia: {suggestion}. "
            f"Responde con empatia, muestra interes profesional, menciona el hidrofacial "
            f"o tratamientos faciales segun lo que ves, y ofrece la valoracion facial con Yesica."
        )

    elif image_type == "BEFORE_AFTER":
        conv.inject_system_event(
            f"IMAGE_ANALYSIS: El usuario envio una foto de antes/despues o resultados. "
            f"Descripcion: {description}. "
            f"Sugerencia: {suggestion}. "
            f"Celebra con entusiasmo, valida que esos resultados son posibles, "
            f"usa esto como motivacion para que continue su proceso."
        )

    else:
        conv.inject_system_event(
            f"IMAGE_ANALYSIS: El usuario envio una imagen. "
            f"Descripcion: {description}. "
            f"Sugerencia: {suggestion}. "
            f"Responde de forma natural y amigable."
        )

    reply = await _generate_reply(conv)
    await _send_and_record(conv, reply)


# ---------------------------------------------------------------------------
# Text handling
# ---------------------------------------------------------------------------

async def _handle_text(conv: ConversationState, text: str) -> None:
    conv.add_message("user", text)

    if conv.phase == "awaiting_slot_selection":
        await _try_parse_slot_selection(conv, text)
        return

    if conv.phase == "collecting_data":
        await _try_collect_data_and_schedule(conv)
        return

    reply = await _generate_reply(conv)
    await _send_and_record(conv, reply)

    # Detect trigger phrase → fetch real slots and send them immediately
    slot_trigger_phrases = ("revisar los horarios", "horarios disponibles de yesica", "dejame revisar")
    reply_lower = reply.lower()
    if any(t in reply_lower for t in slot_trigger_phrases):
        if conv.phase not in ("awaiting_slot_selection", "awaiting_screenshot", "collecting_data", "appointment_confirmed"):
            logger.info(f"Slot trigger detected for {conv.phone} — fetching calendar slots")
            await _fetch_and_inject_slots(conv)
            if conv.phase == "awaiting_slot_selection":
                slot_reply = await _generate_reply(conv)
                await _send_and_record(conv, slot_reply)
        return

    # Detect when Nequi payment instructions were given (fallback path)
    if "3006278237" in reply and conv.phase not in ("awaiting_screenshot", "collecting_data", "appointment_confirmed"):
        conv.phase = "awaiting_screenshot"


# ---------------------------------------------------------------------------
# Slot selection → payment instructions
# ---------------------------------------------------------------------------

async def _try_parse_slot_selection(conv: ConversationState, text: str) -> None:
    """User is picking a time slot. Parse selection, save it, then give payment instructions."""
    if not conv.calendar_slots_json:
        # No slots available, re-fetch
        await _fetch_and_inject_slots(conv)
        reply = await _generate_reply(conv)
        await _send_and_record(conv, reply)
        return

    slots = [
        datetime.fromisoformat(s).replace(tzinfo=COLOMBIA_TZ)
        for s in json.loads(conv.calendar_slots_json)
    ]

    selected_slot = _extract_slot_from_text(text, slots)

    if selected_slot:
        formatted_dt = _format_appointment_datetime(selected_slot)
        # Save the chosen slot — appointment will be created after payment
        conv.appointment_datetime = selected_slot.isoformat()
        conv.phase = "awaiting_screenshot"
        conv.inject_system_event(
            f"El usuario eligio el horario: {formatted_dt}. "
            f"Su cupo esta separado. Ahora dale las instrucciones de pago para confirmar:\n"
            f"Nequi: *3006278237*\n"
            f"Nombre: *Yesica Restrepo*\n"
            f"Valor: *$25.000*\n"
            f"Cuando hayas hecho el pago, enviame el pantallazo del comprobante."
        )
    else:
        # Could not parse — ask user to clarify
        conv.inject_system_event(
            "INSTRUCCION: No se pudo identificar el horario que el usuario quiere. "
            "Pidele amablemente que diga el dia y la hora que prefiere, "
            "por ejemplo: 'mañana a las 10am' o 'el jueves a las 2pm'."
        )

    reply = await _generate_reply(conv)
    await _send_and_record(conv, reply)


# ---------------------------------------------------------------------------
# Data collection & appointment creation
# ---------------------------------------------------------------------------

async def _try_collect_data_and_schedule(conv: ConversationState) -> None:
    """Payment was verified. Collect user data and create the appointment."""
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

    # Use the slot chosen before payment, or fetch new ones as fallback
    if conv.appointment_datetime:
        logger.info(f"[{conv.phone}] Creating appointment for saved slot: {conv.appointment_datetime}")
        try:
            await _create_appointment_from_saved_slot(conv)
        except Exception as e:
            logger.error(f"[{conv.phone}] Failed to create appointment: {e}", exc_info=True)
            conv.phase = "appointment_confirmed"
            conv.inject_system_event(
                "CALENDAR_ERROR: Hubo un problema al crear la cita en el calendario. "
                "Yesica se pondra en contacto manualmente para confirmar el horario."
            )
    else:
        logger.info(f"[{conv.phone}] No saved slot — fetching new slots")
        await _fetch_and_inject_slots(conv)

    reply = await _generate_reply(conv)
    await _send_and_record(conv, reply)


async def _create_appointment_from_saved_slot(conv: ConversationState) -> None:
    """Create a Google Calendar appointment using the slot the user already selected."""
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
            f"Da todos los detalles al usuario: fecha/hora, direccion completa "
            f"(Cra 49b #26b-50, Unidad Ciudad Central, Apto 1618, Torre 2, Bello), "
            f"como llegar en Metro (a pasos de la Estacion Madera), "
            f"llegar 5-10 min antes, cancelar con 24h de anticipacion."
        )
        asyncio.create_task(_notify_yesica_appointment(conv, formatted_dt))
    else:
        conv.inject_system_event(
            "CALENDAR_ERROR: Hubo un problema al crear la cita en el calendario. "
            "Yesica se pondra en contacto manualmente para confirmar el horario."
        )


async def _fetch_and_inject_slots(conv: ConversationState) -> None:
    """Fetch real calendar slots and inject them into the conversation."""
    logger.info(f"Fetching calendar slots for {conv.phone}")
    slots = await calendar.get_available_slots(days_ahead=7)
    if slots:
        conv.calendar_slots_json = json.dumps([s.isoformat() for s in slots])
        conv.phase = "awaiting_slot_selection"
        formatted = calendar.format_slots_for_whatsapp(slots)
        conv.inject_system_event(
            f"CALENDAR_SLOTS: Yesica tiene libre {formatted}. "
            f"INSTRUCCION: Dile esto al usuario en UNA SOLA FRASE corta y natural, "
            f"por ejemplo 'Yesica tiene libre {formatted}' y pregunta que hora le sirve. "
            f"NO hagas lista, NO uses bullet points, NO enumeres horarios individuales. "
            f"Maximo 2 burbujas."
        )
        logger.info(f"Calendar slots fetched successfully for {conv.phone}: {len(slots)} slots")
    else:
        logger.warning(f"No calendar slots found for {conv.phone}")
        conv.inject_system_event(
            "CALENDAR_ERROR: No hay horarios disponibles en el calendario en este momento. "
            "Dile al usuario que Yesica se pondra en contacto para coordinar el horario."
        )


# ---------------------------------------------------------------------------
# Multi-message sending
# ---------------------------------------------------------------------------

async def _send_and_record(conv: ConversationState, reply: str) -> None:
    """Split reply by [MSG], send each part as a separate WhatsApp message."""
    parts = [p.strip() for p in reply.split("[MSG]") if p.strip()]

    if not parts:
        return

    full_reply = " ".join(parts)  # Store as single string in history
    conv.add_message("assistant", full_reply)

    for i, part in enumerate(parts):
        await evolution.send_typing_presence(conv.phone)
        # Delay between messages scaled to message length (more human)
        delay = min(1.0 + len(part) * 0.015, 3.5)
        await asyncio.sleep(delay)
        await evolution.send_text_message(conv.phone, part)


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
            f"INSTRUCCION OBLIGATORIA: En el primer mensaje de la conversacion DEBES empezar con su nombre "
            f"(ej: 'Hola {conv.user_display_name}!' o '{conv.user_display_name}! Que bueno...'). "
            f"En mensajes siguientes usalo de vez en cuando.\n"
        )
    else:
        header += "NOMBRE DEL USUARIO: Desconocido — usa 'amor' o 'chica' y pregunta su nombre naturalmente.\n"

    system_with_context = header + "\n" + SYSTEM_PROMPT

    logger.info(f"[{conv.phone}] Generating reply — name={conv.user_display_name!r}, phase={conv.phase}")
    messages = [{"role": "system", "content": system_with_context}] + conv.messages

    # Inject a final system reminder about the name right before GPT generates
    # (recency bias ensures GPT pays attention to this)
    name = conv.user_display_name
    if name:
        messages.append({
            "role": "system",
            "content": f"RECORDATORIO: El usuario se llama {name}. Usa su nombre en tu respuesta."
        })
    else:
        messages.append({
            "role": "system",
            "content": "RECORDATORIO: No sabes el nombre del usuario. Usa 'amor' o 'chica' y pregunta cómo se llama."
        })

    return await ai.chat(messages)


async def _notify_yesica(conv: ConversationState) -> None:
    """Send Yesica a WhatsApp notification when a payment is verified."""
    settings = get_settings()
    name = conv.collected_name or conv.user_display_name or "Pendiente"
    service = conv.service_interest or "No especificado"
    city = conv.city or "No especificada"

    cita_info = "Pendiente de confirmar"
    if conv.appointment_datetime:
        try:
            slot = datetime.fromisoformat(conv.appointment_datetime).replace(tzinfo=COLOMBIA_TZ)
            cita_info = _format_appointment_datetime(slot)
        except Exception:
            pass

    message = (
        f"🔔 *Nuevo pago de valoración recibido*\n\n"
        f"*Paciente:* {name}\n"
        f"*WhatsApp:* +{conv.phone}\n"
        f"*Tratamiento de interés:* {service}\n"
        f"*Ciudad:* {city}\n"
        f"*Valoración pagada:* $25.000\n"
        f"*Cita solicitada:* {cita_info}\n\n"
        f"Por favor valida el comprobante en el chat del paciente."
    )
    await evolution.send_text_message(settings.yesica_phone, message)


async def _notify_yesica_appointment(
    conv: ConversationState,
    appointment_dt: str,
) -> None:
    """Notify Yesica when an appointment is booked."""
    settings = get_settings()
    name = conv.collected_name or conv.user_display_name or "Pendiente"
    message = (
        f"✅ *Nueva valoración agendada*\n\n"
        f"*Paciente:* {name}\n"
        f"*Teléfono:* +{conv.collected_phone or conv.phone}\n"
        f"*WhatsApp:* +{conv.phone}\n"
        f"*Tratamiento:* {conv.service_interest or 'No especificado'}\n"
        f"*Valoración pagada:* Sí — $25.000\n"
        f"*Fecha:* {appointment_dt}\n\n"
        f"Quedó registrado en tu Google Calendar 📅"
    )
    await evolution.send_text_message(settings.yesica_phone, message)


def _extract_slot_from_text(text: str, slots: list[datetime]) -> datetime | None:
    """Parse natural time references like 'mañana a las 10', 'el jueves a las 3pm'."""
    if not slots:
        return None

    text_clean = text.strip().lower()
    now = datetime.now(COLOMBIA_TZ)

    # --- 1. Try to identify the target DAY ---
    target_date = None
    if "hoy" in text_clean:
        target_date = now.date()
    elif "mañana" in text_clean or "manana" in text_clean:
        target_date = (now + timedelta(days=1)).date()
    else:
        day_names = {
            "lunes": 0, "martes": 1, "miercoles": 2, "miércoles": 2,
            "jueves": 3, "viernes": 4, "sabado": 5, "sábado": 5,
        }
        for name, weekday in day_names.items():
            if name in text_clean:
                days_ahead = (weekday - now.weekday()) % 7
                if days_ahead == 0:
                    # Could be today or next week — pick based on available slots
                    if any(s.date() == now.date() and s.weekday() == weekday for s in slots):
                        target_date = now.date()
                    else:
                        target_date = (now + timedelta(days=7)).date()
                else:
                    target_date = (now + timedelta(days=days_ahead)).date()
                break

    # --- 2. Try to identify the target HOUR ---
    target_hour = None
    target_minute = 0

    # Patterns: "10am", "10 am", "2pm", "2:30 pm", "14:00", "a las 10", "las 3"
    time_match = re.search(
        r'(?:a\s+las\s+|las\s+)?(\d{1,2})\s*(?::(\d{2}))?\s*(am|pm|a\.m\.|p\.m\.)?',
        text_clean,
    )
    if time_match:
        hour = int(time_match.group(1))
        minute = int(time_match.group(2)) if time_match.group(2) else 0
        period = time_match.group(3)

        if period and period.startswith("p") and hour < 12:
            hour += 12
        elif period and period.startswith("a") and hour == 12:
            hour = 0
        elif not period and 1 <= hour <= 7:
            # Ambiguous — assume PM for business hours (1pm-7pm range)
            hour += 12

        target_hour = hour
        target_minute = minute

    # Handle "en la mañana" / "en la tarde" without specific time
    if target_hour is None:
        if "mañana" not in text_clean and ("mañ" in text_clean or "manan" in text_clean):
            # Careful: "mañana" means tomorrow, "en la mañana" means morning
            pass
        if re.search(r"en la mañana|por la mañana|temprano", text_clean):
            target_hour = 9  # default morning
        elif re.search(r"en la tarde|por la tarde|tarde", text_clean):
            target_hour = 14  # default afternoon

    # --- 3. Match against available slots ---

    # Best case: both day and hour known
    if target_date and target_hour is not None:
        target_dt = datetime(
            target_date.year, target_date.month, target_date.day,
            target_hour, target_minute, tzinfo=COLOMBIA_TZ,
        )
        candidates = [s for s in slots if s.date() == target_date]
        if candidates:
            best = min(candidates, key=lambda s: abs((s - target_dt).total_seconds()))
            # Accept if within 40 minutes of requested time
            if abs((best - target_dt).total_seconds()) <= 40 * 60:
                return best

    # Only hour given — find first slot at/near that hour on any day
    if target_hour is not None and target_date is None:
        candidates = [s for s in slots if s.hour == target_hour]
        if not candidates:
            candidates = [s for s in slots if abs(s.hour - target_hour) <= 1]
        if candidates:
            return candidates[0]

    # Only day given — return first slot on that day
    if target_date and target_hour is None:
        candidates = [s for s in slots if s.date() == target_date]
        if candidates:
            return candidates[0]

    return None


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
