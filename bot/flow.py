import asyncio
import json
import logging
import re
from datetime import datetime
from zoneinfo import ZoneInfo

from bot.conversation import ConversationState, load_conversation, save_conversation
from bot.prompts import SYSTEM_PROMPT
from config import get_settings
from services import ai, calendar, evolution

logger = logging.getLogger(__name__)
COLOMBIA_TZ = ZoneInfo("America/Bogota")


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

async def process_message(
    phone: str,
    push_name: str | None,
    message_type: str,
    text_content: str | None,
    media_key_id: str | None,
    media_base64_inline: str | None,  # Some Evolution setups send base64 in webhook
) -> None:
    """Main handler called from the webhook endpoint."""
    conv = load_conversation(phone)

    # Update display name if we got one
    if push_name and not conv.user_display_name:
        conv.user_display_name = push_name

    # Route based on message type
    if message_type == "imageMessage":
        await _handle_image(conv, media_key_id, media_base64_inline)
    else:
        content = text_content or ""
        if not content.strip():
            return  # Ignore empty messages
        await _handle_text(conv, content)

    save_conversation(conv)


# ---------------------------------------------------------------------------
# Image handling
# ---------------------------------------------------------------------------

async def _handle_image(
    conv: ConversationState,
    media_key_id: str | None,
    media_base64_inline: str | None,
) -> None:
    if conv.phase != "awaiting_screenshot":
        # Not expecting an image right now
        conv.inject_system_event(
            "INSTRUCCION_INTERNA: El usuario envio una imagen pero no estabamos esperando un comprobante. "
            "Responde de forma natural y amigable, preguntale si quiso enviarte algo especifico."
        )
        reply = await _generate_reply(conv)
        await _send_and_record(conv, reply)
        return

    # Get base64 of the image
    base64_data = media_base64_inline
    if not base64_data and media_key_id:
        await evolution.send_typing_presence(conv.phone)
        base64_data = await evolution.get_media_base64(media_key_id)

    if not base64_data:
        conv.inject_system_event(
            "PAYMENT_UNCLEAR: No se pudo descargar la imagen. Pide al usuario que la envie de nuevo."
        )
        reply = await _generate_reply(conv)
        await _send_and_record(conv, reply)
        return

    # Verify payment with GPT-4o Vision
    await evolution.send_typing_presence(conv.phone)
    result = await ai.verify_payment_image(base64_data)

    is_valid = (
        result.get("is_valid")
        and result.get("appears_authentic")
    )

    if is_valid:
        conv.phase = "collecting_data"
        conv.payment_verified = True
        conv.inject_system_event(
            f"PAYMENT_VERIFIED: Comprobante verificado exitosamente. "
            f"Monto detectado: {result.get('amount_detected', '$25.000')}. "
            f"Ahora pide el nombre completo y celular del usuario para agendar la valoracion."
        )
        # Notify Yesica (once)
        if not conv.notification_sent:
            asyncio.create_task(
                _notify_yesica(conv.phone, conv.user_display_name)
            )
            conv.notification_sent = True
    elif not result.get("appears_authentic"):
        conv.inject_system_event(
            f"PAYMENT_INVALID: El comprobante parece no ser autentico o fue editado. "
            f"Notas: {result.get('notes', '')}. "
            f"Con mucho tacto y sin acusar al usuario, pidele que se contacte directamente con Yesica "
            f"al 3006278237 para resolver cualquier inconveniente."
        )
    else:
        conv.inject_system_event(
            f"PAYMENT_UNCLEAR: No se pudo verificar el comprobante claramente. "
            f"Notas: {result.get('notes', '')}. "
            f"Pide que lo envie de nuevo con mejor calidad, asegurandose de que se vea el numero destino, "
            f"monto y fecha."
        )

    reply = await _generate_reply(conv)
    await _send_and_record(conv, reply)


# ---------------------------------------------------------------------------
# Text handling
# ---------------------------------------------------------------------------

async def _handle_text(conv: ConversationState, text: str) -> None:
    conv.add_message("user", text)

    # Phase-specific logic before calling AI
    if conv.phase == "awaiting_slot_selection":
        await _try_parse_slot_selection(conv, text)
        return

    if conv.phase == "collecting_data":
        await _try_collect_data_and_schedule(conv)
        return

    # General conversation — let GPT-4o guide the flow
    reply = await _generate_reply(conv)
    await _send_and_record(conv, reply)

    # Detect if bot just gave payment instructions (Nequi 3006278237)
    if "3006278237" in reply and conv.phase == "chatting":
        conv.phase = "awaiting_screenshot"


# ---------------------------------------------------------------------------
# Data collection & scheduling
# ---------------------------------------------------------------------------

async def _try_collect_data_and_schedule(conv: ConversationState) -> None:
    """After payment is verified, collect name/phone then show calendar."""
    # Extract data from conversation so far
    extracted = await ai.extract_user_data(conv.messages)

    name = extracted.get("name") or conv.collected_name
    phone = extracted.get("phone") or conv.collected_phone
    email = extracted.get("email") or conv.collected_email

    # Update stored data
    if name:
        conv.collected_name = name
    if phone:
        conv.collected_phone = phone
    if email:
        conv.collected_email = email

    # Only proceed to calendar once we have at least a name
    if name:
        slots = await calendar.get_available_slots(days_ahead=7)

        if slots:
            conv.calendar_slots_json = json.dumps([s.isoformat() for s in slots])
            conv.phase = "awaiting_slot_selection"
            formatted = calendar.format_slots_for_whatsapp(slots)
            conv.inject_system_event(
                f"CALENDAR_SLOTS: Los siguientes horarios estan disponibles para la valoracion. "
                f"Presentaselos al usuario de forma amigable y pidele que elija uno:\n{formatted}"
            )
        else:
            conv.inject_system_event(
                "CALENDAR_ERROR: No hay horarios disponibles en el calendario en este momento. "
                "Dile al usuario que Yesica se pondra en contacto con el/ella para coordinar el horario personalizado."
            )

    reply = await _generate_reply(conv)
    await _send_and_record(conv, reply)


async def _try_parse_slot_selection(conv: ConversationState, text: str) -> None:
    """Parse the user's slot selection and create the calendar event."""
    if not conv.calendar_slots_json:
        reply = await _generate_reply(conv)
        await _send_and_record(conv, reply)
        return

    slots = [datetime.fromisoformat(s).replace(tzinfo=COLOMBIA_TZ)
             for s in json.loads(conv.calendar_slots_json)]

    selected_slot = _extract_slot_from_text(text, slots)

    if selected_slot:
        # Create the calendar event
        event = await calendar.create_appointment(
            selected_slot,
            conv.collected_name or conv.user_display_name or "Cliente",
            conv.collected_phone or conv.phone,
            conv.collected_email or "",
        )

        conv.appointment_datetime = selected_slot.isoformat()
        conv.phase = "appointment_confirmed"

        if event:
            formatted_dt = _format_appointment_datetime(selected_slot)
            conv.inject_system_event(
                f"APPOINTMENT_CONFIRMED: La cita fue creada exitosamente en el calendario de Yesica. "
                f"Fecha y hora: {formatted_dt}. "
                f"Nombre del cliente: {conv.collected_name or conv.user_display_name}. "
                f"Da todos los detalles de confirmacion: fecha/hora, direccion completa, como llegar, "
                f"recordatorio de llegar 5-10 min antes, y politica de cancelacion (24 horas de anticipacion)."
            )
            # Final notification to Yesica with full details
            asyncio.create_task(
                _notify_yesica_appointment(
                    conv.phone,
                    conv.collected_name or conv.user_display_name,
                    formatted_dt,
                )
            )
        else:
            conv.inject_system_event(
                "CALENDAR_ERROR: Hubo un problema al crear la cita en el calendario. "
                "Dile al usuario que Yesica se pondra en contacto para confirmar el horario manualmente."
            )
    else:
        # Could not parse the selection, let GPT-4o handle it
        pass

    reply = await _generate_reply(conv)
    await _send_and_record(conv, reply)


def _extract_slot_from_text(text: str, slots: list[datetime]) -> datetime | None:
    """Try to match user's text to a slot number or time mention."""
    text_clean = text.strip().lower()

    # Try simple number match (1, 2, 3...)
    match = re.search(r"\b([1-9]\d?)\b", text_clean)
    if match:
        idx = int(match.group(1)) - 1
        if 0 <= idx < len(slots):
            return slots[idx]

    # Try word numbers (primero, segundo, tercero...)
    word_map = {
        "primero": 0, "primera": 0, "1ro": 0, "1ra": 0,
        "segundo": 1, "segunda": 1,
        "tercero": 2, "tercera": 2,
        "cuarto": 3, "cuarta": 3,
        "quinto": 4, "quinta": 4,
    }
    for word, idx in word_map.items():
        if word in text_clean and idx < len(slots):
            return slots[idx]

    return None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _generate_reply(conv: ConversationState) -> str:
    """Build full messages list (with system prompt) and call GPT-4o."""
    messages = [{"role": "system", "content": SYSTEM_PROMPT}] + conv.messages
    return await ai.chat(messages)


async def _send_and_record(conv: ConversationState, reply: str) -> None:
    """Send the reply via Evolution API and record it in conversation history."""
    await evolution.send_typing_presence(conv.phone)
    await asyncio.sleep(1.5)  # Simulate human typing pause
    success = await evolution.send_text_message(conv.phone, reply)
    if success:
        conv.add_message("assistant", reply)


async def _notify_yesica(user_phone: str, user_name: str | None) -> None:
    """Send Yesica a WhatsApp notification when a payment screenshot is received."""
    settings = get_settings()
    name_str = user_name or "Desconocido"
    message = (
        f"*Nuevo comprobante de pago recibido!*\n\n"
        f"*Cliente:* {name_str}\n"
        f"*WhatsApp:* +{user_phone}\n"
        f"*Servicio:* Valoracion Profesional ($25.000)\n\n"
        f"El comprobante fue verificado automaticamente. Pronto se procedera con el agendamiento."
    )
    await evolution.send_text_message(settings.yesica_phone, message)


async def _notify_yesica_appointment(
    user_phone: str,
    user_name: str | None,
    appointment_dt: str,
) -> None:
    """Notify Yesica that an appointment was booked."""
    settings = get_settings()
    name_str = user_name or "Desconocido"
    message = (
        f"*Nueva cita agendada!*\n\n"
        f"*Cliente:* {name_str}\n"
        f"*WhatsApp:* +{user_phone}\n"
        f"*Servicio:* Valoracion Profesional\n"
        f"*Fecha y hora:* {appointment_dt}\n\n"
        f"Ya quedo registrado en tu calendario de Google."
    )
    await evolution.send_text_message(settings.yesica_phone, message)


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
