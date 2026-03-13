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

    # Images are processed immediately (no debounce)
    if message_type == "imageMessage":
        conv = load_conversation(phone)
        if push_name and not conv.user_display_name:
            conv.user_display_name = push_name
        await _handle_image(conv, media_key_id, media_base64_inline)
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

    await _handle_text(conv, combined)
    save_conversation(conv)


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
        base64_data = await evolution.get_media_base64(media_key_id)

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
                f"Ahora pide el nombre completo y celular del usuario para agendar."
            )
            if not conv.notification_sent:
                asyncio.create_task(_notify_yesica(conv))
                conv.notification_sent = True

        elif not analysis.get("payment_appears_authentic"):
            conv.inject_system_event(
                f"PAYMENT_INVALID: El comprobante parece no ser autentico. "
                f"Con mucho tacto pide que contacte a Yesica al 3006278237."
            )
        else:
            conv.inject_system_event(
                f"PAYMENT_UNCLEAR: No se pudo verificar el comprobante. "
                f"Razon: {description}. Pide que lo reenvie con mejor calidad, "
                f"asegurandose de que se vea el numero destino, monto y fecha."
            )

    elif image_type == "PAYMENT" and conv.phase != "awaiting_screenshot":
        # Payment screenshot but not expected — could be trying to pay
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

    # Detect when bot gave payment instructions
    if "3006278237" in reply and conv.phase == "chatting":
        conv.phase = "awaiting_screenshot"


# ---------------------------------------------------------------------------
# Data collection & scheduling
# ---------------------------------------------------------------------------

async def _try_collect_data_and_schedule(conv: ConversationState) -> None:
    extracted = await ai.extract_user_data(conv.messages)

    if extracted.get("name"):
        conv.collected_name = extracted["name"]
        # Update display name too so future messages use real name
        conv.user_display_name = extracted["name"].split()[0]
    if extracted.get("phone"):
        conv.collected_phone = extracted["phone"]
    if extracted.get("email"):
        conv.collected_email = extracted["email"]

    if conv.collected_name:
        slots = await calendar.get_available_slots(days_ahead=7)
        if slots:
            conv.calendar_slots_json = json.dumps([s.isoformat() for s in slots])
            conv.phase = "awaiting_slot_selection"
            formatted = calendar.format_slots_for_whatsapp(slots)
            conv.inject_system_event(
                f"CALENDAR_SLOTS: Horarios disponibles para la valoracion. "
                f"Presentaselos de forma amigable y pide que elijan uno:\n{formatted}"
            )
        else:
            conv.inject_system_event(
                "CALENDAR_ERROR: No hay horarios disponibles. "
                "Dile que Yesica se pondra en contacto para coordinar el horario."
            )

    reply = await _generate_reply(conv)
    await _send_and_record(conv, reply)


async def _try_parse_slot_selection(conv: ConversationState, text: str) -> None:
    if not conv.calendar_slots_json:
        reply = await _generate_reply(conv)
        await _send_and_record(conv, reply)
        return

    slots = [
        datetime.fromisoformat(s).replace(tzinfo=COLOMBIA_TZ)
        for s in json.loads(conv.calendar_slots_json)
    ]

    selected_slot = _extract_slot_from_text(text, slots)

    if selected_slot:
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
                f"APPOINTMENT_CONFIRMED: Cita creada exitosamente. "
                f"Fecha y hora: {formatted_dt}. "
                f"Da todos los detalles: fecha/hora, direccion completa, como llegar en Metro, "
                f"llegar 5-10 min antes, cancelar con 24h de anticipacion."
            )
            asyncio.create_task(
                _notify_yesica_appointment(conv, formatted_dt)
            )
        else:
            conv.inject_system_event(
                "CALENDAR_ERROR: Problema al crear la cita. "
                "Yesica se pondra en contacto para confirmar manualmente."
            )

    reply = await _generate_reply(conv)
    await _send_and_record(conv, reply)


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
    # Inject user's name as a reminder for the AI
    system_with_name = SYSTEM_PROMPT
    if conv.user_display_name:
        system_with_name = (
            f"NOMBRE DEL USUARIO: {conv.user_display_name} — "
            f"Usalo en cada respuesta para generar cercania.\n\n"
            + SYSTEM_PROMPT
        )

    messages = [{"role": "system", "content": system_with_name}] + conv.messages
    return await ai.chat(messages)


async def _notify_yesica(conv: ConversationState) -> None:
    """Send Yesica a WhatsApp notification when a payment is verified."""
    settings = get_settings()
    name = conv.collected_name or conv.user_display_name or "Desconocido"
    service = conv.service_interest or "No especificado"
    city = conv.city or "No especificada"

    message = (
        f"*Nuevo comprobante de pago verificado!*\n\n"
        f"*Cliente:* {name}\n"
        f"*WhatsApp:* +{conv.phone}\n"
        f"*Servicio de interes:* {service}\n"
        f"*Ciudad:* {city}\n"
        f"*Servicio:* Valoracion Profesional - $25.000\n\n"
        f"El comprobante fue verificado automaticamente. "
        f"Se esta procediendo con el agendamiento."
    )
    await evolution.send_text_message(settings.yesica_phone, message)


async def _notify_yesica_appointment(
    conv: ConversationState,
    appointment_dt: str,
) -> None:
    """Notify Yesica when an appointment is booked."""
    settings = get_settings()
    name = conv.collected_name or conv.user_display_name or "Desconocido"
    message = (
        f"*Nueva cita agendada!*\n\n"
        f"*Cliente:* {name}\n"
        f"*WhatsApp:* +{conv.phone}\n"
        f"*Celular registrado:* {conv.collected_phone or 'No proporcionado'}\n"
        f"*Correo:* {conv.collected_email or 'No proporcionado'}\n"
        f"*Servicio de interes:* {conv.service_interest or 'No especificado'}\n"
        f"*Cita:* Valoracion Profesional\n"
        f"*Fecha y hora:* {appointment_dt}\n\n"
        f"Ya quedo registrado en tu Google Calendar."
    )
    await evolution.send_text_message(settings.yesica_phone, message)


def _extract_slot_from_text(text: str, slots: list[datetime]) -> datetime | None:
    text_clean = text.strip().lower()
    match = re.search(r"\b([1-9]\d?)\b", text_clean)
    if match:
        idx = int(match.group(1)) - 1
        if 0 <= idx < len(slots):
            return slots[idx]

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
